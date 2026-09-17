# Daily Tracker

A small website where you (admin) add your brother, set the things he must log every day
(DSA, money, anything else), and he logs them. He gets his login details by email, and a reminder
**only if he hasn't logged by the reminder time**.

```
 browser ──► Cloudflare Worker (Python: Flask + templates)
                 │          └── cron, every minute: reminders due & not logged? ──POST──► Apps Script web app ──► Gmail
                 ▼
             Hyperdrive ──► Supabase Postgres
```

- **Cloudflare Python Worker** runs the Flask app (`src/app.py`, `src/worker.py`) and a cron trigger.
- **Hyperdrive** keeps pooled connections to **Supabase**. The database password is stored in the Hyperdrive
  config, never in code or the browser.
- **Google Apps Script** (`apps-script/Code.gs`), deployed as a web app, sends the emails from your Gmail.

> **Plan:** Python Workers are much heavier than JavaScript. The **Free plan's 10 ms CPU limit** per request is
> very likely to fail (error 1102), especially on login, which runs PBKDF2. Use **Workers Paid ($5/month)**.
> Python Workers + Hyperdrive support is in beta (Sept 2026).

## One-time setup

Prerequisites: Node 20+, and [uv](https://docs.astral.sh/uv/) **0.12.3 or newer** (`uv self update`).

```bash
npm install
cp .env.example .env          # fill in (local only, git-ignored)
uv run python src/app.py init-db    # creates tables + admin + Anubhav in Supabase
npx wrangler login
```

### 1. Hyperdrive → Supabase

Use the Supabase **Session pooler** string. **Caching must be off**, otherwise pages can show stale data for up to a minute:

```bash
npx wrangler hyperdrive create anubhav-supabase --caching-disabled \
  --connection-string="postgresql://postgres.PROJECT:PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres"
```

Paste the returned `id` into `wrangler.jsonc` → `hyperdrive[0].id`.

### 2. Secrets

```bash
npx wrangler secret put SECRET_KEY        # long random string (from .env)
npx wrangler secret put APPS_SCRIPT_URL   # Apps Script web app URL (from .env)
```

### 3. Deploy

```bash
npm run deploy
```

It prints `https://anubhav-tracker.<you>.workers.dev`. Put that (or your custom domain) in `wrangler.jsonc` →
`vars.BASE_URL`, since email links use it, then `npm run deploy` again.

## Google Apps Script (the mailer)

1. Go to <https://script.google.com> → **New project**. Paste `apps-script/Code.gs` and, in **Project Settings**
   ("Show appsscript.json"), `apps-script/appsscript.json`. `SENDER_NAME` at the top is what the emails show as the sender.
2. Run **`authorize`** once and allow permissions ("unverified app" → Advanced → continue).
3. **Deploy → New deployment → Web app**, *Execute as:* **Me**, *Who has access:* **Anyone**.
4. The web app URL is the `APPS_SCRIPT_URL` secret. **Keep it private:** anyone with it can send email as you.

After editing `Code.gs`: **Deploy → Manage deployments → edit → Version: New version** (same URL).

## Admin flow

1. Log in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`. **Members → Add a member**, or use the seeded Anubhav.
2. **Add what they must log:**
   - **DSA problems:** link, topic, difficulty, honest outcome, time spent, a 200+ char explanation,
     time/space complexity and learnings. Copy-pasted explanations are rejected.
   - **Money log:** each transaction with category, note and need/want, then "close the day" with a reflection.
   - **General task:** a summary, 150+ chars of detail and time spent.

   Each task has a reminder time and logs per day.
3. **Email login details.** It's sent right away (the cron retries if it fails).
4. **Send test email** on the Members page checks the Apps Script link.

### How reminders work
- The cron runs every minute. For each member whose login has been sent, tasks whose reminder time has
  passed and that **aren't logged yet today** get one email listing them.
- One email per reminder time per day. Unsent reminders expire at midnight. Failed sends retry every minute
  (up to 10 times), and the last error shows on the admin page.

## Local development

```bash
cp .dev.vars.example .dev.vars
# point wrangler.jsonc hyperdrive.localConnectionString at a local Postgres, then:
npm run dev                       # http://localhost:8787 (first request is slow while Python boots)
curl "http://localhost:8787/cdn-cgi/handler/scheduled?cron=*+*+*+*+*"   # run the cron once
```

## Notes

- Passwords are PBKDF2-SHA256 with 100k iterations, the most Cloudflare's WebCrypto allows. `scrypt` isn't
  available in Workers. To reset one: `uv run python src/app.py set-password USERNAME`.
- The tables have Row Level Security on, so Supabase's public API keys can't read them.
- Backup: `pg_dump "$DATABASE_URL" > backup.sql` (Postgres client tools).
