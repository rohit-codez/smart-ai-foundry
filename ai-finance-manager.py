"""
CoinPilot AI - AI Finance Expense Manager (single file, pure Python)

Features : Transaction analysis | Expense forecasting | Expense charts |
           Bill / receipt generation | Ask-your-money AI assistant

Install  : nothing! Only Python's built-in modules are used (no pip, no venv).
Run      : python ai-finance-manager.py        (opens http://localhost:3000 automatically)
"""
import calendar
import json
import math
import os
import random
import sqlite3
import threading
import webbrowser
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 3000
CURRENCY = "₹"          # change to "$" or "€" if you like
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "finance.db")
EXP_CATS = ["Food", "Transport", "Shopping", "Bills", "Health", "Entertainment", "Education", "Rent", "Other"]
INC_CATS = ["Salary", "Freelance", "Gift", "Other"]
DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
BUDGETS = {"Food": 9000, "Transport": 3000, "Shopping": 6000, "Bills": 5000, "Health": 2000,
           "Entertainment": 2500, "Education": 2000, "Rent": 14000}


# ---------------------------------------------------------------- database
def db():
    con = sqlite3.connect(DB_FILE, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def rows(con, sql, args=()):
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS tx(
            id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, kind TEXT NOT NULL,
            amount REAL NOT NULL, category TEXT NOT NULL, note TEXT DEFAULT '', method TEXT DEFAULT 'UPI');
        CREATE TABLE IF NOT EXISTS budgets(category TEXT PRIMARY KEY, lim REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS bills(
            id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT DEFAULT '', customer TEXT NOT NULL,
            items TEXT NOT NULL, tax REAL DEFAULT 0, total REAL NOT NULL, day TEXT NOT NULL, note TEXT DEFAULT '');
        """)
        if con.execute("SELECT COUNT(*) FROM tx").fetchone()[0] == 0:
            seed(con)


def seed(con):
    """Three months of believable sample money life, so every chart has something to show."""
    rnd, today = random.Random(11), date.today()
    notes = {"Food": ["Lunch with team", "Groceries", "Chai & samosa", "Dinner out", "Fruits & veggies", "Pizza night"],
             "Transport": ["Metro card", "Auto ride", "Petrol", "Cab to airport", "Bus pass"],
             "Shopping": ["New sneakers", "Headphones", "Books & stationery", "Jacket", "Home decor"],
             "Entertainment": ["Movie tickets", "Gaming top-up", "Concert", "Streaming plan"],
             "Health": ["Pharmacy", "Doctor visit", "Gym fee"], "Education": ["Online course", "Exam fee", "Workbook"]}
    chance = {"Food": (.92, 120, 650), "Transport": (.6, 40, 320), "Shopping": (.11, 600, 4500),
              "Entertainment": (.14, 200, 1300), "Health": (.05, 300, 1800), "Education": (.04, 500, 2500)}
    out = []
    for back in range(100, -1, -1):
        d = today - timedelta(days=back)
        w = 1.3 if d.weekday() >= 5 else 1.0
        if d.day == 1:
            out.append((d, "income", 52000, "Salary", "Monthly salary", "Bank"))
        if d.day == 3:
            out.append((d, "expense", 14000, "Rent", "House rent", "Bank"))
        if d.day == 8:
            out.append((d, "expense", rnd.randint(1800, 2700), "Bills", "Electricity bill", "UPI"))
        if d.day == 10:
            out.append((d, "expense", 799, "Bills", "Internet plan", "Card"))
        if d.day == 12:
            out.append((d, "expense", 399, "Bills", "Mobile recharge", "UPI"))
        if d.day in (14, 25) and rnd.random() < .7:
            out.append((d, "income", rnd.randint(3000, 9000), "Freelance", "Freelance project", "Bank"))
        for cat, (p, lo, hi) in chance.items():
            if rnd.random() < p * (w if cat in ("Food", "Entertainment") else 1):
                out.append((d, "expense", rnd.randint(lo, hi), cat, rnd.choice(notes[cat]), rnd.choice(["UPI", "Card", "Cash"])))
    con.executemany("INSERT INTO tx(day,kind,amount,category,note,method) VALUES(?,?,?,?,?,?)",
                    [(d.isoformat(), k, a, c, n, m) for d, k, a, c, n, m in out])
    con.executemany("INSERT INTO budgets(category,lim) VALUES(?,?)", list(BUDGETS.items()))


# ---------------------------------------------------------------- helpers
def tot(con, kind, a, b, cat=None):
    sql, args = "SELECT COALESCE(SUM(amount),0) FROM tx WHERE kind=? AND day BETWEEN ? AND ?", [kind, a, b]
    if cat:
        sql += " AND category=?"
        args.append(cat)
    return con.execute(sql, args).fetchone()[0]


def month_start(t, back):
    y, m = t.year, t.month - back
    while m <= 0:
        m, y = m + 12, y - 1
    return date(y, m, 1)


def month_end(start):
    return date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])


def money(n):
    return CURRENCY + format(round(n), ",")


def trend_avg(vals):
    """Recent months count more, then a half-strength nudge in the direction of the trend."""
    if not vals:
        return 0
    if len(vals) == 1:
        return vals[0]
    w = range(1, len(vals) + 1)
    avg = sum(v * x for v, x in zip(vals, w)) / sum(w)
    slope = max(-.2 * avg, min(.2 * avg, (vals[-1] - vals[0]) / (len(vals) - 1)))
    return max(0, avg + .5 * slope)


def period_range(s):
    t = date.today()
    if "last month" in s:
        end = t.replace(day=1) - timedelta(days=1)
        return end.replace(day=1).isoformat(), end.isoformat(), "last month"
    if "today" in s:
        return t.isoformat(), t.isoformat(), "today"
    if "yesterday" in s:
        y = t - timedelta(days=1)
        return y.isoformat(), y.isoformat(), "yesterday"
    if "week" in s:
        return (t - timedelta(days=6)).isoformat(), t.isoformat(), "in the last 7 days"
    if "year" in s:
        return date(t.year, 1, 1).isoformat(), t.isoformat(), "this year"
    if "all" in s and "time" in s:
        return "2000-01-01", t.isoformat(), "of all time"
    return t.replace(day=1).isoformat(), t.isoformat(), "this month"


# ---------------------------------------------------------------- analysis
def summary(con):
    t = date.today()
    ms, te = t.replace(day=1).isoformat(), t.isoformat()
    inc, exp = tot(con, "income", ms, te), tot(con, "expense", ms, te)
    lm_end = t.replace(day=1) - timedelta(days=1)
    lm_start = lm_end.replace(day=1)
    same_end = min(lm_end, lm_start + timedelta(days=t.day - 1))
    prev_same = tot(con, "expense", lm_start.isoformat(), same_end.isoformat())
    cats = rows(con, "SELECT category, SUM(amount) amt, COUNT(*) n FROM tx WHERE kind='expense' AND day BETWEEN ? AND ? "
                     "GROUP BY category ORDER BY amt DESC", (ms, te))
    wk = {r["w"]: r["s"] for r in con.execute(
        "SELECT CAST(strftime('%w',day) AS INTEGER) w, SUM(amount) s FROM tx WHERE kind='expense' AND day>=? GROUP BY w",
        ((t - timedelta(days=90)).isoformat(),))}
    weekday = [{"day": DAYS[i][:3], "avg": round(wk.get(i, 0) / 13)} for i in range(7)]
    monthly = []
    for back in range(5, -1, -1):
        a = month_start(t, back)
        b = min(month_end(a), t)
        monthly.append({"label": a.strftime("%b"), "income": tot(con, "income", a.isoformat(), b.isoformat()),
                        "expense": tot(con, "expense", a.isoformat(), b.isoformat())})
    daily = []
    for back in range(29, -1, -1):
        d = (t - timedelta(days=back)).isoformat()
        daily.append({"day": d[5:], "amt": tot(con, "expense", d, d)})
    top = rows(con, "SELECT note,amount,day,category FROM tx WHERE kind='expense' AND day BETWEEN ? AND ? "
                    "ORDER BY amount DESC LIMIT 1", (ms, te))
    ins = []
    if cats and exp:
        ins.append(["info", "%s is your biggest spend: %s, %d%% of everything you spent this month."
                    % (cats[0]["category"], money(cats[0]["amt"]), round(cats[0]["amt"] * 100 / exp))])
    if prev_same:
        ch = round((exp - prev_same) * 100 / prev_same)
        ins.append(["warn" if ch > 10 else "good", "Spending is %s%% %s than the same days last month."
                    % (abs(ch), "higher" if ch > 0 else "lower")])
    if inc:
        rate = round((inc - exp) * 100 / inc)
        ins.append(["good" if rate >= 20 else "warn", "You are saving %d%% of your income this month%s."
                    % (rate, "." if rate >= 20 else ". Aim for 20% or more.")])
    if any(wk.values()):
        best = max(wk, key=wk.get)
        ins.append(["info", "%s is your most expensive day of the week." % DAYS[best]])
    for c in EXP_CATS:   # unusual-spend detector: more than mean + 2 standard deviations
        vals = [r[0] for r in con.execute("SELECT amount FROM tx WHERE kind='expense' AND category=? AND day>=?",
                                          (c, (t - timedelta(days=90)).isoformat()))]
        if len(vals) >= 6:
            mean = sum(vals) / len(vals)
            sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
            for r in con.execute("SELECT amount,day FROM tx WHERE kind='expense' AND category=? AND day>=? AND amount>?",
                                 (c, (t - timedelta(days=30)).isoformat(), max(mean + 2 * sd, mean * 1.5))):
                ins.append(["warn", "Unusual %s spend of %s on %s is far above your normal." % (c, money(r["amount"]), r["day"])])
    for r in con.execute("SELECT category, lim FROM budgets"):
        spent = tot(con, "expense", ms, te, r["category"])
        if r["lim"] and spent >= .8 * r["lim"]:
            ins.append(["warn", "%s budget is %d%% used." % (r["category"], round(spent * 100 / r["lim"]))])
    return {"income": inc, "expense": exp, "balance": inc - exp, "rate": round((inc - exp) * 100 / inc) if inc else 0,
            "cats": cats, "weekday": weekday, "monthly": monthly, "daily": daily, "top": top[0] if top else None,
            "insights": ins[:7], "txn": con.execute("SELECT COUNT(*) FROM tx").fetchone()[0]}


# ---------------------------------------------------------------- forecasting
def forecast(con):
    t = date.today()
    days_in = calendar.monthrange(t.year, t.month)[1]
    left = days_in - t.day
    ms, te = t.replace(day=1).isoformat(), t.isoformat()
    done = []   # completed months, oldest first (empty months before the data starts are skipped)
    for back in range(5, 0, -1):
        a = month_start(t, back)
        done.append((a, tot(con, "expense", a.isoformat(), month_end(a).isoformat())))
    while done and done[0][1] == 0:
        done.pop(0)
    nxt = month_start(t, -1)
    next_total = trend_avg([v for _, v in done])
    start14 = (t - timedelta(days=13)).isoformat()

    def base(cat=None):   # daily spending speed: 60% last 14 days + 40% month so far
        return .6 * tot(con, "expense", start14, te, cat) / 14 + .4 * tot(con, "expense", ms, te, cat) / t.day

    spent = tot(con, "expense", ms, te)
    wk = {r["w"]: r["s"] for r in con.execute(
        "SELECT CAST(strftime('%w',day) AS INTEGER) w, SUM(amount) s FROM tx WHERE kind='expense' AND day>=? GROUP BY w",
        ((t - timedelta(days=90)).isoformat(),))}
    mean_w = (sum(wk.values()) / 7) or 1
    week = []
    for i in range(1, 8):   # next 7 days, shaped by which weekday it is
        d = t + timedelta(days=i)
        f = wk.get((d.weekday() + 1) % 7, mean_w) / mean_w
        week.append({"day": d.isoformat()[5:], "amt": round(base() * f)})
    budgets, cats = [], []
    for r in con.execute("SELECT category, lim FROM budgets ORDER BY category"):
        c = r["category"]
        sp = tot(con, "expense", ms, te, c)
        proj = sp + base(c) * left
        pct = round(sp * 100 / r["lim"]) if r["lim"] else 0
        st = "over" if sp > r["lim"] else "risk" if proj > r["lim"] else "warn" if pct >= 80 else "ok"
        budgets.append({"category": c, "limit": r["lim"], "spent": sp, "projected": round(proj), "pct": pct, "status": st})
    for c in EXP_CATS:
        h = [tot(con, "expense", a.isoformat(), month_end(a).isoformat(), c) for a, _ in done]
        if any(h):
            cats.append({"category": c, "next": round(trend_avg(h))})
    cats.sort(key=lambda x: -x["next"])
    total_budget = sum(b["limit"] for b in budgets)
    n = len(done)
    return {"next": round(next_total), "next_label": nxt.strftime("%B %Y"), "hist": [{"label": a.strftime("%b"), "total": v} for a, v in done],
            "conf": "High" if n >= 4 else "Medium" if n >= 2 else "Low",
            "month": {"spent": spent, "projected": round(spent + base() * left), "left": left, "daily": round(base())},
            "safe": round(max(0, total_budget - spent) / left) if left and total_budget else 0,
            "week": week, "budgets": budgets, "cats": cats}


def ask(con, q):
    """A small rule-based assistant: it spots the period, category and intent in your question."""
    s = q.lower()
    a, b, label = period_range(s)
    cat = next((c for c in EXP_CATS + INC_CATS if c.lower() in s), None)
    if any(w in s for w in ("forecast", "predict", "next month", "projection", "will i spend")):
        f = forecast(con)
        return "I expect about %s in expenses for %s (%s confidence). At today's pace this month will end near %s." % (
            money(f["next"]), f["next_label"], f["conf"].lower(), money(f["month"]["projected"]))
    if any(w in s for w in ("biggest", "largest", "highest", "most expensive")):
        r = con.execute("SELECT * FROM tx WHERE kind='expense' AND day BETWEEN ? AND ?" + (" AND category=?" if cat else "") +
                        " ORDER BY amount DESC LIMIT 1", [a, b] + ([cat] if cat else [])).fetchone()
        return "Your biggest expense %s was %s on %s (%s, %s)." % (label, money(r["amount"]), r["day"], r["note"] or r["category"], r["category"]) if r else "No expenses found %s." % label
    inc = any(w in s for w in ("income", "earn", "salary", "received", "freelance"))
    if any(w in s for w in ("balance", "left", "saved", "saving")):
        i, e = tot(con, "income", a, b), tot(con, "expense", a, b)
        return "%s: income %s, spending %s, so you kept %s." % (label.capitalize(), money(i), money(e), money(i - e))
    amt = tot(con, "income" if inc else "expense", a, b, cat)
    if any(w in s for w in ("average", "avg", "per day", "daily")):
        days = max(1, (date.fromisoformat(b) - date.fromisoformat(a)).days + 1)
        return "You spend about %s per day %s%s." % (money(amt / days), label, " on " + cat if cat else "")
    return "You %s %s%s %s." % ("earned" if inc else "spent", money(amt), " on " + cat if cat and not inc else "", label)


# ---------------------------------------------------------------- web server
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
            return self.send(200, PAGE.replace("__CUR__", CURRENCY).encode("utf-8"), "text/html; charset=utf-8")
        with db() as con:
            if p == "/api/tx":
                sql, args = "SELECT * FROM tx WHERE 1=1", []
                if q.get("q"):
                    sql += " AND LOWER(note||' '||category||' '||method) LIKE ?"
                    args.append("%" + q["q"].lower() + "%")
                for key, col in (("kind", "kind"), ("cat", "category")):
                    if q.get(key):
                        sql += " AND %s=?" % col
                        args.append(q[key])
                return self.reply(rows(con, sql + " ORDER BY day DESC, id DESC LIMIT 300", args))
            if p == "/api/summary":
                return self.reply(summary(con))
            if p == "/api/forecast":
                return self.reply(forecast(con))
            if p == "/api/ask":
                return self.reply({"answer": ask(con, q.get("q", ""))})
            if p == "/api/bills":
                out = rows(con, "SELECT * FROM bills ORDER BY id DESC")
                for b in out:
                    b["items"] = json.loads(b["items"])
                return self.reply(out)
        self.reply({"error": "not found"}, 404)

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except Exception:
            return self.reply({"error": "bad request"}, 400)
        with db() as con:
            if p == "/api/tx/add":
                try:
                    amt = float(d["amount"])
                    day = date.fromisoformat(str(d.get("day") or date.today().isoformat())).isoformat()
                except Exception:
                    return self.reply({"ok": False, "msg": "Please enter a valid amount and date."})
                kind = "income" if d.get("kind") == "income" else "expense"
                if amt <= 0:
                    return self.reply({"ok": False, "msg": "Amount must be more than zero."})
                con.execute("INSERT INTO tx(day,kind,amount,category,note,method) VALUES(?,?,?,?,?,?)",
                            (day, kind, amt, str(d.get("category") or "Other"), str(d.get("note", "")).strip(),
                             str(d.get("method") or "UPI")))
                return self.reply({"ok": True, "msg": "Saved %s of %s." % (kind, money(amt))})
            if p == "/api/tx/delete":
                con.execute("DELETE FROM tx WHERE id=?", (int(d["id"]),))
                return self.reply({"ok": True})
            if p == "/api/budget/set":
                con.execute("INSERT OR REPLACE INTO budgets(category,lim) VALUES(?,?)", (str(d["category"]), max(0, float(d["limit"] or 0))))
                return self.reply({"ok": True, "msg": "Budget updated."})
            if p == "/api/bills/add":
                items = [{"desc": str(i.get("desc", "")).strip(), "qty": float(i.get("qty") or 1), "price": float(i.get("price") or 0)}
                         for i in d.get("items", []) if str(i.get("desc", "")).strip()]
                if not str(d.get("customer", "")).strip() or not items:
                    return self.reply({"ok": False, "msg": "Add a customer name and at least one item."})
                tax = max(0, float(d.get("tax") or 0))
                total = round(sum(i["qty"] * i["price"] for i in items) * (1 + tax / 100), 2)
                cur = con.execute("INSERT INTO bills(customer,items,tax,total,day,note) VALUES(?,?,?,?,?,?)",
                                  (str(d["customer"]).strip(), json.dumps(items), tax, total, date.today().isoformat(), str(d.get("note", "")).strip()))
                num = "INV-%s-%03d" % (date.today().strftime("%Y%m"), cur.lastrowid)
                con.execute("UPDATE bills SET number=? WHERE id=?", (num, cur.lastrowid))
                return self.reply({"ok": True, "id": cur.lastrowid, "msg": "Bill " + num + " created."})
            if p == "/api/bills/delete":
                con.execute("DELETE FROM bills WHERE id=?", (int(d["id"]),))
                return self.reply({"ok": True})
        self.reply({"error": "not found"}, 404)


# ---------------------------------------------------------------- web page (HTML + CSS + JS inside Python)
PAGE = r'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>CoinPilot AI - Finance Manager</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#110c09;--card:#1e1611;--line:#3d2d22;--gold:#f5b700;--em:#2ee59d;--red:#ff5d5d;--sky:#4cc9f0;--ink:#fff7ea;--mute:#b39d86}
*{box-sizing:border-box;margin:0;font-family:'Segoe UI',system-ui,sans-serif}
body{background:radial-gradient(circle at 80% -10%,#3b2808 0,var(--bg) 52%);color:var(--ink);min-height:100vh;overflow-x:hidden}
#bg{position:fixed;inset:0;z-index:0}
.wrap{position:relative;z-index:1;max-width:1180px;margin:auto;padding:18px}
#nav{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:22px}
.logo{font-size:24px;font-weight:800;flex:1;min-width:170px}.logo span{color:var(--gold)}
button{cursor:pointer;border:1px solid var(--line);border-radius:12px;padding:10px 16px;font-size:14px;color:var(--ink);background:var(--card);transition:.2s}
button:hover{background:#34261b;transform:translateY(-2px)}
.nav.on{background:var(--gold);color:#2b1d00;font-weight:700;border-color:var(--gold)}
.btn{background:linear-gradient(135deg,var(--gold),#d99100);color:#2b1d00;font-weight:700;border:0}.btn:hover{background:var(--em)}
.btn.sm{padding:6px 12px;font-size:12px}.btn.red{background:#8f1d1d;color:#fff;border:0}.btn.ghost{background:transparent}
input,select{background:#150f0b;border:1px solid var(--line);color:var(--ink);padding:11px 13px;border-radius:10px;font-size:14px;outline:0;width:100%}
input:focus,select:focus{border-color:var(--gold);box-shadow:0 0 0 3px #f5b70028}
.card{background:rgba(30,22,17,.88);backdrop-filter:blur(8px);border:1px solid var(--line);border-radius:20px;padding:20px;margin-bottom:16px}
h1{font-size:clamp(28px,4.6vw,48px);line-height:1.1;background:linear-gradient(90deg,#fff,var(--gold),var(--em));-webkit-background-clip:text;background-clip:text;color:transparent}
h3{color:var(--gold);margin-bottom:12px}.sub{color:var(--mute);margin:8px 0 16px}
.chip{display:inline-block;background:#f5b70022;color:var(--gold);padding:6px 14px;border-radius:99px;font-size:12px;font-weight:700;margin-bottom:12px}
.row{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}.row>*{flex:1;min-width:120px}.row>.fit{flex:0}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:16px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-bottom:16px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:18px;transition:.25s}.stat:hover{transform:translateY(-5px);border-color:var(--gold)}
.stat b{font-size:30px;display:block}.stat span{color:var(--mute);font-size:13px}
table{width:100%;border-collapse:collapse;font-size:14px}th{text-align:left;color:var(--mute);font-weight:600;padding:8px}
td{padding:9px 8px;border-top:1px solid var(--line)}.scroll{overflow-x:auto}
.tag{display:inline-block;padding:3px 11px;border-radius:99px;font-size:12px;font-weight:700}
.t-ok{background:#2ee59d26;color:var(--em)}.t-bad{background:#ff5d5d2b;color:var(--red)}.t-mid{background:#f5b70026;color:var(--gold)}.t-no{background:#3d2d22;color:var(--mute)}
.inc{color:var(--em)}.exp{color:var(--red)}
.seg button{border-radius:0;flex:1}.seg button:first-child{border-radius:12px 0 0 12px}.seg button:last-child{border-radius:0 12px 12px 0}
.seg .on.e{background:var(--red);color:#fff}.seg .on.i{background:var(--em);color:#04281a}
.bar{margin:11px 0}.bar p{font-size:13px;margin-bottom:4px;display:flex;justify-content:space-between;gap:8px}
.bar div{height:11px;border-radius:99px;width:0;transition:width 1s}
.cols{display:flex;align-items:flex-end;gap:10px;height:170px;margin-top:8px}
.cols>div{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%;font-size:11px;color:var(--mute);gap:4px}
.pair{display:flex;align-items:flex-end;gap:3px;width:100%;height:100%;justify-content:center}
.pair span,.cols>div>span{width:46%;max-width:30px;border-radius:7px 7px 0 0;height:0;transition:height 1s}
.donutwrap{display:flex;align-items:center;gap:22px;flex-wrap:wrap}
.donut{width:170px;height:170px;border-radius:50%;position:relative;transition:transform .4s}.donut:hover{transform:rotate(12deg) scale(1.04)}
.donut:after{content:'';position:absolute;inset:36px;background:#1e1611;border-radius:50%}
.legend div{margin:5px 0;font-size:14px}.dot{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:8px}
.ai{display:flex;gap:10px;flex-wrap:wrap}.ai input{flex:1;min-width:220px;font-size:16px;padding:14px}
.bubble{margin-top:14px;padding:14px 18px;border-radius:16px;background:#f5b70016;border:1px solid #f5b70044;min-height:24px;font-size:16px}
.ins{display:flex;gap:12px;padding:10px 0;border-top:1px solid var(--line);font-size:15px}.ins:first-of-type{border-top:0}
svg text{fill:var(--mute);font-size:11px}
.trend path{transition:stroke-dashoffset 1.8s ease}
.msg{min-height:20px;font-weight:600;color:var(--red);margin-bottom:8px}
#modal{position:fixed;inset:0;z-index:40;background:#000c;display:none;place-items:start center;padding:20px;overflow:auto}
#modal.open{display:grid}
#paper{background:#fffdf6;color:#1b1b1b;width:100%;max-width:400px;padding:26px 24px;font-family:'Courier New',monospace;font-size:14px;position:relative;animation:pop .4s cubic-bezier(.2,1.4,.4,1);box-shadow:0 20px 60px #000a}
#paper *{font-family:'Courier New',monospace;color:#1b1b1b}#paper h2{text-align:center;font-size:20px;letter-spacing:2px}
#paper .dash{border-top:2px dashed #999;margin:12px 0}#paper .l{display:flex;justify-content:space-between;gap:10px;margin:5px 0}
#paper .big{font-size:18px;font-weight:bold}#paper .stamp{position:absolute;right:16px;top:70px;transform:rotate(-14deg);border:3px solid #16a34a;color:#16a34a;padding:2px 10px;font-weight:bold;letter-spacing:2px;opacity:.8}
#paper .stamp{color:#16a34a}#paper .c{text-align:center;color:#666;font-size:12px}
@keyframes pop{from{transform:scale(.8) translateY(30px);opacity:0}to{transform:none;opacity:1}}
#toast{position:fixed;bottom:24px;left:50%;transform:translate(-50%,120px);background:var(--em);color:#04281a;font-weight:700;padding:12px 22px;border-radius:14px;z-index:99;transition:.4s}
#toast.show{transform:translate(-50%,0)}#toast.bad{background:var(--red);color:#fff}
@media print{body *{visibility:hidden}#paper,#paper *{visibility:visible}#paper{position:absolute;left:0;top:0;box-shadow:none;max-width:100%}#bg{display:none}}
</style></head><body>
<canvas id="bg"></canvas>
<div class="wrap"><div id="nav"></div><div id="app"></div></div>
<div id="modal"><div><div id="paper"></div><div class="row" style="margin-top:12px;justify-content:center"><button class="btn" onclick="window.print()">Print / Save as PDF</button><button onclick="$('modal').classList.remove('open')">Close</button></div></div></div>
<div id="toast"></div>

<script>
const CUR='__CUR__';
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(p,d){const o=d===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)};return (await fetch(p,o)).json()}
function toast(m,bad){const t=$('toast');t.textContent=m;t.className='show'+(bad?' bad':'');setTimeout(()=>t.className='',2800)}
const fmt=n=>CUR+Math.round(n).toLocaleString('en-IN');
const EXP=['Food','Transport','Shopping','Bills','Health','Entertainment','Education','Rent','Other'],INC=['Salary','Freelance','Gift','Other'];
const CC={Food:'#f5b700',Transport:'#4cc9f0',Shopping:'#ff8a3d',Bills:'#2ee59d',Health:'#ff5d5d',Entertainment:'#b5e61d',Education:'#14b8a6',Rent:'#d6a77a',Other:'#94a3b8',Salary:'#2ee59d',Freelance:'#4cc9f0',Gift:'#f5b700'};
const cc=c=>CC[c]||'#94a3b8';

/* ---------- interactive background: golden coins that float, dodge the mouse and burst on click ---------- */
const bgc=$('bg'),bx=bgc.getContext('2d');let W,H,mx=-999,my=-999,C=[],sym=CUR;
function rs(){W=bgc.width=innerWidth;H=bgc.height=innerHeight}addEventListener('resize',rs);rs();
function coin(burst,x,y){return{x:x??Math.random()*W,y:y??Math.random()*H,r:burst?9+Math.random()*9:8+Math.random()*20,vx:burst?(Math.random()-.5)*9:(Math.random()-.5)*.4,vy:burst?-4-Math.random()*8:-.2-Math.random()*.5,
 ph:Math.random()*6,life:burst?1:2,g:burst,a:burst?.9:.1+Math.random()*.2,s:Math.random()<.2?'+':sym}}
for(let i=0;i<34;i++)C.push(coin());
addEventListener('mousemove',e=>{mx=e.clientX;my=e.clientY});
addEventListener('click',e=>{for(let i=0;i<16;i++)C.push(coin(true,e.clientX,e.clientY))});
(function loop(t){
 bx.clearRect(0,0,W,H);
 for(let i=C.length-1;i>=0;i--){const c=C[i];
  c.x+=c.vx;c.y+=c.vy;if(c.g){c.vy+=.28;c.life-=.012;if(c.life<=0){C.splice(i,1);continue}}else{if(c.y<-40){c.y=H+40;c.x=Math.random()*W}if(c.x<-40)c.x=W+40;if(c.x>W+40)c.x=-40}
  const dx=c.x-mx,dy=c.y-my,d=Math.hypot(dx,dy)||1;if(!c.g&&d<170){c.x+=dx/d*3;c.y+=dy/d*3}
  const sx=Math.abs(Math.cos(t*.0016+c.ph)),al=c.g?Math.max(0,c.life)*c.a:c.a+(d<170?.35*(1-d/170):0);
  bx.save();bx.translate(c.x,c.y);bx.scale(Math.max(.15,sx),1);
  bx.fillStyle=c.s==='+'?`rgba(46,229,157,${al})`:`rgba(245,183,0,${al})`;bx.beginPath();bx.arc(0,0,c.r,0,7);bx.fill();
  bx.strokeStyle=`rgba(255,240,200,${al})`;bx.lineWidth=2;bx.beginPath();bx.arc(0,0,c.r*.72,0,7);bx.stroke();
  bx.fillStyle=`rgba(60,35,0,${Math.min(1,al*2)})`;bx.font='bold '+Math.round(c.r)+'px Segoe UI';bx.textAlign='center';bx.textBaseline='middle';bx.fillText(c.s,0,1);bx.restore();
 }
 requestAnimationFrame(loop);
})(0);

/* ---------- navigation ---------- */
const TABS={dash:'Dashboard',tx:'Transactions',fc:'Forecast',bill:'Bills & Receipts'};let tab='dash';
function go(k){tab=k;$('nav').innerHTML=`<div class="logo">Coin<span>Pilot</span> AI</div>`+Object.entries(TABS).map(([a,b])=>`<button class="nav ${a===k?'on':''}" onclick="go('${a}')">${b}</button>`).join('');({dash:vDash,tx:vTx,fc:vFc,bill:vBill})[k]()}
function grow(){setTimeout(()=>{document.querySelectorAll('[data-w]').forEach(e=>e.style.width=e.dataset.w+'%');document.querySelectorAll('[data-h]').forEach(e=>e.style.height=e.dataset.h+'%');document.querySelectorAll('.trend path').forEach(p=>p.style.strokeDashoffset=0);
 document.querySelectorAll('[data-n]').forEach(el=>{const end=+el.dataset.n,t0=performance.now();(function f(t){const k=Math.min(1,(t-t0)/900),v=end*(1-Math.pow(1-k,3));el.textContent=el.dataset.p?Math.round(v)+'%':fmt(v);if(k<1)requestAnimationFrame(f)})(t0)})},60)}
function bars(list){return list.map(t=>`<div class="bar"><p><span>${esc(t.label)}</span><b>${t.val}</b></p><div data-w="${t.pct}" style="background:${t.color}"></div></div>`).join('')}
function type(el,txt){el.textContent='';let i=0;(function f(){el.textContent=txt.slice(0,i++);if(i<=txt.length)setTimeout(f,14)})()}

/* ================= DASHBOARD: analysis + charts ================= */
async function vDash(){
 const [s,f]=await Promise.all([api('/api/summary'),api('/api/forecast')]);
 const tot=s.cats.reduce((a,c)=>a+c.amt,0)||1,mx=Math.max(1,...s.monthly.flatMap(m=>[m.income,m.expense])),wmx=Math.max(1,...s.weekday.map(w=>w.avg));
 let acc=0;const parts=s.cats.map(c=>{const a=acc;acc+=c.amt/tot*100;return cc(c.category)+' '+a+'% '+acc+'%'});
 const tile=(l,n,col,p)=>`<div class="stat"><b data-n="${n}" ${p?'data-p="1"':''} style="color:${col}">0</b><span>${l}</span></div>`;
 const icon={info:'💡',warn:'⚠️',good:'✅'};
 $('app').innerHTML=`<div style="margin-bottom:18px"><div class="chip">${new Date().toLocaleDateString(undefined,{weekday:'long',day:'numeric',month:'long'})}</div><h1>Your money,<br>made clear.</h1></div>
 <div class="card"><h3>Ask your AI</h3><div class="ai"><input id="aq" placeholder="Try: how much did I spend on food last month?" onkeydown="if(event.key==='Enter')askAI()"><button class="btn" onclick="askAI()">Ask</button></div>
  <div class="row" style="margin:10px 0 0">${['What is my biggest expense this month?','Forecast next month','How much did I earn this year?','Average spend per day this week'].map(q=>`<button class="btn sm ghost fit" onclick="$('aq').value='${q}';askAI()">${q}</button>`).join('')}</div><div class="bubble" id="ans">I can answer questions about spending, income, balance and forecasts.</div></div>
 <div class="stats">${tile('Income this month',s.income,'var(--em)')}${tile('Spent this month',s.expense,'var(--red)')}${tile('Balance',s.balance,s.balance>=0?'var(--gold)':'var(--red)')}${tile('Savings rate',Math.max(0,s.rate),'var(--sky)',1)}</div>
 <div class="card"><h3>AI insights</h3>${s.insights.map(i=>`<div class="ins"><span>${icon[i[0]]}</span><span>${esc(i[1])}</span></div>`).join('')||'<p class="sub">Add some transactions to unlock insights.</p>'}</div>
 <div class="grid2"><div class="card"><h3>Spending by category</h3><div class="donutwrap"><div class="donut" style="background:conic-gradient(${parts.join(',')||'#3d2d22 0 100%'})"></div>
  <div class="legend">${s.cats.map(c=>`<div><span class="dot" style="background:${cc(c.category)}"></span>${c.category}: <b>${fmt(c.amt)}</b></div>`).join('')}</div></div></div>
 <div class="card"><h3>Income vs expense (6 months)</h3><div class="cols">${s.monthly.map(m=>`<div><div class="pair"><span data-h="${m.income/mx*100}" style="background:var(--em)" title="Income ${fmt(m.income)}"></span><span data-h="${m.expense/mx*100}" style="background:var(--red)" title="Expense ${fmt(m.expense)}"></span></div>${m.label}</div>`).join('')}</div>
  <p class="sub" style="margin:10px 0 0;font-size:12px"><span class="inc">&#9632;</span> income &nbsp;<span class="exp">&#9632;</span> expense</p></div></div>
 <div class="card"><h3>Daily spending: last 30 days and 7-day forecast</h3>${trend(s.daily,f.week)}</div>
 <div class="grid2"><div class="card"><h3>Average spend by weekday</h3><div class="cols">${s.weekday.map(w=>`<div>${fmt(w.avg)}<span data-h="${w.avg/wmx*100}" style="background:linear-gradient(var(--gold),var(--em))"></span>${w.day}</div>`).join('')}</div></div>
 <div class="card"><h3>Top categories this month</h3>${bars(s.cats.slice(0,6).map(c=>({label:c.category+' ('+c.n+')',val:fmt(c.amt),pct:c.amt/tot*100,color:cc(c.category)})))}</div></div>`;
 grow();
}
function trend(d,w){
 const all=[...d.map(x=>x.amt),...w.map(x=>x.amt)],mx=Math.max(1,...all),X=i=>30+i*(640/(all.length-1)),Y=v=>190-v/mx*165;
 const line=(a,o)=>a.map((v,i)=>(i?'L':'M')+X(i+o).toFixed(1)+' '+Y(v).toFixed(1)).join(' ');
 const act=d.map(x=>x.amt),fc=[act[act.length-1],...w.map(x=>x.amt)];
 return `<svg class="trend" viewBox="0 0 700 220" width="100%"><defs><linearGradient id="ga" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#f5b700" stop-opacity=".35"/><stop offset="1" stop-color="#f5b700" stop-opacity="0"/></linearGradient></defs>
 ${[0,.5,1].map(k=>`<line x1="30" x2="670" y1="${Y(mx*k)}" y2="${Y(mx*k)}" stroke="#3d2d22"/><text x="0" y="${Y(mx*k)+4}">${fmt(mx*k)}</text>`).join('')}
 <path d="${line(act,0)} L${X(act.length-1)} 190 L30 190Z" fill="url(#ga)" stroke="none"/>
 <path d="${line(act,0)}" pathLength="1" stroke="#f5b700" stroke-width="3" fill="none" stroke-dasharray="1" stroke-dashoffset="1" stroke-linejoin="round"/>
 <path d="${line(fc,act.length-1)}" pathLength="1" stroke="#2ee59d" stroke-width="3" fill="none" stroke-dasharray=".02 .012" stroke-dashoffset="1"/>
 ${all.map((v,i)=>`<circle cx="${X(i)}" cy="${Y(v)}" r="6" fill="${i<d.length?'#f5b700':'#2ee59d'}" opacity="0" onmouseover="this.setAttribute('opacity',1)" onmouseout="this.setAttribute('opacity',0)"><title>${(i<d.length?d[i].day:w[i-d.length].day)}: ${fmt(v)}${i<d.length?'':' (forecast)'}</title></circle>`).join('')}
 <text x="${X(d.length)-20}" y="212" style="fill:#2ee59d">forecast &rarr;</text><text x="30" y="212">${d[0].day}</text></svg>`;
}
async function askAI(){const q=$('aq').value.trim();if(!q)return;$('ans').textContent='Thinking...';const r=await api('/api/ask?q='+encodeURIComponent(q));type($('ans'),r.answer)}

/* ================= TRANSACTIONS ================= */
let kind='expense';
function setKind(k){kind=k;document.querySelectorAll('.seg button').forEach(b=>b.classList.toggle('on',b.dataset.k===k));$('cat').innerHTML=(k==='income'?INC:EXP).map(c=>`<option>${c}</option>`).join('')}
async function vTx(){
 const today=new Date().toISOString().slice(0,10);
 $('app').innerHTML=`<div class="card"><h3>Add a transaction</h3><div class="row seg"><button data-k="expense" class="e on" onclick="setKind('expense')">Expense</button><button data-k="income" class="i" onclick="setKind('income')">Income</button></div>
 <div class="row"><input id="amt" type="number" placeholder="Amount"><select id="cat"></select><input id="day" type="date" value="${today}"><select id="met"><option>UPI</option><option>Card</option><option>Cash</option><option>Bank</option></select></div>
 <div class="row"><input id="note" placeholder="Note (what was it for?)" onkeydown="if(event.key==='Enter')addTx()"><button class="btn fit" onclick="addTx()">Add</button></div></div>
 <div class="card"><h3>History</h3><div class="row"><input id="fq" placeholder="Search..." oninput="loadTx()"><select id="fk" onchange="loadTx()"><option value="">All types</option><option value="expense">Expenses</option><option value="income">Income</option></select>
 <select id="fc" onchange="loadTx()"><option value="">All categories</option>${[...new Set([...EXP,...INC])].map(c=>`<option>${c}</option>`).join('')}</select></div><div class="scroll" id="tl"></div></div>`;
 kind='expense';setKind('expense');loadTx();
}
let TX=[];
async function loadTx(){
 TX=await api('/api/tx?q='+encodeURIComponent($('fq').value)+'&kind='+$('fk').value+'&cat='+encodeURIComponent($('fc').value));
 $('tl').innerHTML=`<table><tr><th>Date</th><th>Category</th><th>Note</th><th>Method</th><th>Amount</th><th></th></tr>${TX.map(t=>`<tr><td>${t.day}</td><td><span class="dot" style="background:${cc(t.category)}"></span>${t.category}</td><td>${esc(t.note)}</td><td>${t.method}</td>
 <td class="${t.kind==='income'?'inc':'exp'}"><b>${t.kind==='income'?'+':'-'}${fmt(t.amount)}</b></td><td><button class="btn sm" onclick="txReceipt(${t.id})">Receipt</button> <button class="btn sm red" onclick="delTx(${t.id})">X</button></td></tr>`).join('')||'<tr><td colspan="6">Nothing found.</td></tr>'}</table>`;
}
async function addTx(){const r=await api('/api/tx/add',{kind,amount:$('amt').value,category:$('cat').value,day:$('day').value,method:$('met').value,note:$('note').value});toast(r.msg,!r.ok);if(r.ok){$('amt').value='';$('note').value='';loadTx()}}
async function delTx(id){await api('/api/tx/delete',{id});loadTx()}

/* ================= FORECAST ================= */
async function vFc(){
 const f=await api('/api/forecast'),mx=Math.max(1,f.next,...f.hist.map(h=>h.total)),m=f.month,st={ok:['t-ok','On track'],warn:['t-mid','Near limit'],risk:['t-mid','Will exceed'],over:['t-bad','Over budget']};
 $('app').innerHTML=`<div class="grid2"><div class="card"><div class="chip">${f.next_label}</div><h3>Expected expenses next month</h3><div style="font-size:54px;font-weight:800;color:var(--gold)" data-n="${f.next}">0</div>
  <p class="sub" style="margin-bottom:0">Confidence: <span class="tag ${f.conf==='High'?'t-ok':f.conf==='Medium'?'t-mid':'t-no'}">${f.conf}</span> &middot; recent months count more, plus your trend.</p></div>
 <div class="card"><h3>This month so far</h3><div class="bar"><p><span>Spent ${fmt(m.spent)}</span><b>Projected ${fmt(m.projected)}</b></p><div data-w="${Math.min(100,m.spent/Math.max(1,m.projected)*100)}" style="background:linear-gradient(90deg,var(--gold),var(--em))"></div></div>
  <p class="sub" style="margin-bottom:6px">${m.left} days left &middot; you spend about <b>${fmt(m.daily)}</b> a day.</p><p>Safe to spend: <b class="inc">${fmt(f.safe)}</b> per day to stay within all budgets.</p></div></div>
 <div class="grid2"><div class="card"><h3>Monthly history + forecast</h3><div class="cols">${f.hist.map(h=>`<div>${fmt(h.total)}<span data-h="${h.total/mx*100}" style="background:var(--gold);width:60%"></span>${h.label}</div>`).join('')}
  <div>${fmt(f.next)}<span data-h="${f.next/mx*100}" style="background:repeating-linear-gradient(45deg,var(--em),var(--em) 6px,#0f6b48 6px,#0f6b48 12px);width:60%"></span>Next</div></div></div>
 <div class="card"><h3>Forecast by category</h3>${bars(f.cats.map(c=>({label:c.category,val:fmt(c.next),pct:c.next/(f.cats[0].next||1)*100,color:cc(c.category)})))}</div></div>
 <div class="card"><h3>Budgets this month</h3><p class="sub" style="margin-top:0">Set a limit for each category. The AI warns you before you cross it.</p><div class="grid2" style="gap:12px">${f.budgets.map(b=>`<div style="border:1px solid var(--line);border-radius:16px;padding:14px">
  <div style="display:flex;justify-content:space-between;align-items:center"><b><span class="dot" style="background:${cc(b.category)}"></span>${b.category}</b><span class="tag ${st[b.status][0]}">${st[b.status][1]}</span></div>
  <div class="bar"><p><span>${fmt(b.spent)} of ${fmt(b.limit)}</span><b>${b.pct}%</b></p><div data-w="${Math.min(100,b.pct)}" style="background:${b.status==='over'?'var(--red)':b.status==='ok'?'var(--em)':'var(--gold)'}"></div></div>
  <p class="sub" style="margin:0 0 8px;font-size:12px">Projected month end: ${fmt(b.projected)}</p><div class="row" style="margin:0"><input id="b_${b.category}" type="number" value="${b.limit}"><button class="btn sm fit" onclick="setBudget('${b.category}')">Save</button></div></div>`).join('')}</div></div>`;
 grow();
}
async function setBudget(c){const r=await api('/api/budget/set',{category:c,limit:$('b_'+c).value});toast(r.msg);vFc()}

/* ================= BILLS & RECEIPTS ================= */
let ITEMS=[{desc:'',qty:1,price:''}];
function paper(html){$('paper').innerHTML=html;$('modal').classList.add('open')}
function txReceipt(id){
 const t=TX.find(x=>x.id===id),n='RCT-'+t.day.replace(/-/g,'')+'-'+String(t.id).padStart(4,'0');
 paper(`<h2>COINPILOT</h2><p class="c">${t.kind==='income'?'PAYMENT RECEIVED':'EXPENSE RECEIPT'}</p><div class="stamp">${t.kind==='income'?'RECEIVED':'PAID'}</div><div class="dash"></div>
 <div class="l"><span>Receipt no</span><b>${n}</b></div><div class="l"><span>Date</span><span>${t.day}</span></div><div class="l"><span>Category</span><span>${esc(t.category)}</span></div><div class="l"><span>Method</span><span>${t.method}</span></div><div class="l"><span>Note</span><span>${esc(t.note||'-')}</span></div>
 <div class="dash"></div><div class="l big"><span>TOTAL</span><span>${fmt(t.amount)}</span></div><div class="dash"></div><p class="c">Generated by CoinPilot AI<br>Thank you!</p>`);
}
function billPaper(b){
 const sub=b.items.reduce((a,i)=>a+i.qty*i.price,0),tax=sub*b.tax/100;
 paper(`<h2>COINPILOT</h2><p class="c">TAX INVOICE</p><div class="dash"></div><div class="l"><span>Invoice</span><b>${b.number}</b></div><div class="l"><span>Date</span><span>${b.day}</span></div><div class="l"><span>Billed to</span><b>${esc(b.customer)}</b></div><div class="dash"></div>
 ${b.items.map(i=>`<div class="l"><span>${esc(i.desc)} x${i.qty}</span><span>${fmt(i.qty*i.price)}</span></div>`).join('')}<div class="dash"></div>
 <div class="l"><span>Subtotal</span><span>${fmt(sub)}</span></div><div class="l"><span>Tax (${b.tax}%)</span><span>${fmt(tax)}</span></div><div class="l big"><span>TOTAL</span><span>${fmt(b.total)}</span></div>
 ${b.note?`<div class="dash"></div><p class="c">${esc(b.note)}</p>`:''}<div class="dash"></div><p class="c">Thank you for your business!</p>`);
}
let BILLS=[];
function itemRows(){return ITEMS.map((it,i)=>`<div class="row"><input placeholder="Item description" value="${esc(it.desc)}" oninput="ITEMS[${i}].desc=this.value"><input class="fit" style="max-width:90px" type="number" min="1" value="${it.qty}" oninput="ITEMS[${i}].qty=+this.value;sumUp()">
 <input class="fit" style="max-width:130px" type="number" placeholder="Price" value="${it.price}" oninput="ITEMS[${i}].price=+this.value;sumUp()"><button class="btn sm red fit" onclick="ITEMS.splice(${i},1);if(!ITEMS.length)ITEMS.push({desc:'',qty:1,price:''});vBill()">X</button></div>`).join('')}
function sumUp(){const s=ITEMS.reduce((a,i)=>a+(i.qty||0)*(i.price||0),0),t=+($('btax')||{value:0}).value||0;$('live').textContent=fmt(s*(1+t/100))}
async function vBill(){
 BILLS=await api('/api/bills');
 $('app').innerHTML=`<div class="grid2"><div class="card"><h3>Create a bill / invoice</h3><div class="row"><input id="bc" placeholder="Customer name"><input id="btax" class="fit" style="max-width:110px" type="number" placeholder="Tax %" oninput="sumUp()"></div>${itemRows()}
  <div class="row"><button onclick="ITEMS.push({desc:'',qty:1,price:''});vBill()">+ Add item</button></div><div class="row"><input id="bn" placeholder="Note on the bill (optional)"></div>
  <p style="font-size:20px;margin-bottom:12px">Total: <b class="inc" id="live">${fmt(0)}</b></p><button class="btn" onclick="makeBill()">Generate bill</button></div>
 <div class="card"><h3>Saved bills</h3><div class="scroll"><table><tr><th>No.</th><th>Customer</th><th>Total</th><th></th></tr>${BILLS.map(b=>`<tr><td>${b.number}</td><td>${esc(b.customer)}</td><td><b>${fmt(b.total)}</b></td>
  <td><button class="btn sm" onclick="billPaper(BILLS[${BILLS.indexOf(b)}])">View</button> <button class="btn sm red" onclick="delBill(${b.id})">X</button></td></tr>`).join('')||'<tr><td colspan="4">No bills yet.</td></tr>'}</table></div>
  <p class="sub" style="font-size:12px;margin:10px 0 0">Tip: in the Transactions tab every row has a Receipt button too.</p></div></div>`;sumUp();
}
async function makeBill(){const r=await api('/api/bills/add',{customer:$('bc').value,tax:$('btax').value,note:$('bn').value,items:ITEMS});toast(r.msg,!r.ok);if(r.ok){ITEMS=[{desc:'',qty:1,price:''}];await vBill();billPaper(BILLS.find(b=>b.id===r.id))}}
async function delBill(id){await api('/api/bills/delete',{id});vBill()}
go('dash');
</script></body></html>
'''

# ---------------------------------------------------------------- start
if __name__ == "__main__":
    init_db()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = "http://localhost:%d" % PORT
    print("\n  CoinPilot AI is running ->  %s\n  Press Ctrl + C to stop.\n" % url)
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")