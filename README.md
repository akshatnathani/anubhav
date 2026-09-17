# Daily Tracker

A small website where you (admin) add your brother, set the things he must log every day
(DSA, money, anything else), and he logs them. He gets his login details by email, and a reminder
**only if he hasn't logged by the reminder time**. Emails go out from your Gmail through a Google Apps
Script web app.

```
 brother's phone ──► [ app (Flask, Docker) ] ──► [ Supabase Postgres ]
                            │
                            └── every minute: reminders due & not logged? ──POST──► [ Apps Script web app ] ──► Gmail
```

The VM only runs one small container (~48 MB RAM). The database lives in Supabase.

## 1. Supabase

1. Create a project (or use the one you were given) and note the **database password**.
2. Click **Connect** (top bar) → **Session pooler** → copy the connection string.
   Use the pooler, not "Direct connection": the direct host is IPv6-only, and Docker usually can't reach it.
3. Put it in `.env` as `DATABASE_URL`, replacing `[YOUR-PASSWORD]`.
   If the password has special characters (`@ : / ? #` …), URL-encode them or pick a simpler password.

On first start the app creates its tables in the `public` schema. It also turns on **Row Level Security** on
every table, so Supabase's public API keys (anon/authenticated) can't read or write them. The app connects
as `postgres` and isn't affected.

Supabase's free tier pauses projects after a week without activity. The app checks for due reminders
every minute, which keeps it active.

## 2. Run the app

```bash
cp .env.example .env      # fill in DATABASE_URL, BASE_URL, ADMIN_PASSWORD, SECRET_KEY, APPS_SCRIPT_URL
docker compose up -d --build
docker compose logs -f app   # should print "database ready"
```

Open `BASE_URL` and log in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`.

On first start, Anubhav (`MEMBER_*` in `.env`) is created with **DSA practice** (2/day, reminder 20:00) and
**Money log** (reminder 21:30). Change these on his admin page, then click **Email login details**.

The site must be reachable from the internet so his phone can open it (Apps Script doesn't need to reach it). Options:
- The VM has a public IP: open `PORT` (put Caddy in front if you want HTTPS).
- It doesn't (home network, Android box): use the included Cloudflare Tunnel. Create a tunnel in the
  Cloudflare dashboard pointing to `http://app:8000`, put its token in `CLOUDFLARE_TUNNEL_TOKEN`, then
  `docker compose --profile tunnel up -d`.

> **About Android:** Docker doesn't run natively in Termux. You need a Linux VM on the device (or a rooted
> device with Docker). If that's a pain, any free-tier ARM VM (Oracle Cloud) or a Raspberry Pi works with the
> exact same files.

## 3. Admin flow

1. **Members → Add a member:** name, email, username, password (leave blank to generate one).
2. On their page, **add what they must log:**
   - **DSA problems:** each problem needs a link, topic, difficulty, an honest outcome, time spent, a 200+ char
     explanation, time/space complexity and learnings. Copy-pasted explanations are rejected.
   - **Money log:** each transaction with category, note and need/want, then "close the day" with a reflection.
   - **General task:** a summary, 150+ chars of detail and time spent.

   Each task has a **reminder time** and **logs per day**.
3. **Email login details.** It goes out within seconds.
4. **View their logs** any time. Pausing a task stops its reminders.

## 4. Google Apps Script (the mailer)

1. Go to <https://script.google.com> → **New project**.
2. Paste `apps-script/Code.gs`. In **Project Settings**, tick "Show appsscript.json" and paste
   `apps-script/appsscript.json`. Optionally change `SENDER_NAME` at the top of `Code.gs`.
3. Pick **`authorize`** in the function dropdown → **Run** → allow the permissions
   ("unverified app" → Advanced → continue; it's your own script).
4. **Deploy → New deployment →** type **Web app**, *Execute as:* **Me**, *Who has access:* **Anyone** → Deploy.
5. Copy the **Web app URL** (`https://script.google.com/macros/s/…/exec`) into `APPS_SCRIPT_URL` in `.env`,
   then run `docker compose up -d` again.
6. On the admin page, click **Send test email**.

Opening the web app URL in a browser shows `{"ok":true,...}` if it's deployed correctly.
**Keep the URL private:** anyone with it can send email from your Gmail. It only lives in `.env`, which git ignores.

If you edit `Code.gs` later: **Deploy → Manage deployments → edit → Version: New version**, so the same URL
serves the new code.

### How reminders work
- Every minute the app checks each member's tasks whose reminder time has passed. If any **aren't logged
  yet today**, it queues one email listing them.
- One email per reminder time per day. If he logs at 20:05, nothing more comes that day unless another task
  has a later reminder time and is still pending.
- Nobody gets reminders until you've sent them their login details.
- Unsent reminders expire at midnight. Failed sends are retried every minute (up to 10 times), and the last
  error shows on the admin page.

Gmail limit: ~100 emails/day on a free account, far more than this needs.

## Backups

Supabase takes daily backups on paid plans. On the free plan, dump it yourself:

```bash
docker run --rm postgres:16-alpine pg_dump "$DATABASE_URL" > backup-$(date +%F).sql
```
