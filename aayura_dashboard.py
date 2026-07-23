"""
AAYURA - Unified CareCall + Krisha dashboard
============================================
One screen that shows BOTH halves of the elderly-care system:

  LEFT  - CareCall live vitals monitor (heart rate, glucose, SpO2,
          missed calls) driven by a fake-sensor loop + a rules engine.
  RIGHT - Krisha, the AI voice companion: call status, a "Call now"
          button, and the live conversation transcript.

The two are wired together: when the rules engine detects an EMERGENCY
it automatically triggers a Krisha call and alerts the family - and you
watch the whole thing happen on one page.

Calls work two ways:
  - REAL  : if Twilio + NGROK_URL are set in .env, it dials the patient's
            real phone (same TwiML flow as krisha_phone.py).
  - DEMO  : if no credentials, it runs a scripted simulated call so the
            dashboard still shows a live conversation for judges.

Run it:   python3 aayura_dashboard.py
Open:     http://localhost:5001
"""

import os
import random
import threading
import time

from flask import Flask, jsonify, request, render_template_string
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
TWILIO_SID = os.getenv("TWILIO_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_TOKEN", "")
TWILIO_NUMBER = os.getenv("TWILIO_NUMBER", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
NGROK_URL = (os.getenv("NGROK_URL", "") or "").rstrip("/")
PATIENT_PHONE = os.getenv("PATIENT_PHONE", "+919160220119")
PATIENT_NAME = os.getenv("PATIENT_NAME", "Lakshmi")
FAMILY_PHONE = os.getenv("FAMILY_PHONE", "")

VOICE = "Polly.Aditi"
PORT = 5001

app = Flask(__name__)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if (anthropic and ANTHROPIC_API_KEY) else None
# Set DEMO_MODE=1 to force scripted (fake) calls even when Twilio keys exist -
# use this while rehearsing so you never dial the patient's real phone.
DEMO_MODE = os.getenv("DEMO_MODE", "").lower() in ("1", "true", "yes")
CAN_CALL_FOR_REAL = (not DEMO_MODE) and bool(
    Client and TWILIO_SID and TWILIO_TOKEN and TWILIO_NUMBER and NGROK_URL)

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

# Krisha call state, shared with the dashboard
call_state = {
    "status": "idle",         # idle / dialing / in_call / ended
    "mode": "-",              # REAL / DEMO
    "reason": None,
    "family_alerted": False,
}
transcript = []               # chronological: {who, text, time}
conversations = {}            # per-CallSid history for the AI brain
_call_lock = threading.Lock()


def add_turn(who, text):
    """who = 'patient' | 'krisha' | 'system'."""
    transcript.append({"who": who, "text": text, "time": time.strftime("%H:%M:%S")})
    del transcript[:-60]


# ---------------------------------------------------------------
# KRISHA'S BRAIN
# ---------------------------------------------------------------
KRISHA_PROMPT = f"""You are Krisha, a warm AI companion on a PHONE CALL with
{PATIENT_NAME}, a 72-year-old woman in India. Keep every reply SHORT - one or
two small sentences, warm and human like a favourite granddaughter. Ask one
thing at a time. Gently check she has taken her medicines and eaten. If she
mentions chest pain, dizziness, falling, breathlessness or sounds very unwell,
stay calm, tell her family is being informed and she should sit down, and add
the token <ALERT> at the very end (never say the word aloud). Only say goodbye
when SHE wants to go, then add <END>. Never give medical advice or dosages."""


def ask_krisha(call_sid, patient_said):
    history = conversations.setdefault(call_sid, [])
    history.append({"role": "user", "content": patient_said})
    try:
        if claude:
            reply = claude.messages.create(
                model="claude-sonnet-5",
                max_tokens=150,
                system=KRISHA_PROMPT,
                messages=history,
            ).content[0].text
        else:
            reply = "I am here with you amma. Please sit and rest, I will stay on the line. <END>"
    except Exception as e:
        print(f"[BRAIN ERROR] {e}")
        reply = "I am sorry amma, I will call you again shortly. Take care. <END>"
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
        entry["actions"].append("AI voice call started")
    if alert_family:
        send_family_alert(reason)
        entry["actions"].append("Family alerted")
    alerts.insert(0, entry)
    del alerts[10:]


# ---------------------------------------------------------------
# ACTIONS - calling the patient (real Twilio or scripted demo)
# ---------------------------------------------------------------
def trigger_voice_call(reason):
    """Fired by the rules engine or the dashboard button."""
    if call_state["status"] in ("dialing", "in_call"):
        return  # a call is already happening
    if CAN_CALL_FOR_REAL:
        start_real_call(reason)
    else:
        threading.Thread(target=run_simulated_call, args=(reason,), daemon=True).start()


def start_real_call(reason):
    call_state.update(status="dialing", mode="REAL", reason=reason, family_alerted=False)
    transcript.clear()
    add_turn("system", f"Dialing {PATIENT_PHONE} ... ({reason})")
    try:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        call = client.calls.create(
            to=PATIENT_PHONE, from_=TWILIO_NUMBER,
            url=f"{NGROK_URL}/voice",
            status_callback=f"{NGROK_URL}/call-status",
            status_callback_event=["completed"],
            timeout=25,
        )
        print(f"[CALL] Dialing {PATIENT_PHONE}... SID {call.sid}")
    except Exception as e:
        print(f"[CALL ERROR] {e}")
        add_turn("system", f"Call failed: {e}")
        call_state["status"] = "ended"


SIM_SCRIPT = [
    ("krisha", "{name} amma, this is Krisha. Your monitor just showed something worrying - {reason}. Are you alright?"),
    ("patient", "I feel a little dizzy and weak, beta."),
    ("krisha", "Okay amma, please sit down slowly right now. I have already told your family - they are on the way."),
    ("patient", "Okay... thank you."),
    ("krisha", "Stay sitting and keep talking to me. If you have juice or something sweet nearby, take a small sip amma."),
    ("patient", "I have some juice here."),
    ("krisha", "Good, sip it slowly. I will stay right here with you until your family reaches you."),
]


def run_simulated_call(reason):
    with _call_lock:
        call_state.update(status="dialing", mode="DEMO", reason=reason, family_alerted=False)
        transcript.clear()
        add_turn("system", f"Krisha is calling {PATIENT_NAME} ... ({reason})")
        time.sleep(1.4)
        call_state["status"] = "in_call"
        for who, line in SIM_SCRIPT:
            add_turn(who, line.format(name=PATIENT_NAME, reason=reason))
            time.sleep(1.9)
        add_turn("system", "Call ended - Krisha stayed until family arrived.")
        call_state["status"] = "ended"


def send_family_alert(reason):
    call_state["family_alerted"] = True
    print(f"[FAMILY ALERT] {patient['name']}: {reason}")
    if not (Client and FAMILY_PHONE and TWILIO_SID):
        return
    try:
        Client(TWILIO_SID, TWILIO_TOKEN).messages.create(
            to=FAMILY_PHONE, from_=TWILIO_NUMBER,
            body=f"CareCall ALERT: {patient['name']} may need attention - {reason}. Please call her now.")
        print(f"[FAMILY ALERT] SMS sent to {FAMILY_PHONE}")
    except Exception as e:
        print(f"[FAMILY ALERT] SMS failed ({e})")


# ---------------------------------------------------------------
# TWILIO WEBHOOK ROUTES (used only for REAL calls)
# ---------------------------------------------------------------
def say_and_listen(text):
    vr = VoiceResponse()
    gather = Gather(input="speech", action="/respond", method="POST",
                    language="en-IN", speech_timeout="auto")
    gather.say(text, voice=VOICE)
    vr.append(gather)
    vr.say("Amma, are you there?", voice=VOICE)
    vr.redirect("/voice")
    return str(vr)


@app.route("/voice", methods=["POST", "GET"])
def voice():
    import datetime
    hour = datetime.datetime.now().hour
    part = "morning" if hour < 12 else ("afternoon" if hour < 17 else "evening")
    call_state["status"] = "in_call"
    greeting = (f"Good {part} {PATIENT_NAME} amma! This is Krisha. "
                f"I called to check on you. How are you feeling today?")
    add_turn("krisha", greeting)
    return say_and_listen(greeting)


@app.route("/respond", methods=["POST"])
def respond():
    call_sid = request.form.get("CallSid", "unknown")
    patient_said = request.form.get("SpeechResult", "")
    if not patient_said:
        return say_and_listen("Sorry amma, I could not hear you. Could you say that again?")
    add_turn("patient", patient_said)
    reply = ask_krisha(call_sid, patient_said)

    if "<ALERT>" in reply:
        send_family_alert(f"reported feeling unwell during call ('{patient_said}')")
        reply = reply.replace("<ALERT>", "").strip()
    clean = reply.replace("<END>", "").strip()
    add_turn("krisha", clean)

    if "<END>" in reply:
        vr = VoiceResponse()
        vr.say(clean, voice=VOICE)
        vr.hangup()
        call_state["status"] = "ended"
        return str(vr)
    return say_and_listen(clean)


@app.route("/call-status", methods=["POST"])
def call_status():
    if request.form.get("CallStatus", "") == "completed":
        call_state["status"] = "ended"
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
        "transcript": transcript,
        "can_call_real": CAN_CALL_FOR_REAL,
    })


@app.route("/action/<what>", methods=["POST"])
def action(what):
    if what == "reset":
        scenario["mode"] = "normal"
        patient.update(missed_calls=0, glucose=110, heart_rate=72)
        alerts.clear()
        last_alert_type[0] = None
        call_state.update(status="idle", mode="-", reason=None, family_alerted=False)
        transcript.clear()
    elif what == "missed_call":
        patient["missed_calls"] += 1
        check_rules()
    elif what == "call_now":
        trigger_voice_call("manual check-in from dashboard")
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
<title>Aayura - CareCall + Krisha</title>
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

  .callhead { display:flex; align-items:center; gap:12px; margin-bottom:14px; }
  .dot { width:12px; height:12px; border-radius:50%; background:#4b5563; }
  .dot.dialing { background:#fbbf24; animation:pulse 0.8s infinite; }
  .dot.in_call { background:#4ade80; animation:pulse 1.2s infinite; }
  .dot.ended { background:#8b949e; }
  .callstat { font-size:15px; font-weight:600; }
  .badge { font-size:11px; padding:2px 8px; border-radius:10px; border:1px solid #30363d; color:#8b949e; margin-left:auto; }
  .fam { font-size:13px; color:#f87171; margin-bottom:12px; display:none; }
  .fam.on { display:block; }

  .chat { background:#0d1117; border:1px solid #30363d; border-radius:10px; padding:14px;
          height:340px; overflow-y:auto; display:flex; flex-direction:column; gap:10px; }
  .bubble { max-width:82%; padding:9px 13px; border-radius:12px; font-size:14px; line-height:1.4; }
  .bubble .who { font-size:11px; color:#8b949e; margin-bottom:3px; }
  .krisha { align-self:flex-start; background:#1c2129; border:1px solid #30363d; border-radius:12px 12px 12px 2px; }
  .patient { align-self:flex-end; background:#16281c; border:1px solid #1f6f42; border-radius:12px 12px 2px 12px; }
  .system { align-self:center; background:transparent; color:#6b7280; font-size:12px; font-style:italic; }
  .chat .empty { color:#4b5563; font-size:14px; margin:auto; }

  .alerts .alert-item { border-left:3px solid #f87171; padding:9px 13px; margin-bottom:9px;
                background:#0d1117; border-radius:0 8px 8px 0; font-size:14px; }
  .alerts .t { color:#8b949e; font-size:12px; }
  .alerts .a { color:#e8a13c; font-size:13px; margin-top:4px; }
  .empty { color:#4b5563; font-size:14px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Aa<span>yura</span> &nbsp;|&nbsp; CareCall + Krisha</h1>
  <div class="sub">Patient: <b id="pname">-</b> &middot; vitals update every 2s &middot; <span id="callmode"></span></div>

  <div class="grid">
    <!-- LEFT: vitals + rules -->
    <div>
      <div class="panel">
        <div id="status" class="status NORMAL">NORMAL</div>
        <div class="cards">
          <div class="card" id="c-hr"><div class="label">Heart rate</div><div><span class="value" id="hr">-</span><span class="unit">bpm</span></div></div>
          <div class="card" id="c-gl"><div class="label">Blood glucose</div><div><span class="value" id="gl">-</span><span class="unit">mg/dL</span></div></div>
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
      </div>

      <div class="panel alerts">
        <h3>Alert log (rules engine)</h3>
        <div id="alertbox"><div class="empty">No alerts yet - all healthy</div></div>
      </div>
    </div>

    <!-- RIGHT: Krisha -->
    <div class="panel">
      <div class="callhead">
        <span class="dot" id="dot"></span>
        <span class="callstat" id="callstat">Krisha idle</span>
        <span class="badge" id="callbadge"></span>
      </div>
      <div class="fam" id="fam">Family has been alerted</div>
      <button class="call" onclick="act('call_now')">Call patient now</button>
      <div class="chat" id="chat"><div class="empty">No call yet. Trigger an emergency or press "Call patient now".</div></div>
    </div>
  </div>
</div>

<script>
const STAT = {idle:'Krisha idle', dialing:'Dialing patient...', in_call:'On call', ended:'Call ended'};
async function act(what){ await fetch('/action/'+what, {method:'POST'}); refresh(); }

async function refresh(){
  const d = await (await fetch('/data')).json();
  document.getElementById('pname').textContent = d.patient.name;
  document.getElementById('callmode').textContent = d.can_call_real ? 'REAL calls armed (Twilio)' : 'DEMO mode (no Twilio keys)';
  document.getElementById('hr').textContent = d.patient.heart_rate;
  document.getElementById('gl').textContent = d.patient.glucose;
  document.getElementById('sp').textContent = d.patient.spo2;
  document.getElementById('mc').textContent = d.patient.missed_calls;

  const st = document.getElementById('status');
  st.textContent = d.patient.status; st.className = 'status ' + d.patient.status;
  document.getElementById('c-hr').className = 'card' + (d.patient.heart_rate>120?' danger':d.patient.heart_rate>100?' warn':'');
  document.getElementById('c-gl').className = 'card' + (d.patient.glucose<70?' danger':d.patient.glucose<85?' warn':'');

  // Krisha panel
  document.getElementById('dot').className = 'dot ' + d.call.status;
  document.getElementById('callstat').textContent = STAT[d.call.status] || 'Krisha';
  document.getElementById('callbadge').textContent = d.call.mode !== '-' ? d.call.mode : '';
  document.getElementById('fam').className = 'fam' + (d.call.family_alerted ? ' on' : '');

  const chat = document.getElementById('chat');
  if(!d.transcript.length){
    chat.innerHTML = '<div class="empty">No call yet. Trigger an emergency or press "Call patient now".</div>';
  } else {
    const near = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 60;
    chat.innerHTML = d.transcript.map(t => t.who==='system'
      ? `<div class="bubble system">${t.text}</div>`
      : `<div class="bubble ${t.who}"><div class="who">${t.who==='krisha'?'Krisha':'Lakshmi'} &middot; ${t.time}</div>${t.text}</div>`
    ).join('');
    if(near) chat.scrollTop = chat.scrollHeight;
  }

  const box = document.getElementById('alertbox');
  box.innerHTML = d.alerts.length ? d.alerts.map(a =>
    `<div class="alert-item"><div class="t">${a.time}</div><div>${a.reason}</div><div class="a">-> ${a.actions.join(' &middot; ')}</div></div>`
  ).join('') : '<div class="empty">No alerts yet - all healthy</div>';
}
setInterval(refresh, 1000); refresh();
</script>
</body>
</html>
"""


@app.route("/")
def home():
    return render_template_string(PAGE)


if __name__ == "__main__":
    threading.Thread(target=simulate_vitals, daemon=True).start()
    print("=" * 56)
    print("AAYURA unified dashboard  ->  http://localhost:%d" % PORT)
    print("Calls:", "REAL (Twilio armed)" if CAN_CALL_FOR_REAL else "DEMO mode (scripted, no Twilio keys)")
    print("Brain:", "Claude" if claude else "scripted fallback")
    print("=" * 56)
    app.run(debug=False, port=PORT)
