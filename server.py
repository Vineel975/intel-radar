"""
Intel Radar — Competitive Intelligence Agent
Railway-ready: Flask serves both frontend HTML and REST API from one process.
"""

import sqlite3, json, os, smtplib, logging, hashlib, threading, re
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
import requests
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ── CONFIG ─────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')
SMTP_HOST     = os.getenv('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT     = int(os.getenv('SMTP_PORT', 587))
SMTP_USER     = os.getenv('SMTP_USER', '')
SMTP_PASS     = os.getenv('SMTP_PASS', '')
SLACK_WEBHOOK = os.getenv('SLACK_WEBHOOK', '')
ALERT_EMAIL   = os.getenv('ALERT_EMAIL', '')
PORT          = int(os.getenv('PORT', 8080))
DB_PATH       = os.getenv('DB_PATH', '/tmp/intel_radar.db')

# Absolute path to the directory where server.py lives
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── APP ────────────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=BASE_DIR, static_url_path='')
CORS(app, resources={r"/*": {"origins": "*"}})

# ── DATABASE ───────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS competitors (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT NOT NULL UNIQUE,
            domain    TEXT,
            enabled   INTEGER DEFAULT 1,
            color     TEXT DEFAULT '#60a5fa',
            initials  TEXT,
            created   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            comp_id     INTEGER REFERENCES competitors(id),
            hash        TEXT UNIQUE,
            type        TEXT,
            title       TEXT,
            summary     TEXT,
            implication TEXT,
            urgency     TEXT,
            source      TEXT,
            url         TEXT,
            seen        INTEGER DEFAULT 0,
            created     TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS runs (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            comp_id   INTEGER REFERENCES competitors(id),
            status    TEXT,
            new_count INTEGER DEFAULT 0,
            total     INTEGER DEFAULT 0,
            error     TEXT,
            created   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            type    TEXT,
            message TEXT,
            created TEXT DEFAULT (datetime('now'))
        );
        """)
        for name, domain, color, initials in [
            ('OpenAI',    'openai.com',    '#60a5fa', 'OA'),
            ('Google',    'google.com',    '#4ade80', 'GO'),
            ('Microsoft', 'microsoft.com', '#a78bfa', 'MS'),
            ('Meta',      'meta.com',      '#fb923c', 'ME'),
        ]:
            db.execute(
                "INSERT OR IGNORE INTO competitors (name,domain,color,initials) VALUES (?,?,?,?)",
                (name, domain, color, initials)
            )
        for k, v in [('schedule_interval','360'),('email_alerts','false'),
                     ('slack_alerts','false'),('alert_urgency','high')]:
            db.execute("INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)", (k, v))
        db.commit()
    log.info("DB ready: %s", DB_PATH)

# ── ANTHROPIC ──────────────────────────────────────────────────────────────────
def build_prompt(company):
    today = datetime.now().strftime('%B %d, %Y')
    return f"""You are an elite competitive intelligence analyst. Today is {today}.

Analyze "{company}" and generate a competitive intelligence report covering their recent activity across news, hiring, products, strategy, and funding.

Return ONLY a raw JSON object — no markdown, no code fences, no explanation before or after:

{{
  "summary": "2-3 sentence executive brief of the most important recent developments",
  "signals": [
    {{
      "type": "news",
      "title": "Short descriptive title under 12 words",
      "summary": "2-3 sentences describing what happened and why it matters",
      "implication": "Specific actionable implication for competitors",
      "urgency": "high",
      "source": "e.g. TechCrunch, LinkedIn Jobs, Official Blog, Reuters",
      "url": "#"
    }}
  ]
}}

Types must be one of: news, job, product, strategic, funding
Urgency must be one of: high, medium, low

Include exactly 6 signals covering a mix of: recent announcements, hiring trends that reveal strategy, product launches or updates, partnerships or acquisitions, and any funding activity.

Return ONLY the JSON object. Nothing else."""

def call_anthropic(company):
    resp = requests.post(
        'https://api.anthropic.com/v1/messages',
        json={
            'model': 'claude-sonnet-4-5-20251001',
            'max_tokens': 2000,
            'messages': [{'role': 'user', 'content': build_prompt(company)}]
        },
        headers={
            'x-api-key': ANTHROPIC_API_KEY,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        },
        timeout=90
    )
    resp.raise_for_status()
    data = resp.json()
    log.info("Anthropic response id: %s, stop_reason: %s", data.get('id'), data.get('stop_reason'))
    text = ' '.join(b['text'] for b in data.get('content', []) if b.get('type') == 'text')
    log.info("Raw text length: %d", len(text))
    m = re.search(r'\{[\s\S]*\}', text.replace('```json','').replace('```','').strip())
    if not m:
        raise ValueError("No JSON in response. Raw text: " + text[:300])
    return json.loads(m.group(0))

def signal_hash(comp_id, title):
    return hashlib.md5(f"{comp_id}:{title}".encode()).hexdigest()

# ── SCAN ───────────────────────────────────────────────────────────────────────
def run_scan(comp_id=None):
    with get_db() as db:
        q = "SELECT * FROM competitors WHERE enabled=1" + (" AND id=?" if comp_id else "")
        rows = db.execute(q, ([comp_id] if comp_id else [])).fetchall()
    for row in rows:
        _scan_one(dict(row))

def _scan_one(comp):
    log.info("Scanning: %s", comp['name'])
    with get_db() as db:
        cur = db.execute("INSERT INTO runs (comp_id,status) VALUES (?,'running')", (comp['id'],))
        run_id = cur.lastrowid
        db.commit()
    try:
        result   = call_anthropic(comp['name'])
        signals  = result.get('signals', [])
        new_sigs = []
        with get_db() as db:
            for s in signals:
                h = signal_hash(comp['id'], s.get('title',''))
                if not db.execute("SELECT id FROM signals WHERE hash=?", (h,)).fetchone():
                    db.execute(
                        "INSERT INTO signals (comp_id,hash,type,title,summary,implication,urgency,source,url) VALUES (?,?,?,?,?,?,?,?,?)",
                        (comp['id'],h,s.get('type','news'),s.get('title',''),s.get('summary',''),
                         s.get('implication',''),s.get('urgency','low'),s.get('source',''),s.get('url',''))
                    )
                    new_sigs.append(s)
            db.execute("UPDATE runs SET status='done',new_count=?,total=? WHERE id=?",
                       (len(new_sigs), len(signals), run_id))
            db.commit()
        log.info("  %s → %d new / %d total", comp['name'], len(new_sigs), len(signals))
        if new_sigs:
            _send_alerts(comp['name'], new_sigs)
    except Exception as e:
        log.error("Scan error %s: %s", comp['name'], e)
        with get_db() as db:
            db.execute("UPDATE runs SET status='error',error=? WHERE id=?", (str(e), run_id))
            db.commit()

# ── ALERTS ─────────────────────────────────────────────────────────────────────
def _send_alerts(comp_name, signals):
    with get_db() as db:
        cfg = {r['key']: r['value'] for r in db.execute("SELECT * FROM settings").fetchall()}
    rank    = {'high':3,'medium':2,'low':1}
    min_r   = rank.get(cfg.get('alert_urgency','high'), 3)
    filtered = [s for s in signals if rank.get(s.get('urgency','low'),1) >= min_r]
    if not filtered: return
    if cfg.get('email_alerts') == 'true' and SMTP_USER and ALERT_EMAIL:
        _email(comp_name, filtered)
    if cfg.get('slack_alerts') == 'true' and SLACK_WEBHOOK:
        _slack(comp_name, filtered)
    with get_db() as db:
        for s in filtered:
            db.execute("INSERT INTO notifications (type,message) VALUES (?,?)",
                       ('alert', f"[{comp_name}] {s.get('title','')} ({s.get('urgency','')})"))
        db.commit()

def _email(comp_name, signals):
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"Intel Radar: {len(signals)} new signal(s) — {comp_name}"
        msg['From'] = SMTP_USER
        msg['To']   = ALERT_EMAIL
        rows = ''.join(
            f"<tr><td style='padding:12px;border-bottom:1px solid #222;'>"
            f"<b style='color:#f0efe8'>{s.get('title','')}</b><br>"
            f"<span style='color:#8e8d86;font-size:13px'>{s.get('summary','')}</span><br>"
            f"<span style='color:#60a5fa;font-size:12px'>→ {s.get('implication','')}</span>"
            f"</td></tr>" for s in signals
        )
        msg.attach(MIMEText(
            f"<div style='background:#0a0a0b;padding:24px;font-family:sans-serif'>"
            f"<h2 style='color:#e8e4d0'>Intel Radar — {comp_name}</h2>"
            f"<table style='width:100%;border-collapse:collapse;background:#111'>{rows}</table></div>",
            'html'
        ))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls(); s.login(SMTP_USER, SMTP_PASS); s.send_message(msg)
        log.info("Email sent for %s", comp_name)
    except Exception as e:
        log.error("Email error: %s", e)

def _slack(comp_name, signals):
    try:
        blocks = [{"type":"header","text":{"type":"plain_text","text":f"Intel Radar: {len(signals)} signals — {comp_name}"}},{"type":"divider"}]
        for s in signals[:5]:
            emoji = {'high':'🔴','medium':'🟡','low':'⚪'}.get(s.get('urgency','low'),'⚪')
            blocks += [{"type":"section","text":{"type":"mrkdwn","text":f"*{s.get('title','')}*\n{emoji} `{s.get('type','').upper()}` `{s.get('urgency','').upper()}`\n{s.get('summary','')}\n_→ {s.get('implication','')}_"}},{"type":"divider"}]
        requests.post(SLACK_WEBHOOK, json={"blocks": blocks}, timeout=10)
    except Exception as e:
        log.error("Slack error: %s", e)

# ── SCHEDULER ──────────────────────────────────────────────────────────────────
scheduler = BackgroundScheduler()

def setup_scheduler():
    with get_db() as db:
        row  = db.execute("SELECT value FROM settings WHERE key='schedule_interval'").fetchone()
        mins = int(row['value']) if row else 360
    scheduler.add_job(run_scan, 'interval', minutes=mins, id='auto_scan', replace_existing=True)
    if not scheduler.running:
        scheduler.start()
    log.info("Scheduler: every %d min", mins)

def reschedule(mins):
    scheduler.reschedule_job('auto_scan', trigger='interval', minutes=int(mins))

# ── FRONTEND ───────────────────────────────────────────────────────────────────
@app.get('/')
def serve_index():
    return send_from_directory(BASE_DIR, 'index.html')

# ── API ROUTES ─────────────────────────────────────────────────────────────────
@app.get('/api/competitors')
def get_competitors():
    with get_db() as db:
        rows = db.execute("""
            SELECT c.*, COUNT(DISTINCT s.id) AS signal_count,
                   SUM(CASE WHEN s.seen=0 THEN 1 ELSE 0 END) AS unseen_count,
                   MAX(s.created) AS last_signal
            FROM competitors c LEFT JOIN signals s ON s.comp_id=c.id
            GROUP BY c.id ORDER BY c.name
        """).fetchall()
    return jsonify([dict(r) for r in rows])

@app.post('/api/competitors')
def add_competitor():
    d = request.json or {}
    name = d.get('name','').strip()
    if not name: return jsonify({'error':'Name required'}),400
    palette = ['#60a5fa','#4ade80','#a78bfa','#fb923c','#f87171','#34d399','#fbbf24']
    with get_db() as db:
        n = db.execute("SELECT COUNT(*) FROM competitors").fetchone()[0]
        db.execute("INSERT INTO competitors (name,domain,color,initials) VALUES (?,?,?,?)",
                   (name, d.get('domain', name.lower().replace(' ','')+'.com'),
                    palette[n % len(palette)], name[:2].upper()))
        db.commit()
        row = db.execute("SELECT * FROM competitors WHERE name=?", (name,)).fetchone()
    return jsonify(dict(row)), 201

@app.delete('/api/competitors/<int:cid>')
def delete_competitor(cid):
    with get_db() as db:
        db.execute("DELETE FROM competitors WHERE id=?", (cid,))
        db.execute("DELETE FROM signals WHERE comp_id=?", (cid,))
        db.commit()
    return jsonify({'ok':True})

@app.patch('/api/competitors/<int:cid>')
def toggle_competitor(cid):
    d = request.json or {}
    with get_db() as db:
        db.execute("UPDATE competitors SET enabled=? WHERE id=?", (1 if d.get('enabled') else 0, cid))
        db.commit()
    return jsonify({'ok':True})

@app.get('/api/signals')
def get_signals():
    comp_id  = request.args.get('comp_id')
    sig_type = request.args.get('type')
    urgency  = request.args.get('urgency')
    since    = request.args.get('since')
    limit    = int(request.args.get('limit',100))
    offset   = int(request.args.get('offset',0))
    q = "SELECT s.*,c.name AS comp_name,c.color FROM signals s JOIN competitors c ON c.id=s.comp_id WHERE 1=1"
    p = []
    if comp_id:  q += " AND s.comp_id=?";  p.append(comp_id)
    if sig_type: q += " AND s.type=?";     p.append(sig_type)
    if urgency:  q += " AND s.urgency=?";  p.append(urgency)
    if since:    q += " AND s.created>=?"; p.append(since)
    q += " ORDER BY s.created DESC LIMIT ? OFFSET ?"; p += [limit, offset]
    with get_db() as db:
        rows  = db.execute(q, p).fetchall()
        total = db.execute("SELECT COUNT(*) FROM signals" + (" WHERE comp_id=?" if comp_id else ""),
                           ([comp_id] if comp_id else [])).fetchone()[0]
    return jsonify({'signals':[dict(r) for r in rows],'total':total})

@app.post('/api/signals/<int:sid>/seen')
def mark_seen(sid):
    with get_db() as db:
        db.execute("UPDATE signals SET seen=1 WHERE id=?", (sid,)); db.commit()
    return jsonify({'ok':True})

@app.post('/api/signals/seen-all')
def mark_all_seen():
    d = request.json or {}
    with get_db() as db:
        if d.get('comp_id'): db.execute("UPDATE signals SET seen=1 WHERE comp_id=?", (d['comp_id'],))
        else:                 db.execute("UPDATE signals SET seen=1")
        db.commit()
    return jsonify({'ok':True})

@app.post('/api/scan')
def trigger_scan():
    d = request.json or {}
    threading.Thread(target=run_scan, args=(d.get('comp_id'),), daemon=True).start()
    return jsonify({'ok':True,'message':'Scan started'})

@app.get('/api/runs')
def get_runs():
    comp_id = request.args.get('comp_id')
    q = "SELECT r.*,c.name AS comp_name FROM runs r JOIN competitors c ON c.id=r.comp_id"
    p = []
    if comp_id: q += " WHERE r.comp_id=?"; p.append(comp_id)
    q += " ORDER BY r.created DESC LIMIT 50"
    with get_db() as db:
        rows = db.execute(q, p).fetchall()
    return jsonify([dict(r) for r in rows])

@app.get('/api/stats')
def get_stats():
    with get_db() as db:
        total  = db.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        today  = db.execute("SELECT COUNT(*) FROM signals WHERE date(created)=date('now')").fetchone()[0]
        high   = db.execute("SELECT COUNT(*) FROM signals WHERE urgency='high' AND seen=0").fetchone()[0]
        comps  = db.execute("SELECT COUNT(*) FROM competitors WHERE enabled=1").fetchone()[0]
        by_t   = db.execute("SELECT type, COUNT(*) c FROM signals GROUP BY type").fetchall()
        by_u   = db.execute("SELECT urgency, COUNT(*) c FROM signals GROUP BY urgency").fetchall()
        by_c   = db.execute("SELECT c.name,COUNT(s.id) c FROM competitors c LEFT JOIN signals s ON s.comp_id=c.id GROUP BY c.id ORDER BY c DESC").fetchall()
        last_r = db.execute("SELECT MAX(created) FROM runs WHERE status='done'").fetchone()[0]
        trend  = db.execute("SELECT date(created) day,COUNT(*) c FROM signals WHERE created>=date('now','-7 days') GROUP BY day ORDER BY day").fetchall()
    return jsonify({'total_signals':total,'new_today':today,'high_urgency':high,'total_comps':comps,
                    'by_type':[dict(r) for r in by_t],'by_urgency':[dict(r) for r in by_u],
                    'by_comp':[dict(r) for r in by_c],'last_run':last_r,
                    'trend_7d':[dict(r) for r in trend]})

@app.get('/api/settings')
def get_settings():
    with get_db() as db:
        rows = db.execute("SELECT key,value FROM settings").fetchall()
    return jsonify({r['key']:r['value'] for r in rows})

@app.post('/api/settings')
def save_settings():
    d = request.json or {}
    with get_db() as db:
        for k,v in d.items():
            db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (k,str(v)))
        db.commit()
    if 'schedule_interval' in d: reschedule(d['schedule_interval'])
    return jsonify({'ok':True})

@app.get('/api/notifications')
def get_notifications():
    with get_db() as db:
        rows = db.execute("SELECT * FROM notifications ORDER BY created DESC LIMIT 50").fetchall()
    return jsonify([dict(r) for r in rows])

@app.get('/api/health')
def health():
    return jsonify({'status':'ok','time':datetime.now().isoformat()})

@app.get('/api/debug')
def debug():
    """Check server config — visit this URL to diagnose issues."""
    with get_db() as db:
        comp_count = db.execute("SELECT COUNT(*) FROM competitors").fetchone()[0]
        sig_count  = db.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        runs       = db.execute("SELECT * FROM runs ORDER BY created DESC LIMIT 3").fetchall()
    return jsonify({
        'api_key_set':   bool(ANTHROPIC_API_KEY),
        'api_key_prefix': ANTHROPIC_API_KEY[:12] + '...' if ANTHROPIC_API_KEY else 'NOT SET',
        'db_path':       DB_PATH,
        'competitors':   comp_count,
        'signals':       sig_count,
        'recent_runs':   [dict(r) for r in runs],
        'base_dir':      BASE_DIR,
    })

# ── BOOT ───────────────────────────────────────────────────────────────────────
# Run at module level so gunicorn triggers DB init (not just __main__)
init_db()
setup_scheduler()

if __name__ == '__main__':
    log.info("Intel Radar on port %d", PORT)
    app.run(host='0.0.0.0', port=PORT, debug=False)
