"""
AAYURA - marketing site + family dashboard (from the approved design)
=====================================================================
A self-contained Flask app that serves the Aayura landing page and the
family dashboard exactly as designed (warm cream / teal / amber, Newsreader
+ Figtree, light & dark). All interactivity is vanilla JS:
  - Home / Family Dashboard tab navigation
  - dark-mode toggle
  - "Simulate emergency" toggle (swaps vitals, voice health, call history,
    and the status banner)
  - expandable "Schedule a call" flow with a working calendar
  - "Call Radha now" places a REAL phone call via the running relay
    (needs Twilio creds + the relay/ngrok up; otherwise it's a no-op).

Run:   python3 aayura_site.py      ->  http://localhost:5050
"""

import os
from flask import Flask, Response, jsonify, send_from_directory
from dotenv import load_dotenv

try:
    from twilio.rest import Client
except Exception:
    Client = None

load_dotenv()

TWILIO_SID = os.getenv("TWILIO_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_TOKEN", "")
TWILIO_NUMBER = os.getenv("TWILIO_NUMBER", "")
PUBLIC_URL = (os.getenv("PUBLIC_URL", "") or os.getenv("NGROK_URL", "") or "").rstrip("/")
PATIENT_PHONE = os.getenv("PATIENT_PHONE", "")
PORT = int(os.getenv("PORT", "5050"))
HERE = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)


@app.route("/assets/<path:fname>")
def assets(fname):
    return send_from_directory(os.path.join(HERE, "assets"), fname)


@app.route("/call", methods=["POST"])
def call():
    """Place a real call to Radha via the relay's /voice-relay webhook."""
    if not (Client and TWILIO_SID and TWILIO_NUMBER and PUBLIC_URL and PATIENT_PHONE):
        return jsonify({"ok": False, "error": "calling not configured (need Twilio + PUBLIC_URL + PATIENT_PHONE)"}), 200
    try:
        c = Client(TWILIO_SID, TWILIO_TOKEN).calls.create(
            to=PATIENT_PHONE, from_=TWILIO_NUMBER,
            url=f"{PUBLIC_URL}/voice-relay", timeout=25)
        return jsonify({"ok": True, "sid": c.sid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200


PAGE = r"""<!DOCTYPE html>
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
  a:hover { color: #0B534B; }
  ::selection { background: #FBEAD8; }
  .ay-root { --bg:#FBF5EA; --card:#FFFDF8; --border:#EBE2D3; --border2:#E4D8C3; --ink:#2A2320; --ink2:#4A4038; --muted:#6B615A; --muted2:#7A6F66; --faint:#9C9186; --warm:#F4EAD8; --chip:#F1E7D6; --chip2:#FBF6EC; --bar:rgba(251,245,234,0.88); --okbg:#F7FAF3; --okbd:#E7EEDF; }
  .ay-root.ay-dark { --bg:#201A16; --card:#2C2521; --border:#3B322B; --border2:#463B32; --ink:#F5ECDD; --ink2:#E6DBCA; --muted:#B8AC9B; --muted2:#AA9E8D; --faint:#8B8070; --warm:#2A231F; --chip:#342B25; --chip2:#302822; --bar:rgba(32,26,22,0.86); --okbg:#243029; --okbd:#35473E; }
  .serif { font-family:'Newsreader',serif; }
  .btn { transition: all .15s; }
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

  <!-- TOP BAR -->
  <div style="position:sticky; top:0; z-index:50; background:var(--bar); backdrop-filter:blur(12px); border-bottom:1px solid var(--border);">
    <div style="max-width:1180px; margin:0 auto; padding:14px 24px; display:flex; align-items:center; gap:20px;">
      <div style="display:flex; align-items:center; gap:11px; cursor:pointer;" onclick="go('landing')">
        <div style="width:34px; height:34px; border-radius:50%; background:radial-gradient(circle at 32% 30%, #F0A45C, #E07A2E 70%); display:flex; align-items:center; justify-content:center; box-shadow:0 2px 8px rgba(224,122,46,0.35);">
          <div style="width:11px; height:11px; border-radius:50%; background:var(--card);"></div>
        </div>
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

  <!-- ================= LANDING ================= -->
  <div id="view-landing">
    <!-- HERO -->
    <section style="max-width:1180px; margin:0 auto; padding:clamp(40px,6vw,84px) 24px clamp(30px,4vw,56px); display:grid; grid-template-columns:1.05fr 0.95fr; gap:clamp(28px,4vw,60px); align-items:center;" class="ay-hero-grid">
      <div>
        <div style="display:inline-flex; align-items:center; gap:8px; background:#FBEAD8; color:#B85F1E; padding:7px 14px; border-radius:999px; font-size:13.5px; font-weight:600; margin-bottom:22px;">
          <span style="width:7px; height:7px; border-radius:50%; background:#E07A2E; display:inline-block;"></span> Namaste — meet Aayura
        </div>
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
              <span style="width:3px; height:10px; background:#0F6E63; border-radius:2px;"></span>
              <span style="width:3px; height:20px; background:#0F6E63; border-radius:2px;"></span>
              <span style="width:3px; height:14px; background:#0F6E63; border-radius:2px;"></span>
              <span style="width:3px; height:22px; background:#0F6E63; border-radius:2px;"></span>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- PROBLEM -->
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

    <!-- HOW IT WORKS -->
    <section style="max-width:1180px; margin:0 auto; padding:clamp(52px,6vw,88px) 24px;">
      <div style="text-align:center; max-width:620px; margin:0 auto 46px;">
        <div style="text-transform:uppercase; letter-spacing:0.14em; font-size:13px; font-weight:700; color:#B85F1E; margin-bottom:14px;">How Aayura works</div>
        <h2 class="serif" style="font-weight:500; font-size:clamp(30px,4vw,46px); line-height:1.08; letter-spacing:-0.01em; margin:0;">Four gentle steps, every single day.</h2>
      </div>
      <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:18px;" id="steps"></div>
    </section>

    <!-- FEATURES -->
    <section style="background:var(--warm);">
      <div style="max-width:1180px; margin:0 auto; padding:clamp(52px,6vw,88px) 24px;">
        <div style="max-width:560px; margin-bottom:40px;">
          <div style="text-transform:uppercase; letter-spacing:0.14em; font-size:13px; font-weight:700; color:#B85F1E; margin-bottom:14px;">Everything she needs, in one warm voice</div>
          <h2 class="serif" style="font-weight:500; font-size:clamp(30px,4vw,46px); line-height:1.08; letter-spacing:-0.01em; margin:0;">Care that feels like family.</h2>
        </div>
        <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:16px;" id="features"></div>
      </div>
    </section>

    <!-- TESTIMONIAL -->
    <section style="max-width:1180px; margin:0 auto; padding:clamp(52px,6vw,88px) 24px;">
      <div style="background:var(--card); border:1px solid var(--border); border-radius:28px; padding:clamp(28px,4vw,56px); display:grid; grid-template-columns:auto 1fr; gap:clamp(24px,3vw,44px); align-items:center;" class="ay-testi-grid">
        <div style="width:clamp(120px,16vw,180px); aspect-ratio:1; border-radius:24px; overflow:hidden; border:1px solid var(--border);">
          <img src="/assets/radha-hero.png" alt="family" style="width:100%; height:100%; object-fit:cover; object-position:center 20%;">
        </div>
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

    <!-- CTA -->
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

  <!-- ================= DASHBOARD ================= -->
  <div id="view-dashboard" style="display:none;">
    <div style="max-width:1180px; margin:0 auto; padding:clamp(20px,3vw,32px) 24px clamp(48px,6vw,72px);">
      <!-- patient header -->
      <div style="display:flex; flex-wrap:wrap; gap:18px; align-items:center; margin-bottom:20px;">
        <div style="width:60px; height:60px; border-radius:50%; overflow:hidden; border:1px solid var(--border);"><img src="/assets/radha-hero.png" alt="Radha" style="width:100%; height:100%; object-fit:cover; object-position:center 20%;"></div>
        <div style="flex:1; min-width:180px;">
          <h1 class="serif" style="font-weight:600; font-size:clamp(26px,3.2vw,36px); margin:0; line-height:1.05;">Radha, 72</h1>
          <div style="color:var(--muted2); font-size:14.5px; margin-top:4px;">Next check-in: <strong style="color:var(--ink);">tomorrow, 08:00</strong> · Kochi, Kerala</div>
        </div>
        <div style="display:flex; align-items:center; gap:10px;">
          <span style="display:inline-flex; align-items:center; gap:7px; background:var(--chip); border:1px solid var(--border2); color:var(--muted2); padding:8px 14px; border-radius:999px; font-size:12.5px; font-weight:600; font-family:ui-monospace,monospace;">DEMO · scripted</span>
          <div onclick="toggleEmergency()" id="emgToggle" class="btn"></div>
        </div>
      </div>

      <div id="banner"></div>

      <div style="display:grid; grid-template-columns:1.5fr 1fr; gap:18px; align-items:start;" class="ay-dash-grid">
        <!-- LEFT -->
        <div style="display:flex; flex-direction:column; gap:18px;">
          <div style="background:var(--card); border:1px solid var(--border); border-radius:22px; padding:24px;">
            <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:16px;">
              <h2 class="serif" style="font-weight:600; font-size:20px; margin:0;">Today's call with Aayura</h2>
              <span style="font-size:13px; color:var(--muted2);">08:04 · 6 min</span>
            </div>
            <p id="callsummary" style="font-size:15.5px; line-height:1.6; color:var(--ink2); margin:0 0 18px;"></p>
            <div style="display:flex; gap:10px; flex-wrap:wrap;">
              <div style="display:flex; align-items:center; gap:8px; background:#DCEDE9; color:#0B534B; padding:9px 14px; border-radius:12px; font-size:14px; font-weight:600;"><span>💊</span> Morning medicine ✓</div>
              <div style="display:flex; align-items:center; gap:8px; background:#DCEDE9; color:#0B534B; padding:9px 14px; border-radius:12px; font-size:14px; font-weight:600;"><span>🍲</span> Breakfast ✓</div>
              <div style="display:flex; align-items:center; gap:8px; background:#FBEAD8; color:#B85F1E; padding:9px 14px; border-radius:12px; font-size:14px; font-weight:600;"><span>🍛</span> Lunch — pending</div>
            </div>
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
                <div id="voicedial" style="width:64px; height:64px; border-radius:50%; display:flex; align-items:center; justify-content:center;">
                  <div id="voicescore" style="width:48px; height:48px; border-radius:50%; background:var(--card); display:flex; align-items:center; justify-content:center; font-family:'Newsreader',serif; font-size:19px; font-weight:600;"></div>
                </div>
                <div>
                  <div id="voicelabel" style="font-weight:700; font-size:15.5px;"></div>
                  <div id="voicenote" style="font-size:13px; color:var(--muted2); max-width:26ch;"></div>
                </div>
              </div>
              <div id="voicebars" style="flex:1; min-width:140px; display:flex; gap:4px; align-items:flex-end; height:56px;"></div>
            </div>
          </div>
        </div>

        <!-- RIGHT -->
        <div style="display:flex; flex-direction:column; gap:18px;">
          <div style="background:#0F6E63; color:#EAF3F0; border-radius:22px; padding:24px;">
            <div style="font-size:13px; color:#9FCDC4; text-transform:uppercase; letter-spacing:0.12em; font-weight:700; margin-bottom:14px;">Upcoming today</div>
            <div style="display:flex; flex-direction:column; gap:12px;">
              <div style="display:flex; gap:12px; align-items:center;"><span style="width:30px; height:30px; border-radius:9px; background:rgba(255,255,255,0.14); display:flex; align-items:center; justify-content:center; font-size:15px;">💊</span><div style="flex:1;"><div style="font-weight:600; font-size:14.5px;">Afternoon medicine</div><div style="font-size:12.5px; color:#AFD5CD;">Reminder call at 14:00</div></div></div>
              <div style="display:flex; gap:12px; align-items:center;"><span style="width:30px; height:30px; border-radius:9px; background:rgba(255,255,255,0.14); display:flex; align-items:center; justify-content:center; font-size:15px;">🍛</span><div style="flex:1;"><div style="font-weight:600; font-size:14.5px;">Lunch check-in</div><div style="font-size:12.5px; color:#AFD5CD;">Reminder call at 13:00</div></div></div>
              <div style="display:flex; gap:12px; align-items:center;"><span style="width:30px; height:30px; border-radius:9px; background:rgba(255,255,255,0.14); display:flex; align-items:center; justify-content:center; font-size:15px;">🌤</span><div style="flex:1;"><div style="font-weight:600; font-size:14.5px;">Evening companionship call</div><div style="font-size:12.5px; color:#AFD5CD;">18:30 · a chat before dinner</div></div></div>
            </div>
          </div>

          <!-- schedule a call -->
          <div style="background:var(--card); border:1px solid var(--border); border-radius:22px; padding:24px;">
            <div onclick="toggleExpand()" style="display:flex; align-items:center; gap:10px; cursor:pointer;">
              <span style="width:32px; height:32px; border-radius:9px; background:#FBEAD8; color:#B85F1E; display:flex; align-items:center; justify-content:center; font-size:16px;">📅</span>
              <div style="flex:1;">
                <h2 class="serif" style="font-weight:600; font-size:20px; margin:0;">Schedule a call</h2>
                <div id="schedNote" style="font-size:13px; color:var(--faint); margin-top:2px;">Tap to choose a day, time &amp; topic</div>
              </div>
              <span id="chevron" style="font-size:22px; color:var(--faint); line-height:1; transition:transform .18s;">⌄</span>
            </div>
            <div id="schedBody" style="display:none; margin-top:18px;"></div>
          </div>

          <!-- call history -->
          <div style="background:var(--card); border:1px solid var(--border); border-radius:22px; padding:24px;">
            <h2 class="serif" style="font-weight:600; font-size:20px; margin:0 0 16px;">Call history</h2>
            <div style="display:flex; flex-direction:column;" id="callhistory"></div>
          </div>

          <!-- emergency contacts -->
          <div style="background:var(--warm); border:1px solid var(--border); border-radius:22px; padding:24px;">
            <h2 class="serif" style="font-weight:600; font-size:20px; margin:0 0 4px;">Emergency contacts</h2>
            <div style="font-size:13px; color:var(--faint); margin-bottom:16px;">Notified in order if Aayura can't reach Radha.</div>
            <div style="display:flex; flex-direction:column; gap:10px;">
              <div style="display:flex; align-items:center; gap:12px;"><span style="width:36px; height:36px; border-radius:50%; background:#DCEDE9; color:#0F6E63; display:flex; align-items:center; justify-content:center; font-weight:700; font-size:13px;">PN</span><div style="flex:1;"><div style="font-weight:600; font-size:14px;">Priya Nair</div><div style="font-size:12.5px; color:var(--muted2);">Daughter · Toronto</div></div><span style="font-size:11px; font-weight:700; color:#0F6E63;">1st</span></div>
              <div style="display:flex; align-items:center; gap:12px;"><span style="width:36px; height:36px; border-radius:50%; background:#FBEAD8; color:#B85F1E; display:flex; align-items:center; justify-content:center; font-weight:700; font-size:13px;">AK</span><div style="flex:1;"><div style="font-weight:600; font-size:14px;">Arun Kumar</div><div style="font-size:12.5px; color:var(--muted2);">Son · Bengaluru</div></div><span style="font-size:11px; font-weight:700; color:#B85F1E;">2nd</span></div>
              <div style="display:flex; align-items:center; gap:12px;"><span style="width:36px; height:36px; border-radius:50%; background:var(--border); color:var(--muted2); display:flex; align-items:center; justify-content:center; font-weight:700; font-size:13px;">MW</span><div style="flex:1;"><div style="font-weight:600; font-size:14px;">Meena (neighbour)</div><div style="font-size:12.5px; color:var(--muted2);">Local · 2 min away</div></div><span style="font-size:11px; font-weight:700; color:var(--muted2);">3rd</span></div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

</div>

<script>
const S = { page:'landing', dark:true, emergency:false, schedExpanded:false, schedMember:0, schedSlot:0, schedTopic:0, scheduleDone:false, viewMonth:6, viewYear:2026, selDay:25, selMonth:6, selYear:2026 };

const STEPS = [
  {n:'01',icon:'☎',title:'Calls',desc:'Aayura phones your parent on a warm, familiar schedule — no app or smartphone required.',bg:'#FBEAD8',fg:'#B85F1E'},
  {n:'02',icon:'💬',title:'Talks',desc:'She chats like a caring granddaughter — asking about their day, gently reminding about meals & medicine.',bg:'#DCEDE9',fg:'#0F6E63'},
  {n:'03',icon:'📈',title:'Monitors',desc:'She reads connected devices and listens for changes in voice, mood and energy that signal decline.',bg:'#FBEAD8',fg:'#B85F1E'},
  {n:'04',icon:'🔔',title:'Alerts',desc:'If something seems wrong — or calls go unanswered — family is notified instantly, with full context.',bg:'#DCEDE9',fg:'#0F6E63'},
];
const FEATURES = [
  {icon:'💊',title:'Medicine & meal reminders',desc:'Gentle, spoken nudges at the right time — and a tick for family when they’re done.'},
  {icon:'❤️',title:'Vitals monitoring',desc:'Heart rate, glucose and oxygen from connected devices, summarised in plain language.'},
  {icon:'🎙',title:'Voice health detection',desc:'Subtle shifts in tone or clarity flag possible decline before it becomes a crisis.'},
  {icon:'🚨',title:'Instant family alerts',desc:'Emergencies and missed calls trigger immediate notifications to everyone who matters.'},
  {icon:'🗣',title:'Speaks their language',desc:'Fluent, natural conversation in the dialect your parent is most comfortable with.'},
  {icon:'🤝',title:'Real companionship',desc:'Daily conversation that eases loneliness — not just a checklist, but a friend.'},
];
const STATS = [
  {big:'1 in 3',t:'elderly Indians live alone or with only a spouse.'},
  {big:'40%+',t:'report persistent loneliness affecting their health.'},
  {big:'104M+',t:'Indians are over 60 — a number set to double by 2050.'},
  {big:'72%',t:'of health declines show up first in daily habits & voice.'},
];
const VNORMAL = [
  {label:'Heart rate',value:'73',unit:'bpm',range:'Normal 60–100 bpm',tag:'Normal',spark:'0,16 14,13 28,17 42,11 56,15 70,12 84,14 100,13'},
  {label:'Blood glucose',value:'115',unit:'mg/dL',range:'Normal 70–140 mg/dL',tag:'Normal',spark:'0,15 14,17 28,12 42,14 56,10 70,13 84,11 100,14'},
  {label:'Blood oxygen',value:'97',unit:'%',range:'Normal 95–100 %',tag:'Normal',spark:'0,10 14,9 28,11 42,8 56,10 70,9 84,8 100,9'},
  {label:'Missed calls',value:'0',unit:'/5',range:'Alert at 5',tag:'Normal',spark:'0,20 14,20 28,20 42,20 56,20 70,20 84,20 100,20'},
];
const VEMERG = [
  {label:'Heart rate',value:'48',unit:'bpm',range:'Normal 60–100 bpm',tag:'Low',bad:1,spark:'0,10 14,12 28,9 42,15 56,13 70,18 84,20 100,22'},
  {label:'Blood glucose',value:'61',unit:'mg/dL',range:'Normal 70–140 mg/dL',tag:'Low',bad:1,spark:'0,8 14,10 28,13 42,15 56,17 70,19 84,20 100,22'},
  {label:'Blood oxygen',value:'91',unit:'%',range:'Normal 95–100 %',tag:'Low',bad:1,spark:'0,8 14,9 28,11 42,14 56,17 70,19 84,21 100,22'},
  {label:'Missed calls',value:'5',unit:'/5',range:'Alert at 5',tag:'ALERT',bad:1,spark:'0,22 14,22 28,18 42,18 56,14 70,14 84,10 100,6'},
];
const CH_NORMAL = [
  {icon:'☎',title:'Morning check-in',time:'Today · 08:04 · 6 min',tag:'Done',bg:'#DCEDE9',fg:'#0F6E63'},
  {icon:'🌤',title:'Evening companionship call',time:'Yesterday · 18:31 · 12 min',tag:'Done',bg:'#DCEDE9',fg:'#0F6E63'},
  {icon:'💊',title:'Afternoon medicine reminder',time:'Yesterday · 14:02 · 3 min',tag:'Done',bg:'#DCEDE9',fg:'#0F6E63'},
  {icon:'🍛',title:'Lunch check-in',time:'Yesterday · 13:00 · 4 min',tag:'Done',bg:'#DCEDE9',fg:'#0F6E63'},
  {icon:'☎',title:'Morning check-in',time:'Yesterday · 08:02 · 7 min',tag:'Done',bg:'#DCEDE9',fg:'#0F6E63'},
];
const CH_EMERG = [
  {icon:'⚠',title:'Emergency escalation sent',time:'Today · 11:20',tag:'ALERT',bg:'#FBE9E6',fg:'#C6392E'},
  {icon:'✕',title:'Missed check-in call',time:'Today · 11:00',tag:'Missed',bg:'#FBE9E6',fg:'#C6392E'},
  {icon:'✕',title:'Missed check-in call',time:'Today · 10:00',tag:'Missed',bg:'#FBE9E6',fg:'#C6392E'},
  {icon:'☎',title:'Morning check-in',time:'Today · 08:04 · 6 min',tag:'Done',bg:'#DCEDE9',fg:'#0F6E63'},
  {icon:'🌤',title:'Evening companionship call',time:'Yesterday · 18:31 · 12 min',tag:'Done',bg:'#DCEDE9',fg:'#0F6E63'},
];
const MEMBERS = ['Priya (daughter)','Arun (son)','Meena (neighbour)'];
const SLOTS = ['08:00','13:00','16:00','18:30'];
const TOPICS = ['Just to chat','Medicine check','How she’s feeling','Remind about appointment'];
const MONTHS = ['January','February','March','April','May','June','July','August','September','October','November','December'];
const WEEKDAYS = ['S','M','T','W','T','F','S'];

const el = id => document.getElementById(id);
const esc = s => (''+s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function go(p){ S.page=p; render(); window.scrollTo(0,0); }
function toggleDark(){ S.dark=!S.dark; render(); }
function toggleEmergency(){ S.emergency=!S.emergency; render(); }
function toggleExpand(){ S.schedExpanded=!S.schedExpanded; render(); }
function prevMonth(){ let m=S.viewMonth-1; if(m<0){S.viewMonth=11;S.viewYear--;}else S.viewMonth=m; render(); }
function nextMonth(){ let m=S.viewMonth+1; if(m>11){S.viewMonth=0;S.viewYear++;}else S.viewMonth=m; render(); }
function pickMember(i){ S.schedMember=i; S.scheduleDone=false; render(); }
function pickSlot(i){ S.schedSlot=i; S.scheduleDone=false; render(); }
function pickTopic(i){ S.schedTopic=i; S.scheduleDone=false; render(); }
function pickDay(n){ S.selDay=n; S.selMonth=S.viewMonth; S.selYear=S.viewYear; S.scheduleDone=false; render(); }
function confirmSchedule(){ S.scheduleDone=true; render(); }
function resetSchedule(){ S.scheduleDone=false; render(); }

async function callRadha(btn){
  const old = btn ? btn.textContent : '';
  if(btn) btn.textContent = 'Calling Radha…';
  try {
    const r = await (await fetch('/call', {method:'POST'})).json();
    if(btn) btn.textContent = r.ok ? '✓ Ringing Radha' : '⚠ ' + (r.error||'call failed');
  } catch(e){ if(btn) btn.textContent = '⚠ call failed'; }
  if(btn) setTimeout(()=>{ btn.textContent = old; }, 3500);
}

const TAB_ON = 'padding:9px 16px;border-radius:999px;font-size:14px;font-weight:600;cursor:pointer;white-space:nowrap;background:var(--card);color:var(--ink);box-shadow:0 1px 4px rgba(0,0,0,0.08);';
const TAB_OFF = 'padding:9px 16px;border-radius:999px;font-size:14px;font-weight:600;cursor:pointer;white-space:nowrap;background:transparent;color:var(--muted2);';
const CHIP = 'padding:9px 14px;border-radius:12px;font-size:13.5px;font-weight:600;cursor:pointer;border:1px solid var(--border2);background:var(--chip2);color:var(--muted);';
const CHIP_ON = 'padding:9px 14px;border-radius:12px;font-size:13.5px;font-weight:600;cursor:pointer;border:1px solid #0F6E63;background:#DCEDE9;color:#0B534B;';

function render(){
  // theme + tab
  el('root').className = 'ay-root' + (S.dark ? ' ay-dark' : '');
  el('darkbtn').textContent = S.dark ? '☀' : '☾';
  el('tab-landing').style.cssText = S.page==='landing' ? TAB_ON : TAB_OFF;
  el('tab-dash').style.cssText = S.page==='dashboard' ? TAB_ON : TAB_OFF;
  el('view-landing').style.display = S.page==='landing' ? '' : 'none';
  el('view-dashboard').style.display = S.page==='dashboard' ? '' : 'none';

  if(S.page==='landing') renderLanding();
  else renderDashboard();
}

function renderLanding(){
  el('problem-stats').innerHTML = STATS.map(s=>`
    <div style="background:rgba(255,255,255,0.07);border:1px solid rgba(255,255,255,0.12);border-radius:18px;padding:24px;">
      <div class="serif" style="font-size:46px;font-weight:500;color:#F3B678;line-height:1;">${s.big}</div>
      <div style="margin-top:10px;color:#CFE4DE;font-size:15px;line-height:1.5;">${esc(s.t)}</div>
    </div>`).join('');
  el('steps').innerHTML = STEPS.map(st=>`
    <div style="background:var(--card);border:1px solid var(--border);border-radius:22px;padding:26px;">
      <div style="width:52px;height:52px;border-radius:16px;background:${st.bg};color:${st.fg};display:flex;align-items:center;justify-content:center;font-size:24px;margin-bottom:18px;">${st.icon}</div>
      <div style="font-family:ui-monospace,monospace;font-size:12px;color:var(--faint);margin-bottom:6px;">STEP ${st.n}</div>
      <h3 class="serif" style="font-weight:600;font-size:22px;margin:0 0 8px;">${st.title}</h3>
      <p style="font-size:14.5px;line-height:1.55;color:var(--muted);margin:0;">${esc(st.desc)}</p>
    </div>`).join('');
  el('features').innerHTML = FEATURES.map(f=>`
    <div style="background:var(--card);border:1px solid var(--border);border-radius:20px;padding:24px;">
      <div style="font-size:26px;margin-bottom:14px;">${f.icon}</div>
      <h3 class="serif" style="font-weight:600;font-size:20px;margin:0 0 7px;">${f.title}</h3>
      <p style="font-size:14.5px;line-height:1.55;color:var(--muted);margin:0;">${esc(f.desc)}</p>
    </div>`).join('');
}

function renderDashboard(){
  const emg = S.emergency;
  // emergency toggle
  const tb = 'padding:8px 16px;border-radius:999px;font-size:12.5px;font-weight:700;cursor:pointer;font-family:ui-monospace,monospace;letter-spacing:0.04em;';
  el('emgToggle').style.cssText = tb + (emg ? 'background:#C6392E;color:#FFF;' : 'background:var(--card);color:var(--muted2);border:1px solid var(--border2);');
  el('emgToggle').textContent = emg ? '● EMERGENCY' : '○ Simulate emergency';

  // banner
  el('banner').innerHTML = emg ? `
    <div style="background:linear-gradient(135deg,#C6392E,#A82C22);color:#FFF;border-radius:22px;padding:22px 24px;margin-bottom:22px;display:flex;flex-wrap:wrap;gap:16px;align-items:center;box-shadow:0 16px 40px -14px rgba(198,57,46,0.6);">
      <div style="width:52px;height:52px;border-radius:14px;background:rgba(255,255,255,0.18);display:flex;align-items:center;justify-content:center;font-size:26px;">⚠</div>
      <div style="flex:1;min-width:220px;">
        <div style="font-weight:800;font-size:19px;letter-spacing:-0.01em;">Emergency — Radha may need help now</div>
        <div style="font-size:14.5px;color:#FBD9D4;margin-top:3px;">5 of 5 calls missed since 08:00 · voice tone flagged unusual · last seen 6h ago. Family &amp; local contact notified.</div>
      </div>
      <div style="display:flex;gap:10px;flex-wrap:wrap;">
        <div onclick="callRadha(this)" class="btn" style="padding:12px 20px;background:#FFF;color:#B0271D;border-radius:12px;font-weight:700;font-size:14.5px;cursor:pointer;">Call Radha now</div>
        <div class="btn" style="padding:12px 20px;background:rgba(255,255,255,0.16);color:#FFF;border:1px solid rgba(255,255,255,0.5);border-radius:12px;font-weight:600;font-size:14.5px;cursor:pointer;">Alert neighbour</div>
      </div>
    </div>` : `
    <div style="background:#DCEDE9;border:1px solid #BFDCD5;color:#0B534B;border-radius:18px;padding:16px 22px;margin-bottom:22px;display:flex;align-items:center;gap:12px;">
      <span style="width:32px;height:32px;border-radius:50%;background:#0F6E63;color:#FFF;display:flex;align-items:center;justify-content:center;font-size:16px;">✓</span>
      <div style="font-size:15.5px;"><strong>All is well today.</strong> Radha sounded cheerful this morning and everything is on track.</div>
    </div>`;

  el('callsummary').textContent = emg
    ? '"Radha did not answer her last few calls. Earlier she sounded unusually tired and her words were unclear. Emergency contacts have been alerted."'
    : '"Radha was in good spirits and talked about her granddaughter\'s visit. She confirmed her morning medicines and had breakfast. She mentioned mild knee stiffness but no pain."';

  // status pill
  el('statuspill').style.cssText = 'font-size:11.5px;font-weight:800;letter-spacing:0.06em;padding:5px 12px;border-radius:999px;' + (emg ? 'color:#C6392E;background:#FBE9E6;' : 'color:#0F6E63;background:#DCEDE9;');
  el('statuspill').textContent = emg ? 'NEEDS ATTENTION' : 'NORMAL';

  // vitals
  const vitals = (emg ? VEMERG : VNORMAL);
  el('vitals').innerHTML = vitals.map(v=>{
    const bad = v.bad;
    return `<div style="border:1px solid ${bad?'#F0C4BE':'var(--okbd)'};background:${bad?'#FBE9E6':'var(--okbg)'};border-radius:16px;padding:16px;cursor:pointer;">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;">
        <span style="font-size:13px;color:var(--muted);font-weight:600;">${v.label}</span>
        <span style="font-size:11px;font-weight:700;color:${bad?'#C6392E':'#0F6E63'};">${v.tag}</span>
      </div>
      <div style="display:flex;align-items:baseline;gap:4px;">
        <span class="serif" style="font-size:30px;font-weight:600;color:${bad?'#C6392E':'var(--ink)'};line-height:1;">${v.value}</span>
        <span style="font-size:13px;color:var(--faint);">${v.unit}</span>
      </div>
      <svg viewBox="0 0 100 26" preserveAspectRatio="none" style="width:100%;height:26px;margin-top:10px;display:block;">
        <polyline points="${v.spark}" fill="none" stroke="${bad?'#C6392E':'#0F6E63'}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></polyline>
      </svg>
      <div style="font-size:11.5px;color:var(--faint);margin-top:6px;">${v.range}</div>
    </div>`;
  }).join('');

  // voice health
  const vColor = emg ? '#C6392E' : '#0F6E63';
  const vDeg = emg ? '187deg' : '331deg';
  el('voicedial').style.background = `conic-gradient(${vColor} ${vDeg}, var(--border) 0)`;
  el('voicescore').textContent = emg ? '52' : '92';
  el('voicescore').style.color = vColor;
  el('voicelabel').textContent = emg ? 'Unusual — flagged' : 'Steady & bright';
  el('voicelabel').style.color = vColor;
  el('voicenote').textContent = emg ? 'Slurred speech and low energy detected this morning.' : 'Warm, clear and energetic — consistent with her baseline.';
  const heights = emg ? [30,44,20,50,26,18,40,22,14,34,20,44,16,28,20,12] : [30,52,40,56,44,50,38,54,42,48,36,52,44,50,40,54];
  el('voicebars').innerHTML = heights.map((h,i)=>`<span style="flex:1;height:${h}%;background:${emg?(i%2?'#E79A93':'#C6392E'):(i%2?'#8FC6BC':'#0F6E63')};border-radius:3px;"></span>`).join('');

  // call history
  const ch = emg ? CH_EMERG : CH_NORMAL;
  el('callhistory').innerHTML = ch.map(c=>`
    <div style="display:flex;gap:13px;align-items:center;padding:11px 0;border-bottom:1px solid var(--border);">
      <span style="width:34px;height:34px;border-radius:50%;background:${c.bg};color:${c.fg};display:flex;align-items:center;justify-content:center;font-size:15px;flex-shrink:0;">${c.icon}</span>
      <div style="flex:1;min-width:0;"><div style="font-weight:600;font-size:14px;">${esc(c.title)}</div><div style="font-size:12.5px;color:var(--faint);">${esc(c.time)}</div></div>
      <span style="font-size:12px;font-weight:700;color:${c.fg};">${c.tag}</span>
    </div>`).join('');

  // schedule
  const selDate = new Date(S.selYear, S.selMonth, S.selDay);
  const selLabel = selDate.toLocaleDateString('en-US',{weekday:'short',month:'short',day:'numeric'});
  const summary = 'on ' + selLabel + ' at ' + SLOTS[S.schedSlot] + ' — ' + TOPICS[S.schedTopic].toLowerCase() + ', requested by ' + MEMBERS[S.schedMember].split(' ')[0];
  el('schedNote').textContent = S.scheduleDone ? ('Scheduled ' + summary) : 'Tap to choose a day, time & topic';
  el('chevron').style.transform = 'rotate(' + (S.schedExpanded ? '180deg' : '0deg') + ')';
  el('schedBody').style.display = S.schedExpanded ? '' : 'none';
  if(S.schedExpanded) el('schedBody').innerHTML = renderSchedule(summary);
}

function renderSchedule(summary){
  if(S.scheduleDone){
    return `<div style="background:#DCEDE9;border:1px solid #BFDCD5;color:#0B534B;border-radius:14px;padding:16px;display:flex;gap:12px;align-items:center;">
      <span style="width:30px;height:30px;border-radius:50%;background:#0F6E63;color:#FFF;display:flex;align-items:center;justify-content:center;font-size:15px;flex-shrink:0;">✓</span>
      <div style="flex:1;font-size:14px;line-height:1.45;"><strong>Call scheduled.</strong> Aayura will call Radha ${esc(summary)}.</div>
      <div onclick="resetSchedule()" style="font-size:13px;font-weight:700;color:#0F6E63;cursor:pointer;white-space:nowrap;">Edit</div>
    </div>`;
  }
  const memberChips = MEMBERS.map((m,i)=>`<div onclick="pickMember(${i})" style="${i===S.schedMember?CHIP_ON:CHIP}">${esc(m)}</div>`).join('');
  const slotChips = SLOTS.map((s,i)=>`<div onclick="pickSlot(${i})" style="${i===S.schedSlot?CHIP_ON:CHIP}">${s}</div>`).join('');
  const topicChips = TOPICS.map((t,i)=>`<div onclick="pickTopic(${i})" style="${i===S.schedTopic?CHIP_ON:CHIP}">${esc(t)}</div>`).join('');

  // calendar
  const vm=S.viewMonth, vy=S.viewYear;
  const firstDow = new Date(vy,vm,1).getDay();
  const dim = new Date(vy,vm+1,0).getDate();
  const today = {y:2026,m:6,d:24};
  const isPast = n => (vy<today.y)||(vy===today.y&&vm<today.m)||(vy===today.y&&vm===today.m&&n<today.d);
  const cellBase='aspect-ratio:1;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:600;border-radius:9px;';
  let cells='';
  for(let i=0;i<firstDow;i++) cells+=`<div style="aspect-ratio:1;"></div>`;
  for(let n=1;n<=dim;n++){
    const past=isPast(n);
    const sel = n===S.selDay && vm===S.selMonth && vy===S.selYear;
    const isToday = vy===today.y&&vm===today.m&&n===today.d;
    let st;
    if(past) st=cellBase+'color:#CDC3B4;cursor:default;';
    else if(sel) st=cellBase+'background:#E07A2E;color:var(--card);cursor:pointer;box-shadow:0 3px 8px rgba(224,122,46,0.35);';
    else st=cellBase+'color:var(--ink2);cursor:pointer;border:1px solid '+(isToday?'#0F6E63':'var(--border)')+';background:var(--chip2);';
    cells += past ? `<div style="${st}">${n}</div>` : `<div onclick="pickDay(${n})" style="${st}">${n}</div>`;
  }
  const weekdayRow = WEEKDAYS.map(w=>`<div style="text-align:center;font-size:10.5px;font-weight:700;color:var(--faint);padding:2px 0;">${w}</div>`).join('');

  return `<div style="display:flex;flex-direction:column;gap:14px;">
    <div><div style="font-size:12.5px;font-weight:700;color:var(--muted);margin-bottom:8px;">Requested by</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">${memberChips}</div></div>
    <div>
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">
        <div style="font-size:12.5px;font-weight:700;color:var(--muted);">Pick a day</div>
        <div style="display:flex;align-items:center;gap:6px;">
          <div onclick="prevMonth()" style="width:26px;height:26px;border-radius:8px;border:1px solid var(--border2);background:var(--chip2);color:var(--muted);display:flex;align-items:center;justify-content:center;font-size:14px;cursor:pointer;">‹</div>
          <div style="font-size:13px;font-weight:700;color:var(--ink);min-width:110px;text-align:center;">${MONTHS[vm]} ${vy}</div>
          <div onclick="nextMonth()" style="width:26px;height:26px;border-radius:8px;border:1px solid var(--border2);background:var(--chip2);color:var(--muted);display:flex;align-items:center;justify-content:center;font-size:14px;cursor:pointer;">›</div>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:repeat(7,1fr);gap:4px;margin-bottom:6px;">${weekdayRow}</div>
      <div style="display:grid;grid-template-columns:repeat(7,1fr);gap:4px;">${cells}</div>
    </div>
    <div><div style="font-size:12.5px;font-weight:700;color:var(--muted);margin-bottom:8px;">At what time?</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">${slotChips}</div></div>
    <div><div style="font-size:12.5px;font-weight:700;color:var(--muted);margin-bottom:8px;">What should she talk about?</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">${topicChips}</div></div>
    <div onclick="confirmSchedule()" class="btn" style="margin-top:4px;padding:14px;background:#E07A2E;color:var(--card);border-radius:14px;font-weight:700;font-size:15px;cursor:pointer;text-align:center;box-shadow:0 6px 16px rgba(224,122,46,0.28);">Schedule with Aayura</div>
  </div>`;
}

render();
</script>
</body>
</html>
"""


@app.route("/")
def home():
    return Response(PAGE, mimetype="text/html")


if __name__ == "__main__":
    print("=" * 56)
    print("AAYURA site  ->  http://localhost:%d" % PORT)
    print("Real calls:", "armed" if (Client and TWILIO_SID and PUBLIC_URL and PATIENT_PHONE) else "not configured (design demo only)")
    print("=" * 56)
    app.run(debug=False, host="0.0.0.0", port=PORT)
