"""
AASHA RELAY - low-latency, interruptible voice agent (Twilio ConversationRelay)
===============================================================================
This is the "human, no-latency" version of Aasha. Instead of the turn-based
<Gather>/<Say> loop (speak -> full STT -> full LLM -> full TTS), Twilio's
ConversationRelay streams the call both ways over a WebSocket:

  caller speaks  ->  Twilio streams transcribed text to us (/relay-ws)
  we stream Claude Haiku tokens back  ->  Twilio speaks them as they arrive
  caller talks over her  ->  Twilio stops the TTS instantly (barge-in)

Why it feels faster:
  - Streaming TTS starts talking on the FIRST token, not after the whole reply
  - Haiku 4.5 is Anthropic's fastest model
  - Barge-in (interruptible="any") lets the patient cut in, like a real call

LANGUAGES: English + Hindi. Telugu is NOT supported by ConversationRelay's TTS
(Google/Amazon/ElevenLabs), so Telugu callers fall back to Hindi voice here.
For Telugu voice, use the <Play>+Sarvam path in aayura_dashboard.py instead.

------------------------------------------------------------------
RUN (local, with your existing ngrok reserved domain)
------------------------------------------------------------------
  pip3 install flask flask-sock twilio anthropic python-dotenv
  Terminal 1:  python3 aasha_relay.py                 (this server, port 5003)
  Terminal 2:  ~/ngrok http 5003 --url=crease-unsure-gallon.ngrok-free.dev
  Terminal 3:  curl -X POST http://localhost:5003/call_me
Your .env (in ~/Desktop) supplies TWILIO_*, ANTHROPIC_API_KEY, NGROK_URL,
PATIENT_PHONE, PATIENT_NAME.
"""

import json
import os
import threading
from xml.sax.saxutils import escape

from flask import Flask, Response, jsonify, request
from flask_sock import Sock
from dotenv import load_dotenv

try:
    from twilio.rest import Client
except Exception:
    Client = None

try:
    import anthropic
except Exception:
    anthropic = None

load_dotenv()

TWILIO_SID = os.getenv("TWILIO_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_TOKEN", "")
TWILIO_NUMBER = os.getenv("TWILIO_NUMBER", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
PUBLIC_URL = (os.getenv("PUBLIC_URL", "") or os.getenv("NGROK_URL", "") or "").rstrip("/")
PATIENT_PHONE = os.getenv("PATIENT_PHONE", "")
PATIENT_NAME = os.getenv("PATIENT_NAME", "Lakshmi")
FAMILY_PHONE = os.getenv("FAMILY_PHONE", "")     # relative to alert if patient doesn't answer
PORT = int(os.getenv("PORT", "5003"))
RELAY_VOICE = os.getenv("RELAY_VOICE", "en-IN-Neural2-A")   # Google TTS voice id

# https -> wss so Twilio opens the media WebSocket on the same tunnel/domain
WSS_URL = PUBLIC_URL.replace("https://", "wss://").replace("http://", "ws://")

app = Flask(__name__)
sock = Sock(app)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if (anthropic and ANTHROPIC_API_KEY) else None

# Per-call conversation history for Claude
conversations = {}

# No <END>/<ALERT> tokens here - in a streaming voice call we let the caller
# hang up naturally, and tokens would get spoken aloud before we could strip them.
AASHA_PROMPT = f"""You are Aasha, a warm, caring AI companion on a live PHONE
CALL with {PATIENT_NAME}, a 72-year-old person in India.

Reply in the SAME language the patient speaks - English or Hindi (Devanagari).
Keep every reply SHORT: one or two small sentences, warm and human like a
favourite granddaughter. Ask one gentle thing at a time - how they slept,
whether they ate, if they took their medicines, how they feel. If they sound
unwell, stay calm and tell them you are letting their family know. Never give
medical advice or dosages. Do not use any special tokens or symbols."""

# Prompt for when Aasha reaches the RELATIVE instead of the patient
AASHA_FAMILY_PROMPT = f"""You are Aasha, a warm AI care companion, on a live
PHONE CALL with a FAMILY MEMBER of {PATIENT_NAME}, a 72-year-old person in India.
You are calling because {PATIENT_NAME} did not answer her scheduled check-in call.

Reply in the SAME language the family member speaks - English or Hindi.
Keep every reply SHORT: one or two small sentences, calm and reassuring.
Explain that {PATIENT_NAME} missed her check-in and gently ask them to call or
visit her to make sure she is okay. Answer their questions simply - if they ask
what happened, say she did not pick up the phone and you wanted someone to check
on her. Never give medical advice. Do not use any special tokens or symbols."""


def build_relay_twiml(greeting, params=None):
    """TwiML that hands the call to ConversationRelay, with optional
    <Parameter> values delivered to the WebSocket in the setup message."""
    param_xml = "".join(
        f'<Parameter name="{escape(k)}" value="{escape(str(v))}"/>'
        for k, v in (params or {}).items()
    )
    return ('<?xml version="1.0" encoding="UTF-8"?><Response><Connect>'
            f'<ConversationRelay url="{WSS_URL}/relay-ws" '
            f'welcomeGreeting="{escape(greeting)}" '
            'ttsProvider="Google" '
            f'voice="{RELAY_VOICE}" '
            'language="en-IN" transcriptionProvider="Google" '
            'interruptible="any" interruptSensitivity="high">'
            f'{param_xml}'
            '</ConversationRelay></Connect></Response>')


def tts_lang_for(text):
    """ConversationRelay TTS language. Telugu isn't supported -> fall back to Hindi."""
    for c in text:
        if "ఀ" <= c <= "౿":       # Telugu -> unsupported, use Hindi voice
            return "hi-IN"
        if "ऀ" <= c <= "ॿ":       # Devanagari
            return "hi-IN"
    return "en-IN"


# tracks calls we've already escalated, so the relative is called at most once
_escalated = set()


def call_family(call_sid, reason):
    """Ring the relative when the patient can't be reached."""
    if call_sid and call_sid in _escalated:
        return
    _escalated.add(call_sid)
    print(f"[ESCALATE] {PATIENT_NAME} unreachable ({reason}) -> calling family")
    if not (Client and FAMILY_PHONE and PUBLIC_URL):
        print("[ESCALATE] skipped (need FAMILY_PHONE + PUBLIC_URL in .env)")
        return
    try:
        call = Client(TWILIO_SID, TWILIO_TOKEN).calls.create(
            to=FAMILY_PHONE, from_=TWILIO_NUMBER,
            url=f"{PUBLIC_URL}/family_voice", timeout=30,
        )
        print(f"[ESCALATE] dialing family {FAMILY_PHONE} ... SID {call.sid}")
    except Exception as e:
        print(f"[ESCALATE ERROR] {e}")


@app.route("/family_voice", methods=["POST", "GET"])
def family_voice():
    """The relative gets an interactive ConversationRelay call, not a recording."""
    greeting = (f"Hello, this is Aasha from Aayura. I am calling because {PATIENT_NAME} "
                f"did not answer her check-in call. Is someone in the family there?")
    return Response(build_relay_twiml(greeting, {"role": "family"}), mimetype="text/xml")


@app.route("/call_status", methods=["POST"])
def call_status():
    """Twilio reports the final call outcome here -> escalate if unreachable."""
    sid = request.form.get("CallSid", "")
    status = request.form.get("CallStatus", "")
    answered_by = request.form.get("AnsweredBy", "")
    print(f"[STATUS] {status} answered_by={answered_by or '-'}")
    if status in ("no-answer", "busy", "failed", "canceled"):
        call_family(sid, f"call {status}")
    return ("", 204)


# ---------------------------------------------------------------
# TwiML: hand the call to ConversationRelay
# ---------------------------------------------------------------
@app.route("/voice-relay", methods=["POST", "GET"])
def voice_relay():
    # Answering Machine Detection: if Twilio says a machine/voicemail picked up,
    # don't talk to the recording - hang up and call the relative instead.
    answered_by = request.values.get("AnsweredBy", "")
    if answered_by.startswith("machine") or answered_by == "fax":
        call_family(request.values.get("CallSid", ""), "voicemail")
        return Response('<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
                        mimetype="text/xml")

    greeting = (f"Namaste {PATIENT_NAME}! This is Aasha. I called to see how you are. "
                f"How are you feeling today?")
    return Response(build_relay_twiml(greeting, {"role": "patient"}), mimetype="text/xml")


# ---------------------------------------------------------------
# The WebSocket: Twilio streams speech in, we stream Claude out
# ---------------------------------------------------------------
def stream_reply(ws, call_sid, user_text, stop, role="patient"):
    """Stream a Haiku reply token-by-token into ConversationRelay's TTS."""
    history = conversations.setdefault(call_sid, [])
    history.append({"role": "user", "content": user_text})
    who = "family" if role == "family" else PATIENT_NAME
    print(f"[HEARD] {who}: {user_text}")
    system = AASHA_FAMILY_PROMPT if role == "family" else AASHA_PROMPT
    full = ""
    try:
        if not claude:
            ws.send(json.dumps({"type": "text",
                                "token": "I am here with you. Please rest.",
                                "last": True}))
            return
        with claude.messages.stream(
            model="claude-haiku-4-5",     # fastest model = lowest latency
            max_tokens=120,
            system=system,
            messages=history,
        ) as stream:
            for delta in stream.text_stream:
                if stop.is_set():         # caller barged in -> stop generating
                    break
                full += delta
                # set the TTS language once, on the first token
                if full == delta:
                    ws.send(json.dumps({"type": "language",
                                        "ttsLanguage": tts_lang_for(delta),
                                        "transcriptionLanguage": "en-IN"}))
                ws.send(json.dumps({"type": "text", "token": delta, "last": False}))
        if not stop.is_set():
            ws.send(json.dumps({"type": "text", "token": "", "last": True}))
            print(f"[AASHA] {full}")
    except Exception as e:
        print(f"[STREAM ERROR] {e}")
    finally:
        if full:
            history.append({"role": "assistant", "content": full})


@sock.route("/relay-ws")
def relay_ws(ws):
    call_sid = None
    role = "patient"                      # set from the setup message's customParameters
    state = {"stop": threading.Event()}   # signals the current reply worker to halt
    while True:
        raw = ws.receive()
        if raw is None:
            break
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        mtype = msg.get("type")

        if mtype == "setup":
            call_sid = msg.get("callSid")
            role = (msg.get("customParameters") or {}).get("role", "patient")
            conversations[call_sid] = []
            print(f"[RELAY] {role} call connected {call_sid}")

        elif mtype == "prompt" and msg.get("last"):
            # a complete caller utterance -> stop any in-flight reply, start a new one
            state["stop"].set()
            state["stop"] = threading.Event()
            threading.Thread(
                target=stream_reply,
                args=(ws, call_sid, msg.get("voicePrompt", ""), state["stop"], role),
                daemon=True,
            ).start()

        elif mtype == "interrupt":
            # caller talked over Aasha -> Twilio already muted TTS; stop generating too
            state["stop"].set()
            print("[RELAY] barge-in")

        elif mtype == "error":
            print(f"[RELAY ERROR] {msg.get('description')}")

    print("[RELAY] call ended")


# ---------------------------------------------------------------
# Place the outbound call that uses the relay
# ---------------------------------------------------------------
@app.route("/call_me", methods=["POST", "GET"])
def call_me():
    if not (Client and TWILIO_SID and TWILIO_NUMBER and PUBLIC_URL and PATIENT_PHONE):
        return jsonify({"error": "missing TWILIO creds / PUBLIC_URL / PATIENT_PHONE in .env"}), 400
    try:
        call = Client(TWILIO_SID, TWILIO_TOKEN).calls.create(
            to=PATIENT_PHONE, from_=TWILIO_NUMBER,
            url=f"{PUBLIC_URL}/voice-relay",
            machine_detection="Enable",     # tells us human vs voicemail via AnsweredBy
            status_callback=f"{PUBLIC_URL}/call_status",
            status_callback_event=["completed"],   # fires with no-answer/busy/failed too
            timeout=25,                     # ring ~25s before counting as no-answer
        )
        print(f"[CALL] Dialing {PATIENT_PHONE}... SID {call.sid}")
        return jsonify({"status": "calling", "sid": call.sid})
    except Exception as e:
        print(f"[CALL ERROR] {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/health")
def health():
    ready = bool(claude and Client and TWILIO_SID and WSS_URL)
    return (f"Aasha Relay ready — ws at {WSS_URL}/relay-ws" if ready
            else "Not ready: check ANTHROPIC_API_KEY, TWILIO_*, and NGROK_URL/PUBLIC_URL in .env")


if __name__ == "__main__":
    print("=" * 60)
    print("AASHA RELAY (low-latency, barge-in)  ->  http://localhost:%d" % PORT)
    print("WebSocket:", f"{WSS_URL}/relay-ws" if WSS_URL else "(set NGROK_URL/PUBLIC_URL!)")
    print("Brain:", "Haiku 4.5" if claude else "MISSING ANTHROPIC_API_KEY")
    print("=" * 60)
    # threaded=True: one thread holds the WebSocket while worker threads stream replies
    app.run(host="0.0.0.0", port=PORT, threaded=True)
