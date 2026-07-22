"""
KRISHA PHONE - the real-phone-call version of CareCall
======================================================
(Astraya architecture, rebuilt for elderly care)

Krisha calls the patient's real phone, speaks with a sweet female
Indian voice (Polly Aditi), LISTENS to what they say, thinks with
Claude, and answers - a real back-and-forth phone conversation.

How a call works:
  1. You run this server + ngrok (so Twilio can reach your laptop)
  2. Something triggers a call (you, or the CareCall rules engine)
  3. Twilio dials the patient -> asks our /voice route what to say
  4. Patient speaks -> Twilio converts to text -> sends to /respond
  5. /respond asks Claude for Krisha's reply -> speaks it -> listens again
  6. When Krisha decides the call is done she says goodbye and hangs up

Lessons from the Astraya project already fixed here:
  - Port 5002 (5000 = macOS AirPlay trap, 5001 = CareCall dashboard)
  - /health route to CHECK the ngrok tunnel BEFORE burning a call
  - Clear console prints at every step so you can see where it breaks

------------------------------------------------------------------
SETUP (one time)
------------------------------------------------------------------
1. Terminal:  pip3 install flask twilio anthropic python-dotenv
2. Make a file named  .env  in the same folder, containing:

   TWILIO_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   TWILIO_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   TWILIO_NUMBER=+1xxxxxxxxxx
   ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxx
   NGROK_URL=
   PATIENT_PHONE=+919160220119
   PATIENT_NAME=Lakshmi

3. In Twilio console: Geo Permissions -> India ENABLED,
   and PATIENT_PHONE must be a VERIFIED caller ID (trial account rule)

------------------------------------------------------------------
RUNNING (three terminals, like Astraya)
------------------------------------------------------------------
  Terminal 1:  python3 krisha_phone.py          (this server)
  Terminal 2:  ngrok http 5002                  (the tunnel)
     -> copy the https://xxxx.ngrok-free.app URL into .env NGROK_URL
     -> restart Terminal 1
  Browser: open  <ngrok-url>/health   - must say "Krisha is ready"
  Terminal 3:  curl -X POST http://localhost:5002/make_call
------------------------------------------------------------------
"""

import os
import threading
import time as time_module
from flask import Flask, request, jsonify
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client
from dotenv import load_dotenv
import anthropic

load_dotenv()

TWILIO_SID = os.getenv("TWILIO_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_TOKEN", "")
TWILIO_NUMBER = os.getenv("TWILIO_NUMBER", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")     # free option: aistudio.google.com
NGROK_URL = (os.getenv("NGROK_URL", "") or "").rstrip("/")   # no trailing slash!
PATIENT_PHONE = os.getenv("PATIENT_PHONE", "+919160220119")
PATIENT_NAME = os.getenv("PATIENT_NAME", "Radha")
FAMILY_PHONE = os.getenv("FAMILY_PHONE", "")        # who gets alerts
MORNING_CALL = os.getenv("MORNING_CALL", "08:00")   # daily call times (24h)
EVENING_CALL = os.getenv("EVENING_CALL", "19:00")

VOICE = "Polly.Aditi"        # sweet female Indian-English voice
PORT = 5002

# ---------- KRISHA'S SCHEDULE & ESCALATION ----------
MORNING_CALL = "08:00"     # daily morning check-in (24h format)
EVENING_CALL = "19:00"     # daily evening check-in
FAMILY_PHONE = os.getenv("FAMILY_PHONE", "")   # who to alert (must be verified on trial!)
MAX_RETRIES = 2        # how many extra tries if patient doesn't answer
RETRY_GAP_SEC = 60     # wait between retries (60s for demo; 15 min in production)

app = Flask(__name__)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# conversation memory per call (resets when server restarts - fine for demo)
conversations = {}
# tracks unanswered-call attempts
retry_state = {"attempts": 0, "reason": ""}

KRISHA_PROMPT = f"""You are Krisha, a warm, lively AI companion speaking on a
PHONE CALL with {PATIENT_NAME}, a 72-year-old woman in India. You call her
every morning and evening to check on her medicines and her wellbeing.

How to talk:
- You are on a phone: keep every reply SHORT - one or two small sentences.
- Warm, playful, human - like a favourite granddaughter, never robotic or
  formal. Use her name sometimes ("{PATIENT_NAME} amma").
- One question at a time, then really respond to her answer before moving on.
- Be interactive and varied: ask about her sleep, her food ("what did you eat
  amma?"), her garden, family, what she watched on TV, her mood. React with
  warmth ("that sounds lovely!", "haha, really?").
- Always cover the important things naturally during the chat: has she taken
  her morning/evening tablets? Has she eaten? How is she feeling?
- If she has not taken medicine, gently encourage her, and ask her to take it
  while you wait on the call.
- KEEP THE CONVERSATION GOING. Do not end the call yourself. Only say goodbye
  when SHE says bye / says she wants to go. Then end warmly and add the token
  <END> at the very end of that goodbye.
- If she mentions chest pain, dizziness, falling, breathlessness, or sounds
  confused or very unwell: stay calm and caring, tell her that her family is
  being informed right away and she should sit down, and add the token
  <ALERT> at the end of that reply (never speak the word ALERT aloud).
- Never give medical advice, dosages or diagnoses.
- Never speak the tokens END or ALERT out loud."""


def ask_krisha(call_sid, patient_said):
    """Send the conversation to the AI brain (Claude or Gemini), get Krisha's next line."""
    history = conversations.setdefault(call_sid, [])
    history.append({"role": "user", "content": patient_said})
    try:
        if claude:
            # Brain option 1: Claude
            reply = claude.messages.create(
                model="claude-sonnet-5",
                max_tokens=150,
                system=KRISHA_PROMPT,
                messages=history,
            ).content[0].text
        elif GEMINI_API_KEY:
            # Brain option 2: Gemini (free tier)
            import requests as rq
            contents = [{"role": ("user" if m["role"] == "user" else "model"),
                         "parts": [{"text": m["content"]}]} for m in history]
            r = rq.post(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}",
                json={"system_instruction": {"parts": [{"text": KRISHA_PROMPT}]},
                      "contents": contents,
                      "generationConfig": {"maxOutputTokens": 150}},
                timeout=20)
            r.raise_for_status()
            reply = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        else:
            reply = "I am sorry amma, my brain key is missing. Goodbye. <END>"
    except Exception as e:
        print(f"[BRAIN ERROR] {e}")
        reply = "I am sorry amma, I will call you again shortly. Take care. <END>"
    history.append({"role": "assistant", "content": reply})
    return reply


def say_and_listen(text):
    """Build TwiML: speak Krisha's line, then listen for the patient."""
    vr = VoiceResponse()
    gather = Gather(input="speech", action="/respond", method="POST",
                    language="en-IN", speech_timeout="auto")
    gather.say(text, voice=VOICE)
    vr.append(gather)
    # if the patient says nothing at all, gently check once
    vr.say("Amma, are you there?", voice=VOICE)
    vr.redirect("/voice")
    return str(vr)


# ---------------------------------------------------------------
# ROUTES (Twilio talks to these through ngrok)
# ---------------------------------------------------------------
@app.route("/health")
def health():
    brain = bool(ANTHROPIC_API_KEY or GEMINI_API_KEY)
    ready = all([TWILIO_SID, TWILIO_TOKEN, TWILIO_NUMBER, brain])
    return ("Krisha is ready" if ready else
            "Missing keys in .env - need TWILIO_SID / TOKEN / NUMBER "
            "and a brain key (ANTHROPIC_API_KEY or GEMINI_API_KEY)")


@app.route("/voice", methods=["POST", "GET"])
def voice():
    """First thing spoken when the patient picks up."""
    import datetime
    hour = datetime.datetime.now().hour
    part = "morning" if hour < 12 else ("afternoon" if hour < 17 else "evening")
    print("[CALL] Patient answered - Krisha greeting now")
    greeting = (f"Good {part} {PATIENT_NAME} amma! This is Krisha. "
                f"I called to check on you. How are you feeling today?")
    return say_and_listen(greeting)


def send_family_alert(reason):
    """Alert the family - real SMS via Twilio if FAMILY_PHONE is set."""
    print(f"[FAMILY ALERT] {PATIENT_NAME}: {reason}")
    if not FAMILY_PHONE:
        print("[FAMILY ALERT] (no FAMILY_PHONE in .env - printed only)")
        return
    try:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        client.messages.create(
            to=FAMILY_PHONE, from_=TWILIO_NUMBER,
            body=f"CareCall ALERT: {PATIENT_NAME} may need attention - {reason}. "
                 f"Please call her now.")
        print(f"[FAMILY ALERT] SMS sent to {FAMILY_PHONE}")
    except Exception as e:
        print(f"[FAMILY ALERT] SMS failed ({e}) - alert printed only")


@app.route("/respond", methods=["POST"])
def respond():
    """Patient spoke -> Twilio sends the text here -> Krisha replies."""
    call_sid = request.form.get("CallSid", "unknown")
    patient_said = request.form.get("SpeechResult", "")
    print(f"[HEARD] {PATIENT_NAME}: {patient_said}")

    if not patient_said:
        return say_and_listen("Sorry amma, I could not hear you. Could you say that again?")

    reply = ask_krisha(call_sid, patient_said)
    print(f"[KRISHA] {reply}")

    # health concern detected mid-conversation -> alert family, keep talking
    if "<ALERT>" in reply:
        send_family_alert(f"reported feeling unwell during call ('{patient_said}')")
        reply = reply.replace("<ALERT>", "").strip()

    if "<END>" in reply:
        vr = VoiceResponse()
        vr.say(reply.replace("<END>", "").strip(), voice=VOICE)
        vr.hangup()
        print("[CALL] Krisha ended the call warmly")
        return str(vr)

    return say_and_listen(reply)


@app.route("/call-status", methods=["POST"])
def call_status():
    """Twilio reports how the call ended - the escalation ladder lives here."""
    status = request.form.get("CallStatus", "")
    print(f"[STATUS] {status}")

    if status in ("no-answer", "busy", "failed"):
        retry_state["attempts"] += 1
        n = retry_state["attempts"]
        if n <= MAX_RETRIES:
            print(f"[ESCALATION] {PATIENT_NAME} didn't answer "
                  f"(attempt {n}/{MAX_RETRIES + 1}) - retrying in {RETRY_GAP_SEC}s")
            def retry_later():
                time_module.sleep(RETRY_GAP_SEC)
                try:
                    import urllib.request
                    urllib.request.urlopen(f"http://localhost:{PORT}/make_call", timeout=10)
                except Exception as e:
                    print(f"[ESCALATION] retry failed: {e}")
            threading.Thread(target=retry_later, daemon=True).start()
        else:
            print(f"[ESCALATION] {PATIENT_NAME} missed all calls - alerting family!")
            send_family_alert(f"did not answer {n} calls in a row")
            call_family()
            retry_state["attempts"] = 0

    elif status == "completed":
        retry_state["attempts"] = 0     # she answered - reset the ladder
    return ("", 204)


def call_family():
    """Ring the family member with a spoken alert."""
    if not (FAMILY_PHONE and NGROK_URL):
        print("[FAMILY CALL] skipped (FAMILY_PHONE or NGROK_URL empty)")
        return
    try:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        call = client.calls.create(
            to=FAMILY_PHONE, from_=TWILIO_NUMBER,
            url=f"{NGROK_URL}/family_voice")
        print(f"[FAMILY CALL] Dialing family {FAMILY_PHONE}... SID {call.sid}")
    except Exception as e:
        print(f"[FAMILY CALL] failed: {e}")


@app.route("/family_voice", methods=["POST", "GET"])
def family_voice():
    """What the family member hears when Krisha calls them."""
    vr = VoiceResponse()
    vr.say(f"Hello, this is Krisha from CareCall. {PATIENT_NAME} did not answer "
           f"her check-in calls today. Please call her or check on her as soon "
           f"as possible. Thank you.", voice=VOICE)
    vr.say("Repeating. " + f"{PATIENT_NAME} did not answer her check-in calls. "
           f"Please check on her soon. Goodbye.", voice=VOICE)
    return str(vr)


@app.route("/make_call", methods=["POST", "GET"])
def make_call():
    """Trigger the outbound call to the patient."""
    if not NGROK_URL:
        return jsonify({"error": "NGROK_URL is empty in .env - start ngrok, paste URL, restart"}), 400
    try:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        call = client.calls.create(
            to=PATIENT_PHONE,
            from_=TWILIO_NUMBER,
            url=f"{NGROK_URL}/voice",
            status_callback=f"{NGROK_URL}/call-status",
            status_callback_event=["completed"],   # final status arrives here
            timeout=25,     # ring ~25s before counting as no-answer
        )
        print(f"[CALL] Dialing {PATIENT_PHONE}... SID {call.sid}")
        return jsonify({"status": "calling", "sid": call.sid})
    except Exception as e:
        print(f"[CALL ERROR] {e}")
        return jsonify({"error": str(e)}), 500


def scheduler():
    """Checks the clock every 30s - triggers the morning & evening calls."""
    import datetime
    already_called = set()
    while True:
        now = datetime.datetime.now().strftime("%H:%M")
        today = datetime.date.today().isoformat()
        for slot in (MORNING_CALL, EVENING_CALL):
            key = f"{today}-{slot}"
            if now == slot and key not in already_called:
                already_called.add(key)
                print(f"[SCHEDULE] It's {slot} - time for the daily call!")
                try:
                    import urllib.request
                    urllib.request.urlopen(f"http://localhost:{PORT}/make_call", timeout=10)
                except Exception as e:
                    print(f"[SCHEDULE] Could not start call: {e}")
        time_module.sleep(30)


if __name__ == "__main__":
    threading.Thread(target=scheduler, daemon=True).start()
    print("=" * 50)
    print("KRISHA PHONE server starting on port", PORT)
    print(f"Daily calls scheduled at {MORNING_CALL} and {EVENING_CALL}")
    print("Health check:  http://localhost:5002/health")
    print("=" * 50)
    app.run(debug=False, port=PORT)
