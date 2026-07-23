"""
CareCall - SyncHack Demo
========================
Vitals simulator + rules engine for the elderly-care voice agent project.

How it works:
  1. A background loop generates fake health data every 2 seconds
  2. The rules engine checks every reading for danger
  3. If danger is found -> it triggers the voice call + family alert
  4. You control emergencies with buttons on the dashboard

Run it:   python carecall_demo.py
Open:     http://localhost:5000
"""

import random
import threading
import time
from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

# ---------------------------------------------------------------
# 1. PATIENT STATE  (this is our pretend grandma)
# ---------------------------------------------------------------
patient = {
    "name": "Lakshmi (72)",
    "heart_rate": 72,      # beats per minute
    "glucose": 110,        # mg/dL
    "spo2": 97,            # blood oxygen %
    "missed_calls": 0,
    "status": "NORMAL",    # NORMAL / WARNING / EMERGENCY
}

# When you press a button, we set a "scenario" and the
# simulator drifts the numbers toward danger realistically
scenario = {"mode": "normal"}

alerts = []   # log of everything the rules engine decided


# ---------------------------------------------------------------
# 2. VITALS SIMULATOR  (replaces Apple Watch for the demo)
# ---------------------------------------------------------------
def simulate_vitals():
    """Runs forever in the background. Generates new numbers every 2 sec."""
    while True:
        mode = scenario["mode"]

        if mode == "normal":
            # small random wiggle around healthy values
            patient["heart_rate"] = random.randint(68, 82)
            patient["glucose"] = random.randint(95, 125)
            patient["spo2"] = random.randint(96, 99)

        elif mode == "glucose_drop":
            # sugar falls ~8 points every tick -> hypoglycemia
            patient["glucose"] = max(45, patient["glucose"] - 8)
            patient["heart_rate"] = random.randint(85, 95)  # body reacts

        elif mode == "hr_spike":
            # heart rate climbs -> tachycardia
            patient["heart_rate"] = min(155, patient["heart_rate"] + 10)

        check_rules()   # <- the brain runs after every new reading
        time.sleep(2)


# ---------------------------------------------------------------
# 3. RULES ENGINE  (the brain - YOUR core logic)
# ---------------------------------------------------------------
def check_rules():
    """Deterministic rules. If something is dangerous -> act."""

    hr = patient["heart_rate"]
    glucose = patient["glucose"]

    # RULE 1: low blood sugar (hypoglycemia)
    if glucose < 70:
        patient["status"] = "EMERGENCY"
        raise_alert("LOW_GLUCOSE", f"Glucose critically low: {glucose} mg/dL",
                    call_patient=True, alert_family=True)

    # RULE 2: dangerously fast heart rate
    elif hr > 120:
        patient["status"] = "EMERGENCY"
        raise_alert("HIGH_HR", f"Heart rate too high: {hr} bpm",
                    call_patient=True, alert_family=True)

    # RULE 3: patient not answering repeated calls
    elif patient["missed_calls"] >= 5:
        patient["status"] = "EMERGENCY"
        raise_alert("MISSED_CALLS", "Patient missed 5 calls in a row",
                    call_patient=False, alert_family=True)

    # RULE 4: early warning zone (not emergency yet)
    elif glucose < 85 or hr > 100:
        patient["status"] = "WARNING"

    else:
        patient["status"] = "NORMAL"
        last_alert_type[0] = None   # recovered -> next emergency alerts again


last_alert_type = [None]   # remembers which emergency already fired


def raise_alert(alert_type, reason, call_patient, alert_family):
    """Decides what actions to take. One alert per incident, not per tick."""
    if last_alert_type[0] == alert_type:
        return
    last_alert_type[0] = alert_type

    entry = {
        "time": time.strftime("%H:%M:%S"),
        "reason": reason,
        "actions": [],
    }

    if call_patient:
        trigger_voice_call(reason)
        entry["actions"].append("AI voice call started")

    if alert_family:
        send_family_alert(reason)
        entry["actions"].append("Family alerted via SMS")

    alerts.insert(0, entry)          # newest alert on top
    del alerts[10:]                  # keep only last 10


# ---------------------------------------------------------------
# 4. ACTIONS  (today: pretend. later: real Vapi + Twilio calls)
# ---------------------------------------------------------------
def trigger_voice_call(reason):
    """
    TODO (step 2 of our build):
    Replace this with a real Vapi API call:

    requests.post("https://api.vapi.ai/call",
        headers={"Authorization": "Bearer YOUR_VAPI_KEY"},
        json={"assistantId": "YOUR_ASSISTANT_ID",
              "customer": {"number": "+91XXXXXXXXXX"}})
    """
    print(f"[VOICE CALL] Calling patient because: {reason}")


def send_family_alert(reason):
    """
    TODO (step 3 of our build):
    Replace with Twilio / WhatsApp API to send a real SMS.
    """
    print(f"[FAMILY SMS] {patient['name']} - {reason}")


# ---------------------------------------------------------------
# 5. WEB DASHBOARD  (what the judges see)
# ---------------------------------------------------------------
PAGE = """
<!DOCTYPE html>
<html>
<head>
<title>CareCall - Live Monitor</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * { margin:0; padding:0; box-sizing:border-box; font-family:'Segoe UI',system-ui,sans-serif; }
  body { background:#0d1117; color:#e6edf3; min-height:100vh; padding:32px 20px; }
  .wrap { max-width:900px; margin:0 auto; }
  h1 { font-size:26px; font-weight:600; letter-spacing:0.5px; }
  h1 span { color:#e8a13c; }
  .sub { color:#8b949e; margin:4px 0 28px; font-size:14px; }

  .status { display:inline-block; padding:6px 18px; border-radius:20px;
            font-weight:600; font-size:14px; margin-bottom:24px; }
  .NORMAL    { background:#0f2e1d; color:#4ade80; border:1px solid #1f6f42; }
  .WARNING   { background:#332600; color:#fbbf24; border:1px solid #8a6d1a; }
  .EMERGENCY { background:#3a0d0d; color:#f87171; border:1px solid #a03030;
               animation:pulse 1s infinite; }
  @keyframes pulse { 50% { opacity:0.55; } }

  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
           gap:16px; margin-bottom:28px; }
  .card { background:#161b22; border:1px solid #30363d; border-radius:12px;
          padding:20px; }
  .card .label { color:#8b949e; font-size:13px; margin-bottom:8px; }
  .card .value { font-size:34px; font-weight:600; }
  .card .unit { font-size:14px; color:#8b949e; margin-left:4px; }
  .danger .value { color:#f87171; }
  .warn .value { color:#fbbf24; }

  .controls { background:#161b22; border:1px solid #30363d; border-radius:12px;
              padding:20px; margin-bottom:28px; }
  .controls h3 { font-size:14px; color:#8b949e; margin-bottom:14px;
                 text-transform:uppercase; letter-spacing:1px; }
  button { background:#21262d; color:#e6edf3; border:1px solid #30363d;
           padding:10px 18px; border-radius:8px; cursor:pointer; font-size:14px;
           margin:0 10px 10px 0; transition:all .15s; }
  button:hover { border-color:#e8a13c; color:#e8a13c; }
  button.reset:hover { border-color:#4ade80; color:#4ade80; }

  .alerts { background:#161b22; border:1px solid #30363d; border-radius:12px; padding:20px; }
  .alerts h3 { font-size:14px; color:#8b949e; margin-bottom:14px;
               text-transform:uppercase; letter-spacing:1px; }
  .alert-item { border-left:3px solid #f87171; padding:10px 14px; margin-bottom:10px;
                background:#1c2129; border-radius:0 8px 8px 0; font-size:14px; }
  .alert-item .t { color:#8b949e; font-size:12px; }
  .alert-item .a { color:#e8a13c; font-size:13px; margin-top:4px; }
  .empty { color:#4b5563; font-size:14px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Care<span>Call</span> &nbsp;|&nbsp; Live health monitor</h1>
  <div class="sub">Patient: <b id="pname">-</b> &middot; data updates every 2 seconds</div>

  <div id="status" class="status NORMAL">NORMAL</div>

  <div class="cards">
    <div class="card" id="c-hr">
      <div class="label">Heart rate</div>
      <div><span class="value" id="hr">-</span><span class="unit">bpm</span></div>
    </div>
    <div class="card" id="c-gl">
      <div class="label">Blood glucose</div>
      <div><span class="value" id="gl">-</span><span class="unit">mg/dL</span></div>
    </div>
    <div class="card">
      <div class="label">Blood oxygen</div>
      <div><span class="value" id="sp">-</span><span class="unit">%</span></div>
    </div>
    <div class="card">
      <div class="label">Missed calls</div>
      <div><span class="value" id="mc">-</span><span class="unit">/5</span></div>
    </div>
  </div>

  <div class="controls">
    <h3>Demo controls (judge can't see this is you)</h3>
    <button onclick="act('glucose_drop')">Simulate glucose drop</button>
    <button onclick="act('hr_spike')">Simulate heart rate spike</button>
    <button onclick="act('missed_call')">Patient misses a call</button>
    <button class="reset" onclick="act('reset')">Reset to normal</button>
  </div>

  <div class="alerts">
    <h3>Alert log (rules engine decisions)</h3>
    <div id="alertbox"><div class="empty">No alerts yet - all healthy</div></div>
  </div>
</div>

<script>
async function act(what) {
  await fetch('/action/' + what, {method:'POST'});
}

async function refresh() {
  const r = await fetch('/data');
  const d = await r.json();
  document.getElementById('pname').textContent = d.patient.name;
  document.getElementById('hr').textContent = d.patient.heart_rate;
  document.getElementById('gl').textContent = d.patient.glucose;
  document.getElementById('sp').textContent = d.patient.spo2;
  document.getElementById('mc').textContent = d.patient.missed_calls;

  const st = document.getElementById('status');
  st.textContent = d.patient.status;
  st.className = 'status ' + d.patient.status;

  document.getElementById('c-hr').className =
      'card' + (d.patient.heart_rate > 120 ? ' danger' : d.patient.heart_rate > 100 ? ' warn' : '');
  document.getElementById('c-gl').className =
      'card' + (d.patient.glucose < 70 ? ' danger' : d.patient.glucose < 85 ? ' warn' : '');

  const box = document.getElementById('alertbox');
  if (d.alerts.length === 0) {
    box.innerHTML = '<div class="empty">No alerts yet - all healthy</div>';
  } else {
    box.innerHTML = d.alerts.map(a =>
      `<div class="alert-item">
         <div class="t">${a.time}</div>
         <div>${a.reason}</div>
         <div class="a">→ ${a.actions.join(' &middot; ')}</div>
       </div>`).join('');
  }
}
setInterval(refresh, 1000);
refresh();
</script>
</body>
</html>
"""


@app.route("/")
def home():
    return render_template_string(PAGE)


@app.route("/data")
def data():
    return jsonify({"patient": patient, "alerts": alerts})


@app.route("/action/<what>", methods=["POST"])
def action(what):
    if what == "reset":
        scenario["mode"] = "normal"
        patient["missed_calls"] = 0
        patient["glucose"] = 110
        patient["heart_rate"] = 72
        alerts.clear()
    elif what == "missed_call":
        patient["missed_calls"] += 1
        check_rules()
    else:
        scenario["mode"] = what
    return jsonify({"ok": True})


# ---------------------------------------------------------------
# START
# ---------------------------------------------------------------
if __name__ == "__main__":
    # start the fake-sensor loop in the background
    threading.Thread(target=simulate_vitals, daemon=True).start()
    print("CareCall demo running -> open http://localhost:5001")
    app.run(debug=False, port=5001)
