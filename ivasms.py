from __future__ import annotations

"""
╔══════════════════════════════════════════════════════════════════════╗
║          iVASMS MASTER BOT — Multi-User Management System           ║
║                     Cookie-Based Edition                            ║
║                                                                      ║
║  • No login form — uses browser cookies + User-Agent                ║
║  • Accepts raw Cookie header OR Cookie-Editor JSON export           ║
║  • Bypasses Cloudflare Turnstile / managed challenge                ║
║  • Force channel join before access                                 ║
║  • Referral system (2 referrals to unlock)                          ║
║  • 1 personal OTP bot per user, live logs streamed to master        ║
║  • Admin panel with cookie refresh command                          ║
╚══════════════════════════════════════════════════════════════════════╝

Install (Python 3.8 compatible):
  pip install requests beautifulsoup4 "python-socketio[client]" \
              websocket-client==1.6.4 "python-telegram-bot==20.7" httpx==0.25.2

Run:
  python ivasms.py
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta

import requests
import socketio
from bs4 import BeautifulSoup
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ── Optional curl_cffi (TLS impersonation) ─────────────────────────────
try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

# ═══════════════════════════════════════════════════════════════════════
#  ⚙️  MASTER BOT CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════

MASTER_BOT_TOKEN = "8782985435:AAF7VcWRImwUDu73GNjkTqiDvzcSab5efhI"

ADMIN_IDS = [8601255091]

REQUIRED_CHANNELS = [
    {
        "channel_id": "@NumbersChannelOfficial",
        "name": "📢 IVASMS Official",
        "url": "https://t.me/NumbersChannelOfficial",
    },
    {
        "channel_id": "@NumbersChannelOfficial",
        "name": "📢 IVASMS Official",
        "url": "https://t.me/NumbersChannelOfficial",
    },
]

MIN_REFERRALS = 2
POINTS_FIRST_LAUNCH = 2
POINTS_WEEKLY_RENEWAL = 1
BOT_RUNTIME_DAYS = 7
OTP_WAIT_TIMEOUT = 300

DATA_FILE = "master_bot_data.json"
LOG_FILE = "master_bot.log"

# ⚠️  Must match the browser that generated your iVASMS cookies.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36"
)

# TLS impersonation profile for curl_cffi.
IMPERSONATE = "chrome110"

# Socket.IO transports — polling first (reliable), then try websocket upgrade.
SOCKET_TRANSPORTS = ["polling", "websocket"]

# Enable this while debugging socket errors; set to False in production.
SOCKET_DEBUG = True

# ═══════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("master-bot")

DIVIDER = "━" * 34

# ═══════════════════════════════════════════════════════════════════════
#  📦  DATA STORAGE
# ═══════════════════════════════════════════════════════════════════════

_data = {"users": {}}


def load_data():
    global _data
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                _data = json.load(f)
            log.info(f"Loaded data: {len(_data.get('users', {}))} users")
        except Exception as e:
            log.error(f"Failed to load data: {e}")
            _data = {"users": {}}
    else:
        _data = {"users": {}}


def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.error(f"Failed to save data: {e}")


def get_user(uid):
    return _data["users"].get(str(uid))


def set_user(uid, data):
    _data["users"][str(uid)] = data
    save_data()


def create_user(uid, name, username, referred_by=None):
    user = {
        "id": uid,
        "name": name,
        "username": username or "",
        "referred_by": referred_by,
        "referrals": [],
        "joined_channels": False,
        "bot_token": None,
        "ivasms_cookies": None,
        "ivasms_user_agent": None,
        "bot_active": False,
        "setup_step": None,
        "points": 0,
        "banned": False,
        "bot_unlocked": False,
        "bot_expires_at": None,
        "registered_at": datetime.now().isoformat(),
        "bot_created_at": None,
        "cookies_updated_at": None,
    }
    set_user(uid, user)

    if referred_by:
        referrer = get_user(referred_by)
        if referrer and uid not in referrer["referrals"]:
            referrer["referrals"].append(uid)
            set_user(referred_by, referrer)

    return user


def get_referral_count(uid):
    user = get_user(uid)
    if not user:
        return 0
    return len(user.get("referrals", []))


def can_create_bot(uid):
    user = get_user(uid)
    if not user:
        return False
    if not user.get("joined_channels"):
        return False
    if get_referral_count(uid) < MIN_REFERRALS:
        return False
    return True


def has_bot(uid):
    user = get_user(uid)
    return bool(user and user.get("bot_token") and user.get("bot_active"))


def bot_is_expired(uid):
    user = get_user(uid)
    if not user or not user.get("bot_expires_at"):
        return False
    try:
        expires = datetime.fromisoformat(user["bot_expires_at"])
        return datetime.now() > expires
    except Exception:
        return False


def get_expiry_str(uid):
    user = get_user(uid)
    if not user or not user.get("bot_expires_at"):
        return "N/A"
    try:
        expires = datetime.fromisoformat(user["bot_expires_at"])
        delta = expires - datetime.now()
        if delta.total_seconds() <= 0:
            return "⚠️ Expired"
        days = delta.days
        hours = delta.seconds // 3600
        return f"{days}d {hours}h remaining"
    except Exception:
        return "N/A"


def set_bot_expiry(uid):
    user = get_user(uid)
    if not user:
        return
    user["bot_expires_at"] = (datetime.now() + timedelta(days=BOT_RUNTIME_DAYS)).isoformat()
    set_user(uid, user)


# ═══════════════════════════════════════════════════════════════════════
#  🔌  ACTIVE USER BOTS
# ═══════════════════════════════════════════════════════════════════════

active_bots = {}
_master_app = None
_master_loop = None


# ═══════════════════════════════════════════════════════════════════════
#  ⏱  EXPIRY WATCHDOG
# ═══════════════════════════════════════════════════════════════════════

async def expiry_watchdog():
    while True:
        await asyncio.sleep(600)
        for uid in list(active_bots.keys()):
            if bot_is_expired(uid):
                user = get_user(uid)
                if not user:
                    continue
                try:
                    active_bots[uid]["instance"].stop()
                except Exception:
                    pass
                active_bots.pop(uid, None)
                user["bot_active"] = False
                set_user(uid, user)
                log.info(f"Bot expired and stopped for user {uid}")
                if _master_app:
                    try:
                        await _master_app.bot.send_message(
                            uid,
                            "╔══════════════════════════════════╗\n"
                            "║   ⏱  Your Bot Has Expired!       ║\n"
                            "╚══════════════════════════════════╝\n\n"
                            "Your 1-week runtime has ended.\n\n"
                            "⭐ <b>Spend 1 point to restart for another week!</b>\n\n"
                            f"Your points: <b>{user.get('points', 0)} pts</b>\n\n"
                            "Use /start → 🔄 Renew Bot.",
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass


# ═══════════════════════════════════════════════════════════════════════
#  📡  CHANNEL MEMBERSHIP
# ═══════════════════════════════════════════════════════════════════════

async def check_channel_membership(bot, user_id):
    missing = []
    for ch in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(ch["channel_id"], user_id)
            if member.status in ("left", "kicked", "banned"):
                missing.append(ch["name"])
        except TelegramError:
            missing.append(ch["name"])
    return len(missing) == 0, missing


def build_channel_buttons():
    rows = []
    for ch in REQUIRED_CHANNELS:
        rows.append([InlineKeyboardButton(f"🔗 {ch['name']}", url=ch["url"])])
    rows.append([InlineKeyboardButton("✅ I've Joined — Check Again", callback_data="check_channels")])
    return InlineKeyboardMarkup(rows)


# ═══════════════════════════════════════════════════════════════════════
#  🌐  iVASMS SESSION (cookie-based)
# ═══════════════════════════════════════════════════════════════════════

BASE_URL      = "https://www.ivasms.com"
SOCKET_SERVER = "https://ivasms.com:2087"
PORTAL_PATH   = "/portal/live/my_sms"
_SKIP_EVENTS = {"connect", "disconnect", "error", "message", "send_message_max_Limit_97587"}

SENDER_MAP = {
    "google": "Google", "goog": "Google", "facebook": "Facebook", "facebk": "Facebook",
    "meta": "Meta", "whatsapp": "WhatsApp", "wa": "WhatsApp", "instagram": "Instagram",
    "insta": "Instagram", "twitter": "Twitter/X", "amazon": "Amazon", "amzn": "Amazon",
    "microsoft": "Microsoft", "msft": "Microsoft", "apple": "Apple", "paypal": "PayPal",
    "snapchat": "Snapchat", "snap": "Snapchat", "tiktok": "TikTok", "telegram": "Telegram",
    "discord": "Discord", "uber": "Uber", "netflix": "Netflix", "linkedin": "LinkedIn",
    "yahoo": "Yahoo", "binance": "Binance", "coinbase": "Coinbase", "okx": "OKX",
    "bybit": "Bybit", "kraken": "Kraken", "airbnb": "Airbnb", "shopify": "Shopify",
    "ebay": "eBay", "steam": "Steam",
}

BODY_PATTERNS = [
    (r'\bgoogle\b', "Google"), (r'\bgmail\b', "Google"), (r'\bfacebook\b', "Facebook"),
    (r'\binstagram\b', "Instagram"), (r'\bwhatsapp\b', "WhatsApp"), (r'\btwitter\b', "Twitter/X"),
    (r'\btiktok\b', "TikTok"), (r'\bamazon\b', "Amazon"), (r'\bmicrosoft\b', "Microsoft"),
    (r'\bapple\b', "Apple"), (r'\bpaypal\b', "PayPal"), (r'\bsnapchat\b', "Snapchat"),
    (r'\btelegram\b', "Telegram"), (r'\bdiscord\b', "Discord"), (r'\buber\b', "Uber"),
    (r'\bnetflix\b', "Netflix"), (r'\blinkedin\b', "LinkedIn"), (r'\bbinance\b', "Binance"),
]

SERVICE_ICONS = {
    "Google": "🔴", "Facebook": "🔵", "Instagram": "🟣", "WhatsApp": "🟢",
    "Twitter/X": "⚫", "TikTok": "⬛", "Amazon": "🟠", "Microsoft": "🔷",
    "Apple": "⚪", "PayPal": "💙", "Snapchat": "🟡", "Telegram": "✈️",
    "Discord": "💜", "Uber": "⬜", "Netflix": "🔴", "LinkedIn": "🔵",
    "Yahoo": "🟣", "Binance": "🟡", "Coinbase": "🔵", "OKX": "⬛",
    "Bybit": "🟡", "Kraken": "🟣", "Airbnb": "🔴", "Shopify": "🟢",
    "eBay": "🔵", "Steam": "🔵",
}


def service_icon(name):
    return SERVICE_ICONS.get(name, "📋")


def detect_service(sender, message):
    s = sender.lower().strip()
    for key, svc in SENDER_MAP.items():
        if key in s:
            return svc
    m = message.lower()
    for pattern, svc in BODY_PATTERNS:
        if re.search(pattern, m, re.IGNORECASE):
            return svc
    return "Unknown Service"


def detect_otp(message):
    patterns = [
        r'\b(\d{6})\b', r'\b(\d{4})\b', r'\b(\d{8})\b',
        r'(?:code|otp|pin|password|passcode)[^\d]{0,10}(\d{4,8})',
        r'(\d{4,8})(?:[^\d]{0,10}(?:code|otp|pin|is your|verification))',
        r'G-(\d{6})', r'(\d{6})\s+is your', r'use\s+(\d{4,8})', r':\s*(\d{4,8})\b',
    ]
    for pat in patterns:
        m = re.search(pat, message, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def parse_cookie_string(cookie_str):
    """Parse cookies from multiple formats:
      - Raw Cookie header:  name1=val1; name2=val2
      - JSON object:        {"name": "value", ...}
      - JSON array (Cookie-Editor / EditThisCookie export):
            [{"name": "...", "value": "...", "domain": "...", ...}, ...]
    """
    cookies = {}
    if not cookie_str:
        return cookies

    cookie_str = cookie_str.strip()

    if cookie_str.startswith("[") or cookie_str.startswith("{"):
        try:
            data = json.loads(cookie_str)
        except Exception:
            data = None

        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "name" in item and "value" in item:
                    cookies[str(item["name"]).strip()] = str(item["value"]).strip()
            if cookies:
                return cookies

        elif isinstance(data, dict):
            if "name" in data and "value" in data:
                cookies[str(data["name"]).strip()] = str(data["value"]).strip()
            else:
                for k, v in data.items():
                    cookies[str(k).strip()] = str(v).strip()
            if cookies:
                return cookies

    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k:
                cookies[k] = v
    return cookies


def make_session(cookies, user_agent):
    """Create a requests.Session (or curl_cffi) preloaded with cookies."""
    if HAS_CURL_CFFI:
        try:
            s = curl_requests.Session(impersonate=IMPERSONATE)
        except Exception:
            s = requests.Session()
    else:
        s = requests.Session()

    s.headers.update({
        "User-Agent": user_agent or DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    })

    for k, v in cookies.items():
        try:
            s.cookies.set(k, v, domain=".ivasms.com", path="/")
        except Exception:
            try:
                s.cookies.set(k, v)
            except Exception:
                pass

    return s


def verify_session(session, cookies, user_agent):
    """Confirm cookies are still valid by hitting the portal page."""
    try:
        r = session.get(f"{BASE_URL}{PORTAL_PATH}", timeout=20, allow_redirects=True)
    except Exception as e:
        log.error(f"verify_session GET failed: {e}")
        return False

    final_url = (r.url or "").lower()
    body = (r.text or "").lower()

    if "login" in final_url or "signin" in final_url:
        return False
    if "cf-chl" in body or "challenge-platform" in body or "turnstile" in body:
        return False
    if "just a moment" in body:
        return False
    if "logout" in body or "dashboard" in body or "live_sms" in body or "my_sms" in body:
        return True
    return r.status_code == 200 and "login" not in body[:2000]


def extract_socket_creds(session):
    url = f"{BASE_URL}{PORTAL_PATH}"
    try:
        r = session.get(url, timeout=20)
        html = r.text
    except Exception as e:
        log.error(f"Failed to fetch portal: {e}")
        return None

    soup = BeautifulSoup(html, "html.parser")

    # Token
    token = None
    for pat in [
        r"(?<![_\w])token\s*:\s*['\"]([A-Za-z0-9+/=]{10,})['\"]",
        r"['\"]token['\"]\s*:\s*['\"]([A-Za-z0-9+/=]{10,})['\"]",
        r"var\s+token\s*=\s*['\"]([A-Za-z0-9+/=]{10,})['\"]",
    ]:
        for m in re.finditer(pat, html):
            candidate = m.group(1)
            start = m.start(1)
            context = html[max(0, start - 15):start]
            if "_token" in context or "_token" in html[max(0, m.start()-20):m.start()]:
                continue
            token = candidate
            break
        if token:
            break
    if not token:
        meta = soup.find("meta", {"name": "csrf-token"})
        if meta and meta.get("content"):
            token = meta["content"]

    # User hash
    user = None
    for pat in [
        r'(?<![_\w])user\s*:\s*["\']([a-f0-9]{32})["\']',
        r'user=([a-f0-9]{32})',
    ]:
        m = re.search(pat, html)
        if m:
            user = m.group(1)
            break

    # Namespace
    ns = "/livesms"
    for m in re.finditer(r"io\s*[\.(]\s*['\"]https://ivasms\.com:2087(/[^'\"]*)?['\"]", html):
        raw_ns = m.group(1)
        ns = raw_ns if raw_ns else "/"
        if ns != "/onlineUser":
            break

    # Event
    event = None
    NON_SMS_PREFIXES = ("connect", "disconnect", "error", "message", "click", "submit")
    for pat in [
        r'liveSMSSocket\.on\s*\(\s*["\']([A-Za-z0-9+/=]{20,})["\']',
        r'socket\.on\s*\(\s*["\']([A-Za-z0-9+/=]{20,})["\']',
        r'socket\.on\s*\(\s*["\']([A-Za-z0-9_]{5,})["\']',
    ]:
        for m in re.finditer(pat, html):
            candidate = m.group(1)
            if candidate in _SKIP_EVENTS:
                continue
            if any(candidate.startswith(p) for p in NON_SMS_PREFIXES):
                continue
            event = candidate
            break
        if event:
            break

    if not token:
        return None

    return {"token": token, "user": user, "namespace": ns, "event": event}


# ═══════════════════════════════════════════════════════════════════════
#  🤖  USER BOT RUNNER
# ═══════════════════════════════════════════════════════════════════════

class UserBot:
    def __init__(self, owner_id, bot_token, ivasms_cookies, ivasms_user_agent):
        self.owner_id = owner_id
        self.bot_token = bot_token
        self.ivasms_cookies = ivasms_cookies
        self.ivasms_user_agent = ivasms_user_agent

        self.number_lists = {}
        self.number_in_use = {}
        self.otp_watchers = defaultdict(list)
        self._seen_keys = set()
        self._sms_queue = None
        self._loop = None
        self.app = None
        self.logs = []
        self._max_logs = 100
        self._running = False
        self.error_count = 0
        self.sms_count = 0
        self.otp_count = 0
        self.started_at = datetime.now()
        self.last_error = None
        self.status = "starting"
        self.cookies_expired = False
        self.socket_connected = False

    def add_log(self, msg, level="INFO"):
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {level}: {msg}"
        self.logs.append(entry)
        if len(self.logs) > self._max_logs:
            self.logs = self.logs[-self._max_logs:]
        if level == "ERROR":
            self.error_count += 1
            self.last_error = msg
            self.status = "error"
        if _master_app and _master_loop:
            asyncio.run_coroutine_threadsafe(
                self._send_log_to_owner(entry), _master_loop
            )

    async def _send_log_to_owner(self, entry):
        try:
            await _master_app.bot.send_message(
                self.owner_id,
                f"🤖 <b>Your Bot Log:</b>\n<code>{entry}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    async def _notify_cookies_expired(self):
        self.cookies_expired = True
        try:
            await _master_app.bot.send_message(
                self.owner_id,
                "╔══════════════════════════════════╗\n"
                "║   🍪  iVASMS Cookies Expired!    ║\n"
                "╚══════════════════════════════════╝\n\n"
                "Your iVASMS session cookies have expired.\n\n"
                "<b>How to fix:</b>\n"
                "1️⃣ Log into iVASMS in your browser\n"
                "2️⃣ Open Cookie-Editor extension → Export\n"
                "3️⃣ Copy the JSON array\n"
                "4️⃣ Send /start → 🍪 Update Cookies\n\n"
                "<i>The bot has been stopped until you refresh cookies.</i>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    def start_socket(self, creds):
        sio = socketio.Client(
            ssl_verify=False,
            logger=SOCKET_DEBUG,
            engineio_logger=SOCKET_DEBUG,
            reconnection=True,
            reconnection_attempts=0,
            reconnection_delay=5,
            reconnection_delay_max=30,
        )
        ns = creds["namespace"]

        @sio.event(namespace=ns)
        def connect():
            self.socket_connected = True
            self.add_log(f"🔌 iVASMS socket connected ({ns})")

        @sio.event(namespace=ns)
        def disconnect():
            self.socket_connected = False
            self.add_log("⚡ Socket disconnected — reconnecting…", "WARN")

        @sio.event(namespace=ns)
        def connect_error(data):
            self.add_log(f"❌ Socket connect_error: {data}", "ERROR")

        def on_sms(data):
            if not isinstance(data, dict):
                return
            raw = f"{data.get('recipient','')}{data.get('originator','')}{data.get('message','')}"
            key = hashlib.md5(raw.encode()).hexdigest()
            if key in self._seen_keys:
                return
            self._seen_keys.add(key)
            msg_preview = data.get('message', '')[:50]
            self.add_log(f"📨 SMS {data.get('originator')} → {data.get('recipient')}: {msg_preview}")
            self.sms_count += 1
            if self._loop and self._sms_queue:
                asyncio.run_coroutine_threadsafe(self._sms_queue.put(data), self._loop)

        if creds.get("event"):
            sio.on(creds["event"], on_sms, namespace=ns)
        else:
            @sio.on("*", namespace=ns)
            def catch_all(event, data):
                if event not in _SKIP_EVENTS and isinstance(data, dict) and "message" in data:
                    on_sms(data)

        def run_socket():
            # Build a Cookie header from the user's stored cookies.
            # The socket server sits behind Cloudflare on port 2087, so it
            # needs the same cf_clearance + session cookies the HTTP requests use.
            cookie_parts = []
            for k, v in self.ivasms_cookies.items():
                cookie_parts.append(f"{k}={v}")
            cookie_header = "; ".join(cookie_parts)

            headers = {
                "User-Agent": self.ivasms_user_agent or DEFAULT_USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
                "Origin": "https://www.ivasms.com",
                "Referer": "https://www.ivasms.com/",
            }
            if cookie_header:
                headers["Cookie"] = cookie_header

            auth = {"token": creds["token"]}
            if creds.get("user"):
                auth["user"] = creds["user"]

            while self._running:
                try:
                    sio.connect(
                        SOCKET_SERVER,
                        namespaces=[ns],
                        transports=SOCKET_TRANSPORTS,
                        auth=auth,
                        headers=headers,
                        socketio_path="socket.io",
                    )
                    sio.wait()
                except Exception as e:
                    self.add_log(f"Socket error: {e} — retrying in 10s", "ERROR")
                    try:
                        sio.disconnect()
                    except Exception:
                        pass
                    time.sleep(10)

        threading.Thread(
            target=run_socket, daemon=True, name=f"socket-{self.owner_id}"
        ).start()

    async def sms_processor(self):
        while self._running:
            try:
                data = await asyncio.wait_for(self._sms_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            phone = str(data.get("recipient") or data.get("range") or "").strip()
            sender = str(data.get("originator", "")).strip()
            message = str(data.get("message", "")).strip()
            if not phone:
                continue
            otp = detect_otp(message)
            watchers = self.otp_watchers.get(phone, [])
            if watchers:
                chat_id, service, event, result_holder = watchers[0]
                if otp:
                    result_holder["data"] = data
                    result_holder["otp"] = otp
                    event.set()
                else:
                    try:
                        await self.app.bot.send_message(
                            chat_id,
                            f"📩 <b>SMS (No OTP)</b>\n"
                            f"📱 <code>{phone}</code>\n"
                            f"📤 <code>{sender}</code>\n"
                            f"💬 {message}",
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass
            else:
                self.add_log(f"Unmatched SMS for {phone}: {message[:50]}")

    async def otp_watcher(self, chat_id, number, service):
        event = asyncio.Event()
        result_holder = {}
        self.otp_watchers[number].append((chat_id, service, event, result_holder))
        try:
            await asyncio.wait_for(event.wait(), timeout=OTP_WAIT_TIMEOUT)
            data = result_holder.get("data", {})
            otp = result_holder.get("otp", "")
            sender = str(data.get("originator", "")).strip()
            message = str(data.get("message", "")).strip()
            detected_svc = detect_service(sender, message)
            icon = service_icon(detected_svc)
            await self.app.bot.send_message(
                chat_id,
                "╔══════════════════════════════════╗\n"
                "║   ✅  OTP Received!               ║\n"
                "╚══════════════════════════════════╝\n\n"
                f"📱 <b>Number:</b> <code>{number}</code>\n"
                f"🔑 <b>OTP:</b> <code>{otp}</code>\n"
                f"{icon} <b>Service:</b> {detected_svc}\n"
                f"📤 <b>Sender:</b> <code>{sender}</code>\n\n"
                f"💬 <i>{message}</i>\n\n"
                f"⏱ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                parse_mode=ParseMode.HTML,
            )
            self.add_log(f"✅ OTP delivered for {number}: {otp}")
            self.otp_count += 1
        except asyncio.TimeoutError:
            await self.app.bot.send_message(
                chat_id,
                f"⌛ <b>OTP Timeout</b>\n"
                f"No OTP received for <code>{number}</code> in {OTP_WAIT_TIMEOUT}s.\n"
                f"Number released. Use /lists to try again.",
                parse_mode=ParseMode.HTML,
            )
            self.add_log(f"⌛ OTP timeout for {number}")
        finally:
            self.number_in_use.pop(number, None)
            self.otp_watchers[number] = [
                w for w in self.otp_watchers[number] if w[2] is not event
            ]

    def get_next_number(self, service):
        for num in self.number_lists.get(service, []):
            if num not in self.number_in_use:
                return num
        return None

    # ── Sub-bot commands ───────────────────────────────────────────────

    async def sub_start(self, update, ctx):
        name = update.effective_user.first_name or "User"
        await update.message.reply_text(
            "╔══════════════════════════════════╗\n"
            "║   🤖  iVASMS OTP Bot — Welcome   ║\n"
            "╚══════════════════════════════════╝\n\n"
            f"Hello <b>{name}</b>! 👋\n\n"
            "Use /lists to browse available numbers.\n"
            "Use /status to check connection.\n"
            "Use /help for usage guide.",
            parse_mode=ParseMode.HTML,
        )
        self.add_log(f"User {update.effective_user.id} started sub-bot")

    async def sub_lists(self, update, ctx):
        if not self.number_lists:
            await update.message.reply_text(
                "📭 No number lists available yet.\n"
                "Ask the bot owner to add numbers via /addlist."
            )
            return
        buttons = []
        lines = [
            "╔══════════════════════════════════╗",
            "║     📋  Available Numbers         ║",
            "╚══════════════════════════════════╝\n",
            DIVIDER,
        ]
        for svc, nums in sorted(self.number_lists.items()):
            available = sum(1 for n in nums if n not in self.number_in_use)
            icon = service_icon(svc)
            lines.append(f"{icon} <b>{svc}</b> — <code>{available}</code> free")
            buttons.append([InlineKeyboardButton(
                f"{icon} {svc} [{available} free]",
                callback_data=f"getnum:{svc}"
            )])
        buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data="refresh")])
        lines.append(DIVIDER)
        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.HTML,
        )

    async def sub_status(self, update, ctx):
        total = sum(len(v) for v in self.number_lists.values())
        in_use = len(self.number_in_use)
        socket_state = "🟢 Connected" if self.socket_connected else "🔴 Disconnected"
        await update.message.reply_text(
            "╔══════════════════════════════════╗\n"
            "║       📊  Bot Status             ║\n"
            "╚══════════════════════════════════╝\n\n"
            f"🔌 <b>iVASMS Socket:</b> {socket_state}\n"
            f"📋 <b>Lists:</b> <code>{len(self.number_lists)}</code>\n"
            f"📱 <b>Total Numbers:</b> <code>{total}</code>\n"
            f"🔄 <b>In Use:</b> <code>{in_use}</code>\n"
            f"📨 <b>SMS Received:</b> <code>{self.sms_count}</code>\n"
            f"🔑 <b>OTPs Delivered:</b> <code>{self.otp_count}</code>\n\n"
            f"⏱ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            parse_mode=ParseMode.HTML,
        )

    async def sub_help(self, update, ctx):
        await update.message.reply_text(
            "╔══════════════════════════════════╗\n"
            "║        ❓  Help & Usage           ║\n"
            "╚══════════════════════════════════╝\n\n"
            "1️⃣ Use /lists to see available services\n"
            "2️⃣ Tap a service to get a number\n"
            "3️⃣ Use that number to request OTP\n"
            "4️⃣ OTP is auto-delivered here!\n\n"
            f"⏱ OTP wait: {OTP_WAIT_TIMEOUT}s\n"
            "🔒 Numbers are exclusive — one user at a time",
            parse_mode=ParseMode.HTML,
        )

    async def sub_addlist(self, update, ctx):
        uid = update.effective_user.id
        if uid != self.owner_id:
            await update.message.reply_text("⛔ Only the bot owner can add number lists.")
            return
        if not ctx.args:
            await update.message.reply_text(
                "Usage: /addlist ServiceName +1234 +5678 …\n"
                "Example: /addlist Google +12025550001 +12025550002"
            )
            return
        service = ctx.args[0]
        raw = " ".join(ctx.args[1:])
        numbers = []
        for token in re.split(r'[\s,;|\n]+', raw):
            token = token.strip()
            digits = re.sub(r'[^\d+]', '', token)
            if 7 <= len(digits.lstrip('+')) <= 15:
                if not digits.startswith('+'):
                    digits = '+' + digits
                numbers.append(digits)
        numbers = list(dict.fromkeys(numbers))
        if not numbers:
            await update.message.reply_text("❌ No valid phone numbers found.")
            return
        if service in self.number_lists:
            self.number_lists[service].extend(numbers)
            self.number_lists[service] = list(dict.fromkeys(self.number_lists[service]))
        else:
            self.number_lists[service] = numbers
        await update.message.reply_text(
            f"✅ <b>{service}</b> list updated!\n"
            f"📊 Total: <code>{len(self.number_lists[service])}</code> numbers",
            parse_mode=ParseMode.HTML,
        )
        self.add_log(f"Owner added {len(numbers)} numbers to '{service}'")

    async def sub_callback(self, update, ctx):
        query = update.callback_query
        await query.answer()
        data = query.data

        if data == "refresh":
            await self.sub_lists.__func__(self, update, ctx)
            return

        if data.startswith("getnum:"):
            service = data[len("getnum:"):]
            number = self.get_next_number(service)
            if not number:
                await query.answer(f"❌ No free numbers for {service}.", show_alert=True)
                return
            uid = query.from_user.id
            self.number_in_use[number] = uid
            icon = service_icon(service)
            await query.edit_message_text(
                "╔══════════════════════════════════╗\n"
                f"║     {icon}  Number Assigned          ║\n"
                "╚══════════════════════════════════╝\n\n"
                f"📱 <b>Number:</b> <code>{number}</code>\n"
                f"{icon} <b>Service:</b> {service}\n\n"
                f"⏳ <i>Waiting for OTP… (up to {OTP_WAIT_TIMEOUT}s)</i>\n"
                f"💡 Request OTP on <b>{service}</b> now!",
                parse_mode=ParseMode.HTML,
            )
            asyncio.ensure_future(
                self.otp_watcher(query.message.chat_id, number, service)
            )
            self.add_log(f"Number {number} assigned to user {uid} for {service}")

    # ── Launch ─────────────────────────────────────────────────────────

    async def run(self):
        self._running = True
        self._sms_queue = asyncio.Queue()
        self._loop = asyncio.get_event_loop()

        self.add_log(f"Starting personal iVASMS bot for user {self.owner_id}")

        try:
            session = await self._loop.run_in_executor(
                None, make_session, self.ivasms_cookies, self.ivasms_user_agent
            )
        except Exception as e:
            self.add_log(f"❌ Failed to build session: {e}", "ERROR")
            return

        ok = await self._loop.run_in_executor(
            None, verify_session, session, self.ivasms_cookies, self.ivasms_user_agent
        )
        if not ok:
            self.add_log("❌ iVASMS cookies are invalid or expired", "ERROR")
            await self._notify_cookies_expired()
            return

        self.add_log("✅ iVASMS session active (cookies valid)")

        creds = await self._loop.run_in_executor(None, extract_socket_creds, session)
        if creds:
            self.start_socket(creds)
            self.add_log(
                f"✅ Socket listener active (ns={creds['namespace']}, "
                f"event={creds.get('event') or 'wildcard'})"
            )
        else:
            self.add_log(
                "⚠️ Could not extract socket credentials — cookies may be stale",
                "ERROR",
            )
            await self._notify_cookies_expired()
            return

        self.app = Application.builder().token(self.bot_token).build()
        self.app.add_handler(CommandHandler("start", self.sub_start))
        self.app.add_handler(CommandHandler("lists", self.sub_lists))
        self.app.add_handler(CommandHandler("status", self.sub_status))
        self.app.add_handler(CommandHandler("help", self.sub_help))
        self.app.add_handler(CommandHandler("addlist", self.sub_addlist))
        self.app.add_handler(CallbackQueryHandler(self.sub_callback))

        if _master_app:
            try:
                await _master_app.bot.send_message(
                    self.owner_id,
                    "╔══════════════════════════════════╗\n"
                    "║   🚀  Your Bot is Now LIVE!       ║\n"
                    "╚══════════════════════════════════╝\n\n"
                    "✅ iVASMS connected (cookies)\n"
                    "🤖 Your personal OTP bot is running!\n\n"
                    "Open your bot and use /start to begin.",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        asyncio.ensure_future(self.sms_processor())

        self.add_log("🤖 Sub-bot polling started")
        self.status = "running"
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)

        try:
            while self._running:
                await asyncio.sleep(1)
        finally:
            try:
                await self.app.updater.stop()
                await self.app.stop()
                await self.app.shutdown()
            except Exception:
                pass
            self.add_log("Bot stopped.")

    def stop(self):
        self._running = False


# ═══════════════════════════════════════════════════════════════════════
#  🎛️  MASTER BOT HANDLERS
# ═══════════════════════════════════════════════════════════════════════

async def master_start(update, ctx):
    uid = update.effective_user.id
    name = update.effective_user.first_name or "User"
    username = update.effective_user.username or ""

    referred_by = None
    if ctx.args:
        try:
            ref_id = int(ctx.args[0])
            if ref_id != uid and get_user(ref_id):
                referred_by = ref_id
        except (ValueError, TypeError):
            pass

    user = get_user(uid)
    if not user:
        user = create_user(uid, name, username, referred_by)
        log.info(f"New user: {uid} ({name}) referred_by={referred_by}")

    if user.get("banned"):
        await update.message.reply_text("🚫 You are banned from this platform.")
        return

    all_joined, missing = await check_channel_membership(ctx.bot, uid)

    if not all_joined:
        user["joined_channels"] = False
        set_user(uid, user)
        await update.message.reply_text(
            f"╔══════════════════════════════════╗\n"
            f"║   👋  Welcome, {name}!            \n"
            f"╚══════════════════════════════════╝\n\n"
            f"⚠️ <b>Please join our channels first!</b>\n\n"
            f"You must join <b>all</b> channels below before using this bot:\n\n"
            f"{DIVIDER}",
            reply_markup=build_channel_buttons(),
            parse_mode=ParseMode.HTML,
        )
        return

    user["joined_channels"] = True
    set_user(uid, user)

    await _show_main_menu(update, uid, name, user)


async def _show_main_menu(update, uid, name, user):
    ref_count = get_referral_count(uid)
    has_active_bot = has_bot(uid)
    points = user.get("points", 0)
    bot_me = await update.get_bot().get_me()
    ref_link = f"https://t.me/{bot_me.username}?start={uid}"
    expired = bot_is_expired(uid)
    expiry_str = get_expiry_str(uid)

    status_lines = []

    if ref_count >= MIN_REFERRALS:
        status_lines.append(f"✅ Referrals: <code>{ref_count}/{MIN_REFERRALS}</code> — Unlocked!")
    else:
        remaining = MIN_REFERRALS - ref_count
        status_lines.append(
            f"⏳ Referrals: <code>{ref_count}/{MIN_REFERRALS}</code> — Need <b>{remaining} more</b>"
        )

    unlocked = user.get("bot_unlocked", False)
    if not unlocked:
        status_lines.append(f"⭐ Points: <b>{points}</b> — Need <b>{POINTS_FIRST_LAUNCH}</b> to create bot")
    else:
        status_lines.append(f"⭐ Points: <b>{points}</b>")

    if has_active_bot and not expired:
        status_lines.append(f"🤖 Bot: <b>Running ✅</b> — ⏱ {expiry_str}")
    elif expired:
        status_lines.append("🤖 Bot: <b>⚠️ Expired</b> — Need 1 pt to renew")
    elif user.get("bot_token"):
        status_lines.append(f"🤖 Bot: <b>Stopped</b> — {expiry_str}")
    else:
        status_lines.append("🤖 Bot: <b>Not configured</b>")

    buttons = []

    if not user.get("bot_token") and can_create_bot(uid):
        if points >= POINTS_FIRST_LAUNCH:
            buttons.append([InlineKeyboardButton("⚙️ Setup My Bot (2 pts)", callback_data="setup_bot")])
        else:
            buttons.append([InlineKeyboardButton(f"❌ Need {POINTS_FIRST_LAUNCH} pts to setup", callback_data="need_points")])

    if user.get("bot_token") and not has_active_bot:
        if points >= POINTS_WEEKLY_RENEWAL:
            buttons.append([InlineKeyboardButton("🔄 Renew Bot (1 pt / week)", callback_data="renew_bot")])
        else:
            buttons.append([InlineKeyboardButton("❌ Need 1 pt to renew bot", callback_data="need_points")])

    if has_active_bot and not expired:
        buttons.append([InlineKeyboardButton("📊 View My Bot Logs", callback_data="view_logs")])
        buttons.append([InlineKeyboardButton("🛑 Stop My Bot", callback_data="stop_bot")])

    buttons.append([InlineKeyboardButton("🍪 Update Cookies", callback_data="update_cookies")])
    buttons.append([InlineKeyboardButton("👥 My Referrals", callback_data="my_referrals")])
    buttons.append([InlineKeyboardButton("📋 My Status", callback_data="my_status")])

    await update.message.reply_text(
        "╔══════════════════════════════════╗\n"
        "║   🤖  iVASMS Bot Platform        ║\n"
        "╚══════════════════════════════════╝\n\n"
        f"Welcome back, <b>{name}</b>! 👋\n\n"
        f"{DIVIDER}\n"
        + "\n".join(status_lines) + "\n\n"
        f"{DIVIDER}\n\n"
        "🔗 <b>Your Referral Link:</b>\n"
        f"<code>{ref_link}</code>\n\n"
        "<i>Share this link to invite friends!</i>",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )


async def master_callback(update, ctx):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    user = get_user(uid)

    if not user:
        await query.answer("Please send /start first.", show_alert=True)
        return

    if user.get("banned"):
        await query.answer("You are banned.", show_alert=True)
        return

    data = query.data

    if data == "check_channels":
        all_joined, missing = await check_channel_membership(ctx.bot, uid)
        if not all_joined:
            await query.answer(
                "❌ You still haven't joined:\n" + "\n".join(missing),
                show_alert=True,
            )
            return
        user["joined_channels"] = True
        set_user(uid, user)
        await query.edit_message_text(
            "✅ <b>Channels verified!</b> Welcome!\n\nUse /start to access the main menu.",
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "my_referrals":
        ref_count = get_referral_count(uid)
        bot_me = await ctx.bot.get_me()
        ref_link = f"https://t.me/{bot_me.username}?start={uid}"
        ref_list = user.get("referrals", [])
        ref_names = []
        for rid in ref_list[:10]:
            ru = get_user(rid)
            if ru:
                ref_names.append(f"  • {ru.get('name', 'Unknown')} ({rid})")
        ref_str = "\n".join(ref_names) if ref_names else "  No referrals yet"
        await query.edit_message_text(
            "╔══════════════════════════════════╗\n"
            "║       👥  Your Referrals          ║\n"
            "╚══════════════════════════════════╝\n\n"
            f"Total: <b>{ref_count}/{MIN_REFERRALS}</b> referrals\n\n"
            f"{DIVIDER}\n"
            f"{ref_str}\n"
            f"{DIVIDER}\n\n"
            "🔗 <b>Your Referral Link:</b>\n"
            f"<code>{ref_link}</code>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="back_menu")
            ]]),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "my_status":
        ref_count = get_referral_count(uid)
        pts = user.get("points", 0)
        expiry_str = get_expiry_str(uid)
        expired = bot_is_expired(uid)
        if has_bot(uid) and not expired:
            bot_status = "🟢 Running"
        elif expired:
            bot_status = "⚠️ Expired"
        elif user.get("bot_token"):
            bot_status = "🟡 Stopped"
        else:
            bot_status = "🔴 Not set up"
        unlocked = user.get("bot_unlocked", False)
        has_cookies = bool(user.get("ivasms_cookies"))
        await query.edit_message_text(
            "╔══════════════════════════════════╗\n"
            "║       📋  Your Account Status    ║\n"
            "╚══════════════════════════════════╝\n\n"
            f"👤 <b>Name:</b> {user.get('name')}\n"
            f"🆔 <b>ID:</b> <code>{uid}</code>\n"
            f"👥 <b>Referrals:</b> <code>{ref_count}/{MIN_REFERRALS}</code>\n"
            f"📢 <b>Channels:</b> {'✅ Joined' if user.get('joined_channels') else '❌ Not joined'}\n"
            f"⭐ <b>Points:</b> <b>{pts}</b>\n"
            f"🔓 <b>Bot Unlocked:</b> {'✅ Yes' if unlocked else '❌ No'}\n"
            f"🤖 <b>Bot:</b> {bot_status}\n"
            f"⏱ <b>Expires:</b> {expiry_str}\n\n"
            f"🍪 iVASMS Cookies: {'✅ Set' if has_cookies else '❌ Not set'}\n"
            f"📅 <b>Joined:</b> {user.get('registered_at','?')[:10]}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="back_menu")
            ]]),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "need_points":
        pts = user.get("points", 0)
        unlocked = user.get("bot_unlocked", False)
        needed = POINTS_WEEKLY_RENEWAL if unlocked else POINTS_FIRST_LAUNCH
        await query.answer(
            f"You need {needed} point(s) but have {pts}.\n"
            f"Ask an admin to add points with /addpoints.",
            show_alert=True,
        )
        return

    if data == "setup_bot":
        if not can_create_bot(uid):
            ref_count = get_referral_count(uid)
            await query.answer(
                f"Need {MIN_REFERRALS} referrals. You have {ref_count}.",
                show_alert=True,
            )
            return
        if has_bot(uid):
            await query.answer("You already have an active bot!", show_alert=True)
            return
        pts = user.get("points", 0)
        if pts < POINTS_FIRST_LAUNCH:
            await query.answer(
                f"Need {POINTS_FIRST_LAUNCH} points. You have {pts}.",
                show_alert=True,
            )
            return
        user["setup_step"] = "awaiting_bot_token"
        set_user(uid, user)
        await query.edit_message_text(
            "╔══════════════════════════════════╗\n"
            "║   ⚙️  Bot Setup — Step 1/3       ║\n"
            "╚══════════════════════════════════╝\n\n"
            f"⭐ <b>Cost: {POINTS_FIRST_LAUNCH} points</b> (deducted on launch)\n"
            f"⏱ <b>Runtime: {BOT_RUNTIME_DAYS} days</b>\n\n"
            "<b>Send your Telegram Bot Token</b>\n\n"
            "1️⃣ Go to @BotFather on Telegram\n"
            "2️⃣ Create a new bot with /newbot\n"
            "3️⃣ Copy the token and send it here\n\n"
            "<i>Format: 123456:ABCDEFabcdef…</i>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_setup")
            ]]),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "update_cookies":
        user["setup_step"] = "awaiting_cookies_update"
        set_user(uid, user)
        await query.edit_message_text(
            "╔══════════════════════════════════╗\n"
            "║   🍪  Update iVASMS Cookies      ║\n"
            "╚══════════════════════════════════╝\n\n"
            "<b>How to get your cookies:</b>\n\n"
            "<b>Option A — Cookie-Editor extension (easiest):</b>\n"
            "1️⃣ Log into iVASMS in your browser\n"
            "2️⃣ Open Cookie-Editor → Export → Export as JSON\n"
            "3️⃣ Paste the whole JSON array here\n\n"
            "<b>Option B — DevTools:</b>\n"
            "1️⃣ F12 → Network → refresh page\n"
            "2️⃣ Click the first document request\n"
            "3️⃣ Copy the <b>Cookie:</b> header value\n"
            "4️⃣ Paste it here\n\n"
            "<i>Either format works. Must be from the same browser/network "
            "you'll run the bot on.</i>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_setup")
            ]]),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "renew_bot":
        pts = user.get("points", 0)
        if pts < POINTS_WEEKLY_RENEWAL:
            await query.answer(
                f"Need {POINTS_WEEKLY_RENEWAL} point. You have {pts}.",
                show_alert=True,
            )
            return
        if not user.get("bot_token") or not user.get("ivasms_cookies"):
            await query.answer(
                "Bot not fully configured. Please set up again.",
                show_alert=True,
            )
            return
        user["points"] = pts - POINTS_WEEKLY_RENEWAL
        set_user(uid, user)
        set_bot_expiry(uid)
        user = get_user(uid)
        expiry_str = get_expiry_str(uid)
        await query.edit_message_text(
            "╔══════════════════════════════════╗\n"
            "║   🔄  Bot Renewed Successfully!  ║\n"
            "╚══════════════════════════════════╝\n\n"
            "✅ <b>1 point deducted</b>\n"
            f"⭐ Remaining points: <b>{user['points']}</b>\n"
            f"⏱ New expiry: <b>{expiry_str}</b>\n\n"
            "🚀 Starting your bot now…",
            parse_mode=ParseMode.HTML,
        )
        await _launch_user_bot(uid, user)
        return

    if data == "start_bot":
        if uid in active_bots:
            await query.answer("Bot is already running!", show_alert=True)
            return
        if not can_create_bot(uid):
            await query.answer("You don't meet the requirements yet.", show_alert=True)
            return
        user = get_user(uid)
        if not user.get("bot_token") or not user.get("ivasms_cookies"):
            await query.answer("Please complete setup first.", show_alert=True)
            return
        pts = user.get("points", 0)
        cost = POINTS_WEEKLY_RENEWAL if user.get("bot_unlocked") else POINTS_FIRST_LAUNCH
        if pts < cost:
            await query.answer(
                f"Need {cost} point(s). You have {pts}.",
                show_alert=True,
            )
            return
        user["points"] = pts - cost
        user["bot_unlocked"] = True
        set_user(uid, user)
        set_bot_expiry(uid)
        expiry_str = get_expiry_str(uid)
        await query.edit_message_text(
            "🚀 <b>Starting your bot…</b>\n\n"
            f"⭐ <b>{cost} point(s) deducted</b>\n"
            f"⏱ Expires: <b>{expiry_str}</b>\n\n"
            "This may take a few seconds.",
            parse_mode=ParseMode.HTML,
        )
        await _launch_user_bot(uid, get_user(uid))
        return

    if data == "stop_bot":
        if uid not in active_bots:
            await query.answer("No bot is running.", show_alert=True)
            return
        bot_entry = active_bots.pop(uid)
        bot_entry["instance"].stop()
        user["bot_active"] = False
        set_user(uid, user)
        await query.edit_message_text(
            "🛑 <b>Your bot has been stopped.</b>\n\nUse /start to restart.",
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "view_logs":
        if uid not in active_bots:
            await query.answer("No active bot found.", show_alert=True)
            return
        instance = active_bots[uid]["instance"]
        logs = instance.logs[-20:]
        log_text = "\n".join(f"<code>{l}</code>" for l in logs) if logs else "<i>No logs yet.</i>"
        await query.edit_message_text(
            "╔══════════════════════════════════╗\n"
            "║       📊  Your Bot Logs          ║\n"
            "╚══════════════════════════════════╝\n\n"
            f"{log_text}\n\n"
            f"<i>Showing last {len(logs)} entries</i>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh Logs", callback_data="view_logs")],
                [InlineKeyboardButton("🔙 Back", callback_data="back_menu")],
            ]),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "cancel_setup":
        user["setup_step"] = None
        set_user(uid, user)
        await query.edit_message_text(
            "❌ Cancelled. Use /start to return to the menu."
        )
        return

    if data == "back_menu":
        name = query.from_user.first_name or "User"
        user = get_user(uid)
        ref_count = get_referral_count(uid)
        has_active = has_bot(uid)
        bot_me = await ctx.bot.get_me()
        ref_link = f"https://t.me/{bot_me.username}?start={uid}"

        status_lines = []
        if ref_count >= MIN_REFERRALS:
            status_lines.append(f"✅ Referrals: <code>{ref_count}/{MIN_REFERRALS}</code> — Unlocked!")
        else:
            remaining = MIN_REFERRALS - ref_count
            status_lines.append(
                f"⏳ Referrals: <code>{ref_count}/{MIN_REFERRALS}</code> — Need <b>{remaining} more</b>"
            )
        if has_active:
            status_lines.append("🤖 Bot Status: <b>Running ✅</b>")
        else:
            status_lines.append("🤖 Bot Status: <b>Not running</b>")

        buttons = []
        if not has_active and can_create_bot(uid) and not user.get("bot_token"):
            buttons.append([InlineKeyboardButton("⚙️ Setup My Bot", callback_data="setup_bot")])
        if has_active:
            buttons.append([InlineKeyboardButton("📊 View My Bot Logs", callback_data="view_logs")])
            buttons.append([InlineKeyboardButton("🛑 Stop My Bot", callback_data="stop_bot")])
        if not has_active and user.get("bot_token") and can_create_bot(uid):
            buttons.append([InlineKeyboardButton("🚀 Start My Bot", callback_data="start_bot")])
        buttons.append([InlineKeyboardButton("🍪 Update Cookies", callback_data="update_cookies")])
        buttons.append([InlineKeyboardButton("👥 My Referrals", callback_data="my_referrals")])
        buttons.append([InlineKeyboardButton("📋 My Status", callback_data="my_status")])

        await query.edit_message_text(
            "╔══════════════════════════════════╗\n"
            "║   🤖  iVASMS Bot Platform        ║\n"
            "╚══════════════════════════════════╝\n\n"
            f"Welcome back, <b>{name}</b>! 👋\n\n"
            f"{DIVIDER}\n"
            + "\n".join(status_lines) + "\n\n"
            f"{DIVIDER}\n\n"
            "🔗 <b>Referral Link:</b>\n"
            f"<code>{ref_link}</code>",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.HTML,
        )
        return


async def master_text(update, ctx):
    uid = update.effective_user.id
    user = get_user(uid)
    if not user:
        await update.message.reply_text("Please send /start first.")
        return

    step = user.get("setup_step")
    if not step:
        return

    text = update.message.text.strip()

    if step == "awaiting_bot_token":
        if not re.match(r'^\d+:[A-Za-z0-9_-]{35,}$', text):
            await update.message.reply_text(
                "❌ That doesn't look like a valid bot token.\n\n"
                "Format: <code>123456789:ABCDEFabcdef…</code>\n\n"
                "Get one from @BotFather.",
                parse_mode=ParseMode.HTML,
            )
            return

        try:
            test_bot = Bot(token=text)
            bot_info = await test_bot.get_me()
        except TelegramError:
            await update.message.reply_text(
                "❌ Could not verify this bot token. Please check it and try again."
            )
            return

        user["bot_token"] = text
        user["setup_step"] = "awaiting_cookies"
        set_user(uid, user)
        await update.message.reply_text(
            "╔══════════════════════════════════╗\n"
            "║   ⚙️  Bot Setup — Step 2/3       ║\n"
            "╚══════════════════════════════════╝\n\n"
            f"✅ Bot <b>@{bot_info.username}</b> verified!\n\n"
            "<b>Now send your iVASMS cookies.</b>\n\n"
            "<b>Option A — Cookie-Editor (recommended):</b>\n"
            "1️⃣ Log into iVASMS in your browser\n"
            "2️⃣ Open Cookie-Editor → Export → Export as JSON\n"
            "3️⃣ Paste the entire JSON array here\n\n"
            "<b>Option B — DevTools:</b>\n"
            "1️⃣ F12 → Network → refresh page\n"
            "2️⃣ Click the first document request\n"
            "3️⃣ Under <b>Request Headers</b>, find <b>Cookie:</b>\n"
            "4️⃣ Copy the entire value and paste it here\n\n"
            "<i>Must include: ivas_sms_session, cf_clearance, XSRF-TOKEN</i>\n\n"
            "⚠️ <i>Use the same browser/network — Cloudflare binds cookies "
            "to your IP, User-Agent, and TLS fingerprint.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    if step in ("awaiting_cookies", "awaiting_cookies_update"):
        cookies = parse_cookie_string(text)
        if not cookies or len(cookies) < 2:
            await update.message.reply_text(
                "❌ Could not parse cookies.\n\n"
                "Please send either:\n"
                "• The raw <b>Cookie:</b> header value (name1=val1; name2=val2…), OR\n"
                "• The full JSON array exported by Cookie-Editor",
                parse_mode=ParseMode.HTML,
            )
            return

        has_session = any(
            k in cookies for k in
            ("ivas_sms_session", "laravel_session", "ivasms_session", "PHPSESSID", "session")
        )
        if not has_session:
            await update.message.reply_text(
                "⚠️ Warning: no obvious session cookie found "
                "(expected <code>ivas_sms_session</code>).\n\n"
                "Make sure you're copying cookies from a logged-in iVASMS tab. "
                "Sending anyway…",
                parse_mode=ParseMode.HTML,
            )

        user["ivasms_cookies"] = cookies
        user["cookies_updated_at"] = datetime.now().isoformat()

        if step == "awaiting_cookies_update":
            ua = user.get("ivasms_user_agent") or DEFAULT_USER_AGENT
            await update.message.reply_text(
                "🔄 <b>Verifying cookies…</b>",
                parse_mode=ParseMode.HTML,
            )

            loop = asyncio.get_event_loop()

            def _verify():
                s = make_session(cookies, ua)
                return verify_session(s, cookies, ua)

            ok = await loop.run_in_executor(None, _verify)
            if not ok:
                await update.message.reply_text(
                    "❌ <b>Cookies rejected by iVASMS.</b>\n\n"
                    "Make sure:\n"
                    "• You're logged into iVASMS in the same browser\n"
                    "• You're on the same network / IP as the bot\n"
                    "• You copied the full Cookie header or JSON export\n\n"
                    "Use /start → 🍪 Update Cookies to try again.",
                    parse_mode=ParseMode.HTML,
                )
                user["setup_step"] = None
                set_user(uid, user)
                return

            user["setup_step"] = None
            set_user(uid, user)
            await update.message.reply_text(
                "✅ <b>Cookies updated successfully!</b>\n\n"
                "Use /start → 🔄 Renew Bot (or 🚀 Start My Bot) to relaunch.",
                parse_mode=ParseMode.HTML,
            )
            return

        user["setup_step"] = "awaiting_user_agent"
        set_user(uid, user)
        await update.message.reply_text(
            "╔══════════════════════════════════╗\n"
            "║   ⚙️  Bot Setup — Step 3/3       ║\n"
            "╚══════════════════════════════════╝\n\n"
            f"✅ Cookies saved ({len(cookies)} cookies).\n\n"
            "<b>Now send your browser's User-Agent.</b>\n\n"
            "1️⃣ In DevTools (same browser you exported cookies from), "
            "go to the <b>Console</b> tab\n"
            "2️⃣ Type: <code>navigator.userAgent</code>\n"
            "3️⃣ Copy the string and send it here\n\n"
            "Or send <code>default</code> to use the preconfigured "
            "Chrome 109 UA (matches the setup this bot is tuned for).",
            parse_mode=ParseMode.HTML,
        )
        return

    if step == "awaiting_user_agent":
        if text.lower().strip() in ("default", "auto", "chrome"):
            ua = DEFAULT_USER_AGENT
        else:
            ua = text.strip()
            if len(ua) < 15 or "mozilla" not in ua.lower():
                await update.message.reply_text(
                    "❌ That doesn't look like a User-Agent.\n"
                    "Send your real User-Agent or the word <code>default</code>.",
                    parse_mode=ParseMode.HTML,
                )
                return

        user["ivasms_user_agent"] = ua
        set_user(uid, user)

        await update.message.reply_text(
            "🔄 <b>Verifying cookies against iVASMS…</b>\n\nPlease wait…",
            parse_mode=ParseMode.HTML,
        )

        cookies = user["ivasms_cookies"]
        loop = asyncio.get_event_loop()

        def _verify():
            s = make_session(cookies, ua)
            return verify_session(s, cookies, ua)

        ok = await loop.run_in_executor(None, _verify)

        if not ok:
            await update.message.reply_text(
                "❌ <b>Cookies were rejected by iVASMS.</b>\n\n"
                "Common causes:\n"
                "• Cloudflare TLS fingerprint mismatch — the bot "
                "impersonates Chrome 110 but your cookies came from a "
                "different browser version\n"
                "• Cookies already expired — re-login and re-export\n"
                "• cf_clearance missing or bound to a different IP\n\n"
                "Use /start → ⚙️ Setup My Bot to try again.",
                parse_mode=ParseMode.HTML,
            )
            user["setup_step"] = None
            user["ivasms_cookies"] = None
            user["ivasms_user_agent"] = None
            user["bot_token"] = None
            set_user(uid, user)
            return

        user["setup_step"] = None
        user["bot_created_at"] = datetime.now().isoformat()
        set_user(uid, user)
        pts = user.get("points", 0)
        cost = POINTS_FIRST_LAUNCH

        await update.message.reply_text(
            "╔══════════════════════════════════╗\n"
            "║   ✅  Setup Complete!             ║\n"
            "╚══════════════════════════════════╝\n\n"
            "🎉 Everything is configured!\n\n"
            "✅ Bot token saved\n"
            "✅ Cookies verified against iVASMS\n\n"
            f"{DIVIDER}\n"
            f"⭐ <b>Your points:</b> {pts}\n"
            f"💰 <b>Launch cost:</b> {cost} points\n"
            f"⏱ <b>Runtime:</b> {BOT_RUNTIME_DAYS} days\n\n"
            + ("🚀 Press below to launch!" if pts >= cost else f"❌ You need {cost} pts. Ask admin for /addpoints."),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    f"🚀 Launch My Bot ({cost} pts)",
                    callback_data="start_bot"
                )
            ]]) if pts >= cost else None,
            parse_mode=ParseMode.HTML,
        )
        return


async def _launch_user_bot(uid, user):
    if uid in active_bots:
        return

    instance = UserBot(
        owner_id=uid,
        bot_token=user["bot_token"],
        ivasms_cookies=user["ivasms_cookies"],
        ivasms_user_agent=user["ivasms_user_agent"] or DEFAULT_USER_AGENT,
    )

    task = asyncio.ensure_future(instance.run())
    active_bots[uid] = {"instance": instance, "task": task}

    user["bot_active"] = True
    set_user(uid, user)
    log.info(f"Launched user bot for uid={uid}")


# ═══════════════════════════════════════════════════════════════════════
#  🛡️  ADMIN COMMANDS
# ═══════════════════════════════════════════════════════════════════════

def _admin_check(update):
    return update.effective_user.id in ADMIN_IDS


async def admin_users(update, ctx):
    if not _admin_check(update):
        return
    users = _data.get("users", {})
    total = len(users)
    active = sum(1 for u in users.values() if u.get("bot_active"))
    lines = [
        "╔══════════════════════════════════╗",
        "║       🛡️  Admin — All Users      ║",
        "╚══════════════════════════════════╝\n",
        f"Total: <b>{total}</b>  |  Active bots: <b>{active}</b>\n",
        DIVIDER,
    ]
    for uid_str, u in list(users.items())[-25:]:
        bot_icon = "🟢" if u.get("bot_active") else "🔴"
        ban_icon = "🚫" if u.get("banned") else ""
        pts = u.get("points", 0)
        lines.append(
            f"{bot_icon}{ban_icon} <code>{uid_str}</code> — <b>{u.get('name','?')}</b>\n"
            f"   refs={len(u.get('referrals',[]))} | pts={pts} | joined={u.get('joined_channels',False)}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def admin_broadcast(update, ctx):
    if not _admin_check(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /broadcast Your message here")
        return
    msg = " ".join(ctx.args)
    sent = 0
    failed = 0
    for uid_str in _data.get("users", {}):
        try:
            await ctx.bot.send_message(
                int(uid_str),
                f"📢 <b>Announcement:</b>\n\n{msg}",
                parse_mode=ParseMode.HTML,
            )
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(
        f"📢 <b>Broadcast Complete!</b>\n✅ Sent: {sent}\n❌ Failed: {failed}"
    )


async def admin_stats(update, ctx):
    if not _admin_check(update):
        return
    users = _data.get("users", {})
    total = len(users)
    verified = sum(1 for u in users.values() if u.get("joined_channels"))
    eligible = sum(1 for u in users.values() if can_create_bot(int(u.get("id", 0))))
    configured = sum(1 for u in users.values() if u.get("bot_token"))
    active = sum(1 for u in users.values() if u.get("bot_active"))
    banned = sum(1 for u in users.values() if u.get("banned"))
    total_pts = sum(u.get("points", 0) for u in users.values())
    total_sms = sum(active_bots[uid]["instance"].sms_count for uid in active_bots)
    total_otp = sum(active_bots[uid]["instance"].otp_count for uid in active_bots)
    await update.message.reply_text(
        "╔══════════════════════════════════╗\n"
        "║       📊  Platform Stats         ║\n"
        "╚══════════════════════════════════╝\n\n"
        f"👥 Total Users: <b>{total}</b>\n"
        f"✅ Channel Verified: <b>{verified}</b>\n"
        f"🔓 Referral Eligible: <b>{eligible}</b>\n"
        f"⚙️ Configured Bots: <b>{configured}</b>\n"
        f"🟢 Active Bots: <b>{active}</b>\n"
        f"🚫 Banned Users: <b>{banned}</b>\n\n"
        f"{DIVIDER}\n"
        f"📨 Total SMS Received: <b>{total_sms}</b>\n"
        f"🔑 Total OTPs Delivered: <b>{total_otp}</b>\n"
        f"⭐ Total Points Issued: <b>{total_pts}</b>\n\n"
        f"⏱ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        parse_mode=ParseMode.HTML,
    )


async def admin_ban_user(update, ctx):
    if not _admin_check(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /banuser <user_id>")
        return
    try:
        target = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    user = get_user(target)
    if not user:
        await update.message.reply_text("❌ User not found.")
        return
    if target in active_bots:
        active_bots[target]["instance"].stop()
        active_bots.pop(target)
    user["bot_active"] = False
    user["bot_token"] = None
    user["joined_channels"] = False
    user["banned"] = True
    set_user(target, user)
    try:
        await ctx.bot.send_message(
            target,
            "🚫 <b>You have been banned from this platform.</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass
    await update.message.reply_text(
        f"✅ User <code>{target}</code> banned.",
        parse_mode=ParseMode.HTML,
    )


async def admin_unban_user(update, ctx):
    if not _admin_check(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /unbanuser <user_id>")
        return
    try:
        target = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    user = get_user(target)
    if not user:
        await update.message.reply_text("❌ User not found.")
        return
    user["banned"] = False
    set_user(target, user)
    try:
        await ctx.bot.send_message(
            target,
            "✅ <b>You have been unbanned!</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass
    await update.message.reply_text(
        f"✅ User <code>{target}</code> unbanned.",
        parse_mode=ParseMode.HTML,
    )


async def admin_add_ref(update, ctx):
    if not _admin_check(update):
        return
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: /addref <user_id> <count>")
        return
    try:
        target = int(ctx.args[0])
        count = int(ctx.args[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid arguments.")
        return
    user = get_user(target)
    if not user:
        await update.message.reply_text("❌ User not found.")
        return
    existing = user.get("referrals", [])
    for i in range(count):
        existing.append(-(len(existing) + i + 1))
    user["referrals"] = existing
    set_user(target, user)
    await update.message.reply_text(
        f"✅ Granted <b>{count}</b> referral(s) to <code>{target}</code>.\n"
        f"New total: <b>{len(user['referrals'])}</b>",
        parse_mode=ParseMode.HTML,
    )


async def admin_reset_user(update, ctx):
    if not _admin_check(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /resetuser <user_id>")
        return
    try:
        target = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    user = get_user(target)
    if not user:
        await update.message.reply_text("❌ User not found.")
        return
    if target in active_bots:
        active_bots[target]["instance"].stop()
        active_bots.pop(target)
    user["bot_token"] = None
    user["ivasms_cookies"] = None
    user["ivasms_user_agent"] = None
    user["bot_active"] = False
    user["setup_step"] = None
    user["bot_created_at"] = None
    set_user(target, user)
    try:
        await ctx.bot.send_message(
            target,
            "🔄 <b>Your bot setup has been reset by an admin.</b>\n\nUse /start to set up again.",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass
    await update.message.reply_text(
        f"✅ User <code>{target}</code> reset.",
        parse_mode=ParseMode.HTML,
    )


async def admin_update_cookies(update, ctx):
    if not _admin_check(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /updatecookies <user_id>")
        return
    try:
        target = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    user = get_user(target)
    if not user:
        await update.message.reply_text("❌ User not found.")
        return
    try:
        await ctx.bot.send_message(
            target,
            "🍪 <b>Please update your iVASMS cookies.</b>\n\n"
            "An admin has requested a cookie refresh.\n\n"
            "Use /start → 🍪 Update Cookies and paste the new cookie "
            "string (raw header or Cookie-Editor JSON).",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass
    await update.message.reply_text(
        f"✅ Notified <code>{target}</code> to update cookies.",
        parse_mode=ParseMode.HTML,
    )


async def admin_stop_all_bots(update, ctx):
    if not _admin_check(update):
        return
    count = len(active_bots)
    if count == 0:
        await update.message.reply_text("ℹ️ No bots are currently running.")
        return
    for uid, entry in list(active_bots.items()):
        entry["instance"].stop()
        user = get_user(uid)
        if user:
            user["bot_active"] = False
            set_user(uid, user)
    active_bots.clear()
    await update.message.reply_text(
        f"🛑 Stopped <b>{count}</b> bot(s).",
        parse_mode=ParseMode.HTML,
    )


async def admin_user_info(update, ctx):
    if not _admin_check(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /userinfo <user_id>")
        return
    try:
        target = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    user = get_user(target)
    if not user:
        await update.message.reply_text("❌ User not found.")
        return

    bot_running = target in active_bots
    instance = active_bots[target]["instance"] if bot_running else None

    sms_count = instance.sms_count if instance else 0
    otp_count = instance.otp_count if instance else 0
    err_count = instance.error_count if instance else 0
    last_err = instance.last_error if instance else "None"
    uptime = str(datetime.now() - instance.started_at).split(".")[0] if instance else "—"
    sock = "🟢" if (instance and instance.socket_connected) else "🔴"

    ref_list = user.get("referrals", [])
    ref_display = ", ".join(str(r) for r in ref_list[:5])
    if len(ref_list) > 5:
        ref_display += f" +{len(ref_list)-5} more"

    cookies_set = "✅ Set" if user.get("ivasms_cookies") else "❌ Not set"
    ua_set = "✅ Set" if user.get("ivasms_user_agent") else "❌ Not set"

    await update.message.reply_text(
        "╔══════════════════════════════════╗\n"
        "║       🔍  User Info              ║\n"
        "╚══════════════════════════════════╝\n\n"
        f"👤 <b>Name:</b> {user.get('name','?')}\n"
        f"🆔 <b>ID:</b> <code>{target}</code>\n"
        f"📛 <b>Username:</b> @{user.get('username') or 'none'}\n"
        f"📅 <b>Registered:</b> {user.get('registered_at','?')[:19]}\n"
        f"🚫 <b>Banned:</b> {'Yes' if user.get('banned') else 'No'}\n\n"
        f"{DIVIDER}\n"
        f"📢 <b>Channels Joined:</b> {'✅' if user.get('joined_channels') else '❌'}\n"
        f"👥 <b>Referrals:</b> {len(ref_list)} — [{ref_display}]\n"
        f"🎯 <b>Referred By:</b> {user.get('referred_by') or 'None'}\n"
        f"⭐ <b>Points:</b> {user.get('points', 0)}\n\n"
        f"{DIVIDER}\n"
        f"🤖 <b>Bot Active:</b> {'🟢 Yes' if bot_running else '🔴 No'}\n"
        f"🔌 <b>Socket:</b> {sock}\n"
        f"🍪 <b>iVASMS Cookies:</b> {cookies_set}\n"
        f"🖥 <b>User-Agent:</b> {ua_set}\n"
        f"🕒 <b>Cookies Updated:</b> {(user.get('cookies_updated_at') or '—')[:19]}\n"
        f"⏱ <b>Bot Uptime:</b> {uptime}\n"
        f"📨 <b>SMS Received:</b> {sms_count}\n"
        f"🔑 <b>OTPs Delivered:</b> {otp_count}\n"
        f"❌ <b>Errors:</b> {err_count}\n"
        f"⚠️ <b>Last Error:</b> {str(last_err)[:60] if last_err else 'None'}",
        parse_mode=ParseMode.HTML,
    )


async def admin_add_points(update, ctx):
    if not _admin_check(update):
        return
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: /addpoints <user_id> <points>")
        return
    try:
        target = int(ctx.args[0])
        pts = int(ctx.args[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid arguments.")
        return
    if pts <= 0:
        await update.message.reply_text("❌ Points must be positive.")
        return
    user = get_user(target)
    if not user:
        await update.message.reply_text("❌ User not found.")
        return
    user["points"] = user.get("points", 0) + pts
    set_user(target, user)
    try:
        await ctx.bot.send_message(
            target,
            f"⭐ <b>+{pts} Points Added!</b>\n\nYour balance: <b>{user['points']} pts</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass
    await update.message.reply_text(
        f"✅ Added <b>{pts} points</b> to <code>{target}</code>. New balance: <b>{user['points']}</b>",
        parse_mode=ParseMode.HTML,
    )


async def admin_remove_points(update, ctx):
    if not _admin_check(update):
        return
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: /rempoints <user_id> <points>")
        return
    try:
        target = int(ctx.args[0])
        pts = int(ctx.args[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid arguments.")
        return
    if pts <= 0:
        await update.message.reply_text("❌ Points must be positive.")
        return
    user = get_user(target)
    if not user:
        await update.message.reply_text("❌ User not found.")
        return
    old = user.get("points", 0)
    user["points"] = max(0, old - pts)
    set_user(target, user)
    deducted = old - user["points"]
    try:
        await ctx.bot.send_message(
            target,
            f"⚠️ <b>-{deducted} Points</b>\n\nYour balance: <b>{user['points']} pts</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass
    await update.message.reply_text(
        f"✅ Removed <b>{deducted} points</b> from <code>{target}</code>. New balance: <b>{user['points']}</b>",
        parse_mode=ParseMode.HTML,
    )


async def admin_bots(update, ctx):
    if not _admin_check(update):
        return

    if not active_bots:
        await update.message.reply_text("ℹ️ No bots are currently running.")
        return

    now = datetime.now()
    active_list, error_list, heavy_list, idle_list = [], [], [], []

    for uid, entry in active_bots.items():
        inst = entry["instance"]
        user = get_user(uid)
        name = user.get("name", "?") if user else "?"
        uptime = str(now - inst.started_at).split(".")[0]
        info = {
            "uid": uid, "name": name, "uptime": uptime,
            "sms": inst.sms_count, "otp": inst.otp_count,
            "errs": inst.error_count, "status": inst.status,
            "last_error": inst.last_error, "cookies_expired": inst.cookies_expired,
            "socket": inst.socket_connected,
        }
        if inst.cookies_expired:
            error_list.append(info)
        elif inst.status == "error" or inst.error_count >= 5:
            error_list.append(info)
        elif inst.sms_count >= 20:
            heavy_list.append(info)
        elif inst.sms_count == 0 and inst.otp_count == 0:
            idle_list.append(info)
        else:
            active_list.append(info)

    def fmt_bot(b, icon):
        sock = "🔌" if b.get("socket") else "🚫"
        return (
            f"{icon}{sock} <code>{b['uid']}</code> — <b>{b['name']}</b>\n"
            f"   ⏱ {b['uptime']} | 📨 {b['sms']} SMS | 🔑 {b['otp']} OTP | ❌ {b['errs']} err"
        )

    lines = [
        "╔══════════════════════════════════╗",
        "║       🤖  Bot Dashboard          ║",
        "╚══════════════════════════════════╝\n",
        f"Total Running: <b>{len(active_bots)}</b>  "
        f"🟢 {len(active_list)}  🔴 {len(error_list)}  🔥 {len(heavy_list)}  😴 {len(idle_list)}\n",
    ]

    if active_list:
        lines.append(DIVIDER)
        lines.append(f"🟢 <b>Active ({len(active_list)})</b>")
        for b in active_list:
            lines.append(fmt_bot(b, "🟢"))
    if heavy_list:
        lines.append(f"\n{DIVIDER}")
        lines.append(f"🔥 <b>Heavy Traffic ({len(heavy_list)})</b>")
        for b in heavy_list:
            lines.append(fmt_bot(b, "🔥"))
    if error_list:
        lines.append(f"\n{DIVIDER}")
        lines.append(f"🔴 <b>Errored ({len(error_list)})</b>")
        for b in error_list:
            tag = "🍪" if b.get("cookies_expired") else "🔴"
            lines.append(fmt_bot(b, tag))
            if b.get("last_error"):
                lines.append(f"   ⚠️ <i>{str(b['last_error'])[:80]}</i>")
    if idle_list:
        lines.append(f"\n{DIVIDER}")
        lines.append(f"😴 <b>Idle ({len(idle_list)})</b>")
        for b in idle_list:
            lines.append(fmt_bot(b, "😴"))

    lines.append(f"\n{DIVIDER}")
    lines.append(f"⏱ <i>{now.strftime('%Y-%m-%d %H:%M:%S')}</i>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def admin_add_all_points(update, ctx):
    if not _admin_check(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /addall <points>")
        return
    try:
        pts = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid number.")
        return
    if pts <= 0:
        await update.message.reply_text("❌ Points must be positive.")
        return

    users = _data.get("users", {})
    total = len(users)
    if total == 0:
        await update.message.reply_text("ℹ️ No users.")
        return

    await update.message.reply_text(
        f"⏳ Adding <b>{pts} points</b> to <b>{total} users</b>…",
        parse_mode=ParseMode.HTML,
    )

    notified = 0
    failed = 0
    for uid_str, u in users.items():
        u["points"] = u.get("points", 0) + pts
        set_user(int(uid_str), u)
        try:
            await ctx.bot.send_message(
                int(uid_str),
                f"🎁 <b>+{pts} Points Added!</b>\n\nYour balance: <b>{u['points']} pts</b>",
                parse_mode=ParseMode.HTML,
            )
            notified += 1
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"✅ <b>Done!</b>\n\n👥 Users: <b>{total}</b>\n📩 Notified: <b>{notified}</b>\n❌ Failed: <b>{failed}</b>",
        parse_mode=ParseMode.HTML,
    )


# ═══════════════════════════════════════════════════════════════════════
#  🚀  MAIN
# ═══════════════════════════════════════════════════════════════════════

async def main():
    global _master_app, _master_loop

    load_data()
    _master_loop = asyncio.get_event_loop()

    log.info("=" * 60)
    log.info(f"  iVASMS MASTER BOT — Starting (curl_cffi={HAS_CURL_CFFI})")
    log.info(f"  Impersonate profile: {IMPERSONATE}")
    log.info(f"  Socket transports:   {SOCKET_TRANSPORTS}")
    log.info(f"  Socket debug:        {SOCKET_DEBUG}")
    log.info("=" * 60)

    _master_app = Application.builder().token(MASTER_BOT_TOKEN).build()

    _master_app.add_handler(CommandHandler("start", master_start))
    _master_app.add_handler(CallbackQueryHandler(master_callback))
    _master_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, master_text))

    _master_app.add_handler(CommandHandler("users", admin_users))
    _master_app.add_handler(CommandHandler("stats", admin_stats))
    _master_app.add_handler(CommandHandler("broadcast", admin_broadcast))
    _master_app.add_handler(CommandHandler("banuser", admin_ban_user))
    _master_app.add_handler(CommandHandler("unbanuser", admin_unban_user))
    _master_app.add_handler(CommandHandler("addref", admin_add_ref))
    _master_app.add_handler(CommandHandler("resetuser", admin_reset_user))
    _master_app.add_handler(CommandHandler("updatecookies", admin_update_cookies))
    _master_app.add_handler(CommandHandler("stopallbots", admin_stop_all_bots))
    _master_app.add_handler(CommandHandler("userinfo", admin_user_info))
    _master_app.add_handler(CommandHandler("addpoints", admin_add_points))
    _master_app.add_handler(CommandHandler("rempoints", admin_remove_points))
    _master_app.add_handler(CommandHandler("bots", admin_bots))
    _master_app.add_handler(CommandHandler("addall", admin_add_all_points))

    for uid_str, user in _data.get("users", {}).items():
        if (
            user.get("bot_active")
            and user.get("bot_token")
            and user.get("ivasms_cookies")
        ):
            uid = int(uid_str)
            if bot_is_expired(uid):
                user["bot_active"] = False
                set_user(uid, user)
                log.info(f"Skipping expired bot for user {uid}")
                continue
            log.info(f"Re-launching bot for user {uid}")
            asyncio.ensure_future(_launch_user_bot(uid, user))

    asyncio.ensure_future(expiry_watchdog())
    log.info("⏱ Expiry watchdog started.")

    for admin_id in ADMIN_IDS:
        try:
            await _master_app.bot.send_message(
                admin_id,
                "╔══════════════════════════════════╗\n"
                "║  🚀  Master Bot is Now LIVE!      ║\n"
                "╚══════════════════════════════════╝\n\n"
                "<b>📊 Info</b>\n"
                "/stats, /users, /userinfo &lt;id&gt;, /bots\n\n"
                "<b>🛡️ Users</b>\n"
                "/banuser, /unbanuser, /resetuser, /addref\n\n"
                "<b>🍪 Cookies</b>\n"
                "/updatecookies &lt;id&gt;\n\n"
                "<b>⭐ Points</b>\n"
                "/addpoints, /rempoints, /addall\n\n"
                "<b>📢 Other</b>\n"
                "/broadcast, /stopallbots\n\n"
                f"🔧 curl_cffi: {'✅' if HAS_CURL_CFFI else '❌'}\n"
                f"🎭 Impersonate: <code>{IMPERSONATE}</code>\n"
                f"🔌 Transports: <code>{', '.join(SOCKET_TRANSPORTS)}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    log.info("🤖 Master bot polling started.")
    await _master_app.initialize()
    await _master_app.start()
    await _master_app.updater.start_polling(drop_pending_updates=True)

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down…")
    finally:
        for entry in active_bots.values():
            entry["instance"].stop()
        await _master_app.updater.stop()
        await _master_app.stop()
        await _master_app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
