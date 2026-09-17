# Daily Tracker

A small website where you (admin) add your brother, set the things he must log every day
(DSA, money, anything else), and he logs them. Google Apps Script emails him his login details, and
emails him a reminder **only if he hasn't logged by the reminder time**.

```
 brother's phone ──► [ app (Flask) ] ──► [ Postgres ]      ← docker compose on your VM
                          ▲
   Google Apps Script ────┘  every 10 min: "anything to email?" → sends via your Gmail → "sent these"
```

## Why Postgres (not MongoDB)

- **RAM:** Postgres idles at ~30–50 MB with the settings in `docker-compose.yml`. MongoDB wants ~150 MB+
  and a WiredTiger cache.
- **ARM / phones:** the Postgres image runs on arm64 and armv7. MongoDB 5+ needs ARMv8.2 or newer (older
  phone/Pi CPUs fail), and its image is ~10× larger.
- **Data shape:** members → tasks → logs is plain relational data.

Measured in testing: **app ~46 MB + db ~49 MB**.

## Run it

```bash
cp .env.example .env      # fill in passwords, BASE_URL, MAIL_SECRET
docker compose up -d --build
```

Open `BASE_URL` and log in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`.

On first start, Anubhav (`MEMBER_*` in `.env`) is created with **DSA practice** (2/day, reminder 20:00) and
**Money log** (reminder 21:30). Change these on his admin page, then click **Email login details**.

The site must be reachable from the internet (his phone and Google Apps Script both call it). Options:
- The VM has a public IP: open `PORT` (put Caddy in front if you want HTTPS).
- It doesn't (home network, Android box): use the included Cloudflare Tunnel. Create a tunnel in the
  Cloudflare dashboard pointing to `http://app:8000`, put its token in `CLOUDFLARE_TUNNEL_TOKEN`, then
  `docker compose --profile tunnel up -d`.

> **About Android:** Docker doesn't run natively in Termux. You need a Linux VM on the device (or a rooted
> device with Docker). If that's a pain, any free-tier ARM VM (Oracle Cloud) or a Raspberry Pi works with the
> exact same files.

## Admin flow

1. **Members → Add a member:** name, email, username, password (leave blank to generate one).
2. On their page, **add what they must log:**
   - **DSA problems:** each problem needs a link, topic, difficulty, an honest outcome, time spent, a 200+ char
     explanation, time/space complexity and learnings. Copy-pasted explanations are rejected.
   - **Money log:** each transaction with category, note and need/want, then "close the day" with a reflection.
   - **General task:** a summary, 150+ chars of detail and time spent.

   Each task has a **reminder time** and **logs per day**.
3. **Send login email.** It's queued, and Apps Script sends it within 10 minutes.
4. **View their logs** any time. Pausing a task stops its reminders.

## Google Apps Script (the mailer)

1. Go to <https://script.google.com> → **New project**.
2. Paste `apps-script/Code.gs`. In **Project Settings**, tick "Show appsscript.json" and paste
   `apps-script/appsscript.json`.
3. In **Project Settings → Script Properties**, add:
   - `SERVER_URL` = your `BASE_URL`
   - `MAIL_SECRET` = same as in `.env`
   - `SENDER_NAME` (optional) = e.g. `Anubhav's Tracker`
4. Run **`setup`** and allow permissions ("unverified app" → Advanced → continue; it's your own script).
   This creates the 10-minute trigger and prints a server check.
5. On the admin page, **Queue test email**. It arrives within 10 minutes.

### How reminders work
- Every 10 minutes Apps Script calls `GET /api/mail/outbox`.
- For each member, the server checks tasks whose reminder time has passed and that **aren't logged yet
  today**. If there are any, it queues one email listing them.
- One email per reminder time per day. If he logs at 18:05, nothing more comes that day unless another task
  has a later reminder time and is still pending.
- Unsent reminders expire at midnight, so he never gets yesterday's reminder.
- Apps Script sends the emails and calls `POST /api/mail/ack`, so nothing is sent twice.

Gmail limit: ~100 emails/day on a free account, far more than this needs.

## Backups

```bash
docker compose exec db pg_dump -U tracker tracker > backup-$(date +%F).sql
```
