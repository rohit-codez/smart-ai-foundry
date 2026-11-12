import base64
import csv
import hashlib
import io
import json
import os
import secrets
import sqlite3
import threading
import time
import webbrowser
from datetime import date, datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 3000
CONFIDENCE_LIMIT = 70
BASE = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE, "attendance.db")
FACE_DIR = os.path.join(BASE, "faces")
SESSIONS = set()

try:
    import cv2
    import numpy as np
    HAS_CV = hasattr(cv2, "face")
except Exception:
    HAS_CV = False

def db():
    con = sqlite3.connect(DB_FILE, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    os.makedirs(FACE_DIR, exist_ok=True)
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS students(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            roll TEXT UNIQUE NOT NULL, name TEXT NOT NULL, course TEXT DEFAULT '',
            marks REAL DEFAULT 0, assignments REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS attendance(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL, day TEXT NOT NULL, time TEXT NOT NULL,
            status TEXT DEFAULT 'Present', method TEXT DEFAULT 'Face',
            UNIQUE(student_id, day));
        """)


def get_setting(key):
    with db() as con:
        row = con.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    return row["v"] if row else None


def set_setting(key, value):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", (key, value))


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200000).hex()
    return salt + "$" + digest


def check_pw(pw, stored):
    salt = stored.split("$")[0]
    return secrets.compare_digest(hash_pw(pw, salt), stored)


recognizer = None
model_lock = threading.Lock()
detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml") if HAS_CV else None


def decode_image(data_url):
    raw = base64.b64decode(data_url.split(",", 1)[1])
    return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)


def find_face(img):
    gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    faces = detector.detectMultiScale(gray, 1.2, 5, minSize=(80, 80))
    if len(faces) == 0:
        return None
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    return cv2.resize(gray[y:y + h, x:x + w], (200, 200))


def train_model():
    global recognizer
    if not HAS_CV:
        return
    images, labels = [], []
    for name in os.listdir(FACE_DIR):
        if name.endswith(".png"):
            img = cv2.imread(os.path.join(FACE_DIR, name), 0)
            if img is not None:
                images.append(img)
                labels.append(int(name.split("_")[0]))
    with model_lock:
        if not images:
            recognizer = None
            return
        model = cv2.face.LBPHFaceRecognizer_create()
        model.train(images, np.array(labels))
        recognizer = model


def face_count(student_id):
    return len([f for f in os.listdir(FACE_DIR) if f.startswith(str(student_id) + "_")])


def mark_present(con, student_id, method="Face", day=None):
    day = day or date.today().isoformat()
    try:
        con.execute("INSERT INTO attendance(student_id,day,time,status,method) VALUES(?,?,?,?,?)",
                    (student_id, day, datetime.now().strftime("%H:%M:%S"), "Present", method))
        return True
    except sqlite3.IntegrityError:
        return False


def predict(total_days, pct, score, recent):
    if total_days < 3:
        return "Not enough data", "Needs at least 3 class days of attendance to predict."
    if score >= 75 and pct >= 75:
        return "High Performing", "Excellent! Try peer mentoring or an advanced project."
    if score < 55 or pct < 65:
        tip = "Meet a mentor this week."
        if pct < 75:
            tip += " Attendance is below 75%, so attend every class."
        if recent < pct:
            tip += " Recent attendance is dropping."
        return "At-Risk", tip
    return "Average", "Steady progress. Better attendance and assignments can lift you to the top band."


def student_stats(con):
    total = con.execute("SELECT COUNT(DISTINCT day) FROM attendance").fetchone()[0]
    last5 = [r[0] for r in con.execute("SELECT DISTINCT day FROM attendance ORDER BY day DESC LIMIT 5")]
    result = []
    for s in con.execute("SELECT * FROM students ORDER BY roll").fetchall():
        present = con.execute("SELECT COUNT(*) FROM attendance WHERE student_id=?", (s["id"],)).fetchone()[0]
        pct = round(present * 100 / total, 1) if total else 0.0
        recent = 0.0
        if last5:
            marks = ",".join("?" * len(last5))
            hit = con.execute(f"SELECT COUNT(*) FROM attendance WHERE student_id=? AND day IN ({marks})",
                              [s["id"]] + last5).fetchone()[0]
            recent = round(hit * 100 / len(last5), 1)
        score = round(0.5 * pct + 0.3 * s["marks"] + 0.2 * s["assignments"], 1)
        label, tip = predict(total, pct, score, recent)
        result.append({"id": s["id"], "roll": s["roll"], "name": s["name"], "course": s["course"],
                       "marks": s["marks"], "assignments": s["assignments"], "present": present,
                       "pct": pct, "recent": recent, "score": score, "label": label, "tip": tip,
                       "faces": face_count(s["id"])})
    return total, result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, data, ctype="application/json", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def reply(self, obj, code=200, headers=None):
        self.send(code, json.dumps(obj).encode(), headers=headers)

    def is_admin(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return "sid" in cookie and cookie["sid"].value in SESSIONS

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        path = url.path

        if path == "/":
            return self.send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/status":
            return self.reply({"has_pw": get_setting("admin_pw") is not None, "admin": self.is_admin(),
                               "face": HAS_CV, "trained": recognizer is not None})
        if path == "/api/me":
            roll = q.get("roll", [""])[0].strip().lower()
            with db() as con:
                total, rows = student_stats(con)
            for s in rows:
                if s["roll"].lower() == roll:
                    return self.reply({"ok": True, "name": s["name"], "pct": s["pct"], "present": s["present"],
                                       "days": total, "label": s["label"], "tip": s["tip"]})
            return self.reply({"ok": False, "msg": "Roll number not found."})

        if not self.is_admin():
            return self.reply({"error": "login"}, 401)

        if path == "/api/students":
            with db() as con:
                return self.reply(student_stats(con)[1])
        if path == "/api/records":
            day = q.get("day", [""])[0]
            sql = ("SELECT a.id,s.roll,s.name,a.day,a.time,a.status,a.method FROM attendance a "
                   "JOIN students s ON s.id=a.student_id")
            with db() as con:
                if day:
                    rows = con.execute(sql + " WHERE a.day=? ORDER BY a.time", (day,)).fetchall()
                else:
                    rows = con.execute(sql + " ORDER BY a.day DESC, a.time DESC LIMIT 300").fetchall()
            return self.reply([dict(r) for r in rows])
        if path == "/api/analysis":
            with db() as con:
                total, rows = student_stats(con)
                today = con.execute("SELECT COUNT(*) FROM attendance WHERE day=?",
                                    (date.today().isoformat(),)).fetchone()[0]
                daily = con.execute("SELECT day, COUNT(*) c FROM attendance GROUP BY day "
                                    "ORDER BY day DESC LIMIT 14").fetchall()
            risk = {}
            for s in rows:
                risk[s["label"]] = risk.get(s["label"], 0) + 1
            avg = round(sum(s["pct"] for s in rows) / len(rows), 1) if rows else 0
            return self.reply({"students": len(rows), "today": today, "days": total, "avg": avg,
                               "daily": [{"day": d["day"], "count": d["c"]} for d in reversed(daily)],
                               "risk": risk,
                               "low": [s for s in rows if s["pct"] < 75 and total > 0]})
        if path == "/api/export":
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow(["Roll", "Name", "Date", "Time", "Status", "Method"])
            with db() as con:
                for r in con.execute("SELECT s.roll,s.name,a.day,a.time,a.status,a.method FROM attendance a "
                                     "JOIN students s ON s.id=a.student_id ORDER BY a.day DESC"):
                    writer.writerow(list(r))
            return self.send(200, out.getvalue().encode(), "text/csv",
                             {"Content-Disposition": "attachment; filename=attendance.csv"})
        self.reply({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            size = int(self.headers.get("Content-Length", 0))
            d = json.loads(self.rfile.read(size) or b"{}")
        except Exception:
            return self.reply({"error": "bad request"}, 400)

        if path == "/api/setup":
            pw = str(d.get("password", ""))
            if get_setting("admin_pw") is not None:
                return self.reply({"ok": False, "msg": "Admin password already exists."})
            if len(pw) < 6:
                return self.reply({"ok": False, "msg": "Password must be at least 6 characters."})
            set_setting("admin_pw", hash_pw(pw))
            return self.login_ok()
        if path == "/api/login":
            stored = get_setting("admin_pw")
            if stored and check_pw(str(d.get("password", "")), stored):
                return self.login_ok()
            time.sleep(0.6)
            return self.reply({"ok": False, "msg": "Wrong password."})
        if path == "/api/logout":
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            if "sid" in cookie:
                SESSIONS.discard(cookie["sid"].value)
            return self.reply({"ok": True})
        if path == "/api/scan":
            return self.scan(d)

        if not self.is_admin():
            return self.reply({"error": "login"}, 401)

        with db() as con:
            if path == "/api/students/add":
                try:
                    con.execute("INSERT INTO students(roll,name,course,marks,assignments) VALUES(?,?,?,?,?)",
                                (str(d["roll"]).strip(), str(d["name"]).strip(), str(d.get("course", "")).strip(),
                                 float(d.get("marks") or 0), float(d.get("assignments") or 0)))
                    return self.reply({"ok": True})
                except sqlite3.IntegrityError:
                    return self.reply({"ok": False, "msg": "That roll number already exists."})
                except Exception:
                    return self.reply({"ok": False, "msg": "Please fill roll number and name."})
            if path == "/api/students/update":
                con.execute("UPDATE students SET marks=?, assignments=? WHERE id=?",
                            (float(d["marks"]), float(d["assignments"]), int(d["id"])))
                return self.reply({"ok": True})
            if path == "/api/students/delete":
                sid = int(d["id"])
                con.execute("DELETE FROM attendance WHERE student_id=?", (sid,))
                con.execute("DELETE FROM students WHERE id=?", (sid,))
                for f in os.listdir(FACE_DIR):
                    if f.startswith(str(sid) + "_"):
                        os.remove(os.path.join(FACE_DIR, f))
                threading.Thread(target=train_model, daemon=True).start()
                return self.reply({"ok": True})
            if path == "/api/records/add":
                added = mark_present(con, int(d["student_id"]), "Manual", d.get("day"))
                return self.reply({"ok": added, "msg": "" if added else "Already marked for that day."})
            if path == "/api/records/delete":
                con.execute("DELETE FROM attendance WHERE id=?", (int(d["id"]),))
                return self.reply({"ok": True})
            if path == "/api/password":
                if not check_pw(str(d.get("old", "")), get_setting("admin_pw")):
                    return self.reply({"ok": False, "msg": "Current password is wrong."})
                if len(str(d.get("new", ""))) < 6:
                    return self.reply({"ok": False, "msg": "New password needs 6+ characters."})
                set_setting("admin_pw", hash_pw(str(d["new"])))
                return self.reply({"ok": True, "msg": "Password changed."})
        if path == "/api/enroll":
            return self.enroll(d)
        self.reply({"error": "not found"}, 404)

    def login_ok(self):
        sid = secrets.token_hex(24)
        SESSIONS.add(sid)
        self.reply({"ok": True}, headers={"Set-Cookie": f"sid={sid}; Path=/; HttpOnly; SameSite=Strict"})

    def scan(self, d):
        if not HAS_CV:
            return self.reply({"ok": False, "msg": "Install opencv-contrib-python + numpy to use face recognition."})
        if recognizer is None:
            return self.reply({"ok": False, "msg": "No faces enrolled yet. Ask the admin to enroll students."})
        try:
            face = find_face(decode_image(d["image"]))
        except Exception:
            return self.reply({"ok": False, "msg": "Could not read camera frame."})
        if face is None:
            return self.reply({"ok": False, "msg": "No face found. Look at the camera."})
        with model_lock:
            label, distance = recognizer.predict(face)
        if distance > CONFIDENCE_LIMIT:
            return self.reply({"ok": False, "msg": "Face not recognised. Try better lighting."})
        with db() as con:
            student = con.execute("SELECT * FROM students WHERE id=?", (label,)).fetchone()
            if not student:
                return self.reply({"ok": False, "msg": "Unknown student."})
            new = mark_present(con, label, "Face")
        self.reply({"ok": True, "name": student["name"], "roll": student["roll"], "new": new,
                    "match": round(max(0, 100 - distance), 1)})

    def enroll(self, d):
        if not HAS_CV:
            return self.reply({"ok": False, "msg": "Install opencv-contrib-python + numpy first."})
        saved = 0
        base = int(time.time() * 1000) % 10**9
        for i, image in enumerate(d.get("images", [])):
            try:
                face = find_face(decode_image(image))
            except Exception:
                face = None
            if face is not None:
                cv2.imwrite(os.path.join(FACE_DIR, f"{int(d['id'])}_{base + i}.png"), face)
                saved += 1
        if saved < 3:
            return self.reply({"ok": False, "msg": "Face not clear. Use good light and face the camera."})
        train_model()
        self.reply({"ok": True, "msg": f"Saved {saved} face samples."})


PAGE = r'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>SmartAttend AI</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#100C1F;--panel:#19132B;--card:#241A3B;--p:#8B5CF6;--lp:#C4B5FD;--w:#F5F3FF;--m:#A69BBF;--ok:#34d399;--warn:#fbbf24;--bad:#f87171}
*{box-sizing:border-box;margin:0;font-family:'Segoe UI',system-ui,sans-serif}
body{background:radial-gradient(circle at 20% 0%,#2a1858 0,var(--bg) 55%);color:var(--w);min-height:100vh;overflow-x:hidden}
#bg{position:fixed;inset:0;z-index:0}
.wrap{position:relative;z-index:1;max-width:1150px;margin:auto;padding:18px}
nav{display:flex;align-items:center;gap:10px;margin-bottom:18px}
.logo{font-size:22px;font-weight:800;flex:1;letter-spacing:.5px}.logo span{color:var(--lp)}
button{cursor:pointer;border:0;border-radius:12px;padding:11px 18px;font-size:14px;color:var(--w);background:var(--card);transition:.2s}
button:hover{background:var(--p);transform:translateY(-2px)}
.btn{background:linear-gradient(135deg,#8B5CF6,#6D28D9);font-weight:700}
.btn.sm{padding:6px 12px;font-size:12px}.btn.red{background:#7f1d1d}
input,select{background:#150f26;border:1px solid #3b2a5c;color:var(--w);padding:11px 13px;border-radius:10px;font-size:14px;outline:0;width:100%}
input:focus,select:focus{border-color:var(--p);box-shadow:0 0 0 3px #8b5cf655}
.card{background:rgba(36,26,59,.82);backdrop-filter:blur(8px);border:1px solid #3b2a5c;border-radius:20px;padding:20px;margin-bottom:16px;transition:transform .15s}
h1{font-size:clamp(30px,5vw,54px);line-height:1.1;background:linear-gradient(90deg,#fff,var(--lp),var(--p));-webkit-background-clip:text;background-clip:text;color:transparent}
h3{color:var(--lp);margin-bottom:12px}.sub{color:var(--m);margin:10px 0 22px}
.chip{display:inline-block;background:#8b5cf633;color:var(--lp);padding:6px 14px;border-radius:99px;font-size:12px;font-weight:700;margin-bottom:12px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:16px}
.hide{display:none!important}
#scanbox{position:relative;border-radius:16px;overflow:hidden;background:#0b0816;aspect-ratio:4/3;margin-bottom:12px}
#cam,#mcam{width:100%;height:100%;object-fit:cover;transform:scaleX(-1)}
#ph{position:absolute;inset:0;display:grid;place-items:center;color:var(--m);font-size:18px}
.beam{position:absolute;left:0;right:0;height:3px;background:linear-gradient(90deg,transparent,#a78bfa,transparent);box-shadow:0 0 22px 6px #8b5cf6;top:0;display:none;animation:beam 2s ease-in-out infinite}
.live .beam{display:block}.live #ph{display:none}
@keyframes beam{0%{top:4%}50%{top:94%}100%{top:4%}}
.cn{position:absolute;width:34px;height:34px;border:3px solid var(--lp);opacity:.9}
.tl{top:12px;left:12px;border-right:0;border-bottom:0}.tr{top:12px;right:12px;border-left:0;border-bottom:0}
.bl{bottom:12px;left:12px;border-right:0;border-top:0}.br{bottom:12px;right:12px;border-left:0;border-top:0}
.msg{min-height:24px;margin-bottom:12px;color:var(--lp);font-weight:600}
.row{display:flex;gap:8px;margin-bottom:12px}
#welcome{position:fixed;inset:0;z-index:50;display:none;place-items:center;background:#100c1fdd;text-align:center}
#welcome.open{display:grid}
.pop{animation:pop .6s cubic-bezier(.2,1.6,.4,1)}@keyframes pop{from{transform:scale(.2);opacity:0}to{transform:scale(1);opacity:1}}
.tick{font-size:90px}
.gauge{display:flex;align-items:center;gap:18px;margin-top:10px}
.gauge svg{transform:rotate(-90deg)}.gauge b{font-size:34px}
.tag{display:inline-block;padding:4px 12px;border-radius:99px;font-size:12px;font-weight:700}
.High{background:#065f4655;color:var(--ok)}.Avg{background:#78350f66;color:var(--warn)}.Risk{background:#7f1d1d77;color:var(--bad)}.None{background:#3b2a5c;color:var(--m)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-bottom:16px}
.stat{background:var(--card);border:1px solid #3b2a5c;border-radius:18px;padding:18px}
.stat b{font-size:34px;display:block;color:var(--lp)}.stat span{color:var(--m);font-size:13px}
.tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}.tab.on{background:var(--p)}
table{width:100%;border-collapse:collapse;font-size:14px}th{text-align:left;color:var(--m);font-weight:600;padding:8px}
td{padding:8px;border-top:1px solid #3b2a5c}.scroll{overflow-x:auto}
.donutwrap{display:flex;align-items:center;gap:22px;flex-wrap:wrap}
.donut{width:150px;height:150px;border-radius:50%;position:relative}
.donut:after{content:'';position:absolute;inset:28px;background:var(--card);border-radius:50%}
.legend div{margin:6px 0;font-size:14px}.dot{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:8px}
#modal{position:fixed;inset:0;z-index:40;background:#000a;display:none;place-items:center;padding:16px}
#modal.open{display:grid}#modal .card{max-width:520px;width:100%}
.mini{width:84px}
</style></head><body>
<canvas id="bg"></canvas>
<div class="wrap">
 <nav>
  <div class="logo">Smart<span>Attend</span> AI</div>
  <button onclick="show('home')">Student</button>
  <button onclick="show('admin')">Admin</button>
 </nav>

 <div id="home">
  <div style="margin:20px 0 26px">
   <div class="chip">LIVE &middot; <span id="clock"></span></div>
   <h1 id="greet">Hello!</h1>
   <p class="sub">Show your face to the camera and your attendance is marked in a blink.</p>
  </div>
  <div class="grid2">
   <div class="card tilt">
    <h3>Face Attendance</h3>
    <div id="scanbox">
     <video id="cam" autoplay playsinline muted></video>
     <div class="cn tl"></div><div class="cn tr"></div><div class="cn bl"></div><div class="cn br"></div>
     <div class="beam"></div><div id="ph">Camera is off</div>
    </div>
    <div class="msg" id="scanmsg">Press start and look at the camera.</div>
    <button class="btn" id="camBtn" onclick="toggleCam()">Start Camera</button>
   </div>
   <div class="card tilt">
    <h3>My Attendance &amp; Prediction</h3>
    <div class="row"><input id="rollIn" placeholder="Enter your roll number" onkeydown="if(event.key==='Enter')myStats()">
     <button class="btn" onclick="myStats()">Check</button></div>
    <div id="me" style="color:var(--m)">Type your roll number to see your attendance percentage and performance outlook.</div>
   </div>
  </div>
 </div>

 <div id="admin" class="hide">
  <div id="loginBox" class="card" style="max-width:430px;margin:40px auto">
   <h3 id="loginTitle">Admin Login</h3>
   <p class="sub" id="loginSub" style="margin-top:0">Only the admin can open this panel.</p>
   <div class="row"><input id="pw" type="password" placeholder="Password"></div>
   <div class="row hide" id="pw2row"><input id="pw2" type="password" placeholder="Confirm password"></div>
   <div class="msg" id="loginMsg" style="color:var(--bad)"></div>
   <button class="btn" id="loginBtn" onclick="doLogin()">Log in</button>
  </div>
  <div id="panel" class="hide">
   <div style="display:flex;align-items:center;margin-bottom:12px"><h1 style="font-size:30px;flex:1">Admin Panel</h1>
    <button onclick="doLogout()">Log out</button></div>
   <div class="tabs" id="tabs"></div>
   <div id="pane"></div>
  </div>
 </div>
</div>

<div id="welcome"><div class="pop"><div class="tick">&#10004;</div><h1 id="wname" style="font-size:44px">Welcome</h1>
 <p class="sub" id="wsub"></p></div></div>

<div id="modal"><div class="card">
 <h3 id="mtitle">Enroll face</h3>
 <div id="scanbox" class="live" style="margin-bottom:12px"><video id="mcam" autoplay playsinline muted></video>
  <div class="cn tl"></div><div class="cn tr"></div><div class="cn bl"></div><div class="cn br"></div><div class="beam"></div></div>
 <div class="msg" id="mmsg"></div>
 <div class="row"><button class="btn" id="capBtn" onclick="capture()">Capture 12 photos</button>
  <button onclick="closeModal()">Close</button></div>
</div></div>

<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path,data){
  const o=data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)};
  const r=await fetch(path,o);return r.json();
}
const cls=l=>l==='High Performing'?'High':l==='Average'?'Avg':l==='At-Risk'?'Risk':'None';

const bgc=$('bg'),bx=bgc.getContext('2d');let W,H,P=[],mx=-999,my=-999;
function rs(){W=bgc.width=innerWidth;H=bgc.height=innerHeight}
addEventListener('resize',rs);rs();
for(let i=0;i<85;i++)P.push({x:Math.random()*W,y:Math.random()*H,vx:(Math.random()-.5)*.6,vy:(Math.random()-.5)*.6});
addEventListener('mousemove',e=>{mx=e.clientX;my=e.clientY});
(function loop(){
  bx.clearRect(0,0,W,H);
  for(const p of P){
    p.x+=p.vx;p.y+=p.vy;if(p.x<0||p.x>W)p.vx*=-1;if(p.y<0||p.y>H)p.vy*=-1;
    const dx=p.x-mx,dy=p.y-my,d=Math.hypot(dx,dy)||1;
    if(d<150){p.x+=dx/d*2.5;p.y+=dy/d*2.5}
    bx.fillStyle='rgba(196,181,253,.85)';bx.beginPath();bx.arc(p.x,p.y,2,0,7);bx.fill();
  }
  for(let i=0;i<P.length;i++)for(let j=i+1;j<P.length;j++){
    const d=Math.hypot(P[i].x-P[j].x,P[i].y-P[j].y);
    if(d<110){bx.strokeStyle='rgba(139,92,246,'+((1-d/110)*.5)+')';bx.beginPath();bx.moveTo(P[i].x,P[i].y);bx.lineTo(P[j].x,P[j].y);bx.stroke()}
  }
  requestAnimationFrame(loop);
})();

document.addEventListener('mousemove',e=>{
  document.querySelectorAll('.tilt').forEach(c=>{
    const r=c.getBoundingClientRect();
    if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom){c.style.transform='';return}
    const x=(e.clientX-r.left)/r.width-.5,y=(e.clientY-r.top)/r.height-.5;
    c.style.transform=`perspective(900px) rotateY(${x*6}deg) rotateX(${-y*6}deg)`;
  });
});

function tick(){
  const n=new Date(),h=n.getHours();
  $('clock').textContent=n.toLocaleTimeString()+' | '+n.toLocaleDateString(undefined,{weekday:'long',day:'numeric',month:'short'});
  $('greet').textContent=(h<12?'Good morning':h<17?'Good afternoon':'Good evening')+', student!';
}
setInterval(tick,1000);tick();

function show(v){
  $('home').classList.toggle('hide',v!=='home');$('admin').classList.toggle('hide',v!=='admin');
  if(v==='admin'){stopCam();adminInit()}
}

function frame(video){
  const c=document.createElement('canvas');c.width=320;c.height=240;
  c.getContext('2d').drawImage(video,0,0,320,240);return c.toDataURL('image/jpeg',.85);
}
let stream=null,timer=null,busy=false;
async function toggleCam(){
  if(stream){stopCam();return}
  try{
    stream=await navigator.mediaDevices.getUserMedia({video:{width:640,height:480}});
    $('cam').srcObject=stream;$('scanbox').classList.add('live');$('camBtn').textContent='Stop Camera';
    $('scanmsg').textContent='Scanning... hold still and look at the camera.';
    timer=setInterval(scanOnce,1800);
  }catch(e){$('scanmsg').textContent='Camera blocked. Allow camera access in the browser.'}
}
function stopCam(){
  if(timer)clearInterval(timer);timer=null;
  if(stream)stream.getTracks().forEach(t=>t.stop());stream=null;
  $('scanbox').classList.remove('live');$('camBtn').textContent='Start Camera';
}
async function scanOnce(){
  if(busy||!stream)return;busy=true;
  try{
    const r=await api('/api/scan',{image:frame($('cam'))});
    if(r.ok){
      stopCam();$('wname').textContent=r.name;
      $('wsub').textContent=(r.new?'Attendance marked for today':'Already marked today')+' | Roll '+r.roll+' | match '+r.match+'%';
      $('welcome').classList.add('open');burst();
      setTimeout(()=>$('welcome').classList.remove('open'),3500);
      $('scanmsg').textContent='Done! You can close the camera.';
    }else $('scanmsg').textContent=r.msg;
  }catch(e){$('scanmsg').textContent='Server not reachable.'}
  busy=false;
}
function burst(){
  for(let i=0;i<36;i++){
    const s=document.createElement('div');s.textContent=['✦','★','●','✧'][i%4];
    s.style.cssText='position:fixed;z-index:60;left:50%;top:50%;font-size:'+(14+Math.random()*18)+'px;color:hsl('+(250+Math.random()*60)+',90%,75%);pointer-events:none';
    document.body.appendChild(s);
    const a=Math.random()*6.28,d=150+Math.random()*320;
    s.animate([{transform:'translate(0,0)',opacity:1},{transform:`translate(${Math.cos(a)*d}px,${Math.sin(a)*d}px) rotate(360deg)`,opacity:0}],{duration:1400,easing:'ease-out'}).onfinish=()=>s.remove();
  }
}

async function myStats(){
  const roll=$('rollIn').value.trim();if(!roll)return;
  const r=await api('/api/me?roll='+encodeURIComponent(roll));
  if(!r.ok){$('me').innerHTML='<span style="color:var(--bad)">'+esc(r.msg)+'</span>';return}
  const C=2*Math.PI*52,col=r.pct>=75?'#34d399':r.pct>=60?'#fbbf24':'#f87171';
  $('me').innerHTML=`<h3 style="margin-bottom:0">${esc(r.name)}</h3>
   <div class="gauge"><svg width="130" height="130"><circle cx="65" cy="65" r="52" stroke="#3b2a5c" stroke-width="12" fill="none"/>
   <circle id="arc" cx="65" cy="65" r="52" stroke="${col}" stroke-width="12" fill="none" stroke-linecap="round" stroke-dasharray="${C}" stroke-dashoffset="${C}" style="transition:1.2s"/></svg>
   <div><b style="color:${col}">${r.pct}%</b><div style="color:var(--m)">${r.present} of ${r.days} class days</div>
   <span class="tag ${cls(r.label)}">${esc(r.label)}</span></div></div>
   <p style="margin-top:12px;color:var(--w)">${esc(r.tip)}</p>`;
  setTimeout(()=>{$('arc').style.strokeDashoffset=C*(1-Math.min(r.pct,100)/100)},50);
}

let ST={},tab='dash',enrollId=0,mstream=null;
const TABS={dash:'Dashboard',stu:'Students',rec:'Records',pred:'Prediction',set:'Settings'};
async function adminInit(){
  ST=await api('/api/status');
  if(ST.admin){$('loginBox').classList.add('hide');$('panel').classList.remove('hide');go(tab);return}
  $('panel').classList.add('hide');$('loginBox').classList.remove('hide');
  const first=!ST.has_pw;
  $('loginTitle').textContent=first?'Create Admin Password':'Admin Login';
  $('loginSub').textContent=first?'First time here! Choose a password (6+ characters). Only you will know it.':'Only the admin can open this panel.';
  $('pw2row').classList.toggle('hide',!first);$('loginBtn').textContent=first?'Create & enter':'Log in';
}
async function doLogin(){
  const pw=$('pw').value;$('loginMsg').textContent='';
  if(!ST.has_pw&&pw!==$('pw2').value){$('loginMsg').textContent='Passwords do not match.';return}
  const r=await api(ST.has_pw?'/api/login':'/api/setup',{password:pw});
  if(r.ok){$('pw').value='';$('pw2').value='';adminInit()}else $('loginMsg').textContent=r.msg;
}
async function doLogout(){await api('/api/logout',{});adminInit()}
function tabs(){$('tabs').innerHTML=Object.entries(TABS).map(([k,v])=>`<button class="tab ${k===tab?'on':''}" onclick="go('${k}')">${v}</button>`).join('')}
async function go(k){
  tab=k;tabs();
  ({dash:vDash,stu:vStu,rec:vRec,pred:vPred,set:vSet})[k]();
}
const stat=(l,n,s='')=>`<div class="stat"><b data-n="${n}" data-s="${s}">0</b><span>${l}</span></div>`;
function countUp(){
  document.querySelectorAll('[data-n]').forEach(el=>{
    const end=parseFloat(el.dataset.n),s=el.dataset.s,t0=performance.now();
    (function f(t){const k=Math.min(1,(t-t0)/900);const v=end*k;el.textContent=(Number.isInteger(end)?Math.round(v):v.toFixed(1))+s;if(k<1)requestAnimationFrame(f)})(t0);
  });
}
async function vDash(){
  const a=await api('/api/analysis');
  $('pane').innerHTML=`<div class="stats">${stat('Students',a.students)}${stat('Present today',a.today)}${stat('Class days',a.days)}${stat('Average attendance',a.avg,'%')}</div>
  <div class="grid2"><div class="card"><h3>Daily attendance (last 14 days)</h3><canvas id="bars" width="520" height="230" style="width:100%"></canvas></div>
  <div class="card"><h3>Performance outlook</h3><div class="donutwrap"><div class="donut" id="donut"></div><div class="legend" id="legend"></div></div></div></div>
  <div class="card"><h3>Students below 75% attendance</h3>${a.low.length?`<div class="scroll"><table><tr><th>Roll</th><th>Name</th><th>Attendance</th></tr>${a.low.map(s=>`<tr><td>${esc(s.roll)}</td><td>${esc(s.name)}</td><td><span class="tag Risk">${s.pct}%</span></td></tr>`).join('')}</table></div>`:'<p style="color:var(--m)">Nobody is below 75% yet.</p>'}</div>`;
  countUp();drawBars(a.daily,Math.max(1,a.students));drawDonut(a.risk);
}
function drawBars(d,max){
  const c=$('bars'),x=c.getContext('2d'),t0=performance.now();
  (function f(t){
    const k=Math.min(1,(t-t0)/800);x.clearRect(0,0,520,230);
    if(!d.length){x.fillStyle='#A69BBF';x.font='15px Segoe UI';x.fillText('No attendance yet. Records appear here.',130,115)}
    const bw=Math.min(30,440/Math.max(d.length,1)-8);
    d.forEach((r,i)=>{
      const h=(r.count/max)*160*k,px=30+i*(bw+10),py=190-h;
      const g=x.createLinearGradient(0,py,0,190);g.addColorStop(0,'#C4B5FD');g.addColorStop(1,'#7C3AED');
      x.fillStyle=g;x.fillRect(px,py,bw,h);
      x.fillStyle='#F5F3FF';x.font='12px Segoe UI';x.fillText(r.count,px+bw/4,py-5);
      x.fillStyle='#A69BBF';x.fillText(r.day.slice(5),px-2,208);
    });
    if(k<1)requestAnimationFrame(f);
  })(t0);
}
function drawDonut(risk){
  const items=[['High Performing','#34d399'],['Average','#fbbf24'],['At-Risk','#f87171'],['Not enough data','#6b5b95']];
  const total=items.reduce((s,i)=>s+(risk[i[0]]||0),0);
  let acc=0,parts=[];
  items.forEach(([n,c])=>{const v=(risk[n]||0)/(total||1)*100;parts.push(`${c} ${acc}% ${acc+v}%`);acc+=v});
  $('donut').style.background=total?`conic-gradient(${parts.join(',')})`:'#3b2a5c';
  $('legend').innerHTML=items.map(([n,c])=>`<div><span class="dot" style="background:${c}"></span>${n}: <b>${risk[n]||0}</b></div>`).join('');
}
async function vStu(){
  const rows=await api('/api/students');
  $('pane').innerHTML=`<div class="card"><h3>Add student</h3>
   <div class="grid2" style="gap:10px"><input id="nr" placeholder="Roll number"><input id="nn" placeholder="Full name"><input id="nc" placeholder="Course / class">
   <input id="nm" type="number" placeholder="Marks % (optional)"><input id="na" type="number" placeholder="Assignments % (optional)"></div>
   <div class="msg" id="smsg" style="margin-top:10px;color:var(--bad)"></div><button class="btn" onclick="addStu()">Add student</button></div>
  <div class="card"><h3>All students (${rows.length})</h3><div class="scroll"><table><tr><th>Roll</th><th>Name</th><th>Course</th><th>Face</th><th></th></tr>
  ${rows.map(s=>`<tr><td>${esc(s.roll)}</td><td>${esc(s.name)}</td><td>${esc(s.course)}</td>
   <td>${s.faces?`<span class="tag High">${s.faces} samples</span>`:'<span class="tag None">not enrolled</span>'}</td>
   <td><button class="btn sm" onclick="enroll(${s.id},'${esc(s.name).replace(/'/g,'')}')">Enroll face</button>
   <button class="btn sm red" onclick="delStu(${s.id})">Delete</button></td></tr>`).join('')}</table></div></div>`;
}
async function addStu(){
  const r=await api('/api/students/add',{roll:$('nr').value,name:$('nn').value,course:$('nc').value,marks:$('nm').value,assignments:$('na').value});
  if(r.ok)vStu();else $('smsg').textContent=r.msg;
}
async function delStu(id){if(confirm('Delete this student and all their records?')){await api('/api/students/delete',{id});vStu()}}

async function enroll(id,name){
  enrollId=id;$('modal').classList.add('open');$('mtitle').textContent='Enroll face: '+name;$('capBtn').disabled=false;
  try{mstream=await navigator.mediaDevices.getUserMedia({video:true});$('mcam').srcObject=mstream;$('mmsg').textContent='Face the camera in good light, then press Capture.'}
  catch(e){$('mmsg').textContent='Camera blocked. Allow camera access.'}
}
async function capture(){
  if(!mstream)return;$('capBtn').disabled=true;const imgs=[];
  for(let i=0;i<12;i++){imgs.push(frame($('mcam')));$('mmsg').textContent='Capturing '+(i+1)+'/12 - turn your head slightly';await new Promise(r=>setTimeout(r,300))}
  $('mmsg').textContent='Training face model...';
  const r=await api('/api/enroll',{id:enrollId,images:imgs});
  $('mmsg').textContent=r.msg;$('capBtn').disabled=false;
  if(r.ok)setTimeout(()=>{closeModal();vStu()},900);
}
function closeModal(){if(mstream)mstream.getTracks().forEach(t=>t.stop());mstream=null;$('modal').classList.remove('open')}

async function vRec(){
  const today=new Date().toISOString().slice(0,10);
  const stu=await api('/api/students');
  $('pane').innerHTML=`<div class="card"><h3>Attendance records</h3>
   <div class="row"><input type="date" id="rd" value="${today}" onchange="loadRec()"><button onclick="document.getElementById('rd').value='';loadRec()">All recent</button>
   <a href="/api/export"><button class="btn">Export CSV</button></a></div>
   <div class="row"><select id="ms">${stu.map(s=>`<option value="${s.id}">${esc(s.roll)} - ${esc(s.name)}</option>`).join('')}</select>
   <button class="btn" onclick="manual()">Mark present (manual)</button></div>
   <div class="msg" id="rmsg" style="color:var(--bad)"></div><div class="scroll" id="rtable"></div></div>`;
  loadRec();
}
async function loadRec(){
  const d=$('rd').value,rows=await api('/api/records'+(d?'?day='+d:''));
  $('rtable').innerHTML=rows.length?`<table><tr><th>Date</th><th>Time</th><th>Roll</th><th>Name</th><th>Status</th><th>Method</th><th></th></tr>
  ${rows.map(r=>`<tr><td>${r.day}</td><td>${r.time}</td><td>${esc(r.roll)}</td><td>${esc(r.name)}</td><td><span class="tag High">${r.status}</span></td><td>${r.method}</td>
  <td><button class="btn sm red" onclick="delRec(${r.id})">X</button></td></tr>`).join('')}</table>`:'<p style="color:var(--m)">No records for this selection.</p>';
}
async function manual(){
  const r=await api('/api/records/add',{student_id:$('ms').value,day:$('rd').value||undefined});
  $('rmsg').textContent=r.ok?'':r.msg;loadRec();
}
async function delRec(id){await api('/api/records/delete',{id});loadRec()}

async function vPred(){
  const rows=await api('/api/students');
  $('pane').innerHTML=`<div class="card"><h3>Student performance prediction</h3>
  <p class="sub" style="margin-top:0">Score = 50% attendance + 30% marks + 20% assignments. Update marks to refresh the prediction.</p>
  <div class="scroll"><table><tr><th>Student</th><th>Attend.</th><th>Marks %</th><th>Assign. %</th><th>Score</th><th>Outlook</th><th>Advice</th><th></th></tr>
  ${rows.map(s=>`<tr><td>${esc(s.name)}<br><small style="color:var(--m)">${esc(s.roll)}</small></td><td>${s.pct}%</td>
  <td><input class="mini" id="m${s.id}" type="number" value="${s.marks}"></td><td><input class="mini" id="a${s.id}" type="number" value="${s.assignments}"></td>
  <td><b>${s.score}</b></td><td><span class="tag ${cls(s.label)}">${esc(s.label)}</span></td><td style="max-width:260px;font-size:13px">${esc(s.tip)}</td>
  <td><button class="btn sm" onclick="saveScore(${s.id})">Save</button></td></tr>`).join('')}</table></div></div>`;
}
async function saveScore(id){await api('/api/students/update',{id,marks:$('m'+id).value||0,assignments:$('a'+id).value||0});vPred()}

function vSet(){
  $('pane').innerHTML=`<div class="card" style="max-width:460px"><h3>Change admin password</h3>
  <div class="row"><input id="op" type="password" placeholder="Current password"></div>
  <div class="row"><input id="np" type="password" placeholder="New password (6+ characters)"></div>
  <div class="msg" id="pmsg"></div><button class="btn" onclick="chPw()">Update password</button>
  <p class="sub">Face recognition: <b>${ST.face?'ready':'not installed (pip install opencv-contrib-python numpy)'}</b></p></div>`;
}
async function chPw(){const r=await api('/api/password',{old:$('op').value,new:$('np').value});$('pmsg').textContent=r.msg;$('pmsg').style.color=r.ok?'var(--ok)':'var(--bad)'}
</script></body></html>
'''

if __name__ == "__main__":
    init_db()
    if HAS_CV:
        train_model()
    else:
        print("NOTE: face recognition is off. Run:  pip install opencv-contrib-python numpy")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"\n  SmartAttend AI is running ->  {url}\n  Press Ctrl + C to stop.\n")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")