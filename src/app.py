"""Daily accountability tracker, running as a Cloudflare Python Worker.

Admin adds members (e.g. a brother) and the things they must log every day. Members log in and log.
Data lives in Supabase Postgres, reached through Cloudflare Hyperdrive. A cron trigger queues reminders
for anything not logged yet and posts each email to a Google Apps Script web app, which sends it from Gmail.

The same module also runs locally with plain CPython for one-off setup:  python src/app.py init-db
"""
import hashlib
import json
import os
import re
import secrets
import sys
import urllib.request
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

import pg8000.dbapi
from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

IN_WORKER = sys.platform == "emscripten"

if IN_WORKER and not hasattr(hashlib, "pbkdf2_hmac"):
    # Pyodide ships without OpenSSL, so hashlib.pbkdf2_hmac is missing. pg8000's SCRAM login and Werkzeug's
    # password hashing both need it; the Workers runtime has a native PBKDF2 in WebCrypto, so use that.
    def _webcrypto_pbkdf2_hmac(hash_name, password, salt, iterations, dklen=None):
        from js import Object, Uint8Array, crypto
        from pyodide.ffi import run_sync, to_js

        algo = {"sha1": "SHA-1", "sha256": "SHA-256", "sha384": "SHA-384", "sha512": "SHA-512"}[
            hash_name.lower().replace("-", "")]
        dklen = dklen or {"SHA-1": 20, "SHA-256": 32, "SHA-384": 48, "SHA-512": 64}[algo]
        key = run_sync(crypto.subtle.importKey("raw", to_js(bytes(password)), "PBKDF2", False, to_js(["deriveBits"])))
        params = to_js({"name": "PBKDF2", "hash": algo, "salt": to_js(bytes(salt)), "iterations": iterations},
                       dict_converter=Object.fromEntries)
        bits = run_sync(crypto.subtle.deriveBits(params, key, dklen * 8))
        return bytes(Uint8Array.new(bits).to_py())

    hashlib.pbkdf2_hmac = _webcrypto_pbkdf2_hmac

# Settings come from Worker vars/secrets (wrangler.jsonc, `wrangler secret put`) or, locally, the environment.
WORKER_ENV = None
TZ = ZoneInfo("Asia/Kolkata")
BASE_URL = ""
APPS_SCRIPT_URL = ""
CURRENCY = "₹"
APP_NAME = "Daily Tracker"


def setting(name, default=""):
    value = getattr(WORKER_ENV, name, None) if WORKER_ENV is not None else None
    if value is None:
        value = os.environ.get(name, default)
    return str(value).strip()


def configure(env=None):
    """Load settings. Called with the Worker env on every request / cron run (cheap; bindings don't change)."""
    global WORKER_ENV, TZ, BASE_URL, APPS_SCRIPT_URL, CURRENCY, APP_NAME
    WORKER_ENV = env
    TZ = ZoneInfo(setting("TIMEZONE", "Asia/Kolkata"))
    BASE_URL = setting("BASE_URL").rstrip("/")
    APPS_SCRIPT_URL = setting("APPS_SCRIPT_URL")
    if "XXXXXXXX" in APPS_SCRIPT_URL:  # still the example placeholder
        APPS_SCRIPT_URL = ""
    CURRENCY = setting("CURRENCY", "₹")
    APP_NAME = setting("APP_NAME", "Daily Tracker")
    app.secret_key = setting("SECRET_KEY") or app.secret_key or secrets.token_hex(32)


DSA_TOPICS = [
    "Arrays", "Strings", "Hashing", "Two Pointers", "Sliding Window", "Binary Search",
    "Stack / Queue", "Linked List", "Recursion / Backtracking", "Trees", "BST", "Heap",
    "Graphs", "Dynamic Programming", "Greedy", "Trie", "Bit Manipulation", "Math",
    "Intervals", "Other",
]
DSA_DIFFICULTY = ["Easy", "Medium", "Hard"]
DSA_OUTCOMES = ["Solved on my own", "Solved with hints", "Read the solution, then coded it myself", "Could not solve"]
MONEY_CATEGORIES = [
    "Food", "Transport", "Shopping", "Bills / Recharge", "Education", "Entertainment",
    "Health", "Gifts", "Savings / Investment", "Salary / Pocket money", "Other",
]
KINDS = {"dsa": "DSA problems", "money": "Money log", "generic": "General task"}

app = Flask(__name__)
app.secret_key = None  # set in configure(): Workers forbid randomness at import time
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    PERMANENT_SESSION_LIFETIME=timedelta(days=60),
)

# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS tasks (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('dsa', 'money', 'generic')),
    description TEXT NOT NULL DEFAULT '',
    remind_at TEXT NOT NULL DEFAULT '19:00',
    min_count INT NOT NULL DEFAULT 1,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    start_day DATE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS dsa_logs (
    id SERIAL PRIMARY KEY,
    task_id INT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    day DATE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    problem TEXT NOT NULL,
    link TEXT NOT NULL,
    topic TEXT NOT NULL,
    difficulty TEXT NOT NULL,
    outcome TEXT NOT NULL,
    minutes INT NOT NULL,
    approach TEXT NOT NULL,
    complexity TEXT NOT NULL,
    mistakes TEXT NOT NULL,
    code TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS dsa_logs_task_day ON dsa_logs (task_id, day);
CREATE TABLE IF NOT EXISTS money_logs (
    id SERIAL PRIMARY KEY,
    task_id INT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    day DATE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    direction TEXT NOT NULL CHECK (direction IN ('expense', 'income')),
    amount NUMERIC(12, 2) NOT NULL,
    category TEXT NOT NULL,
    note TEXT NOT NULL,
    need_or_want TEXT
);
CREATE INDEX IF NOT EXISTS money_logs_task_day ON money_logs (task_id, day);
CREATE TABLE IF NOT EXISTS money_days (
    task_id INT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    day DATE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    no_transactions BOOLEAN NOT NULL,
    reflection TEXT NOT NULL,
    PRIMARY KEY (task_id, day)
);
CREATE TABLE IF NOT EXISTS task_logs (
    id SERIAL PRIMARY KEY,
    task_id INT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    day DATE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    summary TEXT NOT NULL,
    details TEXT NOT NULL,
    minutes INT NOT NULL,
    proof TEXT NOT NULL DEFAULT '',
    blockers TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS task_logs_task_day ON task_logs (task_id, day);
CREATE TABLE IF NOT EXISTS outbox (
    id SERIAL PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    to_email TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    link TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    sent_at TIMESTAMPTZ
);
ALTER TABLE users ADD COLUMN IF NOT EXISTS login_sent_at TIMESTAMPTZ;
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS attempts INT NOT NULL DEFAULT 0;
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS last_error TEXT;
-- Supabase exposes the public schema through its REST API. With RLS on and no policies, the
-- anon/authenticated API keys can't read or write these tables; this app's own connection
-- (the postgres role) bypasses RLS and is unaffected.
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE dsa_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE money_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE money_days ENABLE ROW LEVEL SECURITY;
ALTER TABLE task_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE outbox ENABLE ROW LEVEL SECURITY;
"""


def connect():
    """New connection. In the Worker it goes through Hyperdrive (which pools and keeps TLS to Supabase)."""
    hd = getattr(WORKER_ENV, "HYPERDRIVE", None) if WORKER_ENV is not None else None
    if hd is not None:
        conn = pg8000.dbapi.connect(host=hd.host, port=int(hd.port), user=hd.user, password=hd.password,
                                    database=hd.database, ssl_context=False)
    else:
        url = urlsplit(setting("DATABASE_URL"))
        if not url.hostname:
            raise SystemExit("Set DATABASE_URL (Supabase > Connect > Session pooler)")
        conn = pg8000.dbapi.connect(host=url.hostname, port=url.port or 5432, user=unquote(url.username),
                                    password=unquote(url.password or ""), database=url.path.lstrip("/") or "postgres",
                                    ssl_context="sslmode=disable" not in (url.query or ""))
    conn.autocommit = True
    return conn


def db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def run(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, tuple(params))
    if cur.description is None:
        return []
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def q(sql, params=()):
    return run(db(), sql, params)


def q1(sql, params=()):
    rows = q(sql, params)
    return rows[0] if rows else None


def init_db():
    """Create tables and the admin/member accounts. Run once from your computer: python src/app.py init-db"""
    conn = connect()
    sql = "\n".join(line for line in SCHEMA.splitlines() if not line.strip().startswith("--"))
    for statement in sql.split(";"):
        if statement.strip():
            run(conn, statement.strip())
    username = setting("ADMIN_USERNAME", "admin").lower()
    password = setting("ADMIN_PASSWORD")
    if not run(conn, "SELECT 1 FROM users WHERE username = %s", (username,)):
        if not password:
            raise SystemExit("Set ADMIN_PASSWORD")
        run(conn, "INSERT INTO users (name, email, username, password_hash, is_admin) VALUES (%s, %s, %s, %s, TRUE)",
            ("Admin", setting("ADMIN_EMAIL"), username, hash_password(password)))
        print(f"created admin user '{username}'")
    seed_member(conn)
    conn.close()
    print("database ready")


def seed_member(conn):
    """Optionally create the first member (with DSA + money tasks) from MEMBER_* settings. No email is sent."""
    email, username = setting("MEMBER_EMAIL"), setting("MEMBER_USERNAME").lower()
    if not email or not username:
        return
    if run(conn, "SELECT 1 FROM users WHERE lower(username) = %s OR lower(email) = lower(%s)", (username, email)):
        return
    member = run(conn, "INSERT INTO users (name, email, username, password_hash) VALUES (%s, %s, %s, %s) RETURNING id",
                 (setting("MEMBER_NAME", username), email, username, hash_password(secrets.token_urlsafe(16))))[0]
    for name, kind, desc, remind, count in [
        ("DSA practice", "dsa", "Solve and explain at least 2 problems", "20:00", 2),
        ("Money log", "money", "Log every rupee in or out, then close the day", "21:30", 1),
    ]:
        run(conn, """INSERT INTO tasks (user_id, name, kind, description, remind_at, min_count, start_day)
                     VALUES (%s, %s, %s, %s, %s, %s, %s)""", (member["id"], name, kind, desc, remind, count, today()))
    print(f"created member '{username}' <{email}> with DSA + Money tasks (send the login email from the admin page)")


# ---------------------------------------------------------------- time helpers


def now_local():
    return datetime.now(TZ)


def today():
    return now_local().date()


@app.template_filter("localtime")
def localtime_filter(value, fmt="%d %b, %H:%M"):
    return value.astimezone(TZ).strftime(fmt) if value else ""


@app.template_filter("money")
def money_filter(value):
    return f"{CURRENCY}{float(value or 0):,.2f}"


# ---------------------------------------------------------------- completion logic


def required(task):
    return 1 if task["kind"] == "money" else max(1, task["min_count"])


def counts_for(tasks, since):
    """{(task_id, day): number_of_logs} for the given tasks since a date."""
    ids = [t["id"] for t in tasks]
    if not ids:
        return {}
    tables = ("dsa_logs", "money_days", "task_logs")
    rows = q(
        " UNION ALL ".join(
            f"SELECT task_id, day, COUNT(*) AS n FROM {table} WHERE task_id = ANY(%s) AND day >= %s GROUP BY 1, 2"
            for table in tables
        ),
        (ids, since) * len(tables),
    )
    return {(r["task_id"], r["day"]): r["n"] for r in rows}


def task_status(tasks, grid_days=14):
    day0 = today()
    counts = counts_for(tasks, day0 - timedelta(days=400))
    out = []
    for t in tasks:
        def done(d, t=t):
            return counts.get((t["id"], d), 0) >= required(t)

        d = day0 if done(day0) else day0 - timedelta(days=1)
        streak = 0
        while d >= t["start_day"] and done(d) and streak < 400:
            streak += 1
            d -= timedelta(days=1)
        grid = []
        for i in range(grid_days - 1, -1, -1):
            d = day0 - timedelta(days=i)
            state = ("none" if d < t["start_day"] else "done" if done(d)
                     else "pending" if d == day0 else "missed")
            grid.append({"day": d, "state": state})
        out.append({
            "task": t, "count": counts.get((t["id"], day0), 0), "required": required(t),
            "done": done(day0), "streak": streak, "grid": grid,
        })
    return out


# ---------------------------------------------------------------- auth


# Cloudflare's WebCrypto caps PBKDF2 at 100,000 iterations, and scrypt isn't available in Workers.
PASSWORD_METHOD = "pbkdf2:sha256:100000"


def hash_password(password):
    return generate_password_hash(password, method=PASSWORD_METHOD)



@app.before_request
def load_user():
    g.user = None
    if "user_id" in session:
        g.user = q1("SELECT * FROM users WHERE id = %s AND active", (session["user_id"],))
        if g.user is None:
            session.clear()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login"))
        if not g.user["is_admin"]:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_globals():
    return {"me": g.get("user"), "currency": CURRENCY, "app_name": APP_NAME, "kinds": KINDS}


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        user = q1("SELECT * FROM users WHERE lower(username) = %s AND active", (username,))
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session.clear()
            session.permanent = True
            session["user_id"] = user["id"]
            return redirect(url_for("home"))
        flash("Wrong username or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        current, new = request.form.get("current", ""), request.form.get("new", "")
        if not check_password_hash(g.user["password_hash"], current):
            flash("Current password is wrong.", "error")
        elif len(new) < 6:
            flash("New password needs at least 6 characters.", "error")
        else:
            q("UPDATE users SET password_hash = %s WHERE id = %s", (hash_password(new), g.user["id"]))
            flash("Password changed.", "ok")
            return redirect(url_for("home"))
    return render_template("account.html")


# ---------------------------------------------------------------- member pages


@app.route("/")
@login_required
def home():
    if g.user["is_admin"]:
        return redirect(url_for("admin_home"))
    tasks = q("SELECT * FROM tasks WHERE user_id = %s AND active ORDER BY id", (g.user["id"],))
    return render_template("dashboard.html", rows=task_status(tasks), today=today())


def own_task(task_id):
    task = q1("SELECT * FROM tasks WHERE id = %s", (task_id,))
    if task is None:
        abort(404)
    if task["user_id"] != g.user["id"] and not g.user["is_admin"]:
        abort(403)
    return task


def text(field):
    return (request.form.get(field) or "").strip()


def min_len(errors, value, label, n):
    if len(value) < n:
        errors.append(f"{label} is too short ({len(value)} chars). Write at least {n} characters.")


def int_between(errors, value, label, lo, hi):
    if not value.isdigit():
        errors.append(f"{label} must be a whole number.")
        return None
    n = int(value)
    if not lo <= n <= hi:
        errors.append(f"{label} must be between {lo} and {hi}.")
    return n


URL_RE = re.compile(r"^https?://[^\s.]+\.\S+")


def similarity(a, b):
    """Word-set overlap (0..1). Catches copy-paste with a few words changed."""
    wa, wb = set(re.findall(r"[a-z0-9]+", a.lower())), set(re.findall(r"[a-z0-9]+", b.lower()))
    if not wa or not wb:
        return 0
    return len(wa & wb) / len(wa | wb)


@app.route("/task/<int:task_id>/log", methods=["GET", "POST"])
@login_required
def log_task(task_id):
    task = own_task(task_id)
    if task["user_id"] != g.user["id"]:
        return redirect(url_for("history", member=task["user_id"], task=task_id))
    if not task["active"]:
        flash("This task is paused.", "error")
        return redirect(url_for("home"))
    return {"dsa": log_dsa, "money": log_money, "generic": log_generic}[task["kind"]](task)


def after_log_redirect(task, message):
    done = task_status([task], grid_days=1)[0]["done"]
    flash(message + (" Today's target is done! 🎉" if done else ""), "ok")
    return redirect(url_for("home") if done else url_for("log_task", task_id=task["id"]))


def log_dsa(task):
    day = today()
    errors = []
    if request.method == "POST":
        f = {k: text(k) for k in ("problem", "link", "topic", "difficulty", "outcome", "minutes",
                                   "approach", "complexity", "mistakes", "code")}
        min_len(errors, f["problem"], "Problem name", 3)
        if not URL_RE.match(f["link"]):
            errors.append("Problem link must be a real URL (LeetCode / GFG / Codeforces ...).")
        if f["topic"] not in DSA_TOPICS:
            errors.append("Pick a topic.")
        if f["difficulty"] not in DSA_DIFFICULTY:
            errors.append("Pick a difficulty.")
        if f["outcome"] not in DSA_OUTCOMES:
            errors.append("Pick how it went.")
        minutes = int_between(errors, f["minutes"], "Time spent", 5, 600)
        min_len(errors, f["approach"], "Approach explanation", 200)
        if len(f["approach"].split()) < 35:
            errors.append("Approach explanation needs at least 35 words. Explain it like you're teaching someone.")
        if "O(" not in f["complexity"].replace(" ", "").upper():
            errors.append("Complexity must use Big-O, e.g. 'Time O(n log n), Space O(n)'.")
        elif not re.search(r"time", f["complexity"], re.I) or not re.search(r"space", f["complexity"], re.I):
            errors.append("Complexity must cover both time AND space.")
        min_len(errors, f["mistakes"], "Mistakes / learnings", 60)
        past = q("SELECT day, link, approach FROM dsa_logs WHERE task_id = %s", (task["id"],))

        def norm(u):
            return u.rstrip("/?#").lower()

        if any(p["day"] == day and norm(p["link"]) == norm(f["link"]) for p in past):
            errors.append("You already logged this problem today. Solve a different one.")
        if f["approach"] and any(similarity(p["approach"], f["approach"]) >= 0.7 for p in past):
            errors.append("This explanation is nearly identical to an earlier log. Write it fresh.")
        if not errors:
            q("""INSERT INTO dsa_logs (task_id, day, problem, link, topic, difficulty, outcome, minutes,
                                       approach, complexity, mistakes, code)
                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
              (task["id"], day, f["problem"], f["link"], f["topic"], f["difficulty"], f["outcome"], minutes,
               f["approach"], f["complexity"], f["mistakes"], f["code"]))
            return after_log_redirect(task, "Problem logged.")
    logs = q("SELECT * FROM dsa_logs WHERE task_id = %s AND day = %s ORDER BY id", (task["id"], day))
    return render_template("log_dsa.html", task=task, logs=logs, errors=errors, form=request.form,
                           topics=DSA_TOPICS, difficulties=DSA_DIFFICULTY, outcomes=DSA_OUTCOMES)


def log_money(task):
    day = today()
    errors = []
    closed = q1("SELECT * FROM money_days WHERE task_id = %s AND day = %s", (task["id"], day))
    entries = q("SELECT * FROM money_logs WHERE task_id = %s AND day = %s ORDER BY id", (task["id"], day))
    action = request.form.get("action")

    if request.method == "POST" and closed:
        errors.append("Today is already closed. Entries are locked.")
    elif request.method == "POST" and action == "entry":
        direction, category, note, need = text("direction"), text("category"), text("note"), text("need_or_want")
        amount = None
        try:
            amount = round(float(text("amount")), 2)
            if not 0 < amount < 10_000_000:
                errors.append("Amount must be more than 0.")
        except ValueError:
            errors.append("Amount must be a number.")
        if direction not in ("expense", "income"):
            errors.append("Pick expense or income.")
        if category not in MONEY_CATEGORIES:
            errors.append("Pick a category.")
        min_len(errors, note, "Note (what exactly was it for)", 8)
        if direction == "expense" and need not in ("need", "want"):
            errors.append("For an expense, say honestly if it was a need or a want.")
        if not errors:
            q("""INSERT INTO money_logs (task_id, day, direction, amount, category, note, need_or_want)
                 VALUES (%s, %s, %s, %s, %s, %s, %s)""",
              (task["id"], day, direction, amount, category, note, need if direction == "expense" else None))
            flash("Entry added. Add more, or close the day when you're done.", "ok")
            return redirect(url_for("log_task", task_id=task["id"]))
    elif request.method == "POST" and action == "close":
        no_txn = request.form.get("no_transactions") == "on"
        reflection = text("reflection")
        if not entries and not no_txn:
            errors.append("Add today's transactions first, or tick 'I had no transactions today'.")
        if entries and no_txn:
            errors.append("You have entries today, so untick 'no transactions'.")
        min_len(errors, reflection, "Reflection", 40)
        if not errors:
            q("""INSERT INTO money_days (task_id, day, no_transactions, reflection) VALUES (%s, %s, %s, %s)
                 ON CONFLICT DO NOTHING""", (task["id"], day, no_txn, reflection))
            return after_log_redirect(task, "Day closed.")

    spent = sum(e["amount"] for e in entries if e["direction"] == "expense")
    earned = sum(e["amount"] for e in entries if e["direction"] == "income")
    wants = sum(e["amount"] for e in entries if e["need_or_want"] == "want")
    return render_template("log_money.html", task=task, entries=entries, closed=closed, errors=errors,
                           form=request.form, categories=MONEY_CATEGORIES, spent=spent, earned=earned, wants=wants)


def log_generic(task):
    day = today()
    errors = []
    if request.method == "POST":
        summary, details, proof, blockers = text("summary"), text("details"), text("proof"), text("blockers")
        min_len(errors, summary, "Summary", 10)
        min_len(errors, details, "Details", 150)
        minutes = int_between(errors, text("minutes"), "Time spent", 5, 720)
        if proof and not URL_RE.match(proof):
            errors.append("Proof must be a URL (GitHub commit, doc, screenshot link...).")
        past = q("SELECT details FROM task_logs WHERE task_id = %s", (task["id"],))
        if details and any(similarity(p["details"], details) >= 0.7 for p in past):
            errors.append("These details are nearly identical to an earlier log. Write what you did today.")
        if not errors:
            q("""INSERT INTO task_logs (task_id, day, summary, details, minutes, proof, blockers)
                 VALUES (%s, %s, %s, %s, %s, %s, %s)""",
              (task["id"], day, summary, details, minutes, proof, blockers))
            return after_log_redirect(task, "Logged.")
    logs = q("SELECT * FROM task_logs WHERE task_id = %s AND day = %s ORDER BY id", (task["id"], day))
    return render_template("log_generic.html", task=task, logs=logs, errors=errors, form=request.form)


@app.route("/history")
@login_required
def history():
    member_id = request.args.get("member", type=int) if g.user["is_admin"] else g.user["id"]
    member = q1("SELECT * FROM users WHERE id = %s", (member_id,)) if member_id else None
    if member is None:
        return redirect(url_for("home"))
    tasks = q("SELECT * FROM tasks WHERE user_id = %s ORDER BY id", (member["id"],))
    task_id = request.args.get("task", type=int)
    task = next((t for t in tasks if t["id"] == task_id), tasks[0] if tasks else None)
    data = {}
    if task:
        since = today() - timedelta(days=90)
        data["status"] = task_status([task], grid_days=91)[0]
        if task["kind"] == "dsa":
            data["logs"] = q("SELECT * FROM dsa_logs WHERE task_id = %s AND day >= %s ORDER BY id DESC",
                             (task["id"], since))
            data["stats"] = q1("""SELECT COUNT(*) AS n, COALESCE(SUM(minutes), 0) AS mins,
                                         COUNT(*) FILTER (WHERE outcome = %s) AS own
                                  FROM dsa_logs WHERE task_id = %s""", (DSA_OUTCOMES[0], task["id"]))
            data["topics"] = q("""SELECT topic, COUNT(*) AS n FROM dsa_logs WHERE task_id = %s
                                  GROUP BY topic ORDER BY n DESC""", (task["id"],))
        elif task["kind"] == "money":
            data["logs"] = q("SELECT * FROM money_logs WHERE task_id = %s AND day >= %s ORDER BY id DESC",
                             (task["id"], since))
            data["days"] = q("SELECT * FROM money_days WHERE task_id = %s AND day >= %s ORDER BY day DESC",
                             (task["id"], since))
            data["by_category"] = q("""SELECT category, SUM(amount) AS total FROM money_logs
                                       WHERE task_id = %s AND direction = 'expense' AND day >= %s
                                       GROUP BY category ORDER BY total DESC""",
                                    (task["id"], today() - timedelta(days=30)))
        else:
            data["logs"] = q("SELECT * FROM task_logs WHERE task_id = %s AND day >= %s ORDER BY id DESC",
                             (task["id"], since))
    return render_template("history.html", member=member, tasks=tasks, task=task, data=data)


# ---------------------------------------------------------------- admin

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,30}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def new_password():
    words = ["tiger", "rocket", "mango", "pixel", "cobra", "falcon", "maple", "comet", "ninja", "lotus"]
    return f"{secrets.choice(words)}-{secrets.randbelow(9000) + 1000}"


def queue_email(key, to, subject, body, link="", expires_at=None):
    if not to:
        return False
    # Skip the insert when the key exists, so the minute-by-minute reminder check doesn't burn ids.
    if q1("SELECT 1 FROM outbox WHERE dedupe_key = %s", (key,)):
        return True
    added = q("""INSERT INTO outbox (dedupe_key, to_email, subject, body, link, expires_at)
                 VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (dedupe_key) DO NOTHING RETURNING id""",
              (key, to, subject, body, link, expires_at))
    return True


def queue_login_email(member, password):
    tasks = q("SELECT name, remind_at FROM tasks WHERE user_id = %s AND active ORDER BY id", (member["id"],))
    lines = "\n".join(f"  • {t['name']} (reminder at {t['remind_at']} if not logged)" for t in tasks)
    queue_email(
        f"login:{member['id']}:{secrets.token_hex(4)}", member["email"],
        f"Your {APP_NAME} login",
        f"Hi {member['name']},\n\nYou've been set up on {APP_NAME}. Log in and record your progress every day.\n\n"
        f"Website: {BASE_URL}/login\nUsername: {member['username']}\nPassword: {password}\n\n"
        + (f"What you need to log every day:\n{lines}\n\n" if lines else "")
        + "If something isn't logged by its reminder time, you'll get an email.\n",
        f"{BASE_URL}/login")


@app.route("/admin")
@admin_required
def admin_home():
    members = q("SELECT * FROM users WHERE NOT is_admin ORDER BY id")
    summary = []
    for m in members:
        tasks = q("SELECT * FROM tasks WHERE user_id = %s AND active ORDER BY id", (m["id"],))
        summary.append({"member": m, "status": task_status(tasks, grid_days=1)})
    mail = q1(f"""SELECT COUNT(*) FILTER (WHERE {SENDABLE}) AS pending, MAX(sent_at) AS last_sent,
                         (SELECT last_error FROM outbox WHERE sent_at IS NULL AND last_error IS NOT NULL
                          ORDER BY id DESC LIMIT 1) AS last_error
                  FROM outbox""")
    return render_template("admin_home.html", summary=summary, mail=mail, mail_configured=bool(APPS_SCRIPT_URL))


@app.route("/admin/members", methods=["POST"])
@admin_required
def admin_create_member():
    name, email, username = text("name"), text("email"), text("username").lower()
    password = text("password") or new_password()
    errors = []
    min_len(errors, name, "Name", 2)
    if not EMAIL_RE.match(email):
        errors.append("Enter a valid email.")
    if not USERNAME_RE.match(username):
        errors.append("Username: 3-30 characters, lowercase letters, numbers, . _ -")
    elif q1("SELECT 1 FROM users WHERE lower(username) = %s", (username,)):
        errors.append("That username is taken.")
    if len(password) < 6:
        errors.append("Password needs at least 6 characters (or leave it blank to generate one).")
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("admin_home"))
    member = q1("INSERT INTO users (name, email, username, password_hash) VALUES (%s, %s, %s, %s) RETURNING *",
                (name, email, username, hash_password(password)))
    session[f"pw:{member['id']}"] = password  # kept until the login email is sent
    flash(f"{name} added. Next: add what they must log, then send the login email.", "ok")
    return redirect(url_for("admin_member", member_id=member["id"]))


def get_member(member_id):
    member = q1("SELECT * FROM users WHERE id = %s AND NOT is_admin", (member_id,))
    if member is None:
        abort(404)
    return member


@app.route("/admin/members/<int:member_id>", methods=["GET", "POST"])
@admin_required
def admin_member(member_id):
    member = get_member(member_id)
    if request.method == "POST":
        name, email = text("name"), text("email")
        if len(name) < 2 or not EMAIL_RE.match(email):
            flash("Name and a valid email are required.", "error")
        else:
            q("UPDATE users SET name = %s, email = %s, active = %s WHERE id = %s",
              (name, email, request.form.get("active") == "on", member_id))
            flash("Saved.", "ok")
        return redirect(url_for("admin_member", member_id=member_id))
    tasks = q("SELECT * FROM tasks WHERE user_id = %s ORDER BY active DESC, id", (member_id,))
    return render_template("admin_member.html", member=member, tasks=tasks,
                           status={s["task"]["id"]: s for s in task_status(tasks)},
                           fresh_password=session.get(f"pw:{member_id}"))


@app.route("/admin/members/<int:member_id>/send-login", methods=["POST"])
@admin_required
def admin_send_login(member_id):
    """Email login details: the password set at creation if we still have it, otherwise a fresh one."""
    member = get_member(member_id)
    password = session.pop(f"pw:{member_id}", None)
    if password is None:
        password = new_password()
        q("UPDATE users SET password_hash = %s WHERE id = %s", (hash_password(password), member_id))
    q("UPDATE users SET login_sent_at = now() WHERE id = %s", (member_id,))
    queue_login_email(member, password)
    send_now()
    flash(f"Login email for {member['email']} queued (password: {password}). "
          "The Email section on the Members page shows whether it was sent.", "ok")
    return redirect(url_for("admin_member", member_id=member_id))


@app.route("/admin/members/<int:member_id>/delete", methods=["POST"])
@admin_required
def admin_delete_member(member_id):
    member = get_member(member_id)
    q("DELETE FROM users WHERE id = %s", (member_id,))
    flash(f"Deleted {member['name']} and all their logs.", "ok")
    return redirect(url_for("admin_home"))


def read_task_form(errors, creating):
    f = {"name": text("name"), "description": text("description"), "remind_at": text("remind_at"),
         "kind": text("kind")}
    min_len(errors, f["name"], "Task name", 2)
    if not TIME_RE.match(f["remind_at"]):
        errors.append("Reminder time must be HH:MM (24h).")
    if creating and f["kind"] not in KINDS:
        errors.append("Pick a task type.")
    f["min_count"] = int_between(errors, text("min_count") or "1", "Logs per day", 1, 20)
    return f


@app.route("/admin/members/<int:member_id>/tasks", methods=["POST"])
@admin_required
def admin_add_task(member_id):
    get_member(member_id)
    errors = []
    f = read_task_form(errors, creating=True)
    for e in errors:
        flash(e, "error")
    if not errors:
        q("""INSERT INTO tasks (user_id, name, kind, description, remind_at, min_count, start_day)
             VALUES (%s, %s, %s, %s, %s, %s, %s)""",
          (member_id, f["name"], f["kind"], f["description"], f["remind_at"], f["min_count"], today()))
        flash(f"Added '{f['name']}'.", "ok")
    return redirect(url_for("admin_member", member_id=member_id))


@app.route("/admin/tasks/<int:task_id>", methods=["POST"])
@admin_required
def admin_update_task(task_id):
    task = q1("SELECT * FROM tasks WHERE id = %s", (task_id,))
    if task is None:
        abort(404)
    if request.form.get("action") == "delete":
        q("DELETE FROM tasks WHERE id = %s", (task_id,))
        flash(f"Deleted '{task['name']}' and its logs.", "ok")
    else:
        errors = []
        f = read_task_form(errors, creating=False)
        for e in errors:
            flash(e, "error")
        if not errors:
            q("""UPDATE tasks SET name = %s, description = %s, remind_at = %s, min_count = %s, active = %s
                 WHERE id = %s""",
              (f["name"], f["description"], f["remind_at"], f["min_count"],
               request.form.get("active") == "on", task_id))
            flash("Task saved.", "ok")
    return redirect(url_for("admin_member", member_id=task["user_id"]))


@app.route("/admin/test-email", methods=["POST"])
@admin_required
def admin_test_email():
    to = text("email")
    if not EMAIL_RE.match(to):
        flash("Enter a valid email address.", "error")
    else:
        queue_email(f"test:{secrets.token_hex(6)}", to, f"{APP_NAME}: test email",
                    "If you can read this, the Apps Script mailer is working.", f"{BASE_URL}/")
        send_now()
        flash(f"Test email to {to} queued. See below whether it was sent.", "ok")
    return redirect(url_for("admin_home"))


# ---------------------------------------------------------------- email delivery (via Google Apps Script web app)

MAX_ATTEMPTS = 10
SENDABLE = f"sent_at IS NULL AND attempts < {MAX_ATTEMPTS} AND (expires_at IS NULL OR expires_at > now())"


def queue_due_reminders():
    """Queue one reminder per member per reminder slot, listing only tasks not yet logged today."""
    now = now_local()
    day, hm = now.date(), now.strftime("%H:%M")
    end_of_day = datetime.combine(day + timedelta(days=1), datetime.min.time(), TZ)
    # No reminders before the member has been sent their login details.
    for m in q("SELECT * FROM users WHERE active AND NOT is_admin AND email <> '' AND login_sent_at IS NOT NULL"):
        due = q("""SELECT * FROM tasks WHERE user_id = %s AND active AND start_day <= %s AND remind_at <= %s
                   ORDER BY remind_at, id""", (m["id"], day, hm))
        pending = [s for s in task_status(due, grid_days=1) if not s["done"]]
        if not pending:
            continue
        # A slot is the latest reminder time that has passed: logging one task doesn't trigger a new email,
        # but a later reminder time does (if something is still pending then).
        slot = max(t["remind_at"] for t in due)
        lines = "\n".join(f"  • {s['task']['name']}: {s['count']}/{s['required']} logged" for s in pending)
        queue_email(
            f"remind:{m['id']}:{day}:{slot}", m["email"],
            "⏰ Not logged yet today: " + ", ".join(s["task"]["name"] for s in pending),
            f"Hi {m['name']},\n\nYou haven't logged these for today ({day:%d %b}):\n{lines}\n\n"
            "Take a few minutes and log it now.\n",
            f"{BASE_URL}/", expires_at=end_of_day,
        )


def post_to_apps_script(msg):
    payload = json.dumps({"to": msg["to_email"], "subject": msg["subject"], "body": msg["body"],
                          "link": msg["link"]}).encode()
    # Apps Script answers a POST with a redirect to the result; both fetch and urllib follow it with a GET.
    if IN_WORKER:
        from pyodide.ffi import run_sync
        from pyodide.http import pyfetch

        res = run_sync(pyfetch(APPS_SCRIPT_URL, method="POST", headers={"Content-Type": "application/json"},
                               body=payload.decode()))
        text_body = run_sync(res.string())
    else:
        req = urllib.request.Request(APPS_SCRIPT_URL, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as res:
            text_body = res.read().decode("utf-8", "replace")
    try:
        result = json.loads(text_body)
    except ValueError:
        raise RuntimeError("Apps Script didn't return JSON. Is the web app deployed with access 'Anyone'? "
                           + re.sub(r"\s+", " ", text_body)[:150])
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "Apps Script reported a failure")


def deliver_outbox():
    """Send every waiting email. Each row is claimed atomically, so running this twice never double-sends."""
    tried = [0]
    sent = 0
    while True:
        msg = q1(f"""UPDATE outbox SET sent_at = now(), attempts = attempts + 1
                     WHERE id = (SELECT id FROM outbox WHERE {SENDABLE} AND id <> ALL(%s)
                                 ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
                     RETURNING *""", (tried,))
        if msg is None:
            return sent
        tried.append(msg["id"])
        try:
            post_to_apps_script(msg)
            q("UPDATE outbox SET last_error = NULL WHERE id = %s", (msg["id"],))
            sent += 1
            app.logger.info("mailer: sent #%s to %s", msg["id"], msg["to_email"])
        except Exception as exc:
            q("UPDATE outbox SET sent_at = NULL, last_error = %s WHERE id = %s", (str(exc)[:500], msg["id"]))
            app.logger.warning("mailer: #%s failed (attempt %s): %s", msg["id"], msg["attempts"], exc)


def run_scheduled_jobs():
    """Called by the Worker's cron trigger every minute."""
    with app.app_context():
        queue_due_reminders()
        if APPS_SCRIPT_URL:
            deliver_outbox()


def send_now():
    """Try to deliver right away (login/test emails); the cron retries anything that fails."""
    if APPS_SCRIPT_URL:
        try:
            deliver_outbox()
        except Exception as exc:
            app.logger.warning("mailer: %s", exc)


@app.route("/api/run-jobs", methods=["GET", "POST"])
def api_run_jobs():
    """Queue due reminders and send queued email. Called by the Cloudflare cron and, as a backup that
    doesn't depend on Cloudflare's cron actually firing, by a time trigger in the Apps Script."""
    secret = setting("JOBS_SECRET")
    given = request.headers.get("X-Jobs-Secret") or request.args.get("key", "")
    if not secret or not secrets.compare_digest(given, secret):
        abort(401)
    queue_due_reminders()
    sent = deliver_outbox() if APPS_SCRIPT_URL else 0
    waiting = q1(f"SELECT COUNT(*) AS n FROM outbox WHERE {SENDABLE}")["n"]
    return {"ok": True, "sent": sent, "waiting": waiting}


@app.route("/healthz")
def healthz():
    q("SELECT 1")
    return "ok"


def set_password(username, password):
    conn = connect()
    rows = run(conn, "UPDATE users SET password_hash = %s WHERE lower(username) = lower(%s) RETURNING username",
               (hash_password(password), username))
    conn.close()
    print(f"password updated for '{username}'" if rows else f"no user '{username}'")


if __name__ == "__main__":
    configure()
    if sys.argv[1:] == ["init-db"]:
        init_db()
    elif len(sys.argv) == 3 and sys.argv[1] == "set-password":
        import getpass
        set_password(sys.argv[2], os.environ.get("NEW_PASSWORD") or getpass.getpass("New password: "))
    else:
        print("usage: python src/app.py init-db\n"
              "       python src/app.py set-password USERNAME   (NEW_PASSWORD env or prompt)\n"
              "Reads DATABASE_URL, ADMIN_*, MEMBER_* from the environment.")
