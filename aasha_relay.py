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

import functools
import json
import os
import threading
from xml.sax.saxutils import escape

# Flush every print immediately so the live call transcript shows up in logs
# (and in Render's log stream) as it happens, instead of being buffered.
print = functools.partial(print, flush=True)

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
# On Render, RENDER_EXTERNAL_URL is set automatically to this service's public URL,
# so the wss:// WebSocket address resolves without any manual config. Locally it
# falls back to the ngrok tunnel.
PUBLIC_URL = (os.getenv("PUBLIC_URL", "")
              or os.getenv("RENDER_EXTERNAL_URL", "")
              or os.getenv("NGROK_URL", "") or "").rstrip("/")
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
CALL with {PATIENT_NAME}, a young man in India. {PATIENT_NAME} is male - address
him warmly by his name, use he/him, and never call him amma, dadi or treat him
as a woman.

ALWAYS reply in simple, clear ENGLISH, even if the patient speaks another
language. Do not switch to Hindi.
Keep every reply SHORT: one or two small sentences, warm and human like a
caring friend. Ask one gentle thing at a time - how he slept,
whether they ate, if they took their medicines, how they feel. If they sound
unwell, stay calm and tell them you are letting their family know. Never give
medical advice or dosages. Do not use any special tokens or symbols."""

# Prompt for when Aasha reaches the RELATIVE instead of the patient
AASHA_FAMILY_PROMPT = f"""You are Aasha, a warm AI care companion, on a live
PHONE CALL with a FAMILY MEMBER of {PATIENT_NAME}, a young man in India.
You are calling because {PATIENT_NAME} did not answer his scheduled check-in call.

Reply in the SAME language the family member speaks - English or Hindi.
Keep every reply SHORT: one or two small sentences, calm and reassuring.
Explain that {PATIENT_NAME} missed his check-in and gently ask them to call or
visit him to make sure he is okay. Answer their questions simply - if they ask
what happened, say he did not pick up the phone and you wanted someone to check
on him. Never give medical advice. Do not use any special tokens or symbols."""

# Used when the family is called BECAUSE the patient reported an emergency mid-call
AASHA_FAMILY_EMERGENCY_PROMPT = f"""You are Aasha, a calm AI care companion on a
live PHONE CALL with a FAMILY MEMBER of {PATIENT_NAME}, a young man in India.
This is URGENT: during his check-in call, {PATIENT_NAME} reported something that
may be an emergency (such as a fall or pain). Reply in English, SHORT and calm.
Tell them clearly that {PATIENT_NAME} may need help right now, and ask them to go
to him or call him immediately and get medical help if needed. Answer questions
simply. Do not give a diagnosis. Do not use any special tokens or symbols."""


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

# ---------------------------------------------------------------
# EMERGENCY: text family the moment it's detected, call them when the call ends
# ---------------------------------------------------------------
URGENT_WORDS = ["i fell", "fell down", "fell over", "had a fall", "can't move", "cannot move",
                "can't get up", "cannot get up", "broke my", "broken", "chest pain",
                "can't breathe", "cannot breathe", "breathless", "dizzy", "fainted", "faint",
                "bleeding", "unconscious", "collapsed", "heart attack", "stroke", "severe pain"]
call_flags = {}                                    # call_sid -> {emergency, sms_sent, family_called, reason}
emergency_ctx = {"active": False, "reason": ""}    # drives the family greeting / prompt


def is_emergency(text):
    t = (text or "").lower()
    return any(w in t for w in URGENT_WORDS)


def sms_family(reason):
    """Fire-and-forget SMS to the relative the instant an emergency is detected."""
    if not (Client and FAMILY_PHONE and TWILIO_SID):
        print("[SMS] skipped (need FAMILY_PHONE + Twilio)")
        return
    try:
        Client(TWILIO_SID, TWILIO_TOKEN).messages.create(
            to=FAMILY_PHONE, from_=TWILIO_NUMBER,
            body=f"AAYURA ALERT: {PATIENT_NAME} may need urgent help - {reason}. "
                 f"Please call or check on him right now.")
        print(f"[SMS] emergency SMS sent to {FAMILY_PHONE}")
    except Exception as e:
        print(f"[SMS ERROR] {e}")


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
    if emergency_ctx.get("active"):
        greeting = (f"Hello, this is Aasha from Aayura. This is urgent. During his check-in "
                    f"call, {PATIENT_NAME} {emergency_ctx.get('reason', 'reported a problem')}. "
                    f"Please check on him right away. Is someone in the family there?")
    else:
        greeting = (f"Hello, this is Aasha from Aayura. I am calling because {PATIENT_NAME} "
                    f"did not answer his check-in call. Is someone in the family there?")
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
    if role == "family":
        system = AASHA_FAMILY_EMERGENCY_PROMPT if emergency_ctx.get("active") else AASHA_FAMILY_PROMPT
    else:
        system = AASHA_PROMPT
        # emergency in what the patient just said -> text family immediately (once)
        if is_emergency(user_text):
            f = call_flags.setdefault(call_sid, {})
            f["emergency"] = True
            f["reason"] = f"said: {user_text}"
            if not f.get("sms_sent"):
                f["sms_sent"] = True
                print("[EMERGENCY] detected -> texting family")
                threading.Thread(target=sms_family, args=(f["reason"],), daemon=True).start()
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

    # call ended: if the patient reported an emergency, call the family now
    f = call_flags.get(call_sid or "", {})
    if role == "patient" and f.get("emergency") and not f.get("family_called"):
        f["family_called"] = True
        emergency_ctx["active"] = True
        emergency_ctx["reason"] = f.get("reason", "reported a possible emergency")
        print("[EMERGENCY] patient call ended -> calling family")
        call_family(call_sid, "emergency during call")
    if role == "family":
        emergency_ctx["active"] = False        # reset once family has been reached
    call_flags.pop(call_sid or "", None)
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
