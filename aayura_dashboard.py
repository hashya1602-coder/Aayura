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
import random
import threading
import time
import uuid

import requests
from flask import Flask, jsonify, request, render_template_string, Response
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
  .sched { font-size:13px; color:#8b949e; margin-bottom:14px; }
  .sched b { color:#e6edf3; }
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
  <div class="sub">Patient: <b id="pname">-</b> &middot; vitals update every 2s &middot; <span id="callmode"></span></div>

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
        <h3>Demo controls</h3>
        <button onclick="act('glucose_drop')">Simulate glucose drop</button>
        <button onclick="act('hr_spike')">Simulate heart-rate spike</button>
        <button onclick="act('missed_call')">Patient misses a call</button>
        <button class="reset" onclick="act('reset')">Reset to normal</button>
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

function renderSchedule(d){
  document.getElementById('sched').innerHTML =
    'Daily check-ins: <b>' + (d.schedule||[]).join('</b> &middot; <b>') + '</b>';
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

function openDrill(key){
  if(!LAST) return;
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


@app.route("/")
def home():
    return render_template_string(PAGE)


# Start the vitals simulator once, on import - so it runs under a production
# server (gunicorn) as well as local `python3`. Guarded against double-start.
_bg_started = False


def start_background():
    global _bg_started
    if _bg_started:
        return
    _bg_started = True
    threading.Thread(target=simulate_vitals, daemon=True).start()


start_background()


if __name__ == "__main__":
    print("=" * 56)
    print("AAYURA (Aasha) dashboard  ->  http://localhost:%d" % PORT)
    print("Calls:", "REAL (Twilio armed)" if CAN_CALL_FOR_REAL else "DEMO mode (scripted, no Twilio keys)")
    print("Brain:", "Claude" if claude else "scripted fallback")
    print("=" * 56)
    app.run(debug=False, host="0.0.0.0", port=PORT)
