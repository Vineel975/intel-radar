# 🔍 Intel Radar — Competitive Intelligence Agent

Autonomous agent that monitors competitors (news, jobs, products, strategy) and alerts you via email/Slack.

## Deploy to Railway in 5 minutes

### Step 1 — Push to GitHub

```bash
git init
git add .
git commit -m "Intel Radar"
git remote add origin https://github.com/YOUR_USERNAME/intel-radar.git
git push -u origin main
```

### Step 2 — Deploy on Railway

1. Go to **railway.app** → sign in with GitHub
2. Click **New Project** → **Deploy from GitHub repo**
3. Select your `intel-radar` repo
4. Railway auto-detects Python and deploys ✅

### Step 3 — Add environment variables

In Railway dashboard → your service → **Variables** tab, add:

| Variable | Value |
|---|---|
| `ANTHROPIC_API_KEY` | `sk-ant-...` (required) |
| `SMTP_USER` | your Gmail address (optional) |
| `SMTP_PASS` | Gmail App Password (optional) |
| `ALERT_EMAIL` | where to send alerts (optional) |
| `SLACK_WEBHOOK` | Slack webhook URL (optional) |

### Step 4 — Get your URL

Railway → your service → **Settings** → **Domains** → Generate Domain

Share `https://your-app.railway.app` with your team. Done.

---

## Local development

```bash
pip install -r requirements.txt
cp .env.example .env   # add your ANTHROPIC_API_KEY
python server.py
# open http://localhost:8080
```

## Architecture

- **server.py** — Flask app: serves the frontend + REST API + APScheduler for auto-scans
- **index.html** — Full dashboard UI (no build step, no Node.js)
- **SQLite** — Embedded DB, no external database needed
- **Gunicorn** — Production WSGI server (1 worker, 4 threads — keeps scheduler alive)

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required. Get from console.anthropic.com |
| `PORT` | `8080` | Auto-set by Railway |
| `DB_PATH` | `/tmp/intel.db` | Attach a Railway Volume + set this for persistence |
| `SMTP_HOST` | `smtp.gmail.com` | Email server |
| `SMTP_PORT` | `587` | Email port |
| `SMTP_USER` | — | Gmail address |
| `SMTP_PASS` | — | Gmail App Password |
| `ALERT_EMAIL` | — | Destination for email alerts |
| `SLACK_WEBHOOK` | — | Slack incoming webhook URL |

## Persistent database (optional)

Railway's `/tmp` resets on redeploy. For persistent signals:

1. Railway dashboard → your project → **Add Volume**
2. Mount path: `/data`
3. Add env var: `DB_PATH=/data/intel.db`

Your signals will now survive redeploys.
