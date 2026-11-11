import json
import os
import random
import sqlite3
import threading
import webbrowser
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 3000
LOAN_DAYS = 14        
FINE_PER_DAY = 2      
MAX_LOANS = 5        
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "library.db")


# ---------------------------------------------------------------- database
def db():
    con = sqlite3.connect(DB_FILE, timeout=10)
    con.row_factory = sqlite3.Row
    return con


BOOKS = [
    ("The Midnight Library", "Matt Haig", "Fiction", 2020, 2, "second chances, parallel lives, hopeful"),
    ("Pachinko", "Min Jin Lee", "Fiction", 2017, 2, "family saga, migration, resilience"),
    ("The Kite Runner", "Khaled Hosseini", "Fiction", 2003, 3, "friendship, guilt, redemption"),
    ("Life of Pi", "Yann Martel", "Fiction", 2001, 2, "survival, ocean, faith"),
    ("Cosmos", "Carl Sagan", "Science", 1980, 2, "space, wonder, universe"),
    ("A Brief History of Time", "Stephen Hawking", "Science", 1988, 3, "black holes, physics, time"),
    ("The Selfish Gene", "Richard Dawkins", "Science", 1976, 2, "evolution, genes, biology"),
    ("Sapiens", "Yuval Noah Harari", "History", 2011, 3, "humankind, civilisation, big picture"),
    ("The Discovery of India", "Jawaharlal Nehru", "History", 1946, 2, "india, heritage, culture"),
    ("Guns, Germs, and Steel", "Jared Diamond", "History", 1997, 2, "societies, geography, empires"),
    ("Clean Code", "Robert C. Martin", "Technology", 2008, 3, "programming, craftsmanship, software"),
    ("Automate the Boring Stuff with Python", "Al Sweigart", "Technology", 2015, 4, "python, beginners, scripting"),
    ("The Pragmatic Programmer", "Andrew Hunt", "Technology", 1999, 2, "programming, career, habits"),
    ("Deep Learning with Python", "Francois Chollet", "Technology", 2017, 2, "ai, python, neural networks"),
    ("The Hobbit", "J.R.R. Tolkien", "Fantasy", 1937, 3, "adventure, dragons, quest"),
    ("Harry Potter and the Philosopher's Stone", "J.K. Rowling", "Fantasy", 1997, 4, "magic, school, friendship"),
    ("A Wizard of Earthsea", "Ursula K. Le Guin", "Fantasy", 1968, 2, "magic, islands, coming of age"),
    ("The Girl with the Dragon Tattoo", "Stieg Larsson", "Mystery", 2005, 2, "crime, thriller, journalist"),
    ("And Then There Were None", "Agatha Christie", "Mystery", 1939, 3, "island, whodunit, classic"),
    ("Gone Girl", "Gillian Flynn", "Mystery", 2012, 2, "twist, marriage, thriller"),
    ("Atomic Habits", "James Clear", "Self-Help", 2018, 4, "habits, routine, productivity"),
    ("Deep Work", "Cal Newport", "Self-Help", 2016, 2, "focus, productivity, career"),
    ("Ikigai", "Hector Garcia", "Self-Help", 2016, 2, "purpose, long life, calm"),
    ("Wings of Fire", "A.P.J. Abdul Kalam", "Biography", 1999, 3, "india, science, inspiration"),
    ("Long Walk to Freedom", "Nelson Mandela", "Biography", 1994, 2, "freedom, courage, leadership"),
    ("Steve Jobs", "Walter Isaacson", "Biography", 2011, 2, "innovation, business, design"),
]
MEMBERS = [
    ("LM001", "Aarav Sharma", "Student", ["Technology", "Science"]),
    ("LM002", "Diya Verma", "Student", ["Fantasy", "Fiction"]),
    ("LM003", "Kabir Singh", "Teacher", ["History", "Biography"]),
    ("LM004", "Meera Nair", "Public", ["Mystery", "Fiction"]),
    ("LM005", "Rohit Gupta", "Student", ["Self-Help", "Technology"]),
    ("LM006", "Sana Khan", "Public", ["Science", "Fantasy"]),
]


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS books(
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, author TEXT NOT NULL,
            genre TEXT NOT NULL, year INTEGER DEFAULT 0, copies INTEGER DEFAULT 1, tags TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS members(
            id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
            kind TEXT DEFAULT 'Student', joined TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS loans(
            id INTEGER PRIMARY KEY AUTOINCREMENT, book_id INTEGER NOT NULL, member_id INTEGER NOT NULL,
            issued TEXT NOT NULL, due TEXT NOT NULL, returned TEXT);
        """)
        if con.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0:
            seed(con)


def seed(con):
    """Fill a fresh library with sample books, members and a month of borrowing history."""
    rnd = random.Random(7)
    today = date.today()
    con.executemany("INSERT INTO books(title,author,genre,year,copies,tags) VALUES(?,?,?,?,?,?)", BOOKS)
    for code, name, kind, _ in MEMBERS:
        con.execute("INSERT INTO members(code,name,kind,joined) VALUES(?,?,?,?)",
                    (code, name, kind, (today - timedelta(days=rnd.randint(60, 400))).isoformat()))
    books = con.execute("SELECT id,genre,copies FROM books").fetchall()
    active = {}
    for mid, (_, _, _, likes) in enumerate(MEMBERS, start=1):
        taken = set()
        for _ in range(rnd.randint(6, 9)):
            pool = [b for b in books if b["genre"] in likes] if rnd.random() < 0.8 else books
            b = rnd.choice(pool)
            if b["id"] in taken:
                continue
            taken.add(b["id"])
            issued = today - timedelta(days=rnd.randint(0, 35))
            due = issued + timedelta(days=LOAN_DAYS)
            returned = None
            if (today - issued).days > 9 or active.get(b["id"], 0) >= b["copies"]:
                returned = (issued + timedelta(days=rnd.randint(3, 17))).isoformat()
                if returned > today.isoformat():
                    returned = today.isoformat()
            else:
                active[b["id"]] = active.get(b["id"], 0) + 1
            con.execute("INSERT INTO loans(book_id,member_id,issued,due,returned) VALUES(?,?,?,?,?)",
                        (b["id"], mid, issued.isoformat(), due.isoformat(), returned))


BOOK_SQL = ("SELECT b.*, b.copies - (SELECT COUNT(*) FROM loans l WHERE l.book_id=b.id AND l.returned IS NULL) AS avail,"
            " (SELECT COUNT(*) FROM loans l WHERE l.book_id=b.id) AS times FROM books b")


def rows(con, sql, args=()):
    return [dict(r) for r in con.execute(sql, args).fetchall()]



def search_books(q, genre):
    with db() as con:
        sql, args, where = BOOK_SQL, [], []
        words = [w for w in q.lower().split() if w]
        for w in words:   
            where.append("(LOWER(b.title||' '||b.author||' '||b.genre||' '||b.tags) LIKE ?)")
            args.append("%" + w + "%")
        if genre:
            where.append("b.genre=?")
            args.append(genre)
        if where:
            sql += " WHERE " + " AND ".join(where)
        return rows(con, sql + " ORDER BY times DESC, b.title", args)


def recommend(member_id):
    """Score = taste (genres this member borrows) + similar readers + popularity + availability."""
    with db() as con:
        mine = {r[0] for r in con.execute("SELECT DISTINCT book_id FROM loans WHERE member_id=?", (member_id,))}
        genres = {r["genre"]: r["c"] for r in con.execute(
            "SELECT b.genre, COUNT(*) c FROM loans l JOIN books b ON b.id=l.book_id WHERE l.member_id=? GROUP BY b.genre",
            (member_id,))}
        best = max(genres.values()) if genres else 0
        crowd = {}
        if mine:
            ph = ",".join("?" * len(mine))
            for r in con.execute(
                    f"SELECT book_id, COUNT(DISTINCT member_id) c FROM loans WHERE member_id IN "
                    f"(SELECT DISTINCT member_id FROM loans WHERE book_id IN ({ph}) AND member_id!=?) GROUP BY book_id",
                    list(mine) + [member_id]):
                crowd[r["book_id"]] = r["c"]
        picks = []
        for b in rows(con, BOOK_SQL):
            if b["id"] in mine:
                continue
            taste = 50 * genres.get(b["genre"], 0) / best if best else 0
            score = taste + 12 * crowd.get(b["id"], 0) + 2 * b["times"] + (5 if b["avail"] > 0 else -20)
            why = []
            if genres.get(b["genre"]):
                why.append("You love " + b["genre"])
            if crowd.get(b["id"]):
                why.append(str(crowd[b["id"]]) + " readers with your taste borrowed it")
            b["score"] = score
            b["why"] = " + ".join(why) or "Popular with the community"
            picks.append(b)
        picks.sort(key=lambda x: -x["score"])
        top = picks[:6]
        peak = max([p["score"] for p in top] + [1])
        for p in top:
            p["match"] = max(40, min(99, round(p["score"] * 100 / peak)))
        return top


def similar(book_id):
    with db() as con:
        sql = (BOOK_SQL + " WHERE b.id!=? AND b.id IN (SELECT book_id FROM loans WHERE member_id IN "
               "(SELECT member_id FROM loans WHERE book_id=?)) ORDER BY times DESC LIMIT 4")
        out = rows(con, sql, (book_id, book_id))
        if len(out) < 3:
            g = con.execute("SELECT genre FROM books WHERE id=?", (book_id,)).fetchone()
            if g:
                seen = [o["id"] for o in out] + [book_id]
                out += rows(con, BOOK_SQL + " WHERE b.genre=? AND b.id NOT IN (%s) LIMIT 4" % ",".join("?" * len(seen)),
                            [g[0]] + seen)
        return out[:4]


def stats():
    today = date.today()
    with db() as con:
        one = lambda s, a=(): con.execute(s, a).fetchone()[0]
        daily = {r["issued"]: r["c"] for r in con.execute("SELECT issued, COUNT(*) c FROM loans GROUP BY issued")}
        days = [(today - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
        return {
            "titles": one("SELECT COUNT(*) FROM books"), "copies": one("SELECT SUM(copies) FROM books"),
            "members": one("SELECT COUNT(*) FROM members"), "total": one("SELECT COUNT(*) FROM loans"),
            "active": one("SELECT COUNT(*) FROM loans WHERE returned IS NULL"),
            "overdue": one("SELECT COUNT(*) FROM loans WHERE returned IS NULL AND due<?", (today.isoformat(),)),
            "top": rows(con, "SELECT b.title, b.genre, COUNT(*) c FROM loans l JOIN books b ON b.id=l.book_id "
                             "GROUP BY b.id ORDER BY c DESC, b.title LIMIT 6"),
            "genres": rows(con, "SELECT b.genre, COUNT(*) c FROM loans l JOIN books b ON b.id=l.book_id "
                                "GROUP BY b.genre ORDER BY c DESC"),
            "readers": rows(con, "SELECT m.name, COUNT(*) c FROM loans l JOIN members m ON m.id=l.member_id "
                                 "GROUP BY m.id ORDER BY c DESC LIMIT 3"),
            "daily": [{"day": d[5:], "c": daily.get(d, 0)} for d in days],
        }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, data, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def reply(self, obj, code=200):
        self.send(code, json.dumps(obj).encode())

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        p = url.path
        if p == "/":
            return self.send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if p == "/api/books":
            return self.reply(search_books(q.get("q", ""), q.get("genre", "")))
        if p == "/api/members":
            with db() as con:
                return self.reply(rows(con, (
                    "SELECT m.*, (SELECT COUNT(*) FROM loans l WHERE l.member_id=m.id) total,"
                    " (SELECT COUNT(*) FROM loans l WHERE l.member_id=m.id AND l.returned IS NULL) active"
                    " FROM members m ORDER BY m.code")))
        if p == "/api/loans":
            with db() as con:
                return self.reply(rows(con, (
                    "SELECT l.id, l.issued, l.due, b.title, m.name, m.code FROM loans l JOIN books b ON b.id=l.book_id "
                    "JOIN members m ON m.id=l.member_id WHERE l.returned IS NULL ORDER BY l.due")))
        if p == "/api/stats":
            return self.reply(stats())
        if p == "/api/recommend":
            return self.reply(recommend(int(q.get("member", 0))))
        if p == "/api/similar":
            return self.reply(similar(int(q.get("book", 0))))
        self.reply({"error": "not found"}, 404)

    def do_POST(self):
        try:
            d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except Exception:
            return self.reply({"error": "bad request"}, 400)
        p = urlparse(self.path).path
        today = date.today()
        with db() as con:
            if p == "/api/books/add":
                title, author = str(d.get("title", "")).strip(), str(d.get("author", "")).strip()
                if not title or not author:
                    return self.reply({"ok": False, "msg": "Title and author are needed."})
                con.execute("INSERT INTO books(title,author,genre,year,copies,tags) VALUES(?,?,?,?,?,?)",
                            (title, author, str(d.get("genre") or "Fiction"), int(d.get("year") or 0),
                             max(1, int(d.get("copies") or 1)), str(d.get("tags", "")).strip()))
                return self.reply({"ok": True, "msg": "Book added to the shelf."})
            if p == "/api/members/add":
                name = str(d.get("name", "")).strip()
                if not name:
                    return self.reply({"ok": False, "msg": "Please type the member name."})
                n = con.execute("SELECT COUNT(*) FROM members").fetchone()[0] + 1
                con.execute("INSERT INTO members(code,name,kind,joined) VALUES(?,?,?,?)",
                            ("LM%03d" % n, name, str(d.get("kind") or "Student"), today.isoformat()))
                return self.reply({"ok": True, "msg": "Welcome, " + name + "! Card LM%03d created." % n})
            if p == "/api/issue":
                bid, mid = int(d["book_id"]), int(d["member_id"])
                book = con.execute(BOOK_SQL + " WHERE b.id=?", (bid,)).fetchone()
                if not book or book["avail"] < 1:
                    return self.reply({"ok": False, "msg": "No copy is free right now."})
                if con.execute("SELECT COUNT(*) FROM loans WHERE member_id=? AND returned IS NULL", (mid,)).fetchone()[0] >= MAX_LOANS:
                    return self.reply({"ok": False, "msg": "This member already holds %d books." % MAX_LOANS})
                if con.execute("SELECT 1 FROM loans WHERE member_id=? AND book_id=? AND returned IS NULL", (mid, bid)).fetchone():
                    return self.reply({"ok": False, "msg": "Member already has this book."})
                due = today + timedelta(days=LOAN_DAYS)
                con.execute("INSERT INTO loans(book_id,member_id,issued,due) VALUES(?,?,?,?)",
                            (bid, mid, today.isoformat(), due.isoformat()))
                return self.reply({"ok": True, "msg": "Issued! Due back on " + due.strftime("%d %b") + "."})
            if p == "/api/return":
                loan = con.execute("SELECT * FROM loans WHERE id=? AND returned IS NULL", (int(d["id"]),)).fetchone()
                if not loan:
                    return self.reply({"ok": False, "msg": "Loan not found."})
                late = max(0, (today - date.fromisoformat(loan["due"])).days)
                con.execute("UPDATE loans SET returned=? WHERE id=?", (today.isoformat(), loan["id"]))
                msg = "Returned. Thank you!" if not late else "Returned %d day(s) late. Fine: Rs %d." % (late, late * FINE_PER_DAY)
                return self.reply({"ok": True, "msg": msg})
        self.reply({"error": "not found"}, 404)


PAGE = r'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>ShelfSense - Smart Library</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:
*{box-sizing:border-box;margin:0;font-family:'Segoe UI',system-ui,sans-serif}
body{background:radial-gradient(circle at 85% -10%,
.wrap{position:relative;z-index:1;max-width:1180px;margin:auto;padding:18px}
nav{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:22px}
.logo{font-size:23px;font-weight:800;flex:1;min-width:170px}.logo span{color:var(--amber)}
button{cursor:pointer;border:0;border-radius:12px;padding:10px 16px;font-size:14px;color:var(--ink);background:var(--card);transition:.2s}
button:hover{background:
.nav.on{background:var(--teal);color:
.btn{background:linear-gradient(135deg,var(--teal),
.btn.sm{padding:6px 12px;font-size:12px}.btn.red{background:
input,select{background:
input:focus,select:focus{border-color:var(--teal);box-shadow:0 0 0 3px 
.card{background:rgba(13,42,48,.85);backdrop-filter:blur(8px);border:1px solid var(--line);border-radius:20px;padding:20px;margin-bottom:16px}
h1{font-size:clamp(30px,5vw,52px);line-height:1.1;background:linear-gradient(90deg,
h3{color:var(--teal);margin-bottom:12px}.sub{color:var(--mute);margin:10px 0 18px}
.chip{display:inline-block;background:
.pill{border:1px solid var(--line);background:transparent;padding:7px 14px;border-radius:99px;font-size:13px}.pill.on{background:var(--amber);color:#422006;border-color:var(--amber);font-weight:700}
.row{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}.row>*{flex:1;min-width:130px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:18px;margin-top:14px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
.book{cursor:pointer;transition:.25s;position:relative}.book:hover{transform:translateY(-8px) rotate(-1deg)}
.cover{height:225px;border-radius:6px 14px 14px 6px;padding:16px 14px 14px 22px;display:flex;flex-direction:column;justify-content:space-between;
 box-shadow:-6px 8px 22px
.cover:after{content:'';position:absolute;inset:0;background:linear-gradient(100deg,
.cover small{font-size:10px;letter-spacing:1.5px;text-transform:uppercase;opacity:.85;font-weight:700}
.cover b{font-size:17px;line-height:1.2}.cover i{font-size:12px;opacity:.85}
.avail{position:absolute;top:-8px;right:-6px;z-index:2;padding:4px 10px;border-radius:99px;font-size:11px;font-weight:800;box-shadow:0 4px 10px 
.ok{background:var(--teal);color:
.match{position:absolute;top:-8px;left:-6px;z-index:2;background:var(--amber);color:
.why{font-size:12px;color:var(--mute);margin-top:8px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:16px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:16px}
.stat b{font-size:32px;display:block;color:var(--amber)}.stat span{color:var(--mute);font-size:13px}
table{width:100%;border-collapse:collapse;font-size:14px}th{text-align:left;color:var(--mute);font-weight:600;padding:8px}
td{padding:9px 8px;border-top:1px solid var(--line)}.scroll{overflow-x:auto}
.tag{display:inline-block;padding:3px 11px;border-radius:99px;font-size:12px;font-weight:700}
.t-ok{background:
.bar{margin:10px 0}.bar div{height:12px;border-radius:99px;background:linear-gradient(90deg,var(--teal),var(--amber));width:0;transition:width 1s}
.bar p{font-size:13px;margin-bottom:4px;display:flex;justify-content:space-between;gap:8px}
.cols{display:flex;align-items:flex-end;gap:6px;height:150px;margin-top:10px}
.cols div{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%;font-size:10px;color:var(--mute)}
.cols span{width:100%;border-radius:6px 6px 0 0;background:linear-gradient(var(--amber),var(--teal));height:0;transition:height 1s}
.donutwrap{display:flex;align-items:center;gap:22px;flex-wrap:wrap}
.donut{width:150px;height:150px;border-radius:50%;position:relative}.donut:after{content:'';position:absolute;inset:30px;background:var(--card);border-radius:50%}
.legend div{margin:5px 0;font-size:14px}.dot{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:8px}


@keyframes pop{from{transform:scale(.8);opacity:0}to{transform:scale(1);opacity:1}}


.big{font-size:17px;padding:15px 18px}
</style></head><body>
<canvas id="bg"></canvas>
<div class="wrap">
 <nav><div class="logo">Shelf<span>Sense</span></div><div id="nav" class="row" style="margin:0;flex:0"></div></nav>
 <div id="view"></div>
</div>
<div id="modal"><div class="card" id="mbody"></div></div>
<div id="toast"></div>

<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(p,d){const o=d===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)};return (await fetch(p,o)).json()}
function toast(m){const t=$('toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2800)}
const GENRES=['Fiction','Science','History','Technology','Fantasy','Mystery','Self-Help','Biography'];
const COL={Fiction:['#14b8a6','#0f4c4a'],Science:['#f59e0b','#7c3a0a'],History:['#f0704f','#7a2a12'],Technology:['#22c55e','#14532d'],
 Fantasy:['#e11d48','#6b0f26'],Mystery:['#64a0b0','#16323c'],'Self-Help':['#eab308','#6b4a07'],Biography:['#06b6d4','#134e5c']};
const gc=g=>COL[g]||['#14b8a6','#0f4c4a'];

/* ---------- animated background: drifting book spines that dodge the mouse ---------- */
const bgc=$('bg'),bx=bgc.getContext('2d');let W,H,S=[],mx=-999,my=-999;
function rs(){W=bgc.width=innerWidth;H=bgc.height=innerHeight}addEventListener('resize',rs);rs();
const SC=['45,212,191','251,191,36','251,113,133'];
for(let i=0;i<46;i++)S.push({x:Math.random()*W,y:Math.random()*H,w:6+Math.random()*10,h:30+Math.random()*50,v:.15+Math.random()*.45,c:SC[i%3],r:(Math.random()-.5)*.5});
addEventListener('mousemove',e=>{mx=e.clientX;my=e.clientY});
(function loop(){
 bx.clearRect(0,0,W,H);
 for(const s of S){
  s.y-=s.v;if(s.y<-90){s.y=H+60;s.x=Math.random()*W}
  const dx=s.x-mx,dy=s.y-my,d=Math.hypot(dx,dy)||1;if(d<170){s.x+=dx/d*3;s.y+=dy/d*1.5}
  bx.save();bx.translate(s.x,s.y);bx.rotate(s.r+(d<170?dx/900:0));
  bx.fillStyle='rgba('+s.c+',.16)';bx.fillRect(0,0,s.w,s.h);bx.fillStyle='rgba('+s.c+',.4)';bx.fillRect(0,4,s.w,3);bx.restore();
 }
 requestAnimationFrame(loop);
})();

/* ---------- navigation ---------- */
const TABS={lib:'Library',mem:'Members',desk:'Desk',stat:'Statistics',rec:'For You'};
let tab='lib',MEM=[];
function go(k){tab=k;$('nav').innerHTML=Object.entries(TABS).map(([a,b])=>`<button class="nav ${a===k?'on':''}" onclick="go('${a}')">${b}</button>`).join('');
 ({lib:vLib,mem:vMem,desk:vDesk,stat:vStat,rec:vRec})[k]()}
function cover(b){const [a,c]=gc(b.genre);return `<div class="cover" style="background:linear-gradient(145deg,${a},${c})"><small>${esc(b.genre)}</small><b>${esc(b.title)}</b><i>${esc(b.author)}</i></div>`}
function card(b,extra=''){return `<div class="book" onclick="openBook(${b.id})">${extra}<span class="avail ${b.avail>0?'ok':'no'}">${b.avail>0?b.avail+' free':'all out'}</span>${cover(b)}</div>`}

/* ---------- 1. library interface + 5. search ---------- */
let genre='',tm;
function vLib(){
 const h=new Date().getHours();
 $('view').innerHTML=`<div style="margin:6px 0 22px"><div class="chip">OPEN TODAY &middot; <span id="clk"></span></div>
  <h1>${h<12?'Good morning':h<17?'Good afternoon':'Good evening'}, reader.<br>What shall we explore?</h1>
  <p class="sub">Search by title, author, topic or mood words like "habits", "space" or "twist".</p>
  <div class="row"><input id="q" class="big" placeholder="Search the whole library..." oninput="clearTimeout(tm);tm=setTimeout(load,200)">
  <button class="btn" style="flex:0" onclick="surprise()">Surprise me</button></div>
  <div class="row" style="gap:6px"><button class="pill on" onclick="pick('',this)">All</button>${GENRES.map(g=>`<button class="pill" onclick="pick('${g}',this)">${g}</button>`).join('')}</div>
  <p class="sub" id="cnt" style="margin:0"></p><div class="grid" id="grid"></div></div>`;
 genre='';clock();load();
}
function clock(){const e=$('clk');if(!e)return;e.textContent=new Date().toLocaleTimeString()+' | '+new Date().toLocaleDateString(undefined,{weekday:'long',day:'numeric',month:'short'});setTimeout(clock,1000)}
function pick(g,el){genre=g;document.querySelectorAll('.pill').forEach(p=>p.classList.remove('on'));el.classList.add('on');load()}
async function load(){
 const r=await api('/api/books?q='+encodeURIComponent($('q').value)+'&genre='+encodeURIComponent(genre));
 $('cnt').textContent=r.length+' book'+(r.length==1?'':'s')+' found';
 $('grid').innerHTML=r.length?r.map(b=>card(b)).join(''):'<p class="sub">Nothing matches. Try another word.</p>';
}
async function surprise(){const r=(await api('/api/books?q=&genre=')).filter(b=>b.avail>0);openBook(r[Math.floor(Math.random()*r.length)].id)}
async function openBook(id){
 const [all,sim]=[await api('/api/books'),await api('/api/similar?book='+id)];MEM=await api('/api/members');
 const b=all.find(x=>x.id===id);
 $('mbody').innerHTML=`<div style="display:flex;gap:18px;flex-wrap:wrap"><div style="width:170px">${cover(b)}</div>
  <div style="flex:1;min-width:210px"><h3 style="margin-bottom:4px">${esc(b.title)}</h3><p>${esc(b.author)} &middot; ${b.year||'n/a'}</p>
  <p class="sub" style="margin:10px 0">${b.tags.split(',').map(t=>`<span class="tag t-warn" style="margin:2px">${esc(t.trim())}</span>`).join('')}</p>
  <p>Borrowed <b style="color:var(--amber)">${b.times}</b> times &middot; <b style="color:${b.avail>0?'var(--teal)':'var(--coral)'}">${b.avail} of ${b.copies}</b> copies free</p>
  <div class="row" style="margin-top:14px"><select id="who">${MEM.map(m=>`<option value="${m.id}">${esc(m.name)} (${m.code})</option>`).join('')}</select>
  <button class="btn" ${b.avail>0?'':'disabled'} onclick="issue(${b.id},$('who').value,true)">Borrow</button></div></div></div>
  ${sim.length?`<h3 style="margin-top:18px">Readers also borrowed</h3><div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(120px,1fr))">${sim.map(s=>`<div class="book" onclick="openBook(${s.id})"><div class="cover" style="height:150px;padding:10px 8px 8px 16px;background:linear-gradient(145deg,${gc(s.genre)[0]},${gc(s.genre)[1]})"><small>${esc(s.genre)}</small><b style="font-size:13px">${esc(s.title)}</b></div></div>`).join('')}</div>`:''}
  <button style="margin-top:16px" onclick="$('modal').classList.remove('open')">Close</button>`;
 $('modal').classList.add('open');
}
$('modal').addEventListener('click',e=>{if(e.target.id==='modal')$('modal').classList.remove('open')});
async function issue(b,m,fromModal){
 const r=await api('/api/issue',{book_id:b,member_id:m});toast(r.msg);
 if(r.ok){if(fromModal)$('modal').classList.remove('open');go(tab)}
}

/* ---------- 2. book + member data ---------- */
async function vMem(){
 const m=await api('/api/members');
 $('view').innerHTML=`<div class="grid2"><div class="card"><h3>Add a member</h3>
   <div class="row"><input id="mn" placeholder="Full name"><select id="mk"><option>Student</option><option>Teacher</option><option>Public</option></select></div>
   <button class="btn" onclick="addMem()">Create library card</button></div>
  <div class="card"><h3>Add a book</h3>
   <div class="row"><input id="bt" placeholder="Title"><input id="ba" placeholder="Author"></div>
   <div class="row"><select id="bg">${GENRES.map(g=>`<option>${g}</option>`).join('')}</select><input id="by" type="number" placeholder="Year"><input id="bc" type="number" placeholder="Copies"></div>
   <div class="row"><input id="bk" placeholder="Keywords, comma separated"></div><button class="btn" onclick="addBook()">Put on shelf</button></div></div>
  <div class="card"><h3>Members (${m.length})</h3><div class="scroll"><table><tr><th>Card</th><th>Name</th><th>Type</th><th>Joined</th><th>Holding</th><th>Total borrowed</th></tr>
  ${m.map(x=>`<tr><td>${x.code}</td><td>${esc(x.name)}</td><td>${x.kind}</td><td>${x.joined}</td><td><span class="tag ${x.active?'t-warn':'t-ok'}">${x.active} now</span></td><td>${x.total}</td></tr>`).join('')}</table></div></div>`;
}
async function addMem(){const r=await api('/api/members/add',{name:$('mn').value,kind:$('mk').value});toast(r.msg);if(r.ok)vMem()}
async function addBook(){const r=await api('/api/books/add',{title:$('bt').value,author:$('ba').value,genre:$('bg').value,year:$('by').value,copies:$('bc').value,tags:$('bk').value});toast(r.msg);if(r.ok)vMem()}

/* ---------- desk: issue + return ---------- */
async function vDesk(){
 const [m,b,l]=await Promise.all([api('/api/members'),api('/api/books'),api('/api/loans')]);
 const today=new Date().toISOString().slice(0,10);
 $('view').innerHTML=`<div class="card"><h3>Issue a book</h3><div class="row">
  <select id="dm">${m.map(x=>`<option value="${x.id}">${esc(x.name)} (${x.code})</option>`).join('')}</select>
  <select id="db">${b.filter(x=>x.avail>0).map(x=>`<option value="${x.id}">${esc(x.title)}</option>`).join('')}</select>
  <button class="btn" style="flex:0" onclick="issue($('db').value,$('dm').value,false)">Issue</button></div></div>
  <div class="card"><h3>Books out right now (${l.length})</h3><div class="scroll"><table><tr><th>Book</th><th>Member</th><th>Issued</th><th>Due</th><th>Status</th><th></th></tr>
  ${l.map(x=>`<tr><td>${esc(x.title)}</td><td>${esc(x.name)}</td><td>${x.issued}</td><td>${x.due}</td>
  <td><span class="tag ${x.due<today?'t-late':'t-ok'}">${x.due<today?'Overdue':'On time'}</span></td>
  <td><button class="btn sm" onclick="giveBack(${x.id})">Return</button></td></tr>`).join('')||'<tr><td colspan="6">Every book is on the shelf.</td></tr>'}</table></div></div>`;
}
async function giveBack(id){const r=await api('/api/return',{id});toast(r.msg);vDesk()}

/* ---------- 3. borrowing statistics ---------- */
async function vStat(){
 const s=await api('/api/stats'),tot=s.genres.reduce((a,g)=>a+g.c,0)||1,mx=Math.max(1,...s.daily.map(d=>d.c)),tp=Math.max(1,...s.top.map(t=>t.c));
 const tile=(l,n,c)=>`<div class="stat"><b style="${c?'color:'+c:''}" data-n="${n}">0</b><span>${l}</span></div>`;
 let acc=0;const parts=s.genres.map(g=>{const a=acc;acc+=g.c/tot*100;return gc(g.genre)[0]+' '+a+'% '+acc+'%'});
 $('view').innerHTML=`<div class="stats">${tile('Book titles',s.titles)}${tile('Total copies',s.copies)}${tile('Members',s.members)}${tile('All-time loans',s.total)}${tile('Out right now',s.active)}${tile('Overdue',s.overdue,'var(--coral)')}</div>
 <div class="grid2"><div class="card"><h3>Most borrowed books</h3>${s.top.map(t=>`<div class="bar"><p><span>${esc(t.title)}</span><b>${t.c}</b></p><div data-w="${t.c/tp*100}"></div></div>`).join('')}</div>
 <div class="card"><h3>Loans by genre</h3><div class="donutwrap"><div class="donut" style="background:conic-gradient(${parts.join(',')})"></div>
  <div class="legend">${s.genres.map(g=>`<div><span class="dot" style="background:${gc(g.genre)[0]}"></span>${g.genre}: <b>${g.c}</b></div>`).join('')}</div></div></div></div>
 <div class="grid2"><div class="card"><h3>Loans per day (last 14 days)</h3><div class="cols">${s.daily.map(d=>`<div>${d.c}<span data-h="${d.c/mx*100}"></span>${d.day.slice(3)}</div>`).join('')}</div></div>
 <div class="card"><h3>Top readers</h3>${s.readers.map((r,i)=>`<p style="font-size:18px;margin:12px 0">${['1st','2nd','3rd'][i]} &nbsp;<b>${esc(r.name)}</b> <span class="tag t-warn">${r.c} books</span></p>`).join('')}</div></div>`;
 document.querySelectorAll('[data-n]').forEach(el=>{const end=+el.dataset.n,t0=performance.now();(function f(t){const k=Math.min(1,(t-t0)/900);el.textContent=Math.round(end*k);if(k<1)requestAnimationFrame(f)})(t0)});
 setTimeout(()=>{document.querySelectorAll('[data-w]').forEach(e=>e.style.width=e.dataset.w+'%');document.querySelectorAll('[data-h]').forEach(e=>e.style.height=e.dataset.h+'%')},60);
}

/* ---------- 4. book recommendation ---------- */
async function vRec(){
 MEM=await api('/api/members');
 $('view').innerHTML=`<div class="card"><h3>Picked just for you</h3><p class="sub" style="margin-top:0">We study what a member has borrowed, find readers with a similar taste, and rank the books they will most likely enjoy.</p>
  <div class="row"><select id="rm" onchange="showRec()">${MEM.map(m=>`<option value="${m.id}">${esc(m.name)} (${m.code})</option>`).join('')}</select></div></div><div class="grid" id="rg"></div>`;
 showRec();
}
async function showRec(){
 const r=await api('/api/recommend?member='+$('rm').value);
 $('rg').innerHTML=r.map(b=>`<div><div class="book" onclick="openBook(${b.id})"><span class="match">${b.match}% match</span>${cover(b)}</div><p class="why">${esc(b.why)}</p></div>`).join('');
}
go('lib');
</script></body></html>
'''

if __name__ == "__main__":
    init_db()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = "http://localhost:%d" % PORT
    print("\n  ShelfSense Library is running ->  %s\n  Press Ctrl + C to stop.\n" % url)
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")