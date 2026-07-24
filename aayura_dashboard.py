"""
AAYURA - vitals monitor + Aasha, the trilingual AI voice companion
==================================================================
  LEFT  - live vitals monitor (heart rate, glucose, SpO2, missed calls)
          driven by a fake-sensor loop + a rules engine.
  RIGHT - Aasha, the AI voice companion: call status, language picker,
          a "Call now" button, and the live conversation transcript.

Aasha phones the patient's real phone, LISTENS, thinks with Claude, and
replies in the SAME language the patient speaks - English, Hindi (hi-IN)
or Telugu (te-IN) - using Google text-to-speech voices. When the rules
engine detects an EMERGENCY it auto-triggers a call and alerts family.

Calls work two ways:
  REAL : if Twilio + PUBLIC_URL are set, it dials the patient for real.
  DEMO : otherwise a scripted call plays so the dashboard still demos.

Run locally:  python3 aayura_dashboard.py     ->  http://localhost:5001
Deploy:       gunicorn aayura_dashboard:app   (Render reads render.yaml)
"""

import os
import base64
import collections
import datetime
import random
import threading
import time
import uuid

import requests
from flask import Flask, jsonify, request, render_template_string, Response, send_from_directory
from dotenv import load_dotenv

# Optional deps - the demo still runs without Twilio / Anthropic installed
try:
    from twilio.rest import Client
    from twilio.twiml.voice_response import VoiceResponse, Gather
except Exception:
    Client = None
    VoiceResponse = Gather = None

try:
    import anthropic
except Exception:
    anthropic = None

load_dotenv()

# ---------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------
AGENT_NAME = "Aasha"
TWILIO_SID = os.getenv("TWILIO_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_TOKEN", "")
TWILIO_NUMBER = os.getenv("TWILIO_NUMBER", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# Public base URL Twilio uses to reach our webhooks (the deployed domain in
# production, or the ngrok tunnel locally).
PUBLIC_URL = (os.getenv("PUBLIC_URL", "") or os.getenv("NGROK_URL", "") or "").rstrip("/")
PATIENT_PHONE = os.getenv("PATIENT_PHONE", "+919160220119")
PATIENT_NAME = os.getenv("PATIENT_NAME", "Lakshmi")
FAMILY_PHONE = os.getenv("FAMILY_PHONE", "")
# Language Aasha starts a call in (also what Twilio transcribes first).
DEFAULT_LANG = os.getenv("PRIMARY_LANG", "hi-IN")
PORT = int(os.getenv("PORT", "5001"))   # hosts (Render/Railway) inject $PORT

# Sarvam AI - external TTS used for Telugu, which Twilio's <Say> cannot speak.
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY", "")
SARVAM_MODEL = os.getenv("SARVAM_MODEL", "bulbul:v2")
SARVAM_SPEAKER = os.getenv("SARVAM_SPEAKER", "anushka")   # female voice for Aasha

app = Flask(__name__)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if (anthropic and ANTHROPIC_API_KEY) else None
# Set DEMO_MODE=1 to force scripted (fake) calls even when Twilio keys exist -
# use this while rehearsing so you never dial the patient's real phone.
DEMO_MODE = os.getenv("DEMO_MODE", "").lower() in ("1", "true", "yes")
CAN_CALL_FOR_REAL = (not DEMO_MODE) and bool(
    Client and TWILIO_SID and TWILIO_TOKEN and TWILIO_NUMBER and PUBLIC_URL)

# ---------------------------------------------------------------
# LANGUAGES  (Google voices - the only provider with a Telugu voice)
# ---------------------------------------------------------------
VOICE_FOR = {
    "en-IN": "Google.en-IN-Standard-A",
    "hi-IN": "Google.hi-IN-Standard-A",
    "te-IN": "Google.te-IN-Standard-A",
}
LANG_LABEL = {"en-IN": "English", "hi-IN": "Hindi", "te-IN": "Telugu"}

# Clinically-informed "normal" reference ranges shown under each vital card.
RANGES = {
    "heart_rate": {"low": 60, "high": 100, "unit": "bpm", "label": "Heart rate"},
    "glucose":    {"low": 70, "high": 140, "unit": "mg/dL", "label": "Blood glucose"},
    "spo2":       {"low": 95, "high": 100, "unit": "%", "label": "Blood oxygen"},
}
# Daily check-in schedule (display only, for the schedule panel).
SCHEDULE = [os.getenv("MORNING_CALL", "08:00"), os.getenv("EVENING_CALL", "19:00")]

GREETINGS = {
    "en-IN": f"Hello {PATIENT_NAME}! This is Aasha. I called to check on you. How are you feeling today?",
    "hi-IN": f"नमस्ते {PATIENT_NAME}! मैं आशा बोल रही हूँ। मैंने आपका हालचाल जानने के लिए फ़ोन किया है। आज आप कैसा महसूस कर रही हैं?",
    "te-IN": f"నమస్తే {PATIENT_NAME}! నేను ఆశ మాట్లాడుతున్నాను. మీరు ఎలా ఉన్నారో తెలుసుకోవడానికి ఫోన్ చేశాను. ఈరోజు మీకు ఎలా అనిపిస్తోంది?",
}
REPROMPT = {
    "en-IN": f"{PATIENT_NAME}, are you there?",
    "hi-IN": "क्या आप सुन रही हैं?",
    "te-IN": "మీరు వింటున్నారా?",
}


def detect_lang(text):
    """Pick the language from the script the text is written in."""
    for c in text:
        if "ఀ" <= c <= "౿":
            return "te-IN"          # Telugu block
    for c in text:
        if "ऀ" <= c <= "ॿ":
            return "hi-IN"          # Devanagari block
    return "en-IN"


# Twilio has no Telugu voice, so we synthesize Telugu with Sarvam AI and play
# the audio to the caller via <Play>. Generated clips are cached in memory and
# served from /audio/<id>.wav (Twilio fetches them over PUBLIC_URL).
audio_store = {}   # id -> wav bytes


def synth_sarvam(text, lang):
    """Return an audio id for a Sarvam-generated clip, or None on failure."""
    if not SARVAM_API_KEY:
        return None
    try:
        r = requests.post(
            "https://api.sarvam.ai/text-to-speech",
            headers={"api-subscription-key": SARVAM_API_KEY, "Content-Type": "application/json"},
            json={
                "inputs": [text[:500]],
                "target_language_code": lang,
                "speaker": SARVAM_SPEAKER,
                "model": SARVAM_MODEL,
                "speech_sample_rate": 8000,     # Twilio phone audio
                "enable_preprocessing": True,
            },
            timeout=15,
        )
        r.raise_for_status()
        wav = base64.b64decode(r.json()["audios"][0])
        aid = uuid.uuid4().hex
        audio_store[aid] = wav
        for k in list(audio_store)[:-50]:       # keep only the last 50 clips
            audio_store.pop(k, None)
        return aid
    except Exception as e:
        print(f"[SARVAM ERROR] {e}")
        return None


def voice_say(container, text, lang):
    """Speak `text` on a VoiceResponse/Gather. Telugu -> Sarvam <Play>,
    everything else -> Twilio <Say>."""
    if lang == "te-IN" and SARVAM_API_KEY and PUBLIC_URL:
        aid = synth_sarvam(text, lang)
        if aid:
            container.play(f"{PUBLIC_URL}/audio/{aid}.wav")
            return
        print("[SARVAM] Telugu synth failed - falling back to Hindi voice")
        lang = "hi-IN"     # audible fallback (mispronounced) rather than silence
    container.say(text, voice=VOICE_FOR.get(lang, VOICE_FOR["en-IN"]))


# ---------------------------------------------------------------
# SHARED STATE
# ---------------------------------------------------------------
patient = {
    "name": f"{PATIENT_NAME} (72)",
    "heart_rate": 72,
    "glucose": 110,
    "spo2": 97,
    "missed_calls": 0,
    "status": "NORMAL",       # NORMAL / WARNING / EMERGENCY
}
scenario = {"mode": "normal"}
alerts = []
last_alert_type = [None]

# rolling per-vital history for the drill-down charts (~5 min at 2s cadence)
VITAL_HISTORY = {
    "heart_rate": collections.deque(maxlen=150),
    "glucose": collections.deque(maxlen=150),
    "spo2": collections.deque(maxlen=150),
}
# outcome record per completed call: time, language, mode, level, flags, summary
call_log = []

call_state = {
    "status": "idle",         # idle / dialing / in_call / ended
    "mode": "-",              # REAL / DEMO
    "lang": DEFAULT_LANG,      # current conversation language
    "reason": None,
    "family_alerted": False,
    "finalized": False,        # guard so each call is logged once
}
transcript = []               # chronological: {who, text, time, lang}
conversations = {}            # per-CallSid history for the AI brain
_call_lock = threading.Lock()


def add_turn(who, text, lang="en-IN"):
    """who = 'patient' | 'aasha' | 'system'."""
    transcript.append({"who": who, "text": text, "time": time.strftime("%H:%M:%S"), "lang": lang})
    del transcript[:-60]


# ---------------------------------------------------------------
# CALL OUTCOMES - read how the patient spoke to flag health issues
# ---------------------------------------------------------------
# Symptom lexicon across English + Hindi + Telugu. "urgent" symptoms push the
# whole call to a "concern" outcome; the rest to "watch".
SYMPTOMS = {
    "dizziness":       {"urgent": True,  "words": ["dizzy", "giddy", "faint", "चक्कर", "కళ్లు తిరుగు", "తల తిరుగు"]},
    "chest pain":      {"urgent": True,  "words": ["chest pain", "सीने में दर्द", "छाती", "ఛాతీ నొప్పి"]},
    "breathlessness":  {"urgent": True,  "words": ["breathless", "short of breath", "साँस", "ఊపిరి", "శ్వాస"]},
    "a fall":          {"urgent": True,  "words": ["i fell", "fell down", "गिर गय", "గిరిపో", "పడిపో", "పడ్డా"]},
    "weakness":        {"urgent": False, "words": ["weak", "so tired", "no energy", "कमज़ोर", "थकान", "నీరసం", "బలహీన"]},
    "pain":            {"urgent": False, "words": ["pain", "aches", "it hurts", "दर्द", "నొప్పి"]},
    "not eaten":       {"urgent": False, "words": ["not eaten", "didn't eat", "haven't eaten", "नहीं खाया", "తినలేదు"]},
    "missed medicine": {"urgent": False, "words": ["not taken", "didn't take", "forgot my", "दवा नहीं", "మందు తీసుకోలేదు"]},
}


def analyze_transcript(patient_lines):
    """Return {level, flags, summary} from what the patient said on the call."""
    if not patient_lines:
        return {"level": "no-answer", "flags": [], "summary": "Patient did not speak."}
    text = " ".join(patient_lines).lower()
    flags, urgent = [], False
    for flag, spec in SYMPTOMS.items():
        if any(w.lower() in text for w in spec["words"]):
            flags.append(flag)
            urgent = urgent or spec["urgent"]
    level = "concern" if urgent else ("watch" if flags else "ok")
    summary = ("Mentioned " + ", ".join(flags) + "." if flags
               else "No health concerns mentioned; sounded fine.")
    return {"level": level, "flags": flags, "summary": summary}


def finalize_call(mode):
    """Log the just-finished call with a health assessment (once per call)."""
    if call_state.get("finalized"):
        return
    call_state["finalized"] = True
    patient_lines = [t["text"] for t in transcript if t["who"] == "patient"]
    a = analyze_transcript(patient_lines)
    call_log.insert(0, {
        "time": time.strftime("%H:%M"),
        "date": time.strftime("%b %d"),
        "mode": mode,
        "lang": LANG_LABEL.get(call_state.get("lang"), "English"),
        "reason": call_state.get("reason") or "check-in",
        "turns": len(patient_lines),
        "level": a["level"],
        "flags": a["flags"],
        "summary": a["summary"],
    })
    del call_log[20:]
    if a["level"] == "concern":
        print(f"[OUTCOME] health concern: {a['summary']}")


# ---------------------------------------------------------------
# AASHA'S BRAIN
# ---------------------------------------------------------------
AASHA_PROMPT = f"""You are Aasha, a warm, caring AI companion on a PHONE CALL
with {PATIENT_NAME}, a 72-year-old person in India.

LANGUAGE: The patient may speak Hindi, Telugu or English - sometimes mixing
them. ALWAYS reply in the SAME language the patient just used. Reply in Telugu
script for Telugu, Devanagari for Hindi, and plain English for English. Do not
mix two scripts in one reply.

STYLE: keep every reply SHORT - one or two small sentences, warm and human like
a favourite granddaughter. Ask one gentle thing at a time - how they slept, if
they ate, whether they took their medicines, how they feel.

SAFETY: If they mention chest pain, dizziness, a fall, breathlessness, or sound
very unwell, stay calm, tell them their family is being informed and they
should sit down, and add the token <ALERT> at the very end (never say the word
aloud). Only say goodbye when THEY want to go, then add <END>. Never give
medical advice, dosages or a diagnosis. Never speak the tokens out loud."""


def ask_aasha(call_sid, patient_said):
    history = conversations.setdefault(call_sid, [])
    history.append({"role": "user", "content": patient_said})
    try:
        if claude:
            # Haiku 4.5 = Anthropic's fastest model. On a phone call, low latency
            # matters more than deep reasoning, and replies are only 1-2 sentences,
            # so Haiku is the right brain. Short max_tokens = faster generation.
            reply = claude.messages.create(
                model="claude-haiku-4-5",
                max_tokens=120,
                system=AASHA_PROMPT,
                messages=history,
            ).content[0].text
        else:
            reply = "I am here with you. Please sit and rest, I will stay on the line. <END>"
    except Exception as e:
        print(f"[BRAIN ERROR] {e}")
        reply = "I am sorry, I will call you again shortly. Take care. <END>"
    history.append({"role": "assistant", "content": reply})
    return reply


# ---------------------------------------------------------------
# VITALS SIMULATOR + RULES ENGINE
# ---------------------------------------------------------------
def simulate_vitals():
    while True:
        mode = scenario["mode"]
        if mode == "normal":
            patient["heart_rate"] = random.randint(68, 82)
            patient["glucose"] = random.randint(95, 125)
            patient["spo2"] = random.randint(96, 99)
        elif mode == "glucose_drop":
            patient["glucose"] = max(45, patient["glucose"] - 8)
            patient["heart_rate"] = random.randint(85, 95)
        elif mode == "hr_spike":
            patient["heart_rate"] = min(155, patient["heart_rate"] + 10)
        _t = time.strftime("%H:%M:%S")
        for _k in VITAL_HISTORY:
            VITAL_HISTORY[_k].append({"t": _t, "v": patient[_k]})
        check_rules()
        time.sleep(2)


def check_rules():
    hr = patient["heart_rate"]
    glucose = patient["glucose"]

    if glucose < 70:
        patient["status"] = "EMERGENCY"
        raise_alert("LOW_GLUCOSE", f"Glucose critically low: {glucose} mg/dL", True, True)
    elif hr > 120:
        patient["status"] = "EMERGENCY"
        raise_alert("HIGH_HR", f"Heart rate too high: {hr} bpm", True, True)
    elif patient["missed_calls"] >= 5:
        patient["status"] = "EMERGENCY"
        raise_alert("MISSED_CALLS", "Patient missed 5 calls in a row", False, True)
    elif glucose < 85 or hr > 100:
        patient["status"] = "WARNING"
    else:
        patient["status"] = "NORMAL"
        last_alert_type[0] = None


def raise_alert(alert_type, reason, call_patient, alert_family):
    if last_alert_type[0] == alert_type:
        return
    last_alert_type[0] = alert_type

    entry = {"time": time.strftime("%H:%M:%S"), "reason": reason, "actions": []}
    if call_patient:
        trigger_voice_call(reason)
        entry["actions"].append("Aasha voice call started")
    if alert_family:
        send_family_alert(reason)
        entry["actions"].append("Family alerted")
    alerts.insert(0, entry)
    del alerts[10:]


# ---------------------------------------------------------------
# ACTIONS - calling the patient (real Twilio or scripted demo)
# ---------------------------------------------------------------
def trigger_voice_call(reason):
    if call_state["status"] in ("dialing", "in_call"):
        return
    if CAN_CALL_FOR_REAL:
        start_real_call(reason)
    else:
        threading.Thread(target=run_simulated_call, args=(reason,), daemon=True).start()


def start_real_call(reason):
    call_state.update(status="dialing", mode="REAL", reason=reason, family_alerted=False, finalized=False)
    transcript.clear()
    add_turn("system", f"Dialing {PATIENT_PHONE} ... ({reason})")
    try:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        call = client.calls.create(
            to=PATIENT_PHONE, from_=TWILIO_NUMBER,
            url=f"{PUBLIC_URL}/voice",
            status_callback=f"{PUBLIC_URL}/call-status",
            status_callback_event=["completed"],
            timeout=25,
        )
        print(f"[CALL] Dialing {PATIENT_PHONE}... SID {call.sid}")
    except Exception as e:
        print(f"[CALL ERROR] {e}")
        add_turn("system", f"Call failed: {e}")
        call_state["status"] = "ended"


# scripted demo conversation - bilingual to show off the multilingual brain
SIM_SCRIPT = [
    ("aasha", "hi-IN", "{name} जी, मैं आशा बोल रही हूँ। आपकी तबीयत को लेकर एक चिंता दिखी - {reason}. आप ठीक हैं?"),
    ("patient", "hi-IN", "थोड़ा चक्कर आ रहा है बेटा।"),
    ("aasha", "hi-IN", "ठीक है {name} जी, आप धीरे से बैठ जाइए। मैंने आपके परिवार को बता दिया है, वे आ रहे हैं।"),
    ("patient", "en-IN", "Okay... thank you."),
    ("aasha", "en-IN", "Stay sitting and keep talking to me. If you have juice nearby, take a small sip."),
    ("patient", "en-IN", "I have some juice here."),
    ("aasha", "en-IN", "Good, sip it slowly. I will stay right here with you until your family reaches you."),
]


def run_simulated_call(reason):
    with _call_lock:
        call_state.update(status="dialing", mode="DEMO", reason=reason, family_alerted=False, finalized=False)
        transcript.clear()
        add_turn("system", f"Aasha is calling {PATIENT_NAME} ... ({reason})")
        time.sleep(1.4)
        call_state["status"] = "in_call"
        for who, lang, line in SIM_SCRIPT:
            call_state["lang"] = lang
            add_turn(who, line.format(name=PATIENT_NAME, reason=reason), lang)
            time.sleep(1.9)
        add_turn("system", "Call ended - Aasha stayed until family arrived.")
        call_state["status"] = "ended"
        finalize_call("DEMO")


def send_family_alert(reason):
    call_state["family_alerted"] = True
    print(f"[FAMILY ALERT] {patient['name']}: {reason}")
    if not (Client and FAMILY_PHONE and TWILIO_SID):
        return
    try:
        Client(TWILIO_SID, TWILIO_TOKEN).messages.create(
            to=FAMILY_PHONE, from_=TWILIO_NUMBER,
            body=f"AAYURA ALERT: {patient['name']} may need attention - {reason}. Please call now.")
        print(f"[FAMILY ALERT] SMS sent to {FAMILY_PHONE}")
    except Exception as e:
        print(f"[FAMILY ALERT] SMS failed ({e})")


# ---------------------------------------------------------------
# TWILIO WEBHOOK ROUTES (used only for REAL calls)
# ---------------------------------------------------------------
def say_and_listen(text, lang):
    vr = VoiceResponse()
    gather = Gather(input="speech", action="/respond", method="POST",
                    language=lang, speechTimeout="auto")
    voice_say(gather, text, lang)
    vr.append(gather)
    voice_say(vr, REPROMPT.get(lang, REPROMPT["en-IN"]), lang)
    vr.redirect("/voice")
    return str(vr)


@app.route("/audio/<aid>.wav")
def audio(aid):
    wav = audio_store.get(aid)
    if not wav:
        return ("not found", 404)
    return Response(wav, mimetype="audio/wav")


@app.route("/voice", methods=["POST", "GET"])
def voice():
    lang = call_state.get("lang", DEFAULT_LANG)
    call_state["status"] = "in_call"
    greeting = GREETINGS.get(lang, GREETINGS["en-IN"])
    add_turn("aasha", greeting, lang)
    return say_and_listen(greeting, lang)


@app.route("/respond", methods=["POST"])
def respond():
    call_sid = request.form.get("CallSid", "unknown")
    patient_said = request.form.get("SpeechResult", "")
    if not patient_said:
        lang = call_state.get("lang", DEFAULT_LANG)
        return say_and_listen(REPROMPT.get(lang, REPROMPT["en-IN"]), lang)

    p_lang = detect_lang(patient_said)
    add_turn("patient", patient_said, p_lang)
    reply = ask_aasha(call_sid, patient_said)

    if "<ALERT>" in reply:
        send_family_alert(f"reported feeling unwell during call ('{patient_said}')")
        reply = reply.replace("<ALERT>", "").strip()
    clean = reply.replace("<END>", "").strip()
    r_lang = detect_lang(clean)
    call_state["lang"] = r_lang     # next Gather listens in Aasha's language
    add_turn("aasha", clean, r_lang)

    if "<END>" in reply:
        vr = VoiceResponse()
        voice_say(vr, clean, r_lang)
        vr.hangup()
        call_state["status"] = "ended"
        finalize_call("REAL")
        return str(vr)
    return say_and_listen(clean, r_lang)


@app.route("/call-status", methods=["POST"])
def call_status():
    if request.form.get("CallStatus", "") == "completed":
        call_state["status"] = "ended"
        finalize_call("REAL")
    return ("", 204)


# ---------------------------------------------------------------
# DASHBOARD API
# ---------------------------------------------------------------
@app.route("/data")
def data():
    return jsonify({
        "patient": patient,
        "alerts": alerts,
        "call": call_state,
        "lang_label": LANG_LABEL.get(call_state["lang"], "English"),
        "agent": AGENT_NAME,
        "patient_name": PATIENT_NAME,
        "transcript": transcript,
        "can_call_real": CAN_CALL_FOR_REAL,
        "ranges": RANGES,
        "history": {k: list(v) for k, v in VITAL_HISTORY.items()},
        "call_log": call_log,
        "schedule": SCHEDULE,
        "patient_phone": PATIENT_PHONE,
        "family_phone": FAMILY_PHONE,
    })


@app.route("/action/<what>", methods=["POST"])
def action(what):
    lang_map = {"lang_en": "en-IN", "lang_hi": "hi-IN", "lang_te": "te-IN"}
    if what == "reset":
        scenario["mode"] = "normal"
        patient.update(missed_calls=0, glucose=110, heart_rate=72)
        alerts.clear()
        last_alert_type[0] = None
        call_state.update(status="idle", mode="-", lang=DEFAULT_LANG, reason=None,
                          family_alerted=False, finalized=False)
        transcript.clear()
    elif what == "missed_call":
        patient["missed_calls"] += 1
        check_rules()
    elif what == "call_now":
        trigger_voice_call("manual check-in from dashboard")
    elif what in lang_map:
        call_state["lang"] = lang_map[what]
    else:
        scenario["mode"] = what
    return jsonify({"ok": True})


# ---------------------------------------------------------------
# PAGE
# ---------------------------------------------------------------
PAGE = """
<!DOCTYPE html>
<html>
<head>
<title>Aayura - Aasha voice companion</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * { margin:0; padding:0; box-sizing:border-box; font-family:'Segoe UI',system-ui,sans-serif; }
  body { background:#0d1117; color:#e6edf3; min-height:100vh; padding:28px 20px; }
  .wrap { max-width:1200px; margin:0 auto; }
  h1 { font-size:26px; font-weight:600; letter-spacing:0.5px; }
  h1 span { color:#e8a13c; }
  .sub { color:#8b949e; margin:4px 0 22px; font-size:14px; }
  .grid { display:grid; grid-template-columns:1.15fr 1fr; gap:20px; align-items:start; }
  @media (max-width:880px){ .grid { grid-template-columns:1fr; } }

  .panel { background:#161b22; border:1px solid #30363d; border-radius:14px; padding:20px; margin-bottom:20px; }
  .panel h3 { font-size:13px; color:#8b949e; margin-bottom:14px; text-transform:uppercase; letter-spacing:1px; }

  .status { display:inline-block; padding:6px 18px; border-radius:20px; font-weight:600; font-size:14px; margin-bottom:18px; }
  .NORMAL    { background:#0f2e1d; color:#4ade80; border:1px solid #1f6f42; }
  .WARNING   { background:#332600; color:#fbbf24; border:1px solid #8a6d1a; }
  .EMERGENCY { background:#3a0d0d; color:#f87171; border:1px solid #a03030; animation:pulse 1s infinite; }
  @keyframes pulse { 50% { opacity:0.55; } }

  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:14px; }
  .card { background:#0d1117; border:1px solid #30363d; border-radius:12px; padding:18px; }
  .card .label { color:#8b949e; font-size:13px; margin-bottom:8px; }
  .card .value { font-size:32px; font-weight:600; }
  .card .unit { font-size:13px; color:#8b949e; margin-left:4px; }
  .danger .value { color:#f87171; }
  .warn .value { color:#fbbf24; }

  button { background:#21262d; color:#e6edf3; border:1px solid #30363d; padding:10px 16px;
           border-radius:8px; cursor:pointer; font-size:14px; margin:0 8px 8px 0; transition:all .15s; }
  button:hover { border-color:#e8a13c; color:#e8a13c; }
  button.reset:hover { border-color:#4ade80; color:#4ade80; }
  button.call { border-color:#e8a13c; color:#e8a13c; font-weight:600; }
  button.call:hover { background:#e8a13c; color:#0d1117; }
  button.lang.active { border-color:#e8a13c; color:#e8a13c; background:#241a08; }

  .callhead { display:flex; align-items:center; gap:12px; margin-bottom:14px; }
  .dot { width:12px; height:12px; border-radius:50%; background:#4b5563; }
  .dot.dialing { background:#fbbf24; animation:pulse 0.8s infinite; }
  .dot.in_call { background:#4ade80; animation:pulse 1.2s infinite; }
  .dot.ended { background:#8b949e; }
  .callstat { font-size:15px; font-weight:600; }
  .badge { font-size:11px; padding:2px 8px; border-radius:10px; border:1px solid #30363d; color:#8b949e; }
  .badge.mode { margin-left:auto; }
  .fam { font-size:13px; color:#f87171; margin-bottom:12px; display:none; }
  .fam.on { display:block; }
  .langrow { margin-bottom:12px; }
  .langrow .lbl { font-size:12px; color:#8b949e; margin-bottom:6px; text-transform:uppercase; letter-spacing:1px; }

  .chat { background:#0d1117; border:1px solid #30363d; border-radius:10px; padding:14px;
          height:320px; overflow-y:auto; display:flex; flex-direction:column; gap:10px; }
  .bubble { max-width:82%; padding:9px 13px; border-radius:12px; font-size:14px; line-height:1.5; }
  .bubble .who { font-size:11px; color:#8b949e; margin-bottom:3px; }
  .aasha { align-self:flex-start; background:#1c2129; border:1px solid #30363d; border-radius:12px 12px 12px 2px; }
  .patient { align-self:flex-end; background:#16281c; border:1px solid #1f6f42; border-radius:12px 12px 2px 12px; }
  .system { align-self:center; background:transparent; color:#6b7280; font-size:12px; font-style:italic; }
  .chat .empty { color:#4b5563; font-size:14px; margin:auto; }

  .alerts .alert-item { border-left:3px solid #f87171; padding:9px 13px; margin-bottom:9px;
                background:#0d1117; border-radius:0 8px 8px 0; font-size:14px; }
  .alerts .t { color:#8b949e; font-size:12px; }
  .alerts .a { color:#e8a13c; font-size:13px; margin-top:4px; }
  .empty { color:#4b5563; font-size:14px; }

  .card.clickable { cursor:pointer; }
  .card.clickable:hover { border-color:#e8a13c; }
  .card .range { font-size:11px; color:#6b7280; margin-top:6px; }
  .card .range.out { color:#f87171; }

  /* drill-down modal */
  .modal { position:fixed; inset:0; background:rgba(0,0,0,.6); display:none;
           align-items:center; justify-content:center; z-index:50; padding:20px; }
  .modal.show { display:flex; }
  .modal .box { background:#161b22; border:1px solid #30363d; border-radius:14px; padding:22px; max-width:560px; width:100%; }
  .modal .box h2 { font-size:18px; }
  .modal .now { font-size:30px; font-weight:600; margin-top:4px; }
  .modal .meta { color:#8b949e; font-size:13px; margin:2px 0 4px; }
  .modal .stats { display:flex; gap:22px; margin-top:12px; font-size:12px; color:#8b949e; }
  .modal .stats b { color:#e6edf3; font-size:16px; display:block; }
  .modal .close { float:right; cursor:pointer; color:#8b949e; font-size:22px; line-height:1; }
  .spark { width:100%; height:130px; background:#0d1117; border:1px solid #30363d; border-radius:10px; margin-top:8px; display:block; }

  /* schedule + outcomes */
  .sched { margin-bottom:14px; }
  .schedchip { display:inline-block; font-size:13px; background:#0d1117; border:1px solid #30363d;
               border-radius:8px; padding:5px 12px; margin:0 8px 8px 0; color:#e6edf3; }
  .nextcall { font-size:13px; color:#8b949e; margin-top:2px; }
  .nextcall b { color:#e8a13c; }
  .call-item { background:#0d1117; border:1px solid #30363d; border-radius:10px; padding:12px 14px; margin-bottom:10px; }
  .call-item .top { display:flex; align-items:center; gap:8px; font-size:12px; color:#8b949e; margin-bottom:6px; flex-wrap:wrap; }
  .call-item .sum { font-size:14px; }
  .lvl { font-size:11px; font-weight:600; padding:2px 8px; border-radius:10px; }
  .lvl.ok { background:#0f2e1d; color:#4ade80; border:1px solid #1f6f42; }
  .lvl.watch { background:#332600; color:#fbbf24; border:1px solid #8a6d1a; }
  .lvl.concern { background:#3a0d0d; color:#f87171; border:1px solid #a03030; }
  .lvl.no-answer { background:#21262d; color:#8b949e; border:1px solid #30363d; }
  .chips { margin-top:6px; }
  .chip { display:inline-block; font-size:11px; background:#241a08; color:#e8a13c;
          border:1px solid #5a4416; border-radius:10px; padding:2px 8px; margin:3px 4px 0 0; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Aa<span>yura</span></h1>
  <div class="sub">Patient: <b id="pname">-</b> &middot; Next check-in: <b id="nextcall" style="color:#e8a13c">-</b> &middot; <span id="callmode"></span></div>

  <div class="grid">
    <!-- LEFT: vitals + rules -->
    <div>
      <div class="panel">
        <div id="status" class="status NORMAL">NORMAL</div>
        <div class="cards">
          <div class="card clickable" id="c-hr" onclick="openDrill('heart_rate')"><div class="label">Heart rate</div><div><span class="value" id="hr">-</span><span class="unit">bpm</span></div><div class="range" id="r-hr"></div></div>
          <div class="card clickable" id="c-gl" onclick="openDrill('glucose')"><div class="label">Blood glucose</div><div><span class="value" id="gl">-</span><span class="unit">mg/dL</span></div><div class="range" id="r-gl"></div></div>
          <div class="card clickable" id="c-sp" onclick="openDrill('spo2')"><div class="label">Blood oxygen</div><div><span class="value" id="sp">-</span><span class="unit">%</span></div><div class="range" id="r-sp"></div></div>
          <div class="card"><div class="label">Missed calls</div><div><span class="value" id="mc">-</span><span class="unit">/5</span></div><div class="range">Alert at 5</div></div>
        </div>
        <div style="font-size:12px;color:#6b7280;margin-top:10px">Tap a vital to see its history &rarr;</div>
      </div>

      <div class="panel">
        <h3>Demo simulation</h3>
        <div style="font-size:14px;color:#8b949e;margin-bottom:12px">Trigger emergencies and reset the patient on the dedicated simulation page.</div>
        <button class="call" onclick="location.href='/demo'">Open demo controls &rarr;</button>
      </div>

      <div class="panel alerts">
        <h3>Alert log (rules engine)</h3>
        <div id="alertbox"><div class="empty">No alerts yet - all healthy</div></div>
      </div>
    </div>

    <!-- RIGHT: Aasha + outcomes -->
    <div>
      <div class="panel">
      <div class="callhead">
        <span class="dot" id="dot"></span>
        <span class="callstat" id="callstat">Aasha idle</span>
        <span class="badge">🗣 <span id="langlabel">Hindi</span></span>
        <span class="badge mode" id="callbadge"></span>
      </div>
      <div class="fam" id="fam">Family has been alerted</div>

      <div class="langrow">
        <div class="lbl">Call language</div>
        <button class="lang" data-l="lang_en" onclick="act('lang_en')">English</button>
        <button class="lang" data-l="lang_hi" onclick="act('lang_hi')">Hindi</button>
        <button class="lang" data-l="lang_te" onclick="act('lang_te')">Telugu</button>
      </div>

      <button class="call" onclick="act('call_now')">Call patient now</button>
      <div class="chat" id="chat"><div class="empty">No call yet. Trigger an emergency or press "Call patient now".</div></div>
      </div>

      <div class="panel">
        <h3>Call schedule &amp; outcomes</h3>
        <div class="sched" id="sched"></div>
        <div id="calllog"><div class="empty">No calls yet.</div></div>
      </div>
    </div>
  </div>
</div>

<!-- drill-down modal -->
<div class="modal" id="modal" onclick="if(event.target===this)closeDrill()">
  <div class="box">
    <span class="close" onclick="closeDrill()">&times;</span>
    <h2 id="m-title">Vital</h2>
    <div class="now"><span id="m-now">-</span> <span style="font-size:14px;color:#8b949e" id="m-unit"></span></div>
    <div class="meta" id="m-range"></div>
    <svg class="spark" id="m-spark" viewBox="0 0 500 130" preserveAspectRatio="none"></svg>
    <div class="stats">
      <div><b id="m-min">-</b>min</div>
      <div><b id="m-max">-</b>max</div>
      <div><b id="m-avg">-</b>avg</div>
      <div><b id="m-n">-</b>readings</div>
    </div>
  </div>
</div>

<script>
const STAT = {idle:'Aasha idle', dialing:'Dialing patient...', in_call:'On call', ended:'Call ended'};
const LANGKEY = {'en-IN':'lang_en','hi-IN':'lang_hi','te-IN':'lang_te'};
const VMAP = {heart_rate:'r-hr', glucose:'r-gl', spo2:'r-sp'};
const VNAME = {heart_rate:'Heart rate', glucose:'Blood glucose', spo2:'Blood oxygen'};
let LAST = null, DRILL = null;

async function act(what){ await fetch('/action/'+what, {method:'POST'}); refresh(); }
const inRange = (v, r) => v >= r.low && v <= r.high;

function renderRanges(d){
  for(const key in VMAP){
    const r = d.ranges[key]; if(!r) continue;
    const el = document.getElementById(VMAP[key]);
    el.textContent = `Normal ${r.low}–${r.high} ${r.unit}`;
    el.className = 'range' + (inRange(d.patient[key], r) ? '' : ' out');
  }
}

function nextCall(sched){
  if(!sched || !sched.length) return '-';
  const now = new Date(), cur = now.getHours()*60 + now.getMinutes();
  const mins = sched.map(t => { const [h,m]=t.split(':').map(Number); return h*60+m; });
  for(let i=0;i<mins.length;i++){ if(mins[i] > cur) return 'today ' + sched[i]; }
  return 'tomorrow ' + sched[0];
}
function renderSchedule(d){
  const s = d.schedule || [];
  document.getElementById('sched').innerHTML =
    s.map(t => `<span class="schedchip">&#128337; ${t}</span>`).join('') +
    `<div class="nextcall">Next check-in: <b>${nextCall(s)}</b></div>`;
  const nc = document.getElementById('nextcall');
  if(nc) nc.textContent = nextCall(s);
}

function renderCallLog(d){
  const box = document.getElementById('calllog');
  if(!d.call_log || !d.call_log.length){ box.innerHTML='<div class="empty">No calls yet. Trigger one to see its outcome here.</div>'; return; }
  box.innerHTML = d.call_log.map(c => `
    <div class="call-item">
      <div class="top">
        <span>${c.date} ${c.time}</span><span>&middot; ${c.lang}</span><span>&middot; ${c.mode}</span>
        <span class="lvl ${c.level}" style="margin-left:auto">${c.level.toUpperCase().replace('-',' ')}</span>
      </div>
      <div class="sum">${c.summary}</div>
      ${c.flags && c.flags.length ? '<div class="chips">'+c.flags.map(f=>`<span class="chip">${f}</span>`).join('')+'</div>' : ''}
    </div>`).join('');
}

async function openDrill(key){
  if(!LAST){ try { LAST = await (await fetch('/data')).json(); } catch(e){ return; } }
  DRILL = key;
  document.getElementById('m-title').textContent = VNAME[key];
  const r = LAST.ranges[key];
  document.getElementById('m-unit').textContent = r.unit;
  document.getElementById('m-range').textContent = `Normal range ${r.low}–${r.high} ${r.unit}`;
  document.getElementById('modal').classList.add('show');
  drawDrill(key);
}
function closeDrill(){ DRILL = null; document.getElementById('modal').classList.remove('show'); }

function drawDrill(key){
  const hist = (LAST.history[key]||[]).map(p=>p.v);
  const r = LAST.ranges[key];
  document.getElementById('m-now').textContent = LAST.patient[key];
  if(!hist.length){ document.getElementById('m-spark').innerHTML=''; return; }
  const mn=Math.min(...hist), mx=Math.max(...hist);
  document.getElementById('m-min').textContent = mn;
  document.getElementById('m-max').textContent = mx;
  document.getElementById('m-avg').textContent = Math.round(hist.reduce((a,b)=>a+b,0)/hist.length);
  document.getElementById('m-n').textContent = hist.length;
  const W=500,H=130,pad=10;
  const lo=Math.min(mn,r.low), hi=Math.max(mx,r.high), span=(hi-lo)||1;
  const x=i=>pad+i*(W-2*pad)/Math.max(1,hist.length-1);
  const y=v=>H-pad-(v-lo)*(H-2*pad)/span;
  const pts=hist.map((v,i)=>`${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const bt=y(r.high), bb=y(r.low);
  document.getElementById('m-spark').innerHTML =
    `<rect x="0" y="${bt.toFixed(1)}" width="${W}" height="${Math.max(0,bb-bt).toFixed(1)}" fill="#1f6f42" opacity="0.16"/>`+
    `<polyline points="${pts}" fill="none" stroke="#e8a13c" stroke-width="2" stroke-linejoin="round"/>`;
}

async function refresh(){
  const d = await (await fetch('/data')).json();
  LAST = d;
  document.getElementById('pname').textContent = d.patient.name;
  document.getElementById('callmode').textContent = d.can_call_real ? 'REAL calls armed (Twilio)' : 'DEMO mode (scripted)';
  document.getElementById('hr').textContent = d.patient.heart_rate;
  document.getElementById('gl').textContent = d.patient.glucose;
  document.getElementById('sp').textContent = d.patient.spo2;
  document.getElementById('mc').textContent = d.patient.missed_calls;
  renderRanges(d);

  const st = document.getElementById('status');
  st.textContent = d.patient.status; st.className = 'status ' + d.patient.status;
  document.getElementById('c-hr').className = 'card clickable' + (d.patient.heart_rate>120?' danger':d.patient.heart_rate>100?' warn':'');
  document.getElementById('c-gl').className = 'card clickable' + (d.patient.glucose<70?' danger':d.patient.glucose<85?' warn':'');
  document.getElementById('c-sp').className = 'card clickable' + (d.patient.spo2<95?' warn':'');

  // Aasha panel
  document.getElementById('dot').className = 'dot ' + d.call.status;
  document.getElementById('callstat').textContent = STAT[d.call.status] || 'Aasha';
  document.getElementById('callbadge').textContent = d.call.mode !== '-' ? d.call.mode : '';
  document.getElementById('langlabel').textContent = d.lang_label;
  document.getElementById('fam').className = 'fam' + (d.call.family_alerted ? ' on' : '');
  document.querySelectorAll('.lang').forEach(b =>
    b.classList.toggle('active', b.dataset.l === LANGKEY[d.call.lang]));

  const chat = document.getElementById('chat');
  if(!d.transcript.length){
    chat.innerHTML = '<div class="empty">No call yet. Trigger an emergency or press "Call patient now".</div>';
  } else {
    const near = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 60;
    chat.innerHTML = d.transcript.map(t => t.who==='system'
      ? `<div class="bubble system">${t.text}</div>`
      : `<div class="bubble ${t.who}"><div class="who">${t.who==='aasha'?d.agent:d.patient_name} &middot; ${t.time}</div>${t.text}</div>`
    ).join('');
    if(near) chat.scrollTop = chat.scrollHeight;
  }

  const box = document.getElementById('alertbox');
  box.innerHTML = d.alerts.length ? d.alerts.map(a =>
    `<div class="alert-item"><div class="t">${a.time}</div><div>${a.reason}</div><div class="a">-> ${a.actions.join(' &middot; ')}</div></div>`
  ).join('') : '<div class="empty">No alerts yet - all healthy</div>';

  renderSchedule(d);
  renderCallLog(d);
  if(DRILL) drawDrill(DRILL);   // keep an open chart live
}
setInterval(refresh, 1000); refresh();
</script>
</body>
</html>
"""


@app.route("/assets/<path:fname>")
def assets(fname):
    return send_from_directory(os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets"), fname)


# ---------------------------------------------------------------
# SITE - the designed landing + family dashboard, wired to the real backend
# ---------------------------------------------------------------
SITE_PAGE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aayura — an AI companion for elderly parents</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;0,6..72,600;1,6..72,400&family=Figtree:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body { background: #FBF5EA; font-family: 'Figtree', system-ui, sans-serif; color: #2A2320; -webkit-font-smoothing: antialiased; }
  a { color: #0F6E63; text-decoration: none; }
  ::selection { background: #FBEAD8; }
  .ay-root { --bg:#FBF5EA; --card:#FFFDF8; --border:#EBE2D3; --border2:#E4D8C3; --ink:#2A2320; --ink2:#4A4038; --muted:#6B615A; --muted2:#7A6F66; --faint:#9C9186; --warm:#F4EAD8; --chip:#F1E7D6; --chip2:#FBF6EC; --bar:rgba(251,245,234,0.88); --okbg:#F7FAF3; --okbd:#E7EEDF; }
  .ay-root.ay-dark { --bg:#201A16; --card:#2C2521; --border:#3B322B; --border2:#463B32; --ink:#F5ECDD; --ink2:#E6DBCA; --muted:#B8AC9B; --muted2:#AA9E8D; --faint:#8B8070; --warm:#2A231F; --chip:#342B25; --chip2:#302822; --bar:rgba(32,26,22,0.86); --okbg:#243029; --okbd:#35473E; }
  .serif { font-family:'Newsreader',serif; }
  .btn { transition: all .15s; }
  .modal { position:fixed; inset:0; background:rgba(0,0,0,.55); display:none; align-items:center; justify-content:center; z-index:80; padding:20px; }
  .modal.show { display:flex; }
  @media (max-width: 860px) {
    .ay-hero-grid { grid-template-columns: 1fr !important; }
    .ay-dash-grid { grid-template-columns: 1fr !important; }
    .ay-testi-grid { grid-template-columns: 1fr !important; text-align: center; }
    .ay-hide-sm { display: none !important; }
  }
</style>
</head>
<body>
<div class="ay-root ay-dark" id="root" style="min-height:100vh; background:var(--bg); color:var(--ink);">

  <div style="position:sticky; top:0; z-index:50; background:var(--bar); backdrop-filter:blur(12px); border-bottom:1px solid var(--border);">
    <div style="max-width:1180px; margin:0 auto; padding:14px 24px; display:flex; align-items:center; gap:20px;">
      <div style="display:flex; align-items:center; gap:11px; cursor:pointer;" onclick="go('landing')">
        <div style="width:34px; height:34px; border-radius:50%; background:radial-gradient(circle at 32% 30%, #F0A45C, #E07A2E 70%); display:flex; align-items:center; justify-content:center; box-shadow:0 2px 8px rgba(224,122,46,0.35);"><div style="width:11px; height:11px; border-radius:50%; background:var(--card);"></div></div>
        <div class="serif" style="font-size:23px; font-weight:600; letter-spacing:-0.01em;">Aayura</div>
      </div>
      <div style="flex:1;"></div>
      <div style="display:flex; background:var(--chip); border:1px solid var(--border2); border-radius:999px; padding:4px; gap:2px;">
        <div onclick="go('landing')" id="tab-landing" class="tab">Home</div>
        <div onclick="go('dashboard')" id="tab-dash" class="tab">Family Dashboard</div>
      </div>
      <div onclick="toggleDark()" title="Toggle dark mode" id="darkbtn" class="btn" style="width:38px; height:38px; border-radius:999px; border:1px solid var(--border2); background:var(--card); display:flex; align-items:center; justify-content:center; font-size:16px; cursor:pointer;">☀</div>
      <div style="width:1px; height:26px; background:var(--border2);" class="ay-hide-sm"></div>
      <div onclick="go('dashboard')" class="ay-hide-sm btn" style="padding:10px 18px; background:#0F6E63; color:#FDFCF9; border-radius:999px; font-weight:600; font-size:14.5px; cursor:pointer; white-space:nowrap;">Protect your loved one</div>
    </div>
  </div>

  <!-- LANDING -->
  <div id="view-landing">
    <section style="max-width:1180px; margin:0 auto; padding:clamp(40px,6vw,84px) 24px clamp(30px,4vw,56px); display:grid; grid-template-columns:1.05fr 0.95fr; gap:clamp(28px,4vw,60px); align-items:center;" class="ay-hero-grid">
      <div>
        <div style="display:inline-flex; align-items:center; gap:8px; background:#FBEAD8; color:#B85F1E; padding:7px 14px; border-radius:999px; font-size:13.5px; font-weight:600; margin-bottom:22px;"><span style="width:7px; height:7px; border-radius:50%; background:#E07A2E; display:inline-block;"></span> Namaste — meet Aayura</div>
        <h1 class="serif" style="font-weight:500; font-size:clamp(44px,6.4vw,78px); line-height:1.02; letter-spacing:-0.02em; margin:0 0 20px;">She's never<br>alone.</h1>
        <p style="font-size:clamp(17px,1.6vw,20px); line-height:1.55; color:var(--muted); max-width:34ch; margin:0 0 30px;">An AI voice companion who calls your parents like a caring granddaughter — talking, reminding, and quietly watching over their health, every single day.</p>
        <div style="display:flex; flex-wrap:wrap; gap:13px; margin-bottom:26px;">
          <div onclick="go('dashboard')" class="btn" style="padding:15px 26px; background:#E07A2E; color:var(--card); border-radius:14px; font-weight:700; font-size:16px; cursor:pointer; box-shadow:0 6px 18px rgba(224,122,46,0.3);">Protect your loved one</div>
          <div onclick="callRadha(this)" class="btn" style="padding:15px 26px; background:var(--card); color:var(--ink); border:1px solid var(--border2); border-radius:14px; font-weight:600; font-size:16px; cursor:pointer;">▸ Hear a call</div>
        </div>
        <div style="display:flex; align-items:center; gap:18px; flex-wrap:wrap; color:var(--muted2); font-size:14px;">
          <span style="display:flex; align-items:center; gap:7px;"><span style="color:#0F6E63; font-weight:700;">✓</span> No smartphone needed</span>
          <span style="display:flex; align-items:center; gap:7px;"><span style="color:#0F6E63; font-weight:700;">✓</span> Speaks their language</span>
          <span style="display:flex; align-items:center; gap:7px;"><span style="color:#0F6E63; font-weight:700;">✓</span> Family stays informed</span>
        </div>
      </div>
      <div style="position:relative;">
        <div style="position:absolute; inset:-14px; background:radial-gradient(circle at 60% 35%, #F7D9B5, transparent 68%); border-radius:32px;"></div>
        <div style="position:relative; border-radius:28px; overflow:hidden; border:1px solid var(--border); box-shadow:0 24px 60px -20px rgba(90,60,30,0.32); aspect-ratio:4/5; display:flex; align-items:flex-end; justify-content:center;">
          <img src="/assets/radha-hero.png" alt="Grandmother on a phone call" style="position:absolute; inset:0; width:100%; height:100%; object-fit:cover; object-position:center 22%;">
          <div style="position:absolute; inset:0; background:linear-gradient(to top, rgba(30,20,12,0.34), transparent 42%);"></div>
          <div style="position:relative; margin:0 18px 18px; width:calc(100% - 36px); background:rgba(255,253,248,0.94); backdrop-filter:blur(6px); border-radius:18px; padding:14px 16px; display:flex; align-items:center; gap:12px; box-shadow:0 10px 28px -12px rgba(60,40,20,0.4);">
            <div style="width:44px; height:44px; border-radius:50%; background:radial-gradient(circle at 32% 30%, #F0A45C, #E07A2E 72%); display:flex; align-items:center; justify-content:center; color:#FFF; font-size:19px;">☎</div>
            <div style="flex:1; min-width:0; color:#2A2320;">
              <div style="font-weight:700; font-size:14.5px;">Aayura is calling Radha…</div>
              <div style="font-size:13px; color:#7A6F66;">"Beti, did you take your morning medicine?"</div>
            </div>
            <div style="display:flex; gap:3px; align-items:flex-end; height:24px;">
              <span style="width:3px; height:10px; background:#0F6E63; border-radius:2px;"></span><span style="width:3px; height:20px; background:#0F6E63; border-radius:2px;"></span><span style="width:3px; height:14px; background:#0F6E63; border-radius:2px;"></span><span style="width:3px; height:22px; background:#0F6E63; border-radius:2px;"></span>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section style="background:#0F6E63; color:#EAF3F0;">
      <div style="max-width:1180px; margin:0 auto; padding:clamp(48px,6vw,80px) 24px;">
        <div style="max-width:560px; margin-bottom:38px;">
          <div style="text-transform:uppercase; letter-spacing:0.14em; font-size:13px; font-weight:700; color:#8FC6BC; margin-bottom:14px;">The quiet crisis at home</div>
          <h2 class="serif" style="font-weight:500; font-size:clamp(30px,4vw,46px); line-height:1.08; letter-spacing:-0.01em; margin:0 0 14px;">Millions of parents in India spend their days alone.</h2>
          <p style="font-size:17px; line-height:1.55; color:#BBDAD3; margin:0;">As children move to cities and abroad, elders are left without daily contact — and small health warnings go unnoticed until it's too late.</p>
        </div>
        <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:16px;" id="problem-stats"></div>
      </div>
    </section>

    <section style="max-width:1180px; margin:0 auto; padding:clamp(52px,6vw,88px) 24px;">
      <div style="text-align:center; max-width:620px; margin:0 auto 46px;">
        <div style="text-transform:uppercase; letter-spacing:0.14em; font-size:13px; font-weight:700; color:#B85F1E; margin-bottom:14px;">How Aayura works</div>
        <h2 class="serif" style="font-weight:500; font-size:clamp(30px,4vw,46px); line-height:1.08; letter-spacing:-0.01em; margin:0;">Four gentle steps, every single day.</h2>
      </div>
      <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:18px;" id="steps"></div>
    </section>

    <section style="background:var(--warm);">
      <div style="max-width:1180px; margin:0 auto; padding:clamp(52px,6vw,88px) 24px;">
        <div style="max-width:560px; margin-bottom:40px;">
          <div style="text-transform:uppercase; letter-spacing:0.14em; font-size:13px; font-weight:700; color:#B85F1E; margin-bottom:14px;">Everything she needs, in one warm voice</div>
          <h2 class="serif" style="font-weight:500; font-size:clamp(30px,4vw,46px); line-height:1.08; letter-spacing:-0.01em; margin:0;">Care that feels like family.</h2>
        </div>
        <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:16px;" id="features"></div>
      </div>
    </section>

    <section style="max-width:1180px; margin:0 auto; padding:clamp(52px,6vw,88px) 24px;">
      <div style="background:var(--card); border:1px solid var(--border); border-radius:28px; padding:clamp(28px,4vw,56px); display:grid; grid-template-columns:auto 1fr; gap:clamp(24px,3vw,44px); align-items:center;" class="ay-testi-grid">
        <div style="width:clamp(120px,16vw,180px); aspect-ratio:1; border-radius:24px; overflow:hidden; border:1px solid var(--border);"><img src="/assets/family.png" onerror="this.onerror=null;this.src='/assets/radha-hero.png'" alt="family" style="width:100%; height:100%; object-fit:cover; object-position:center 45%;"></div>
        <div>
          <div style="color:#E07A2E; font-size:28px; line-height:1; margin-bottom:14px;">★★★★★</div>
          <blockquote class="serif" style="font-weight:400; font-style:italic; font-size:clamp(21px,2.6vw,30px); line-height:1.35; letter-spacing:-0.01em; margin:0 0 20px; color:var(--ink);">"I live in Toronto. Aayura calls Amma every morning — and now I finally sleep peacefully. When her oxygen dipped last month, I knew before she even mentioned it."</blockquote>
          <div style="display:flex; align-items:center; gap:12px;">
            <div style="width:44px; height:44px; border-radius:50%; background:#DCEDE9; display:flex; align-items:center; justify-content:center; font-weight:700; color:#0F6E63;">PN</div>
            <div><div style="font-weight:700; font-size:15px;">Priya Nair</div><div style="font-size:13.5px; color:var(--muted2);">Daughter · Toronto → Kochi</div></div>
          </div>
        </div>
      </div>
    </section>

    <section style="max-width:1180px; margin:0 auto; padding:0 24px clamp(56px,7vw,96px);">
      <div style="background:linear-gradient(135deg,#E07A2E,#CB6B22); color:#FFF8EF; border-radius:30px; padding:clamp(38px,5vw,70px); text-align:center; box-shadow:0 24px 60px -24px rgba(203,107,34,0.55);">
        <h2 class="serif" style="font-weight:500; font-size:clamp(30px,4.4vw,52px); line-height:1.06; letter-spacing:-0.02em; margin:0 0 16px;">Give them a voice.<br>Give yourself peace of mind.</h2>
        <p style="font-size:clamp(16px,1.6vw,19px); line-height:1.55; color:#FCE7D3; max-width:44ch; margin:0 auto 30px;">Set up Aayura in minutes. She'll take it from there — with warmth, patience, and unwavering attention.</p>
        <div style="display:flex; flex-wrap:wrap; gap:13px; justify-content:center;">
          <div onclick="go('dashboard')" class="btn" style="padding:16px 30px; background:var(--card); color:#B85F1E; border-radius:14px; font-weight:700; font-size:16.5px; cursor:pointer;">Protect your loved one</div>
          <div class="btn" style="padding:16px 30px; background:rgba(255,255,255,0.14); color:#FFF8EF; border:1px solid rgba(255,255,255,0.4); border-radius:14px; font-weight:600; font-size:16.5px; cursor:pointer;">Talk to our care team</div>
        </div>
      </div>
    </section>

    <footer style="border-top:1px solid var(--border);">
      <div style="max-width:1180px; margin:0 auto; padding:28px 24px; display:flex; flex-wrap:wrap; gap:16px; align-items:center; justify-content:space-between; color:var(--muted2); font-size:13.5px;">
        <div style="display:flex; align-items:center; gap:9px;">
          <div style="width:22px; height:22px; border-radius:50%; background:radial-gradient(circle at 32% 30%, #F0A45C, #E07A2E 70%);"></div>
          <span class="serif" style="font-size:16px; font-weight:600; color:var(--ink);">Aayura</span>
          <span style="margin-left:6px;">Companionship for grandparents. Peace of mind for families.</span>
        </div>
        <div style="display:flex; gap:20px;"><a href="#0">Privacy</a><a href="#0">Care standards</a><a href="#0">Contact</a></div>
      </div>
    </footer>
  </div>

  <!-- DASHBOARD (wired to /data & /action) -->
  <div id="view-dashboard" style="display:none;">
    <div style="max-width:1180px; margin:0 auto; padding:clamp(20px,3vw,32px) 24px clamp(48px,6vw,72px);">
      <div style="display:flex; flex-wrap:wrap; gap:18px; align-items:center; margin-bottom:20px;">
        <div style="width:60px; height:60px; border-radius:50%; overflow:hidden; border:1px solid var(--border);"><img src="/assets/radha-hero.png" alt="patient" style="width:100%; height:100%; object-fit:cover; object-position:center 20%;"></div>
        <div style="flex:1; min-width:180px;">
          <h1 class="serif" id="d-name" style="font-weight:600; font-size:clamp(26px,3.2vw,36px); margin:0; line-height:1.05;">—</h1>
          <div style="color:var(--muted2); font-size:14.5px; margin-top:4px;">Next check-in: <strong id="d-next" style="color:var(--ink);">—</strong> · Kochi, Kerala</div>
        </div>
        <div style="display:flex; align-items:center; gap:10px;">
          <span id="d-mode" style="display:inline-flex; align-items:center; gap:7px; background:var(--chip); border:1px solid var(--border2); color:var(--muted2); padding:8px 14px; border-radius:999px; font-size:12.5px; font-weight:600; font-family:ui-monospace,monospace;">—</span>
        </div>
      </div>

      <div id="banner"></div>

      <div style="display:grid; grid-template-columns:1.5fr 1fr; gap:18px; align-items:start;" class="ay-dash-grid">
        <!-- LEFT -->
        <div style="display:flex; flex-direction:column; gap:18px;">
          <div style="background:var(--card); border:1px solid var(--border); border-radius:22px; padding:24px;">
            <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:16px;">
              <h2 class="serif" style="font-weight:600; font-size:20px; margin:0;">Latest call with Aayura</h2>
              <span id="d-lasttime" style="font-size:13px; color:var(--muted2);"></span>
            </div>
            <p id="callsummary" style="font-size:15.5px; line-height:1.6; color:var(--ink2); margin:0;"></p>
            <div id="callflags" style="display:flex; gap:8px; flex-wrap:wrap; margin-top:14px;"></div>
          </div>

          <div style="background:var(--card); border:1px solid var(--border); border-radius:22px; padding:24px;">
            <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:6px;">
              <h2 class="serif" style="font-weight:600; font-size:20px; margin:0;">Vitals</h2>
              <span id="statuspill"></span>
            </div>
            <div style="font-size:13px; color:var(--faint); margin-bottom:16px;">Tap a vital to see its history →</div>
            <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:14px;" id="vitals"></div>
          </div>

          <div style="background:var(--card); border:1px solid var(--border); border-radius:22px; padding:24px;">
            <h2 class="serif" style="font-weight:600; font-size:20px; margin:0 0 4px;">Voice health</h2>
            <div style="font-size:13px; color:var(--faint); margin-bottom:16px;">Aayura listens for subtle changes in tone, energy &amp; clarity.</div>
            <div style="display:flex; align-items:center; gap:18px; flex-wrap:wrap;">
              <div style="display:flex; align-items:center; gap:14px;">
                <div id="voicedial" style="width:64px; height:64px; border-radius:50%; display:flex; align-items:center; justify-content:center;"><div id="voicescore" style="width:48px; height:48px; border-radius:50%; background:var(--card); display:flex; align-items:center; justify-content:center; font-family:'Newsreader',serif; font-size:19px; font-weight:600;"></div></div>
                <div><div id="voicelabel" style="font-weight:700; font-size:15.5px;"></div><div id="voicenote" style="font-size:13px; color:var(--muted2); max-width:26ch;"></div></div>
              </div>
              <div id="voicebars" style="flex:1; min-width:140px; display:flex; gap:4px; align-items:flex-end; height:56px;"></div>
            </div>
          </div>

          <!-- Live conversation transcript -->
          <div id="transcriptCard" style="background:var(--card); border:1px solid var(--border); border-radius:22px; padding:24px; display:none;">
            <h2 class="serif" style="font-weight:600; font-size:20px; margin:0 0 14px;">Live conversation</h2>
            <div id="transcript" style="display:flex; flex-direction:column; gap:9px; max-height:300px; overflow-y:auto;"></div>
          </div>
        </div>

        <!-- RIGHT -->
        <div style="display:flex; flex-direction:column; gap:18px;">
          <!-- Aayura call controls -->
          <div style="background:#0F6E63; color:#EAF3F0; border-radius:22px; padding:24px;">
            <div style="display:flex; align-items:center; gap:10px; margin-bottom:14px;">
              <span id="calldot" style="width:11px; height:11px; border-radius:50%; background:#9FCDC4;"></span>
              <span id="callstat" style="font-weight:700; font-size:15px;">Aayura idle</span>
              <span id="callmode2" style="margin-left:auto; font-size:11px; font-family:ui-monospace,monospace; background:rgba(255,255,255,0.14); padding:3px 9px; border-radius:999px;"></span>
            </div>
            <div style="font-size:12px; color:#9FCDC4; text-transform:uppercase; letter-spacing:0.1em; font-weight:700; margin-bottom:8px;">Call language</div>
            <div style="display:flex; gap:8px; margin-bottom:16px;">
              <div onclick="act('lang_en')" class="langchip" data-l="en-IN">English</div>
              <div onclick="act('lang_hi')" class="langchip" data-l="hi-IN">Hindi</div>
              <div onclick="act('lang_te')" class="langchip" data-l="te-IN">Telugu</div>
            </div>
            <div onclick="callRadha(this)" class="btn" style="padding:14px; background:#E07A2E; color:#FFF; border-radius:12px; font-weight:700; font-size:15px; cursor:pointer; text-align:center; box-shadow:0 6px 16px rgba(224,122,46,0.3);">☎ Call now</div>
            <div id="famAlerted" style="display:none; margin-top:10px; font-size:13px; color:#FBD9D4;">⚠ Family has been alerted.</div>
          </div>

          <!-- Demo simulation -->
          <div style="background:var(--card); border:1px solid var(--border); border-radius:22px; padding:24px;">
            <h2 class="serif" style="font-weight:600; font-size:20px; margin:0 0 4px;">Demo simulation</h2>
            <div style="font-size:13px; color:var(--faint); margin-bottom:14px;">Drive the fake-sensor feed to trigger the rules engine.</div>
            <div style="display:flex; gap:8px; flex-wrap:wrap;">
              <div onclick="act('glucose_drop')" class="dbtn">Glucose drop</div>
              <div onclick="act('hr_spike')" class="dbtn">Heart-rate spike</div>
              <div onclick="act('missed_call')" class="dbtn">Miss a call</div>
              <div onclick="act('reset')" class="dbtn" style="border-color:#BFDCD5;color:#0B534B;">Reset</div>
            </div>
          </div>

          <!-- Call history / outcomes -->
          <div style="background:var(--card); border:1px solid var(--border); border-radius:22px; padding:24px;">
            <h2 class="serif" style="font-weight:600; font-size:20px; margin:0 0 4px;">Call history &amp; outcomes</h2>
            <div id="d-sched" style="font-size:13px; color:var(--faint); margin-bottom:14px;"></div>
            <div style="display:flex; flex-direction:column;" id="callhistory"></div>
          </div>

          <!-- Emergency contacts -->
          <div style="background:var(--warm); border:1px solid var(--border); border-radius:22px; padding:24px;">
            <h2 class="serif" style="font-weight:600; font-size:20px; margin:0 0 4px;">Emergency contacts</h2>
            <div style="font-size:13px; color:var(--faint); margin-bottom:16px;">Notified in order if Aayura can't reach the patient.</div>
            <div id="contacts" style="display:flex; flex-direction:column; gap:10px;"></div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- drill-down modal -->
  <div class="modal" id="modal" onclick="if(event.target===this)closeDrill()">
    <div style="background:var(--card); border:1px solid var(--border); border-radius:20px; padding:24px; max-width:560px; width:100%;">
      <span onclick="closeDrill()" style="float:right; cursor:pointer; color:var(--faint); font-size:22px; line-height:1;">&times;</span>
      <h2 class="serif" id="m-title" style="font-size:20px; margin:0;">Vital</h2>
      <div style="font-size:30px; font-weight:600; margin-top:4px;" class="serif"><span id="m-now">—</span> <span id="m-unit" style="font-size:14px; color:var(--muted2);"></span></div>
      <div id="m-range" style="color:var(--muted2); font-size:13px; margin:2px 0 6px;"></div>
      <svg id="m-spark" viewBox="0 0 500 130" preserveAspectRatio="none" style="width:100%; height:130px; background:var(--chip2); border:1px solid var(--border); border-radius:10px; margin-top:8px; display:block;"></svg>
      <div style="display:flex; gap:22px; margin-top:12px; font-size:12px; color:var(--muted2);">
        <div><b id="m-min" style="color:var(--ink); font-size:16px; display:block;">—</b>min</div>
        <div><b id="m-max" style="color:var(--ink); font-size:16px; display:block;">—</b>max</div>
        <div><b id="m-avg" style="color:var(--ink); font-size:16px; display:block;">—</b>avg</div>
        <div><b id="m-n" style="color:var(--ink); font-size:16px; display:block;">—</b>readings</div>
      </div>
    </div>
  </div>

</div>

<script>
const S = { page:'landing', dark:true };
let LAST = null, DRILL = null;
const el = id => document.getElementById(id);
const esc = s => (''+s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const VK = {heart_rate:'Heart rate', glucose:'Blood glucose', spo2:'Blood oxygen'};
const UNIT = {heart_rate:'bpm', glucose:'mg/dL', spo2:'%'};

const STEPS = [
  {n:'01',icon:'☎',title:'Calls',desc:'Aayura phones your parent on a warm, familiar schedule — no app or smartphone required.',bg:'#FBEAD8',fg:'#B85F1E'},
  {n:'02',icon:'💬',title:'Talks',desc:'She chats like a caring granddaughter — asking about their day, gently reminding about meals & medicine.',bg:'#DCEDE9',fg:'#0F6E63'},
  {n:'03',icon:'📈',title:'Monitors',desc:'She reads connected devices and listens for changes in voice, mood and energy that signal decline.',bg:'#FBEAD8',fg:'#B85F1E'},
  {n:'04',icon:'🔔',title:'Alerts',desc:'If something seems wrong — or calls go unanswered — family is notified instantly, with full context.',bg:'#DCEDE9',fg:'#0F6E63'},
];
const FEATURES = [
  {icon:'💊',title:'Medicine & meal reminders',desc:'Gentle, spoken nudges at the right time — and a tick for family when they are done.'},
  {icon:'❤️',title:'Vitals monitoring',desc:'Heart rate, glucose and oxygen from connected devices, summarised in plain language.'},
  {icon:'🎙',title:'Voice health detection',desc:'Subtle shifts in tone or clarity flag possible decline before it becomes a crisis.'},
  {icon:'🚨',title:'Instant family alerts',desc:'Emergencies and missed calls trigger immediate notifications to everyone who matters.'},
  {icon:'🗣',title:'Speaks their language',desc:'Fluent, natural conversation in English, Hindi or Telugu — whichever they prefer.'},
  {icon:'🤝',title:'Real companionship',desc:'Daily conversation that eases loneliness — not just a checklist, but a friend.'},
];
const STATS = [
  {big:'1 in 3',t:'elderly Indians live alone or with only a spouse.'},
  {big:'40%+',t:'report persistent loneliness affecting their health.'},
  {big:'104M+',t:'Indians are over 60 — a number set to double by 2050.'},
  {big:'72%',t:'of health declines show up first in daily habits & voice.'},
];
const STAT = {idle:'Aayura idle', dialing:'Dialing…', in_call:'On call', ended:'Call ended'};
const LANGKEY = {'en-IN':'en-IN','hi-IN':'hi-IN','te-IN':'te-IN'};

function go(p){ S.page=p; renderShell(); if(p==='landing') renderLanding(); else if(LAST) renderDash(LAST); window.scrollTo(0,0); }
function toggleDark(){ S.dark=!S.dark; renderShell(); }
async function act(what){ await fetch('/action/'+what,{method:'POST'}); refresh(); }
async function callRadha(btn){
  const old = btn ? btn.textContent : '';
  if(btn) btn.textContent='Calling…';
  try{ const r=await (await fetch('/action/call_now',{method:'POST'})).json(); if(btn) btn.textContent='☎ Ringing…'; }
  catch(e){ if(btn) btn.textContent='⚠ failed'; }
  if(btn) setTimeout(()=>{ btn.textContent=old||'☎ Call now'; }, 3000);
  refresh();
}

const TAB_ON='padding:9px 16px;border-radius:999px;font-size:14px;font-weight:600;cursor:pointer;white-space:nowrap;background:var(--card);color:var(--ink);box-shadow:0 1px 4px rgba(0,0,0,0.08);';
const TAB_OFF='padding:9px 16px;border-radius:999px;font-size:14px;font-weight:600;cursor:pointer;white-space:nowrap;background:transparent;color:var(--muted2);';

function renderShell(){
  el('root').className='ay-root'+(S.dark?' ay-dark':'');
  el('darkbtn').textContent=S.dark?'☀':'☾';
  el('tab-landing').style.cssText=S.page==='landing'?TAB_ON:TAB_OFF;
  el('tab-dash').style.cssText=S.page==='dashboard'?TAB_ON:TAB_OFF;
  el('view-landing').style.display=S.page==='landing'?'':'none';
  el('view-dashboard').style.display=S.page==='dashboard'?'':'none';
}
function renderLanding(){
  el('problem-stats').innerHTML=STATS.map(s=>`<div style="background:rgba(255,255,255,0.07);border:1px solid rgba(255,255,255,0.12);border-radius:18px;padding:24px;"><div class="serif" style="font-size:46px;font-weight:500;color:#F3B678;line-height:1;">${s.big}</div><div style="margin-top:10px;color:#CFE4DE;font-size:15px;line-height:1.5;">${esc(s.t)}</div></div>`).join('');
  el('steps').innerHTML=STEPS.map(st=>`<div style="background:var(--card);border:1px solid var(--border);border-radius:22px;padding:26px;"><div style="width:52px;height:52px;border-radius:16px;background:${st.bg};color:${st.fg};display:flex;align-items:center;justify-content:center;font-size:24px;margin-bottom:18px;">${st.icon}</div><div style="font-family:ui-monospace,monospace;font-size:12px;color:var(--faint);margin-bottom:6px;">STEP ${st.n}</div><h3 class="serif" style="font-weight:600;font-size:22px;margin:0 0 8px;">${st.title}</h3><p style="font-size:14.5px;line-height:1.55;color:var(--muted);margin:0;">${esc(st.desc)}</p></div>`).join('');
  el('features').innerHTML=FEATURES.map(f=>`<div style="background:var(--card);border:1px solid var(--border);border-radius:20px;padding:24px;"><div style="font-size:26px;margin-bottom:14px;">${f.icon}</div><h3 class="serif" style="font-weight:600;font-size:20px;margin:0 0 7px;">${f.title}</h3><p style="font-size:14.5px;line-height:1.55;color:var(--muted);margin:0;">${esc(f.desc)}</p></div>`).join('');
}

function nextCall(sched){
  if(!sched||!sched.length) return '—';
  const now=new Date(), cur=now.getHours()*60+now.getMinutes();
  const mins=sched.map(t=>{const [h,m]=t.split(':').map(Number);return h*60+m;});
  for(let i=0;i<mins.length;i++){ if(mins[i]>cur) return 'today '+sched[i]; }
  return 'tomorrow '+sched[0];
}
function vTag(key,val,r){
  if(key==='missed_calls') return val>=5?['ALERT',1]:['Normal',0];
  if(val<r.low) return ['Low',1]; if(val>r.high) return ['High',1]; return ['Normal',0];
}
function sparkPoints(hist,r){
  const arr=(hist||[]).slice(-16).map(p=>p.v);
  if(arr.length<2) return '0,13 100,13';
  const mn=Math.min(...arr), mx=Math.max(...arr), span=(mx-mn)||1;
  return arr.map((v,i)=>{ const x=(i*(100/(arr.length-1))).toFixed(1); const y=(23-((v-mn)/span)*18).toFixed(1); return x+','+y; }).join(' ');
}

function renderDash(d){
  el('d-name').textContent = d.patient.name;
  el('d-next').textContent = nextCall(d.schedule);
  el('d-mode').textContent = d.can_call_real ? 'LIVE · real calls' : 'DEMO · scripted';
  const st = d.patient.status;
  const emg = st==='EMERGENCY', warn = st==='WARNING';

  el('banner').innerHTML = emg ? `
    <div style="background:linear-gradient(135deg,#C6392E,#A82C22);color:#FFF;border-radius:22px;padding:22px 24px;margin-bottom:22px;display:flex;flex-wrap:wrap;gap:16px;align-items:center;box-shadow:0 16px 40px -14px rgba(198,57,46,0.6);">
      <div style="width:52px;height:52px;border-radius:14px;background:rgba(255,255,255,0.18);display:flex;align-items:center;justify-content:center;font-size:26px;">⚠</div>
      <div style="flex:1;min-width:220px;"><div style="font-weight:800;font-size:19px;">Emergency — ${esc(d.patient_name)} may need help now</div><div style="font-size:14.5px;color:#FBD9D4;margin-top:3px;">${esc((d.alerts[0]&&d.alerts[0].reason)||'A vital reading crossed the danger threshold.')} · Aayura is calling and family is being notified.</div></div>
      <div onclick="callRadha(this)" class="btn" style="padding:12px 20px;background:#FFF;color:#B0271D;border-radius:12px;font-weight:700;font-size:14.5px;cursor:pointer;">Call now</div>
    </div>` : warn ? `
    <div style="background:#FBEAD8;border:1px solid #EBc492;color:#B85F1E;border-radius:18px;padding:16px 22px;margin-bottom:22px;display:flex;align-items:center;gap:12px;">
      <span style="width:32px;height:32px;border-radius:50%;background:#E07A2E;color:#FFF;display:flex;align-items:center;justify-content:center;font-size:16px;">!</span>
      <div style="font-size:15.5px;"><strong>Keeping a close eye.</strong> A reading is drifting toward the edge of normal.</div>
    </div>` : `
    <div style="background:#DCEDE9;border:1px solid #BFDCD5;color:#0B534B;border-radius:18px;padding:16px 22px;margin-bottom:22px;display:flex;align-items:center;gap:12px;">
      <span style="width:32px;height:32px;border-radius:50%;background:#0F6E63;color:#FFF;display:flex;align-items:center;justify-content:center;font-size:16px;">✓</span>
      <div style="font-size:15.5px;"><strong>All is well today.</strong> ${esc(d.patient_name)} is on track and everything looks healthy.</div>
    </div>`;

  // latest call summary
  const last = d.call_log && d.call_log[0];
  el('d-lasttime').textContent = last ? (last.date+' '+last.time+' · '+last.lang) : '';
  el('callsummary').textContent = last ? ('"'+last.summary+'"') : 'No calls yet today. Press “Call now” to reach '+d.patient_name+'.';
  el('callflags').innerHTML = (last&&last.flags&&last.flags.length)
    ? last.flags.map(f=>`<div style="background:#FBEAD8;color:#B85F1E;padding:8px 13px;border-radius:12px;font-size:13.5px;font-weight:600;">${esc(f)}</div>`).join('')
    : (last ? '<div style="background:#DCEDE9;color:#0B534B;padding:8px 13px;border-radius:12px;font-size:13.5px;font-weight:600;">No concerns flagged ✓</div>' : '');

  // status pill
  el('statuspill').style.cssText='font-size:11.5px;font-weight:800;letter-spacing:0.06em;padding:5px 12px;border-radius:999px;'+(emg?'color:#C6392E;background:#FBE9E6;':warn?'color:#B85F1E;background:#FBEAD8;':'color:#0F6E63;background:#DCEDE9;');
  el('statuspill').textContent = emg?'NEEDS ATTENTION':warn?'WATCHING':'NORMAL';

  // vitals
  const order=['heart_rate','glucose','spo2'];
  let html='';
  order.forEach(k=>{
    const r=d.ranges[k], val=d.patient[k], [tag,bad]=vTag(k,val,r);
    html+=`<div onclick="openDrill('${k}')" style="border:1px solid ${bad?'#F0C4BE':'var(--okbd)'};background:${bad?'#FBE9E6':'var(--okbg)'};border-radius:16px;padding:16px;cursor:pointer;">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;"><span style="font-size:13px;color:var(--muted);font-weight:600;">${VK[k]}</span><span style="font-size:11px;font-weight:700;color:${bad?'#C6392E':'#0F6E63'};">${tag}</span></div>
      <div style="display:flex;align-items:baseline;gap:4px;"><span class="serif" style="font-size:30px;font-weight:600;color:${bad?'#C6392E':'var(--ink)'};line-height:1;">${val}</span><span style="font-size:13px;color:var(--faint);">${UNIT[k]}</span></div>
      <svg viewBox="0 0 100 26" preserveAspectRatio="none" style="width:100%;height:26px;margin-top:10px;display:block;"><polyline points="${sparkPoints(d.history[k],r)}" fill="none" stroke="${bad?'#C6392E':'#0F6E63'}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></polyline></svg>
      <div style="font-size:11.5px;color:var(--faint);margin-top:6px;">Normal ${r.low}–${r.high} ${r.unit}</div></div>`;
  });
  const mc=d.patient.missed_calls, mbad=mc>=5;
  html+=`<div style="border:1px solid ${mbad?'#F0C4BE':'var(--okbd)'};background:${mbad?'#FBE9E6':'var(--okbg)'};border-radius:16px;padding:16px;">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;"><span style="font-size:13px;color:var(--muted);font-weight:600;">Missed calls</span><span style="font-size:11px;font-weight:700;color:${mbad?'#C6392E':'#0F6E63'};">${mbad?'ALERT':'Normal'}</span></div>
    <div style="display:flex;align-items:baseline;gap:4px;"><span class="serif" style="font-size:30px;font-weight:600;color:${mbad?'#C6392E':'var(--ink)'};line-height:1;">${mc}</span><span style="font-size:13px;color:var(--faint);">/5</span></div>
    <div style="font-size:11.5px;color:var(--faint);margin-top:16px;">Alert at 5</div></div>`;
  el('vitals').innerHTML=html;

  // voice health (derived from status)
  const V = emg?{s:52,c:'#C6392E',deg:'187deg',l:'Unusual — flagged',n:'Low energy / unclear speech detected.'}
          : warn?{s:74,c:'#B85F1E',deg:'266deg',l:'A little low',n:'Slightly tired tone this morning.'}
          : {s:92,c:'#0F6E63',deg:'331deg',l:'Steady & bright',n:'Warm, clear and energetic — consistent with baseline.'};
  el('voicedial').style.background=`conic-gradient(${V.c} ${V.deg}, var(--border) 0)`;
  el('voicescore').textContent=V.s; el('voicescore').style.color=V.c;
  el('voicelabel').textContent=V.l; el('voicelabel').style.color=V.c;
  el('voicenote').textContent=V.n;
  const heights=emg?[30,44,20,50,26,18,40,22,14,34,20,44,16,28,20,12]:[30,52,40,56,44,50,38,54,42,48,36,52,44,50,40,54];
  el('voicebars').innerHTML=heights.map((h,i)=>`<span style="flex:1;height:${h}%;background:${emg?(i%2?'#E79A93':'#C6392E'):(i%2?'#8FC6BC':'#0F6E63')};border-radius:3px;"></span>`).join('');

  // call controls
  el('calldot').style.background = d.call.status==='in_call'?'#7CE0C0':d.call.status==='dialing'?'#F3B678':'#9FCDC4';
  el('callstat').textContent = STAT[d.call.status]||'Aayura';
  el('callmode2').textContent = d.call.mode!=='-'?d.call.mode:'';
  el('famAlerted').style.display = d.call.family_alerted?'block':'none';
  document.querySelectorAll('.langchip').forEach(b=>{
    const on=b.dataset.l===d.call.lang;
    b.style.cssText='padding:8px 14px;border-radius:10px;font-size:13.5px;font-weight:600;cursor:pointer;'+(on?'background:#FFF;color:#0B534B;':'background:rgba(255,255,255,0.14);color:#EAF3F0;');
  });

  // transcript
  const tc=el('transcriptCard');
  if(d.transcript && d.transcript.length){
    tc.style.display='';
    const box=el('transcript');
    const near=box.scrollHeight-box.scrollTop-box.clientHeight<60;
    box.innerHTML=d.transcript.map(t=> t.who==='system'
      ? `<div style="align-self:center;color:var(--faint);font-size:12px;font-style:italic;">${esc(t.text)}</div>`
      : `<div style="max-width:82%;align-self:${t.who==='patient'?'flex-end':'flex-start'};background:${t.who==='patient'?'#DCEDE9':'var(--chip2)'};color:${t.who==='patient'?'#0B534B':'var(--ink)'};border:1px solid var(--border);border-radius:12px;padding:9px 13px;font-size:14px;line-height:1.45;"><div style="font-size:11px;color:var(--muted2);margin-bottom:2px;">${t.who==='aasha'?'Aayura':esc(d.patient_name)} · ${t.time}</div>${esc(t.text)}</div>`
    ).join('');
    if(near) box.scrollTop=box.scrollHeight;
  } else tc.style.display='none';

  // schedule + call history
  el('d-sched').innerHTML='Daily check-ins: '+(d.schedule||[]).map(t=>'<b style="color:var(--ink2);">'+t+'</b>').join(' · ');
  const LVL={ok:['✓','#DCEDE9','#0F6E63','OK'],watch:['◑','#FBEAD8','#B85F1E','WATCH'],concern:['⚠','#FBE9E6','#C6392E','CONCERN'],'no-answer':['✕','var(--chip)','#7A6F66','NO ANSWER']};
  el('callhistory').innerHTML=(d.call_log||[]).length ? d.call_log.map(c=>{
    const L=LVL[c.level]||LVL.ok;
    return `<div style="display:flex;gap:13px;align-items:flex-start;padding:11px 0;border-bottom:1px solid var(--border);">
      <span style="width:34px;height:34px;border-radius:50%;background:${L[1]};color:${L[2]};display:flex;align-items:center;justify-content:center;font-size:15px;flex-shrink:0;">${L[0]}</span>
      <div style="flex:1;min-width:0;"><div style="font-weight:600;font-size:14px;">${esc(c.summary)}</div><div style="font-size:12.5px;color:var(--faint);">${c.date} ${c.time} · ${esc(c.lang)} · ${c.mode}</div></div>
      <span style="font-size:11px;font-weight:700;color:${L[2]};white-space:nowrap;">${L[3]}</span></div>`;
  }).join('') : '<div style="color:var(--faint);font-size:14px;">No calls yet — trigger one to see its outcome here.</div>';

  // contacts (real family number as 1st)
  el('contacts').innerHTML = `
    <div style="display:flex;align-items:center;gap:12px;"><span style="width:36px;height:36px;border-radius:50%;background:#DCEDE9;color:#0F6E63;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px;">FM</span><div style="flex:1;"><div style="font-weight:600;font-size:14px;">Family</div><div style="font-size:12.5px;color:var(--muted2);">${esc(d.family_phone||'not set')}</div></div><span style="font-size:11px;font-weight:700;color:#0F6E63;">1st</span></div>
    <div style="display:flex;align-items:center;gap:12px;"><span style="width:36px;height:36px;border-radius:50%;background:#FBEAD8;color:#B85F1E;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px;">AK</span><div style="flex:1;"><div style="font-weight:600;font-size:14px;">Arun Kumar</div><div style="font-size:12.5px;color:var(--muted2);">Son · Bengaluru</div></div><span style="font-size:11px;font-weight:700;color:#B85F1E;">2nd</span></div>
    <div style="display:flex;align-items:center;gap:12px;"><span style="width:36px;height:36px;border-radius:50%;background:var(--border);color:var(--muted2);display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px;">MW</span><div style="flex:1;"><div style="font-weight:600;font-size:14px;">Meena (neighbour)</div><div style="font-size:12.5px;color:var(--muted2);">Local · 2 min away</div></div><span style="font-size:11px;font-weight:700;color:var(--muted2);">3rd</span></div>`;

  if(DRILL) drawDrill(DRILL);
}

async function openDrill(key){
  if(!LAST){ try{ LAST=await (await fetch('/data')).json(); }catch(e){ return; } }
  DRILL=key;
  el('m-title').textContent=VK[key];
  el('m-unit').textContent=LAST.ranges[key].unit;
  el('m-range').textContent='Normal range '+LAST.ranges[key].low+'–'+LAST.ranges[key].high+' '+LAST.ranges[key].unit;
  el('modal').classList.add('show'); drawDrill(key);
}
function closeDrill(){ DRILL=null; el('modal').classList.remove('show'); }
function drawDrill(key){
  const hist=(LAST.history[key]||[]).map(p=>p.v), r=LAST.ranges[key];
  el('m-now').textContent=LAST.patient[key];
  if(!hist.length){ el('m-spark').innerHTML=''; return; }
  const mn=Math.min(...hist),mx=Math.max(...hist);
  el('m-min').textContent=mn; el('m-max').textContent=mx;
  el('m-avg').textContent=Math.round(hist.reduce((a,b)=>a+b,0)/hist.length); el('m-n').textContent=hist.length;
  const W=500,H=130,pad=10, lo=Math.min(mn,r.low), hi=Math.max(mx,r.high), span=(hi-lo)||1;
  const x=i=>pad+i*(W-2*pad)/Math.max(1,hist.length-1), y=v=>H-pad-(v-lo)*(H-2*pad)/span;
  const pts=hist.map((v,i)=>x(i).toFixed(1)+','+y(v).toFixed(1)).join(' ');
  const bt=y(r.high), bb=y(r.low);
  el('m-spark').innerHTML=`<rect x="0" y="${bt.toFixed(1)}" width="${W}" height="${Math.max(0,bb-bt).toFixed(1)}" fill="#1f6f42" opacity="0.16"/><polyline points="${pts}" fill="none" stroke="#E07A2E" stroke-width="2" stroke-linejoin="round"/>`;
}

async function refresh(){
  try{ const d=await (await fetch('/data')).json(); LAST=d; if(S.page==='dashboard') renderDash(d); }catch(e){}
}

// styles for demo buttons & lang chips (injected)
const st=document.createElement('style');
st.textContent='.dbtn{padding:9px 13px;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer;border:1px solid var(--border2);background:var(--chip2);color:var(--muted);} .dbtn:hover{border-color:#E07A2E;color:#E07A2E;}';
document.head.appendChild(st);

renderShell(); renderLanding(); refresh(); setInterval(refresh,1500);
</script>
</body>
</html>
"""


@app.route("/")
def home():
    return Response(SITE_PAGE, mimetype="text/html")


# ---------------------------------------------------------------
# DEMO SIMULATION PAGE (separate from the main dashboard)
# ---------------------------------------------------------------
DEMO_PAGE = """
<!DOCTYPE html>
<html>
<head>
<title>Aayura - Demo simulation</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * { margin:0; padding:0; box-sizing:border-box; font-family:'Segoe UI',system-ui,sans-serif; }
  body { background:#0d1117; color:#e6edf3; min-height:100vh; padding:28px 20px; }
  .wrap { max-width:760px; margin:0 auto; }
  a.back { color:#e8a13c; text-decoration:none; font-size:14px; }
  h1 { font-size:24px; font-weight:600; margin-top:10px; } h1 span { color:#e8a13c; }
  .sub { color:#8b949e; margin:4px 0 22px; font-size:14px; }
  .panel { background:#161b22; border:1px solid #30363d; border-radius:14px; padding:20px; margin-bottom:20px; }
  .panel h3 { font-size:13px; color:#8b949e; margin-bottom:14px; text-transform:uppercase; letter-spacing:1px; }
  .status { display:inline-block; padding:6px 18px; border-radius:20px; font-weight:600; font-size:14px; margin-bottom:16px; }
  .NORMAL { background:#0f2e1d; color:#4ade80; border:1px solid #1f6f42; }
  .WARNING { background:#332600; color:#fbbf24; border:1px solid #8a6d1a; }
  .EMERGENCY { background:#3a0d0d; color:#f87171; border:1px solid #a03030; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:14px; }
  .card { background:#0d1117; border:1px solid #30363d; border-radius:12px; padding:16px; }
  .card .label { color:#8b949e; font-size:12px; margin-bottom:6px; }
  .card .value { font-size:28px; font-weight:600; }
  .card .unit { font-size:12px; color:#8b949e; margin-left:3px; }
  button { background:#21262d; color:#e6edf3; border:1px solid #30363d; padding:12px 18px;
           border-radius:8px; cursor:pointer; font-size:14px; margin:0 8px 10px 0; transition:all .15s; }
  button:hover { border-color:#e8a13c; color:#e8a13c; }
  button.reset:hover { border-color:#4ade80; color:#4ade80; }
  .hint { color:#6b7280; font-size:13px; margin-top:8px; }
  .hint a { color:#e8a13c; }
</style>
</head>
<body>
<div class="wrap">
  <a class="back" href="/">&larr; Back to dashboard</a>
  <h1>Aa<span>yura</span> &middot; Demo simulation</h1>
  <div class="sub">Drive the fake-sensor feed to demo the rules engine and Aasha's auto-call.</div>

  <div class="panel">
    <h3>Live patient state</h3>
    <div id="status" class="status NORMAL">NORMAL</div>
    <div class="cards">
      <div class="card"><div class="label">Heart rate</div><div><span class="value" id="hr">-</span><span class="unit">bpm</span></div></div>
      <div class="card"><div class="label">Blood glucose</div><div><span class="value" id="gl">-</span><span class="unit">mg/dL</span></div></div>
      <div class="card"><div class="label">Blood oxygen</div><div><span class="value" id="sp">-</span><span class="unit">%</span></div></div>
      <div class="card"><div class="label">Missed calls</div><div><span class="value" id="mc">-</span><span class="unit">/5</span></div></div>
    </div>
  </div>

  <div class="panel">
    <h3>Demo controls</h3>
    <button onclick="act('glucose_drop')">Simulate glucose drop</button>
    <button onclick="act('hr_spike')">Simulate heart-rate spike</button>
    <button onclick="act('missed_call')">Patient misses a call</button>
    <button class="reset" onclick="act('reset')">Reset to normal</button>
    <div class="hint">Glucose &lt; 70 or heart rate &gt; 120 raises an EMERGENCY, which auto-calls Aasha and alerts family. Watch it unfold on the <a href="/">dashboard</a>.</div>
  </div>
</div>
<script>
  async function act(w){ await fetch('/action/'+w,{method:'POST'}); refresh(); }
  async function refresh(){
    const d = await (await fetch('/data')).json();
    document.getElementById('hr').textContent = d.patient.heart_rate;
    document.getElementById('gl').textContent = d.patient.glucose;
    document.getElementById('sp').textContent = d.patient.spo2;
    document.getElementById('mc').textContent = d.patient.missed_calls;
    const st = document.getElementById('status');
    st.textContent = d.patient.status; st.className = 'status ' + d.patient.status;
  }
  setInterval(refresh, 1000); refresh();
</script>
</body>
</html>
"""


@app.route("/demo")
def demo():
    return render_template_string(DEMO_PAGE)


# ---------------------------------------------------------------
# SAMPLE DATA - preload Radha so the dashboard looks alive on first load
# ---------------------------------------------------------------
def seed_sample_data():
    if VITAL_HISTORY["heart_rate"]:
        return
    now = time.time()
    n = 90
    for i in range(n):
        t = time.strftime("%H:%M:%S", time.localtime(now - (n - i) * 120))
        VITAL_HISTORY["heart_rate"].append({"t": t, "v": random.randint(68, 84)})
        VITAL_HISTORY["glucose"].append({"t": t, "v": random.randint(96, 128)})
        VITAL_HISTORY["spo2"].append({"t": t, "v": random.randint(96, 99)})
    patient.update(heart_rate=80, glucose=115, spo2=96, missed_calls=0, status="NORMAL")

    def _d(off):
        return (datetime.date.today() - datetime.timedelta(days=off)).strftime("%b %d")
    call_log[:] = [
        {"time": "08:04", "date": _d(0), "mode": "REAL", "lang": "Hindi", "reason": "morning check-in",
         "turns": 6, "level": "ok", "flags": [], "summary": "Slept well, took morning tablets, ate breakfast. Sounded cheerful."},
        {"time": "19:06", "date": _d(1), "mode": "REAL", "lang": "Telugu", "reason": "evening check-in",
         "turns": 5, "level": "watch", "flags": ["not eaten"], "summary": "Skipped dinner - said she had no appetite. Otherwise fine."},
        {"time": "08:11", "date": _d(1), "mode": "REAL", "lang": "Hindi", "reason": "morning check-in",
         "turns": 7, "level": "concern", "flags": ["dizziness", "weakness"], "summary": "Felt dizzy and weak on waking; family was alerted."},
        {"time": "19:02", "date": _d(2), "mode": "REAL", "lang": "English", "reason": "evening check-in",
         "turns": 6, "level": "ok", "flags": [], "summary": "Took evening medicines, watched TV, in good spirits."},
    ]


# Start the vitals simulator once, on import - so it runs under a production
# server (gunicorn) as well as local `python3`. Guarded against double-start.
_bg_started = False


def start_background():
    global _bg_started
    if _bg_started:
        return
    _bg_started = True
    seed_sample_data()
    threading.Thread(target=simulate_vitals, daemon=True).start()


start_background()


if __name__ == "__main__":
    print("=" * 56)
    print("AAYURA (Aasha) dashboard  ->  http://localhost:%d" % PORT)
    print("Calls:", "REAL (Twilio armed)" if CAN_CALL_FOR_REAL else "DEMO mode (scripted, no Twilio keys)")
    print("Brain:", "Claude" if claude else "scripted fallback")
    print("=" * 56)
    app.run(debug=False, host="0.0.0.0", port=PORT)
