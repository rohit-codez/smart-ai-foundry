"""
PulseCBT - Smart Computer Based Test system (single file, pure Python)

Features : Student & Admin login | MCQ question panel | Timer based exams |
           Automatic result calculation | Performance analysis & prediction

Install  : nothing! Only Python's built-in modules are used (no pip, no venv).
Run      : python smart-cbt-exam.py        (opens http://localhost:3000 automatically)
"""
import hashlib
import json
import os
import random
import secrets
import sqlite3
import threading
import time
import webbrowser
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 3000
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pulsecbt.db")
SESSIONS = {}          # session id -> {"role", "uid", "name"}


# ---------------------------------------------------------------- database
def db():
    con = sqlite3.connect(DB_FILE, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def rows(con, sql, args=()):
    return [dict(r) for r in con.execute(sql, args).fetchall()]


SEED = [  # question, options, correct index, topic, explanation
    ("What is the type of the result of 3 / 2 in Python?", ["int", "float", "str", "complex"], 1, "Data Types",
     "The / operator always gives a float, even for whole numbers."),
    ("Which keyword is used to create a function?", ["func", "define", "def", "fun"], 2, "Functions",
     "Functions start with the def keyword."),
    ("What does len([1, 2, [3, 4]]) return?", ["2", "3", "4", "5"], 1, "Data Types",
     "The inner list counts as one single item, so there are 3 items."),
    ("Which values does range(2, 10, 3) produce?", ["2, 5, 8", "2, 5, 8, 10", "2, 4, 6, 8", "3, 6, 9"], 0, "Control Flow",
     "It starts at 2, jumps by 3 and stops before 10."),
    ("Which of these data types is immutable?", ["list", "dict", "tuple", "set"], 2, "Data Types",
     "A tuple cannot be changed after it is created."),
    ("What does the break statement do inside a loop?", ["Skips one round", "Exits the loop", "Ends the program",
                                                          "Restarts the loop"], 1, "Control Flow",
     "break leaves the nearest loop immediately."),
    ("What is the output of print('Py' * 3)?", ["Py3", "PyPyPy", "Py Py Py", "Error"], 1, "Basics",
     "Multiplying a string repeats it."),
    ("Which symbol starts a single-line comment?", ["//", "#", "/*", "--"], 1, "Basics",
     "Python comments begin with #."),
    ("What does a function return if it has no return statement?", ["0", "None", "An empty string", "An error"], 1,
     "Functions", "Such a function quietly returns None."),
    ("What is the value of 7 // 2 ?", ["3.5", "3", "4", "2"], 1, "Basics",
     "// is floor division and drops the decimal part."),
]


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT, roll TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
            pw TEXT NOT NULL, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS exams(
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, subject TEXT DEFAULT '',
            minutes INTEGER DEFAULT 10, pass_pct INTEGER DEFAULT 40, marks_per_q INTEGER DEFAULT 1,
            published INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS questions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, exam_id INTEGER NOT NULL, text TEXT NOT NULL,
            a TEXT, b TEXT, c TEXT, d TEXT, correct INTEGER NOT NULL, topic TEXT DEFAULT 'General',
            explain TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS attempts(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, exam_id INTEGER NOT NULL,
            started REAL NOT NULL, finished REAL, score INTEGER DEFAULT 0, total INTEGER DEFAULT 0,
            answers TEXT DEFAULT '{}', qorder TEXT DEFAULT '[]', switches INTEGER DEFAULT 0);
        """)
        if con.execute("SELECT COUNT(*) FROM exams").fetchone()[0] == 0:
            cur = con.execute("INSERT INTO exams(title,subject,minutes,pass_pct) VALUES(?,?,?,?)",
                              ("Python Fundamentals Quiz", "Python", 10, 40))
            for t, o, c, topic, ex in SEED:
                con.execute("INSERT INTO questions(exam_id,text,a,b,c,d,correct,topic,explain) VALUES(?,?,?,?,?,?,?,?,?)",
                            (cur.lastrowid, t, o[0], o[1], o[2], o[3], c, topic, ex))


def get_setting(key):
    with db() as con:
        r = con.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    return r["v"] if r else None


def set_setting(key, value):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", (key, value))


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100000).hex()


def check_pw(pw, stored):
    return secrets.compare_digest(hash_pw(pw, stored.split("$")[0]), stored)


# ---------------------------------------------------------------- exam engine
def limit_secs(con, att):
    return con.execute("SELECT minutes FROM exams WHERE id=?", (att["exam_id"],)).fetchone()["minutes"] * 60


def finalize(con, att):
    """Automatic result calculation: count right answers and store the score."""
    if att["finished"]:
        return
    qs = con.execute("SELECT id,correct FROM questions WHERE exam_id=?", (att["exam_id"],)).fetchall()
    mp = con.execute("SELECT marks_per_q FROM exams WHERE id=?", (att["exam_id"],)).fetchone()["marks_per_q"]
    ans = json.loads(att["answers"] or "{}")
    right = sum(1 for q in qs if ans.get(str(q["id"])) == q["correct"])
    con.execute("UPDATE attempts SET finished=?, score=?, total=? WHERE id=?",
                (time.time(), right * mp, len(qs) * mp, att["id"]))


def live_attempt(con, uid, aid):
    """Return the unfinished attempt; auto-submit it if the time has run out."""
    att = con.execute("SELECT * FROM attempts WHERE id=? AND user_id=?", (aid, uid)).fetchone()
    if not att or att["finished"]:
        return None
    if time.time() > att["started"] + limit_secs(con, att) + 3:
        finalize(con, att)
        return None
    return att


def pct_of(a):
    return round(a["score"] * 100 / a["total"], 1) if a["total"] else 0.0


def result_of(con, att):
    ex = con.execute("SELECT * FROM exams WHERE id=?", (att["exam_id"],)).fetchone()
    ans = json.loads(att["answers"] or "{}")
    review, right, skipped = [], 0, 0
    for qid in json.loads(att["qorder"]):
        q = con.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
        if not q:
            continue
        ch = ans.get(str(qid))
        right += ch == q["correct"]
        skipped += ch is None
        review.append({"text": q["text"], "opts": [q["a"], q["b"], q["c"], q["d"]], "correct": q["correct"],
                       "chosen": ch, "topic": q["topic"], "explain": q["explain"]})
    pct = pct_of(att)
    return {"title": ex["title"], "score": att["score"], "total": att["total"], "pct": pct,
            "passed": pct >= ex["pass_pct"], "pass_pct": ex["pass_pct"], "right": right, "skipped": skipped,
            "wrong": len(review) - right - skipped, "secs": round(att["finished"] - att["started"]),
            "switches": att["switches"], "review": review}


def predict(pcts, weak):
    """Weighted average of past scores (recent exams count more) plus the trend."""
    if len(pcts) < 2:
        return {"value": None, "label": "Need more data", "tip": "Finish at least 2 exams to unlock your prediction."}
    w = range(1, len(pcts) + 1)
    avg = sum(p * x for p, x in zip(pcts, w)) / sum(w)
    slope = max(-15, min(15, (pcts[-1] - pcts[0]) / (len(pcts) - 1)))
    val = max(0, min(100, round(avg + 0.5 * slope)))
    label = ("Top Performer" if val >= 75 else "On Track" if val >= 55 else "Borderline" if val >= 40 else "At Risk")
    tip = "Your scores are climbing. Keep going!" if slope > 2 else \
        "Your scores are dropping. Slow down and revise." if slope < -2 else "Your scores are steady."
    if weak:
        tip += " Focus on " + weak[0]["topic"] + " next."
    return {"value": val, "label": label, "tip": tip}


def student_analysis(con, uid):
    atts = rows(con, "SELECT a.*, e.title, e.pass_pct FROM attempts a JOIN exams e ON e.id=a.exam_id "
                     "WHERE a.user_id=? AND a.finished IS NOT NULL ORDER BY a.finished", (uid,))
    topics, history = {}, []
    for a in atts:
        ans = json.loads(a["answers"] or "{}")
        for q in con.execute("SELECT id,correct,topic FROM questions WHERE exam_id=?", (a["exam_id"],)):
            t = topics.setdefault(q["topic"], [0, 0])
            t[1] += 1
            t[0] += ans.get(str(q["id"])) == q["correct"]
        history.append({"id": a["id"], "title": a["title"], "pct": pct_of(a), "score": a["score"], "total": a["total"],
                        "day": datetime.fromtimestamp(a["finished"]).strftime("%d %b"),
                        "passed": pct_of(a) >= a["pass_pct"]})
    tlist = sorted(({"topic": k, "pct": round(v[0] * 100 / v[1]), "n": v[1]} for k, v in topics.items()),
                   key=lambda x: x["pct"])
    weak = [t for t in tlist if t["pct"] < 60]
    return {"history": history, "topics": tlist, "predict": predict([h["pct"] for h in history], weak)}


def exam_analytics(con, eid):
    atts = rows(con, "SELECT a.*, u.name FROM attempts a JOIN users u ON u.id=a.user_id "
                     "WHERE a.exam_id=? AND a.finished IS NOT NULL", (eid,))
    ex = con.execute("SELECT pass_pct FROM exams WHERE id=?", (eid,)).fetchone()
    pcts = [pct_of(a) for a in atts]
    qs = rows(con, "SELECT id,text,correct FROM questions WHERE exam_id=?", (eid,))
    hard = []
    for q in qs:
        hit = sum(json.loads(a["answers"] or "{}").get(str(q["id"])) == q["correct"] for a in atts)
        hard.append({"text": q["text"], "pct": round(hit * 100 / len(atts)) if atts else 0})
    top = sorted(atts, key=lambda a: -pct_of(a))[:5]
    return {"count": len(atts), "avg": round(sum(pcts) / len(pcts), 1) if pcts else 0,
            "pass_rate": round(100 * sum(p >= ex["pass_pct"] for p in pcts) / len(pcts)) if pcts else 0,
            "best": max(pcts) if pcts else 0,
            "buckets": [sum(lo <= p < hi for p in pcts) for lo, hi in ((0, 40), (40, 60), (60, 80), (80, 101))],
            "hardest": sorted(hard, key=lambda x: x["pct"])[:5] if atts else [],
            "top": [{"name": a["name"], "pct": pct_of(a)} for a in top]}


# ---------------------------------------------------------------- web server
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

    def sess(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return SESSIONS.get(c["sid"].value) if "sid" in c else None

    def login_ok(self, role, uid, name):
        sid = secrets.token_hex(24)
        SESSIONS[sid] = {"role": role, "uid": uid, "name": name}
        self.reply({"ok": True}, headers={"Set-Cookie": "sid=%s; Path=/; HttpOnly; SameSite=Strict" % sid})

    # ------------------------------------------------------------- GET
    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        p, s = url.path, self.sess()
        if p == "/":
            return self.send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if p == "/api/status":
            return self.reply({"has_admin": get_setting("admin_pw") is not None,
                               "role": s["role"] if s else None, "name": s["name"] if s else ""})
        if not s:
            return self.reply({"error": "login"}, 401)
        with db() as con:
            if p == "/api/result":
                att = con.execute("SELECT * FROM attempts WHERE id=?", (int(q.get("attempt", 0)),)).fetchone()
                if not att or not att["finished"] or (s["role"] == "student" and att["user_id"] != s["uid"]):
                    return self.reply({"error": "not found"}, 404)
                return self.reply(result_of(con, att))
            if s["role"] == "student":
                if p == "/api/exams":
                    out = rows(con, (
                        "SELECT e.id,e.title,e.subject,e.minutes,e.pass_pct,e.published,"
                        "(SELECT COUNT(*) FROM questions q WHERE q.exam_id=e.id) n,"
                        "(SELECT COUNT(*) FROM attempts a WHERE a.exam_id=e.id AND a.user_id=? AND a.finished IS NOT NULL) tries,"
                        "(SELECT MAX(a.score*100.0/a.total) FROM attempts a WHERE a.exam_id=e.id AND a.user_id=? AND a.total>0) best,"
                        "(SELECT id FROM attempts a WHERE a.exam_id=e.id AND a.user_id=? AND a.finished IS NULL) running "
                        "FROM exams e WHERE e.published=1"), (s["uid"],) * 3)
                    return self.reply([e for e in out if e["n"] > 0])
                if p == "/api/my":
                    return self.reply(student_analysis(con, s["uid"]))
            else:
                if p == "/api/admin/overview":
                    studs = rows(con, (
                        "SELECT u.name,u.roll,u.created,(SELECT COUNT(*) FROM attempts a WHERE a.user_id=u.id AND a.finished IS NOT NULL) tries,"
                        "(SELECT ROUND(AVG(a.score*100.0/a.total),1) FROM attempts a WHERE a.user_id=u.id AND a.finished IS NOT NULL AND a.total>0) avg "
                        "FROM users u ORDER BY u.name"))
                    one = lambda sql: con.execute(sql).fetchone()[0]
                    return self.reply({"students": len(studs), "exams": one("SELECT COUNT(*) FROM exams"),
                                       "questions": one("SELECT COUNT(*) FROM questions"),
                                       "attempts": one("SELECT COUNT(*) FROM attempts WHERE finished IS NOT NULL"),
                                       "list": studs})
                if p == "/api/admin/exams":
                    return self.reply(rows(con, (
                        "SELECT e.*,(SELECT COUNT(*) FROM questions q WHERE q.exam_id=e.id) n,"
                        "(SELECT COUNT(*) FROM attempts a WHERE a.exam_id=e.id AND a.finished IS NOT NULL) tries FROM exams e")))
                if p == "/api/admin/questions":
                    return self.reply(rows(con, "SELECT * FROM questions WHERE exam_id=?", (int(q.get("exam", 0)),)))
                if p == "/api/admin/results":
                    return self.reply(rows(con, (
                        "SELECT a.id,u.name,u.roll,e.title,a.score,a.total,a.switches,a.finished FROM attempts a "
                        "JOIN users u ON u.id=a.user_id JOIN exams e ON e.id=a.exam_id WHERE a.finished IS NOT NULL "
                        "ORDER BY a.finished DESC LIMIT 200")))
                if p == "/api/admin/analytics":
                    return self.reply(exam_analytics(con, int(q.get("exam", 0))))
        self.reply({"error": "not found"}, 404)

    # ------------------------------------------------------------- POST
    def do_POST(self):
        p = urlparse(self.path).path
        try:
            d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except Exception:
            return self.reply({"error": "bad request"}, 400)

        # ---- public: login / register
        if p == "/api/admin/setup":
            pw = str(d.get("password", ""))
            if get_setting("admin_pw") is not None:
                return self.reply({"ok": False, "msg": "Admin password already exists."})
            if len(pw) < 6:
                return self.reply({"ok": False, "msg": "Password must be at least 6 characters."})
            set_setting("admin_pw", hash_pw(pw))
            return self.login_ok("admin", 0, "Admin")
        if p == "/api/admin/login":
            stored = get_setting("admin_pw")
            if stored and check_pw(str(d.get("password", "")), stored):
                return self.login_ok("admin", 0, "Admin")
            time.sleep(0.6)
            return self.reply({"ok": False, "msg": "Wrong admin password."})
        if p == "/api/student/register":
            name, roll, pw = str(d.get("name", "")).strip(), str(d.get("roll", "")).strip(), str(d.get("password", ""))
            if not name or not roll or len(pw) < 4:
                return self.reply({"ok": False, "msg": "Fill name, roll number and a password (4+ characters)."})
            try:
                with db() as con:
                    cur = con.execute("INSERT INTO users(roll,name,pw,created) VALUES(?,?,?,?)",
                                      (roll, name, hash_pw(pw), datetime.now().strftime("%Y-%m-%d")))
                return self.login_ok("student", cur.lastrowid, name)
            except sqlite3.IntegrityError:
                return self.reply({"ok": False, "msg": "That roll number is already registered."})
        if p == "/api/student/login":
            with db() as con:
                u = con.execute("SELECT * FROM users WHERE roll=?", (str(d.get("roll", "")).strip(),)).fetchone()
            if u and check_pw(str(d.get("password", "")), u["pw"]):
                return self.login_ok("student", u["id"], u["name"])
            time.sleep(0.6)
            return self.reply({"ok": False, "msg": "Wrong roll number or password."})
        if p == "/api/logout":
            c = SimpleCookie(self.headers.get("Cookie", ""))
            if "sid" in c:
                SESSIONS.pop(c["sid"].value, None)
            return self.reply({"ok": True})

        s = self.sess()
        if not s:
            return self.reply({"error": "login"}, 401)
        with db() as con:
            if s["role"] == "student":
                return self.student_post(con, s, p, d)
            return self.admin_post(con, p, d)

    def student_post(self, con, s, p, d):
        uid = s["uid"]
        if p == "/api/start":
            ex = con.execute("SELECT * FROM exams WHERE id=? AND published=1", (int(d["exam_id"]),)).fetchone()
            if not ex:
                return self.reply({"ok": False, "msg": "Exam not available."})
            att = con.execute("SELECT * FROM attempts WHERE user_id=? AND exam_id=? AND finished IS NULL",
                              (uid, ex["id"])).fetchone()
            if att and live_attempt(con, uid, att["id"]) is None:   # time already over -> auto submitted
                return self.reply({"ok": True, "done": att["id"]})
            if not att:
                ids = [r[0] for r in con.execute("SELECT id FROM questions WHERE exam_id=?", (ex["id"],))]
                if not ids:
                    return self.reply({"ok": False, "msg": "This exam has no questions yet."})
                random.shuffle(ids)
                cur = con.execute("INSERT INTO attempts(user_id,exam_id,started,qorder) VALUES(?,?,?,?)",
                                  (uid, ex["id"], time.time(), json.dumps(ids)))
                att = con.execute("SELECT * FROM attempts WHERE id=?", (cur.lastrowid,)).fetchone()
            qs = []
            for qid in json.loads(att["qorder"]):
                r = con.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
                if r:
                    qs.append({"id": r["id"], "text": r["text"], "opts": [r["a"], r["b"], r["c"], r["d"]], "topic": r["topic"]})
            secs = ex["minutes"] * 60
            return self.reply({"ok": True, "attempt": att["id"], "title": ex["title"], "secs": secs,
                               "remaining": max(1, round(att["started"] + secs - time.time())),
                               "questions": qs, "answers": json.loads(att["answers"] or "{}")})
        if p in ("/api/answer", "/api/switch", "/api/submit"):
            aid = int(d.get("attempt", 0))
            att = live_attempt(con, uid, aid)
            if att is None:
                return self.reply({"ok": False, "done": aid})
            if p == "/api/answer":
                ans = json.loads(att["answers"] or "{}")
                if d.get("choice") in (0, 1, 2, 3):
                    ans[str(int(d["qid"]))] = d["choice"]
                else:
                    ans.pop(str(int(d["qid"])), None)
                con.execute("UPDATE attempts SET answers=? WHERE id=?", (json.dumps(ans), aid))
            elif p == "/api/switch":
                con.execute("UPDATE attempts SET switches=switches+1 WHERE id=?", (aid,))
            else:
                finalize(con, att)
            return self.reply({"ok": True, "attempt": aid})
        self.reply({"error": "not found"}, 404)

    def admin_post(self, con, p, d):
        if p == "/api/admin/exam/add":
            title = str(d.get("title", "")).strip()
            if not title:
                return self.reply({"ok": False, "msg": "Please type an exam title."})
            con.execute("INSERT INTO exams(title,subject,minutes,pass_pct,marks_per_q) VALUES(?,?,?,?,?)",
                        (title, str(d.get("subject", "")).strip(), max(1, int(d.get("minutes") or 10)),
                         min(100, int(d.get("pass_pct") or 40)), max(1, int(d.get("marks") or 1))))
            return self.reply({"ok": True, "msg": "Exam created. Now add questions."})
        if p == "/api/admin/exam/toggle":
            con.execute("UPDATE exams SET published=1-published WHERE id=?", (int(d["id"]),))
            return self.reply({"ok": True})
        if p == "/api/admin/exam/delete":
            eid = int(d["id"])
            for t in ("questions", "attempts"):
                con.execute("DELETE FROM %s WHERE exam_id=?" % t, (eid,))
            con.execute("DELETE FROM exams WHERE id=?", (eid,))
            return self.reply({"ok": True})
        if p == "/api/admin/question/add":
            o = [str(x).strip() for x in d.get("opts", [])]
            if not str(d.get("text", "")).strip() or len(o) != 4 or not all(o):
                return self.reply({"ok": False, "msg": "Write the question and all 4 options."})
            con.execute("INSERT INTO questions(exam_id,text,a,b,c,d,correct,topic,explain) VALUES(?,?,?,?,?,?,?,?,?)",
                        (int(d["exam_id"]), str(d["text"]).strip(), o[0], o[1], o[2], o[3], int(d["correct"]),
                         str(d.get("topic", "")).strip() or "General", str(d.get("explain", "")).strip()))
            return self.reply({"ok": True, "msg": "Question added."})
        if p == "/api/admin/question/delete":
            con.execute("DELETE FROM questions WHERE id=?", (int(d["id"]),))
            return self.reply({"ok": True})
        if p == "/api/admin/password":
            if not check_pw(str(d.get("old", "")), get_setting("admin_pw")):
                return self.reply({"ok": False, "msg": "Current password is wrong."})
            if len(str(d.get("new", ""))) < 6:
                return self.reply({"ok": False, "msg": "New password needs 6+ characters."})
            set_setting("admin_pw", hash_pw(str(d["new"])))
            return self.reply({"ok": True, "msg": "Password changed."})
        self.reply({"error": "not found"}, 404)


# ---------------------------------------------------------------- web page (HTML + CSS + JS inside Python)
PAGE = r'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>PulseCBT - Smart Exams</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#060c0b;--card:#0f1c19;--line:#21392f;--lime:#a3e635;--cyan:#22d3ee;--org:#fb923c;--red:#fb7185;--ink:#f0fdf4;--mute:#8ba89f}
*{box-sizing:border-box;margin:0;font-family:'Segoe UI',system-ui,sans-serif}
body{background:var(--bg);color:var(--ink);min-height:100vh;overflow-x:hidden}
#bg{position:fixed;inset:0;z-index:0}
.wrap{position:relative;z-index:1;max-width:1150px;margin:auto;padding:18px}
#nav{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:20px}
.logo{font-size:23px;font-weight:800;flex:1;min-width:150px}.logo span{color:var(--lime)}
button{cursor:pointer;border:0;border-radius:12px;padding:10px 16px;font-size:14px;color:var(--ink);background:var(--card);border:1px solid var(--line);transition:.2s}
button:hover:not(:disabled){background:#173a30;transform:translateY(-2px)}button:disabled{opacity:.4;cursor:not-allowed}
.nav.on,.pill.on{background:var(--lime);color:#1a2e05;font-weight:700;border-color:var(--lime)}
.btn{background:linear-gradient(135deg,var(--lime),#65a30d);color:#1a2e05;font-weight:700;border:0}
.btn:hover:not(:disabled){background:var(--cyan)}.btn.sm{padding:6px 12px;font-size:12px}.btn.red{background:#9f1239;color:#fff}
.btn.ghost{background:transparent;border:1px solid var(--line);color:var(--ink);font-weight:400}
input,select,textarea{background:#08130f;border:1px solid var(--line);color:var(--ink);padding:11px 13px;border-radius:10px;font-size:14px;outline:0;width:100%}
input:focus,select:focus,textarea:focus{border-color:var(--lime);box-shadow:0 0 0 3px #a3e63526}
.card{background:rgba(15,28,25,.88);backdrop-filter:blur(8px);border:1px solid var(--line);border-radius:20px;padding:20px;margin-bottom:16px}
h1{font-size:clamp(32px,5.5vw,58px);line-height:1.08;background:linear-gradient(90deg,#fff,var(--lime),var(--cyan));-webkit-background-clip:text;background-clip:text;color:transparent}
h3{color:var(--lime);margin-bottom:12px}.sub{color:var(--mute);margin:10px 0 18px}
.chip{display:inline-block;background:#22d3ee1f;color:var(--cyan);padding:6px 14px;border-radius:99px;font-size:12px;font-weight:700;margin-bottom:12px}
.pill{border-radius:99px;padding:8px 18px}
.row{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}.row>*{flex:1;min-width:120px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:16px}
.auth{max-width:430px;margin:26px auto}.hero{text-align:center;margin-top:18px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:16px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:16px}
.stat b{font-size:32px;display:block;color:var(--lime)}.stat span{color:var(--mute);font-size:13px}
table{width:100%;border-collapse:collapse;font-size:14px}th{text-align:left;color:var(--mute);font-weight:600;padding:8px}
td{padding:9px 8px;border-top:1px solid var(--line)}.scroll{overflow-x:auto}
.tag{display:inline-block;padding:3px 11px;border-radius:99px;font-size:12px;font-weight:700}
.t-ok{background:#a3e6352b;color:var(--lime)}.t-bad{background:#fb71852b;color:var(--red)}.t-mid{background:#fb923c2b;color:var(--org)}.t-no{background:#21392f;color:var(--mute)}
.exam{transition:.25s}.exam:hover{transform:translateY(-6px);border-color:var(--lime)}
.bar{margin:11px 0}.bar p{font-size:13px;margin-bottom:4px;display:flex;justify-content:space-between;gap:8px}
.bar div{height:11px;border-radius:99px;width:0;transition:width 1s;background:linear-gradient(90deg,var(--cyan),var(--lime))}
.bar.w div{background:linear-gradient(90deg,var(--org),var(--red))}
.cols{display:flex;align-items:flex-end;gap:8px;height:160px;margin-top:8px}
.cols div{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%;font-size:11px;color:var(--mute);gap:3px}
.cols span{width:100%;border-radius:7px 7px 0 0;background:linear-gradient(var(--lime),var(--cyan));height:0;transition:height 1s;max-width:46px}
.msg{min-height:22px;margin-bottom:10px;font-weight:600;color:var(--red)}
/* exam room */
.room{display:grid;grid-template-columns:1fr 270px;gap:16px}@media(max-width:800px){.room{grid-template-columns:1fr}}
.top{display:flex;align-items:center;gap:16px;margin-bottom:16px}.top h3{flex:1;margin:0}
.qtext{font-size:21px;line-height:1.45;margin:10px 0 18px}
.opt{display:flex;gap:12px;align-items:center;width:100%;text-align:left;padding:14px 16px;margin-bottom:10px;border-radius:14px;font-size:16px}
.opt i{font-style:normal;width:30px;height:30px;border-radius:9px;display:grid;place-items:center;background:#173a30;font-weight:800;flex-shrink:0}
.opt.sel{border-color:var(--lime);background:#1a3a1c}.opt.sel i{background:var(--lime);color:#1a2e05}
.pal{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}
.pb{padding:0;height:40px;border-radius:10px;font-weight:700}.pb.ans{background:var(--lime);color:#1a2e05;border-color:var(--lime)}
.pb.flag{background:var(--org);color:#431407;border-color:var(--org)}.pb.cur{outline:2px solid var(--cyan);outline-offset:2px}
.timer{position:relative;width:70px;height:70px}.timer svg{transform:rotate(-90deg)}.timer b{position:absolute;inset:0;display:grid;place-items:center;font-size:14px}
.timer.low b{color:var(--red);animation:blink 1s infinite}@keyframes blink{50%{opacity:.4}}
.key{font-size:12px;color:var(--mute);margin-top:10px}
/* result */
.ring{position:relative;width:170px;height:170px}.ring svg{transform:rotate(-90deg)}.ring b{position:absolute;inset:0;display:grid;place-items:center;font-size:38px}
.rev{border-top:1px solid var(--line);padding:14px 0}.rev .o{padding:8px 12px;border-radius:10px;margin:5px 0;font-size:14px;border:1px solid var(--line)}
.o.right{background:#a3e6352b;border-color:var(--lime)}.o.wrong{background:#fb71852b;border-color:var(--red)}
#toast{position:fixed;bottom:24px;left:50%;transform:translate(-50%,120px);background:var(--lime);color:#1a2e05;font-weight:700;padding:12px 22px;border-radius:14px;z-index:99;transition:.4s}
#toast.show{transform:translate(-50%,0)}#toast.bad{background:var(--red);color:#4c0519}
</style></head><body>
<canvas id="bg"></canvas>
<div class="wrap"><div id="nav"></div><div id="app"></div></div>
<div id="toast"></div>

<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(p,d){const o=d===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)};const r=await fetch(p,o);return r.json()}
function toast(m,bad){const t=$('toast');t.textContent=m;t.className='show'+(bad?' bad':'');setTimeout(()=>t.className='',2800)}
const tagc=p=>p>=75?'t-ok':p>=40?'t-mid':'t-bad';

/* ---------- interactive background: glowing dot grid, mouse lens + click ripples ---------- */
const bgc=$('bg'),bx=bgc.getContext('2d');let W,H,mx=-999,my=-999,R=[];const G=34;
function rs(){W=bgc.width=innerWidth;H=bgc.height=innerHeight}addEventListener('resize',rs);rs();
addEventListener('mousemove',e=>{mx=e.clientX;my=e.clientY});
addEventListener('click',e=>R.push({x:e.clientX,y:e.clientY,t:performance.now()}));
(function loop(t){
 bx.clearRect(0,0,W,H);R=R.filter(r=>t-r.t<1600);
 for(let x=G/2;x<W;x+=G)for(let y=G/2;y<H;y+=G){
  const d=Math.hypot(x-mx,y-my),near=Math.max(0,1-d/190);
  let rip=0;for(const r of R){const age=(t-r.t)/1600,rad=age*520,dd=Math.abs(Math.hypot(x-r.x,y-r.y)-rad);if(dd<45)rip=Math.max(rip,(1-dd/45)*(1-age))}
  const wave=(Math.sin(x*.012+y*.01+t*.0012)+1)*.5*.18,s=1.3+near*3.6+rip*3.2,a=.13+wave+near*.75+rip*.8;
  const g=Math.min(1,x/W);bx.fillStyle=(near>.05||rip>.05)?`rgba(${Math.round(163-129*g)},${Math.round(230-18*g)},${Math.round(53+184*g)},${Math.min(a,1)})`:`rgba(139,200,170,${a})`;
  const ox=near*(x-mx)/(d||1)*-8,oy=near*(y-my)/(d||1)*-8;
  bx.beginPath();bx.arc(x+ox,y+oy,s,0,7);bx.fill();
 }
 requestAnimationFrame(loop);
})(0);

/* ---------- login / register ---------- */
let ME={},ST={},role='student',reg=false,tab='';
async function boot(){ST=await api('/api/status');ME=ST;ST.role?home():renderAuth()}
function setRole(r){role=r;reg=false;renderAuth()}
function renderAuth(){
 nav();const adm=role==='admin',first=adm&&!ST.has_admin;
 $('app').innerHTML=`<div class="hero"><div class="chip">COMPUTER BASED TEST</div><h1>Think fast.<br>Beat the clock.</h1>
 <p class="sub">Timed MCQ exams, instant results and a prediction of how you will do next.</p></div>
 <div class="card auth"><div class="row"><button class="pill ${!adm?'on':''}" onclick="setRole('student')">Student</button><button class="pill ${adm?'on':''}" onclick="setRole('admin')">Admin</button></div>
 <h3>${adm?(first?'Create admin password':'Admin login'):(reg?'Create student account':'Student login')}</h3>
 ${!adm&&reg?'<div class="row"><input id="f_name" placeholder="Full name"></div>':''}
 ${!adm?'<div class="row"><input id="f_roll" placeholder="Roll number"></div>':''}
 <div class="row"><input id="f_pw" type="password" placeholder="Password" onkeydown="if(event.key==='Enter')authGo()"></div>
 ${first?'<div class="row"><input id="f_pw2" type="password" placeholder="Confirm password"></div>':''}
 <div class="msg" id="amsg"></div><button class="btn" style="width:100%" onclick="authGo()">${adm?(first?'Create & enter':'Log in'):(reg?'Register':'Log in')}</button>
 ${!adm?`<p class="sub" style="text-align:center;margin:14px 0 0;cursor:pointer" onclick="reg=!reg;renderAuth()">${reg?'Already registered? Log in':'New here? Create an account'}</p>`:''}</div>`;
}
async function authGo(){
 const adm=role==='admin',pw=$('f_pw').value;let r;
 if(adm){if(!ST.has_admin&&pw!==$('f_pw2').value){$('amsg').textContent='Passwords do not match.';return}
  r=await api(ST.has_admin?'/api/admin/login':'/api/admin/setup',{password:pw})}
 else r=await api(reg?'/api/student/register':'/api/student/login',{name:reg?$('f_name').value:'',roll:$('f_roll').value,password:pw});
 if(r.ok)boot();else $('amsg').textContent=r.msg;
}
async function logout(){await api('/api/logout',{});stopRoom();tab='';boot()}

/* ---------- navigation ---------- */
const STU={exams:'Exams',prog:'My Progress'},ADM={ov:'Overview',ex:'Exams',an:'Analytics',res:'Results',set:'Settings'};
function nav(){
 const T=ME.role==='admin'?ADM:ME.role==='student'?STU:{};
 $('nav').innerHTML=`<div class="logo">Pulse<span>CBT</span></div>`+Object.entries(T).map(([k,v])=>`<button class="nav ${k===tab?'on':''}" onclick="stopRoom();go('${k}')">${v}</button>`).join('')+(ME.role?`<span class="chip" style="margin:0 0 0 6px">${esc(ME.name)}</span><button onclick="logout()">Log out</button>`:'');
}
function go(k){tab=k;nav();({exams:vExams,prog:vProg,ov:aOv,ex:aEx,an:aAn,res:aRes,set:aSet})[k]()}
function home(){go(ME.role==='admin'?'ov':'exams')}
function grow(){setTimeout(()=>{document.querySelectorAll('[data-w]').forEach(e=>e.style.width=e.dataset.w+'%');document.querySelectorAll('[data-h]').forEach(e=>e.style.height=e.dataset.h+'%')},60)}
function bars(list,warn){return list.map(t=>`<div class="bar ${warn&&t.pct<60?'w':''}"><p><span>${esc(t.label)}</span><b>${t.pct}%</b></p><div data-w="${t.pct}"></div></div>`).join('')}

/* ================= STUDENT ================= */
async function vExams(){
 const L=await api('/api/exams');
 $('app').innerHTML=`<h1 style="font-size:34px;margin-bottom:6px">Choose your exam</h1><p class="sub" style="margin-top:0">Once you start, the clock keeps running even if you close the page.</p>
 <div class="grid2">${L.map(e=>`<div class="card exam"><div class="chip">${esc(e.subject||'General')}</div><h3 style="font-size:20px">${esc(e.title)}</h3>
  <p class="sub" style="margin:0 0 14px">${e.n} questions &middot; ${e.minutes} min &middot; pass at ${e.pass_pct}%</p>
  <p style="margin-bottom:14px">${e.tries?`Attempts: <b>${e.tries}</b> &middot; Best: <span class="tag ${tagc(e.best)}">${Math.round(e.best)}%</span>`:'<span class="tag t-no">Not attempted</span>'}</p>
  <button class="btn" onclick="startExam(${e.id})">${e.running?'Resume exam':'Start exam'}</button></div>`).join('')||'<p class="sub">No exams are published yet.</p>'}</div>`;
}
let X=null;
async function startExam(id){
 const r=await api('/api/start',{exam_id:id});
 if(!r.ok){toast(r.msg,1);return}
 if(r.done)return showResult(r.done);
 X={...r,i:0,flags:{},end:Date.now()+r.remaining*1000,live:true};
 $('app').innerHTML=`<div class="room"><div class="card"><div class="top"><h3>${esc(X.title)}</h3><div class="timer" id="tm"><svg width="70" height="70"><circle cx="35" cy="35" r="30" stroke="#21392f" stroke-width="7" fill="none"/><circle id="tc" cx="35" cy="35" r="30" stroke="#a3e635" stroke-width="7" fill="none" stroke-linecap="round" stroke-dasharray="188.5" stroke-dashoffset="0"/></svg><b id="tt"></b></div></div>
  <div id="qbox"></div></div>
  <div><div class="card"><h3>Question palette</h3><div class="pal" id="pal"></div>
  <p class="key"><span class="tag t-ok">answered</span> <span class="tag t-mid">review</span></p>
  <p class="key">Keys: 1-4 pick &middot; &larr; &rarr; move &middot; F flag</p></div>
  <button class="btn" style="width:100%" onclick="submitExam(false)">Submit exam</button></div></div>`;
 renderQ();tickTimer();X.t=setInterval(tickTimer,250);
 document.addEventListener('keydown',keys);document.addEventListener('visibilitychange',away);
}
function stopRoom(){if(X){X.live=false;clearInterval(X.t);document.removeEventListener('keydown',keys);document.removeEventListener('visibilitychange',away);X=null}}
function away(){if(document.hidden&&X&&X.live){api('/api/switch',{attempt:X.attempt});toast('Tab switch recorded. Stay on the exam page.',1)}}
function keys(e){
 if(!X||!X.live||/INPUT|TEXTAREA/.test(e.target.tagName))return;
 if(['1','2','3','4'].includes(e.key))pick(+e.key-1);else if(e.key==='ArrowRight')mv(1);else if(e.key==='ArrowLeft')mv(-1);else if(e.key==='f'||e.key==='F')flag();
}
function tickTimer(){
 if(!X)return;const left=Math.max(0,Math.ceil((X.end-Date.now())/1000)),f=left/X.secs;
 $('tt').textContent=String(Math.floor(left/60)).padStart(2,'0')+':'+String(left%60).padStart(2,'0');
 $('tc').style.strokeDashoffset=188.5*(1-f);$('tc').style.stroke=f<.1?'#fb7185':f<.25?'#fb923c':'#a3e635';
 $('tm').classList.toggle('low',f<.1);
 if(left<=0){toast("Time is up! Submitting...",1);submitExam(true)}
}
function renderQ(){
 const q=X.questions[X.i],ch=X.answers[q.id];
 $('qbox').innerHTML=`<span class="chip">Question ${X.i+1} of ${X.questions.length} &middot; ${esc(q.topic)}</span><div class="qtext">${esc(q.text)}</div>
 ${q.opts.map((o,k)=>`<button class="opt ${ch===k?'sel':''}" onclick="pick(${k})"><i>${'ABCD'[k]}</i>${esc(o)}</button>`).join('')}
 <div class="row" style="margin-top:16px"><button onclick="mv(-1)" ${X.i?'':'disabled'}>&larr; Previous</button><button onclick="flag()">${X.flags[q.id]?'Remove flag':'Mark for review'}</button>
 <button onclick="clearA()">Clear answer</button><button class="btn" onclick="mv(1)" ${X.i<X.questions.length-1?'':'disabled'}>Next &rarr;</button></div>`;
 $('pal').innerHTML=X.questions.map((x,n)=>`<button class="pb ${X.flags[x.id]?'flag':X.answers[x.id]!==undefined?'ans':''} ${n===X.i?'cur':''}" onclick="X.i=${n};renderQ()">${n+1}</button>`).join('');
}
function mv(n){const j=X.i+n;if(j>=0&&j<X.questions.length){X.i=j;renderQ()}}
function pick(k){const q=X.questions[X.i];X.answers[q.id]=k;api('/api/answer',{attempt:X.attempt,qid:q.id,choice:k});renderQ()}
function clearA(){const q=X.questions[X.i];delete X.answers[q.id];api('/api/answer',{attempt:X.attempt,qid:q.id,choice:null});renderQ()}
function flag(){const q=X.questions[X.i];X.flags[q.id]=!X.flags[q.id];renderQ()}
async function submitExam(auto){
 const got=Object.keys(X.answers).length;
 if(!auto&&!confirm('You answered '+got+' of '+X.questions.length+' questions. Submit now?'))return;
 const id=X.attempt;stopRoom();await api('/api/submit',{attempt:id});showResult(id);
}
async function showResult(id){
 stopRoom();tab='';nav();const r=await api('/api/result?attempt='+id),C=2*Math.PI*72,col=r.passed?'#a3e635':'#fb7185';
 const m=Math.floor(r.secs/60),sc=r.secs%60;
 $('app').innerHTML=`<div class="card" style="text-align:center"><div class="chip">${esc(r.title)}</div><h1 style="font-size:40px;margin-bottom:16px">${r.passed?'You passed! 🎉':'Keep practising!'}</h1>
 <div style="display:flex;justify-content:center;margin-bottom:14px"><div class="ring"><svg width="170" height="170"><circle cx="85" cy="85" r="72" stroke="#21392f" stroke-width="14" fill="none"/>
 <circle id="rc" cx="85" cy="85" r="72" stroke="${col}" stroke-width="14" fill="none" stroke-linecap="round" stroke-dasharray="${C}" stroke-dashoffset="${C}" style="transition:1.4s"/></svg><b style="color:${col}">${Math.round(r.pct)}%</b></div></div>
 <div class="stats"><div class="stat"><b>${r.score}/${r.total}</b><span>Score</span></div><div class="stat"><b style="color:var(--lime)">${r.right}</b><span>Correct</span></div>
 <div class="stat"><b style="color:var(--red)">${r.wrong}</b><span>Wrong</span></div><div class="stat"><b style="color:var(--org)">${r.skipped}</b><span>Skipped</span></div>
 <div class="stat"><b>${m}:${String(sc).padStart(2,'0')}</b><span>Time taken</span></div><div class="stat"><b>${r.switches}</b><span>Tab switches</span></div></div>
 ${ME.role==='student'?'<button class="btn" onclick="go(\'prog\')">See my analysis &amp; prediction</button> ':''}<button onclick="home()">Back</button></div>
 <div class="card"><h3>Answer review</h3>${r.review.map((q,n)=>`<div class="rev"><b>${n+1}. ${esc(q.text)}</b> <span class="tag t-no">${esc(q.topic)}</span>
 ${q.opts.map((o,k)=>`<div class="o ${k===q.correct?'right':k===q.chosen?'wrong':''}">${'ABCD'[k]}. ${esc(o)}${k===q.correct?' &#10004;':k===q.chosen?' &#10008; your answer':''}</div>`).join('')}
 ${q.chosen===null?'<p class="sub" style="margin:6px 0 0">You skipped this one.</p>':''}${q.explain?`<p class="sub" style="margin:6px 0 0">&#128161; ${esc(q.explain)}</p>`:''}</div>`).join('')}</div>`;
 setTimeout(()=>{$('rc').style.strokeDashoffset=C*(1-Math.min(r.pct,100)/100)},60);
}
async function vProg(){
 const a=await api('/api/my'),p=a.predict;
 const col=p.value===null?'var(--mute)':p.value>=75?'var(--lime)':p.value>=55?'var(--cyan)':p.value>=40?'var(--org)':'var(--red)';
 $('app').innerHTML=`<div class="card"><h3>Performance prediction</h3><div style="display:flex;gap:22px;align-items:center;flex-wrap:wrap">
  <div style="font-size:64px;font-weight:800;color:${col}">${p.value===null?'?':p.value+'%'}</div><div style="flex:1;min-width:220px"><span class="tag ${p.value===null?'t-no':tagc(p.value)}">${p.label}</span>
  <p style="margin-top:10px">${esc(p.tip)}</p><p class="sub" style="margin:6px 0 0;font-size:12px">Predicted score for your next exam, from recent results and your trend.</p></div></div></div>
 <div class="grid2"><div class="card"><h3>Score history</h3>${a.history.length?`<div class="cols">${a.history.slice(-10).map(h=>`<div>${Math.round(h.pct)}<span data-h="${h.pct}"></span>${h.day}</div>`).join('')}</div>`:'<p class="sub">Take an exam to see your history.</p>'}</div>
 <div class="card"><h3>Topic strength</h3>${a.topics.length?bars(a.topics.map(t=>({label:t.topic+' ('+t.n+' Qs)',pct:t.pct})),true):'<p class="sub">No topic data yet.</p>'}<p class="sub" style="font-size:12px;margin:0">Orange bars are topics below 60%.</p></div></div>
 <div class="card"><h3>Past attempts</h3><div class="scroll"><table><tr><th>Exam</th><th>Date</th><th>Score</th><th>Result</th><th></th></tr>${a.history.slice().reverse().map(h=>`<tr><td>${esc(h.title)}</td><td>${h.day}</td><td>${h.score}/${h.total} (${Math.round(h.pct)}%)</td><td><span class="tag ${h.passed?'t-ok':'t-bad'}">${h.passed?'Passed':'Failed'}</span></td><td><button class="btn sm" onclick="showResult(${h.id})">Review</button></td></tr>`).join('')||'<tr><td colspan="5">Nothing yet.</td></tr>'}</table></div></div>`;
 grow();
}

/* ================= ADMIN ================= */
async function aOv(){
 const o=await api('/api/admin/overview'),t=(l,n)=>`<div class="stat"><b>${n}</b><span>${l}</span></div>`;
 $('app').innerHTML=`<div class="stats">${t('Students',o.students)}${t('Exams',o.exams)}${t('Questions',o.questions)}${t('Submitted attempts',o.attempts)}</div>
 <div class="card"><h3>Students</h3><div class="scroll"><table><tr><th>Name</th><th>Roll</th><th>Joined</th><th>Exams taken</th><th>Average</th></tr>
 ${o.list.map(s=>`<tr><td>${esc(s.name)}</td><td>${esc(s.roll)}</td><td>${s.created}</td><td>${s.tries}</td><td>${s.avg===null?'-':`<span class="tag ${tagc(s.avg)}">${s.avg}%</span>`}</td></tr>`).join('')||'<tr><td colspan="5">No students registered yet.</td></tr>'}</table></div></div>`;
}
async function aEx(){
 const L=await api('/api/admin/exams');
 $('app').innerHTML=`<div class="card"><h3>Create exam</h3><div class="row"><input id="et" placeholder="Exam title"><input id="es" placeholder="Subject"></div>
 <div class="row"><input id="em" type="number" placeholder="Minutes (10)"><input id="ep" type="number" placeholder="Pass % (40)"><input id="ek" type="number" placeholder="Marks per question (1)"></div>
 <button class="btn" onclick="addExam()">Create exam</button></div>
 <div class="card"><h3>All exams</h3><div class="scroll"><table><tr><th>Title</th><th>Time</th><th>Qs</th><th>Taken</th><th>Status</th><th></th></tr>
 ${L.map(e=>`<tr><td>${esc(e.title)}<br><small style="color:var(--mute)">${esc(e.subject)}</small></td><td>${e.minutes} min</td><td>${e.n}</td><td>${e.tries}</td>
 <td><span class="tag ${e.published?'t-ok':'t-no'}">${e.published?'Published':'Hidden'}</span></td>
 <td><button class="btn sm" onclick="manage(${e.id},'${esc(e.title).replace(/'/g,'')}')">Questions</button> <button class="btn sm ghost" onclick="togExam(${e.id})">${e.published?'Hide':'Publish'}</button> <button class="btn sm red" onclick="delExam(${e.id})">Delete</button></td></tr>`).join('')}</table></div></div><div id="qm"></div>`;
}
async function addExam(){const r=await api('/api/admin/exam/add',{title:$('et').value,subject:$('es').value,minutes:$('em').value,pass_pct:$('ep').value,marks:$('ek').value});toast(r.msg,!r.ok);if(r.ok)aEx()}
async function togExam(id){await api('/api/admin/exam/toggle',{id});aEx()}
async function delExam(id){if(confirm('Delete this exam, its questions and all results?')){await api('/api/admin/exam/delete',{id});aEx()}}
async function manage(id,title){
 const Q=await api('/api/admin/questions?exam='+id);
 $('qm').innerHTML=`<div class="card"><h3>Questions: ${esc(title)} (${Q.length})</h3>
 <div class="row"><textarea id="qt" rows="2" placeholder="Type the question"></textarea></div>
 <div class="grid2" style="gap:8px;margin-bottom:12px">${'ABCD'.split('').map((l,k)=>`<input id="o${k}" placeholder="Option ${l}">`).join('')}</div>
 <div class="row"><select id="qc"><option value="0">Correct: A</option><option value="1">Correct: B</option><option value="2">Correct: C</option><option value="3">Correct: D</option></select><input id="qp" placeholder="Topic (for analysis)"></div>
 <div class="row"><input id="qe" placeholder="Explanation shown after the exam (optional)"></div>
 <button class="btn" onclick="addQ(${id},'${esc(title).replace(/'/g,'')}')">Add question</button>
 <div class="scroll" style="margin-top:14px"><table><tr><th>#</th><th>Question</th><th>Answer</th><th>Topic</th><th></th></tr>${Q.map((q,n)=>`<tr><td>${n+1}</td><td>${esc(q.text)}</td><td>${'ABCD'[q.correct]}</td><td>${esc(q.topic)}</td><td><button class="btn sm red" onclick="delQ(${q.id},${id},'${esc(title).replace(/'/g,'')}')">X</button></td></tr>`).join('')}</table></div></div>`;
 $('qm').scrollIntoView({behavior:'smooth'});
}
async function addQ(id,title){const r=await api('/api/admin/question/add',{exam_id:id,text:$('qt').value,opts:[0,1,2,3].map(k=>$('o'+k).value),correct:+$('qc').value,topic:$('qp').value,explain:$('qe').value});toast(r.msg,!r.ok);if(r.ok)manage(id,title)}
async function delQ(q,id,title){await api('/api/admin/question/delete',{id:q});manage(id,title)}
async function aAn(){
 const L=await api('/api/admin/exams');
 $('app').innerHTML=`<div class="card"><h3>Exam analytics</h3><div class="row"><select id="ae" onchange="showAn()">${L.map(e=>`<option value="${e.id}">${esc(e.title)}</option>`).join('')}</select></div></div><div id="anb"></div>`;
 if(L.length)showAn();
}
async function showAn(){
 const a=await api('/api/admin/analytics?exam='+$('ae').value),mx=Math.max(1,...a.buckets),t=(l,n)=>`<div class="stat"><b>${n}</b><span>${l}</span></div>`;
 $('anb').innerHTML=a.count?`<div class="stats">${t('Attempts',a.count)}${t('Average score',a.avg+'%')}${t('Pass rate',a.pass_rate+'%')}${t('Best score',a.best+'%')}</div>
 <div class="grid2"><div class="card"><h3>Score distribution</h3><div class="cols">${a.buckets.map((c,i)=>`<div>${c}<span data-h="${c/mx*100}"></span>${['0-39','40-59','60-79','80+'][i]}</div>`).join('')}</div></div>
 <div class="card"><h3>Hardest questions</h3>${bars(a.hardest.map(h=>({label:h.text.slice(0,48),pct:h.pct})),true)}<p class="sub" style="font-size:12px;margin:0">% of students who answered correctly.</p></div></div>
 <div class="card"><h3>Leaderboard</h3>${a.top.map((s,i)=>`<p style="font-size:17px;margin:10px 0">${i+1}. <b>${esc(s.name)}</b> <span class="tag ${tagc(s.pct)}">${s.pct}%</span></p>`).join('')}</div>`:'<div class="card"><p class="sub">No one has completed this exam yet.</p></div>';
 grow();
}
async function aRes(){
 const R=await api('/api/admin/results');
 $('app').innerHTML=`<div class="card"><h3>All results</h3><div class="scroll"><table><tr><th>Student</th><th>Roll</th><th>Exam</th><th>Score</th><th>Tab switches</th><th>Submitted</th><th></th></tr>
 ${R.map(r=>{const p=r.total?Math.round(r.score*100/r.total):0;return `<tr><td>${esc(r.name)}</td><td>${esc(r.roll)}</td><td>${esc(r.title)}</td><td><span class="tag ${tagc(p)}">${r.score}/${r.total} (${p}%)</span></td><td>${r.switches?`<span class="tag t-mid">${r.switches}</span>`:0}</td><td>${new Date(r.finished*1000).toLocaleString()}</td><td><button class="btn sm" onclick="showResult(${r.id})">View</button></td></tr>`}).join('')||'<tr><td colspan="7">No results yet.</td></tr>'}</table></div></div>`;
}
function aSet(){
 $('app').innerHTML=`<div class="card" style="max-width:460px"><h3>Change admin password</h3><div class="row"><input id="op" type="password" placeholder="Current password"></div>
 <div class="row"><input id="np" type="password" placeholder="New password (6+ characters)"></div><button class="btn" onclick="chPw()">Update password</button></div>`;
}
async function chPw(){const r=await api('/api/admin/password',{old:$('op').value,new:$('np').value});toast(r.msg,!r.ok)}
boot();
</script></body></html>
'''

# ---------------------------------------------------------------- start
if __name__ == "__main__":
    init_db()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = "http://localhost:%d" % PORT
    print("\n  PulseCBT is running ->  %s\n  Press Ctrl + C to stop.\n" % url)
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")