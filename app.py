"""Daily accountability tracker.

Admin adds members (e.g. a brother) and the things they must log every day.
Members log in and log. Emails are not sent from here: a Google Apps Script
polls /api/mail/outbox, sends what it finds from Gmail, and acks it.
"""
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

import click
import psycopg
from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from psycopg.rows import dict_row
from werkzeug.security import check_password_hash, generate_password_hash

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://tracker:tracker@localhost:5432/tracker")
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Kolkata"))
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
MAIL_SECRET = os.environ.get("MAIL_SECRET", "")
CURRENCY = os.environ.get("CURRENCY", "₹")
APP_NAME = os.environ.get("APP_NAME", "Daily Tracker")

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
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
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
"""


def connect():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)


def db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def q(sql, params=None):
    cur = db().execute(sql, params)
    return cur.fetchall() if cur.description else []


def q1(sql, params=None):
    rows = q(sql, params)
    return rows[0] if rows else None


@app.cli.command("init-db")
def init_db_command():
    """Create tables (waiting for Postgres to come up) and the admin account."""
    for _ in range(60):
        try:
            conn = connect()
            break
        except psycopg.OperationalError:
            click.echo("waiting for database...")
            time.sleep(2)
    else:
        raise SystemExit("database never became reachable")
    with conn:
        conn.execute(SCHEMA)
        username = os.environ.get("ADMIN_USERNAME", "admin").lower()
        password = os.environ.get("ADMIN_PASSWORD")
        if not password:
            raise SystemExit("Set ADMIN_PASSWORD in .env")
        if conn.execute("SELECT 1 FROM users WHERE username = %s", (username,)).fetchone() is None:
            conn.execute(
                "INSERT INTO users (name, email, username, password_hash, is_admin) VALUES (%s, %s, %s, %s, TRUE)",
                ("Admin", os.environ.get("ADMIN_EMAIL", ""), username, generate_password_hash(password)),
            )
            click.echo(f"created admin user '{username}'")
        seed_member(conn)
    click.echo("database ready")


def seed_member(conn):
    """Optionally create the first member (with DSA + money tasks) from .env. No email is sent."""
    email = os.environ.get("MEMBER_EMAIL", "").strip()
    username = os.environ.get("MEMBER_USERNAME", "").strip().lower()
    if not email or not username:
        return
    if conn.execute("SELECT 1 FROM users WHERE lower(username) = %s OR lower(email) = lower(%s)",
                    (username, email)).fetchone():
        return
    member = conn.execute(
        "INSERT INTO users (name, email, username, password_hash) VALUES (%s, %s, %s, %s) RETURNING id",
        (os.environ.get("MEMBER_NAME", username), email, username, generate_password_hash(secrets.token_urlsafe(16))),
    ).fetchone()
    conn.execute(
        """INSERT INTO tasks (user_id, name, kind, description, remind_at, min_count, start_day) VALUES
           (%(id)s, 'DSA practice', 'dsa', 'Solve and explain at least 2 problems', '20:00', 2, %(day)s),
           (%(id)s, 'Money log', 'money', 'Log every rupee in or out, then close the day', '21:30', 1, %(day)s)""",
        {"id": member["id"], "day": today()},
    )
    click.echo(f"created member '{username}' <{email}> with DSA + Money tasks "
               "(send the login email from the admin page)")


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
    rows = q(
        " UNION ALL ".join(
            f"SELECT task_id, day, COUNT(*) AS n FROM {table}"
            " WHERE task_id = ANY(%(ids)s) AND day >= %(since)s GROUP BY 1, 2"
            for table in ("dsa_logs", "money_days", "task_logs")
        ),
        {"ids": ids, "since": since},
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
        time.sleep(1)
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
            q("UPDATE users SET password_hash = %s WHERE id = %s", (generate_password_hash(new), g.user["id"]))
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
    q("""INSERT INTO outbox (dedupe_key, to_email, subject, body, link, expires_at)
         VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (dedupe_key) DO NOTHING""",
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
    mail = q1("""SELECT COUNT(*) FILTER (WHERE sent_at IS NULL AND (expires_at IS NULL OR expires_at > now())) AS pending,
                        MAX(sent_at) AS last_sent FROM outbox""")
    return render_template("admin_home.html", summary=summary, mail=mail, mail_configured=bool(MAIL_SECRET))


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
                (name, email, username, generate_password_hash(password)))
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
        q("UPDATE users SET password_hash = %s WHERE id = %s", (generate_password_hash(password), member_id))
    queue_login_email(member, password)
    q("UPDATE users SET login_sent_at = now() WHERE id = %s", (member_id,))
    flash(f"Login email queued for {member['email']} (password: {password}). "
          "It goes out on the next Apps Script run.", "ok")
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
        flash(f"Test email queued for {to}. It goes out on the next Apps Script run.", "ok")
    return redirect(url_for("admin_home"))


# ---------------------------------------------------------------- mail API (polled by Google Apps Script)


def require_mail_secret():
    if not MAIL_SECRET:
        abort(503, "MAIL_SECRET is not set on the server")
    given = request.headers.get("X-Mail-Secret") or request.args.get("secret", "")
    if not secrets.compare_digest(given, MAIL_SECRET):
        abort(401)


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


@app.route("/api/mail/outbox")
def mail_outbox():
    require_mail_secret()
    queue_due_reminders()
    rows = q("""SELECT id, to_email, subject, body, link FROM outbox
                WHERE sent_at IS NULL AND (expires_at IS NULL OR expires_at > now())
                ORDER BY id LIMIT 50""")
    return jsonify({"messages": [
        {"id": r["id"], "to": r["to_email"], "subject": r["subject"], "body": r["body"], "link": r["link"]}
        for r in rows
    ]})


@app.route("/api/mail/ack", methods=["POST"])
def mail_ack():
    require_mail_secret()
    ids = [int(i) for i in (request.get_json(silent=True) or {}).get("ids", []) if str(i).isdigit()]
    if ids:
        q("UPDATE outbox SET sent_at = now() WHERE id = ANY(%s) AND sent_at IS NULL", (ids,))
    return jsonify({"acked": len(ids)})


@app.route("/healthz")
def healthz():
    q("SELECT 1")
    return "ok"
