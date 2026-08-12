"""
 Railway maintenance redeploy marker: Telegram session recovery 2026-08-12.
 Kyiv Alert Monitor v6 — low-latency async pipeline
- Production trigger: @kyiv_airraid_alert
- Normal mode: hourly analysis of 3 channels published in the news group
- Alert mode (24/7): only @kievreal1 in the alert-only channel
- Night pause: no hourly summaries 01:00-07:00 Europe/Kyiv, one big recap at 07:00
- Health check every 12h: private warning to owner if channels go silent
"""
import asyncio
import html
import json
import os
import re
import sqlite3
import time
import httpx
from collections import deque
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from alert_rules import classify_telegram_alert

# --- Credentials ---
TELEGRAM_API_ID = int(os.environ["TELEGRAM_API_ID"])
TELEGRAM_API_HASH = os.environ["TELEGRAM_API_HASH"]
TELEGRAM_SESSION = os.environ["TELEGRAM_SESSION"]
TEST_TELEGRAM_SESSION = os.environ.get("TEST_TELEGRAM_SESSION")
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_USER_ID = int(BOT_TOKEN.split(":", 1)[0])
TEST_MODE = os.environ.get("TEST_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}
ALERT_CHANNEL_ID = os.environ["TARGET_CHAT_ID"]
SUMMARY_CHAT_ID = os.environ.get("SUMMARY_CHAT_ID")
SUMMARY_CHAT_LINK = os.environ.get("SUMMARY_CHAT_LINK")
TEST_CHAT_ID = os.environ.get("TEST_CHAT_ID")
if TEST_MODE and not TEST_CHAT_ID:
    raise RuntimeError("TEST_CHAT_ID is required when TEST_MODE=true")
if not TEST_MODE and not SUMMARY_CHAT_ID:
    raise RuntimeError("SUMMARY_CHAT_ID is required when TEST_MODE=false")
if not TEST_MODE and not SUMMARY_CHAT_LINK:
    raise RuntimeError("SUMMARY_CHAT_LINK is required when TEST_MODE=false")
if not TEST_MODE and "t.me/kyivairalert" in SUMMARY_CHAT_LINK.lower():
    raise RuntimeError("SUMMARY_CHAT_LINK must point to the news group, not the alert channel")
if not TEST_MODE and SUMMARY_CHAT_ID == ALERT_CHANNEL_ID:
    raise RuntimeError("SUMMARY_CHAT_ID must differ from TARGET_CHAT_ID")
ALERT_OUTPUT_CHAT_ID = TEST_CHAT_ID if TEST_MODE else ALERT_CHANNEL_ID
SUMMARY_OUTPUT_CHAT_ID = TEST_CHAT_ID if TEST_MODE else SUMMARY_CHAT_ID
OWNER_CHAT_ID = os.environ["OWNER_CHAT_ID"]
OPS_CHAT_ID = os.environ.get("OPS_CHAT_ID", OWNER_CHAT_ID)  # operational alerts; legacy fallback
ADMIN_USER_IDS = {
    int(value.strip())
    for value in os.environ.get("ADMIN_USER_IDS", OWNER_CHAT_ID).split(",")
    if value.strip()
}
# --- Channels ---
KYIV_INFO_CHANNEL = "kievinfo_kyiv"
AMK_CHANNEL = "AMK_Mapping"
ALERT_FEED_CHANNEL = "kievreal1"
UKRAINE_NEWS_CHANNEL = "shv_ukr"
BACKUP_TRIGGER_CHANNEL = "kyiv_airraid_alert"
ALL_CONTENT_CHANNELS = [KYIV_INFO_CHANNEL, UKRAINE_NEWS_CHANNEL, AMK_CHANNEL]
ALERT_FEED_CHANNELS = [ALERT_FEED_CHANNEL]

SUMMARY_INTERVAL = 180 if TEST_MODE else 3600  # 3 minutes in test, 1 hour in production
HEALTH_CHECK_INTERVAL = 43200  # 12 hours
SILENCE_THRESHOLD = 4 * 3600  # 4 hours of total silence = warning
ALERT_FEED_POLL_INTERVAL = float(os.environ.get("ALERT_FEED_POLL_INTERVAL", "5"))
ALERT_RECOVERY_MAX_MESSAGES = int(os.environ.get("ALERT_RECOVERY_MAX_MESSAGES", "3"))

# --- Timezone / night pause ---
TZ = ZoneInfo("Europe/Kyiv")  # EET/EEST auto
NIGHT_START = 1   # 01:00 EET/EEST
NIGHT_END = 7     # 07:00 EET/EEST

MODEL = "claude-haiku-4-5"

ALERT_START_MESSAGE = "🚨 <b>AIR ALERT — KYIV</b>"


def build_all_clear_message():
    """Return the public all-clear message with a safe link to the news group."""
    safe_link = html.escape(SUMMARY_CHAT_LINK or "", quote=True)
    return (
        "✅ <b>ALL CLEAR — KYIV</b>\n\n"
        f'<a href="{safe_link}">Join Kyiv News →</a>'
    )

# --- Cross-source categories (normal mode) ---
# A source is only provenance. Every buffered message is evaluated against every category.
CATEGORIES = {
    "kyiv_city": {
        "name": "Kyiv City",
        "icon": "🏙️",
        "criteria": (
            "Concrete disruptions or consequences affecting life in Kyiv city: roads, bridges, traffic, "
            "metro/public transport, power, water, heating, fires, accidents, police operations, curfews, "
            "shelters, evacuations, damage, casualties, or direct consequences of attacks on Kyiv."
        ),
    },
    "ukraine_national": {
        "name": "Ukrainian National Developments",
        "icon": "🇺🇦",
        "criteria": (
            "Consequential Ukrainian political, governmental, parliamentary, legislative, economic, energy, "
            "diplomatic, sanctions, international-support, corruption, legal, or civil-society developments."
        ),
    },
    "military": {
        "name": "Military Developments",
        "icon": "🗺️",
        "criteria": (
            "Russia-Ukraine war developments: frontline changes, troop movements, offensives, operations, "
            "equipment or notable losses, fortifications, preparations, and strategic analysis. Exclude "
            "Middle East events unless they directly and materially concern the war in Ukraine."
        ),
    },
    "air_defence": {
        "name": "Air Defence Monitoring",
        "icon": "⚔️",
        "criteria": (
            "Missile, Shahed or other UAV launches and movements, aviation activity, air-defence actions, "
            "interceptions, impacts, affected areas, and numerical attack recaps. Preserve all stated counts "
            "and never invent or combine incompatible quantities."
        ),
    },
}

CATEGORY_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "categories": {
            "type": "object",
            "properties": {
                key: {
                    "type": "object",
                    "properties": {
                        "selected_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "bullets": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["selected_ids", "bullets"],
                    "additionalProperties": False,
                }
                for key in CATEGORIES
            },
            "required": list(CATEGORIES.keys()),
            "additionalProperties": False,
        }
    },
    "required": ["categories"],
    "additionalProperties": False,
}


CATEGORY_STATS_DB_PATH = os.environ.get(
    "CATEGORY_STATS_DB_PATH", "/data/kyiv_monitor_category_stats.sqlite3"
)
NORMAL_HISTORY_BOOTSTRAP_HOURS = 1
NORMAL_MESSAGE_RETENTION_DAYS = 7

# --- Pre-filters ---
AD_INDICATORS = ["#реклама","реклама","знижк","розпродаж","промокод","купуй","придбай","акція","магазин","замовляй","доставка"]
DONATION_INDICATORS = [
    "донат", "донатів", "підтримати", "підтримайте", "підтримку", "підтримкою",
    "кавою", "на каву", "mono", "моно", "monobank", "приват", "privat",
    "банка", "банку", "збір", "збору", "картка", "карта", "реквізит",
]
ENGAGEMENT_INDICATORS = [
    "щиро вдяч", "дуже вдяч", "дякуємо кожному", "ви найкращі", "тримаємось",
    "з днем", "вітаємо", "вітаю", "найкращі були та будете",
]
SECURITY_KEYWORDS = ["тривога","відбій","балістика","ракета","шахед","шахеди","бпла","вибух","вибухи","ппо","повітряна ціль","укриття","загроза","обстріл","приліт","дрон","дрони","mig","міг","siren","raid","missile","drone","explosion","alert","attack","ballistic","shahed","interception","strike"]
ALERT_TACTICAL_KEYWORDS = SECURITY_KEYWORDS + [
    "ціль", "цілі", "рух", "рухається", "рухаються", "курс", "напрям", "летить",
    "летять", "чисто", "знищено", "знищена", "знищений", "збито", "увага",
    "target", "heading", "moving", "destroyed", "shot down", "area clear",
]
ALERT_TERSE_FOLLOWUP_KEYWORDS = [
    "підліта", "в бік", "у бік", "на бориспіль", "на бровари", "від броварів",
    "через водосховище", "уважно", "гучно", "димер", "згурів", "троєщин",
    "рембаз", "осещин", "тец-6", "березан", "українк",
]

# --- State ---
buffers = {ch: [] for ch in ALL_CONTENT_CHANNELS}
alert_active = False
alert_started_at = None
telegram_alert_state = None
last_send_time = 0
last_message_time = time.time()
last_summary_success_time = time.monotonic()
summary_watchdog_attempts = set()
stats_db_ready = False
production_client = None
content_source_entities = {}
MIN_SEND_INTERVAL = 1.0 if TEST_MODE else 0.2

# Created in main(); one shared connection pool avoids a new TLS handshake per message.
http_client = None
send_lock = None
test_command_lock = None
translation_slots = None
summary_lock = None
alert_transition_lock = None
bot_output_message_ids = set()
simulator_processed_message_ids = set()
recent_alert_messages = deque()
alert_delivery_tasks = set()
alert_generation = 0
ALERT_DEDUP_WINDOW = 180
TEST_SOURCE_PREFIX = "[TEST_SOURCE:kievreal1]"
TEST_BUFFER_CHANNEL = AMK_CHANNEL
TEST_SAMPLE_MESSAGES = [
    "⚠️ Київщина: зафіксовано рух ударних БпЛА Shahed drone у напрямку Києва.",
    "Ракетна небезпека: missile launch activity зафіксована з північного напрямку.",
    "Група БпЛА продовжує рух; air-defense monitoring reports drone activity near Kyiv region.",
]


def contains_any(text, keywords):
    return any(k.lower() in text.lower() for k in keywords)


def is_authorized_admin(sender_id):
    return sender_id in ADMIN_USER_IDS

def is_non_operational_alert_message(text):
    """Reject fundraising, payment details, thanks and greeting posts even if they mention attacks."""
    compact_digits = re.sub(r"[\s-]", "", text)
    has_payment_number = bool(re.search(r"(?<!\d)\d{13,19}(?!\d)", compact_digits))
    return (
        has_payment_number
        or contains_any(text, DONATION_INDICATORS)
        or contains_any(text, ENGAGEMENT_INDICATORS)
    )

def is_actionable_alert_message(text):
    """Allow explicit threats plus the terse location/direction follow-ups used by @kievreal1."""
    if contains_any(text, ALERT_TACTICAL_KEYWORDS):
        return True
    clean = clean_text(text)
    if len(clean) > 500:
        return False
    if contains_any(clean, ALERT_TERSE_FOLLOWUP_KEYWORDS):
        return True
    # @kievreal1 frequently posts terse course updates such as
    # "На Тарасівку від Боярки" without repeating "БпЛА".
    return bool(re.match(r"^(?:ще\s+)?на\s+.{2,80}\s+(?:від|з|із|зі)\s+.{2,80}[.!]?\s*$", clean, re.I))

def is_pure_ad(text):
    if contains_any(text, SECURITY_KEYWORDS):
        return False
    return contains_any(text, AD_INDICATORS)

def normalize_alert_for_dedup(text):
    """Normalize formatting noise while retaining locations, targets and quantities."""
    normalized = clean_text(text).lower()
    normalized = re.sub(r"https?://\S+|t\.me/\S+|@\w+", " ", normalized)
    normalized = re.sub(r"[^\w\s]", " ", normalized, flags=re.UNICODE)
    return re.sub(r"\s+", " ", normalized).strip()

def should_publish_alert(text, source, now=None):
    """First arrival wins; suppress only near-identical reports seen in the last three minutes."""
    timestamp = time.monotonic() if now is None else now
    normalized = normalize_alert_for_dedup(text)
    if not normalized:
        return False

    while recent_alert_messages and timestamp - recent_alert_messages[0][0] > ALERT_DEDUP_WINDOW:
        recent_alert_messages.popleft()

    tokens = set(normalized.split())
    for _, previous_source, previous_text, previous_tokens in recent_alert_messages:
        if normalized == previous_text:
            print(f"[ALERT DUPLICATE] @{source} matches @{previous_source}: {normalized[:100]}")
            return False
        union = tokens | previous_tokens
        jaccard = len(tokens & previous_tokens) / len(union) if union else 0.0
        similarity = SequenceMatcher(None, normalized, previous_text).ratio()
        enough_context = min(len(tokens), len(previous_tokens)) >= 4
        if enough_context and ((similarity >= 0.86 and jaccard >= 0.62) or jaccard >= 0.82):
            print(
                f"[ALERT DUPLICATE] @{source} near @{previous_source} "
                f"similarity={similarity:.2f} jaccard={jaccard:.2f}: {normalized[:100]}"
            )
            return False

    recent_alert_messages.append((timestamp, source, normalized, tokens))
    return True

def clean_text(text):
    text = re.sub(r'^\s*#\w+\s*', '', text)
    text = re.sub(r'\s*#\w+\s*', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def clean_alert_source_text(text):
    """Remove @kievreal1 promotional footers before filtering and translation."""
    clean = clean_text(text)
    footer_patterns = (
        r"(?im)^\s*(?:надіслати новину|send news|report news)\b.*$",
        r"(?im)^\s*(?:👉\s*)?(?:підписатися|subscribe)\s*$",
        r"(?im)^\s*ㅤ\s*$",
    )
    for pattern in footer_patterns:
        clean = re.sub(pattern, "", clean)
    return re.sub(r"\n{3,}", "\n\n", clean).strip()

def is_night():
    h = datetime.now(TZ).hour
    return NIGHT_START <= h < NIGHT_END


def seconds_until_next_hour():
    """Return the delay to the next exact Europe/Kyiv clock hour."""
    now = datetime.now(TZ)
    elapsed = now.minute * 60 + now.second + now.microsecond / 1_000_000
    return max(0.1, 3600 - elapsed)

async def reconcile_alert_state(source):
    """Apply the explicit trigger state and retry when public delivery fails."""
    return await apply_alert_state(telegram_alert_state, source)


def set_alert_state(new_state, reason):
    """The only function allowed to mutate the effective alert state."""
    global alert_active, alert_started_at, alert_generation
    if new_state == alert_active:
        return False
    previous = alert_active
    alert_active = new_state
    alert_started_at = time.monotonic() if new_state else None
    alert_generation += 1
    persist_operational_state("alert_active", "1" if new_state else "0")
    print(
        f"[ALERT STATE] {previous} -> {new_state} reason={reason} "
        f"generation={alert_generation}"
    )
    return True


async def drain_alert_delivery_tasks(timeout=5.0):
    """Finish or cancel translations before publishing ALL CLEAR."""
    active = [task for task in alert_delivery_tasks if not task.done()]
    if not active:
        return
    done, pending = await asyncio.wait(active, timeout=timeout)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    print(f"[ALERT TASKS] completed={len(done)} cancelled={len(pending)}")


async def apply_alert_state(desired, source, *, startup=False, public_message=None):
    """Serialize an alert transition and commit it only after delivery succeeds."""
    global alert_transition_lock
    if desired is None:
        print("⚠️ No valid Telegram alert state; preserving last known state")
        return False
    if alert_transition_lock is None:
        alert_transition_lock = asyncio.Lock()
    async with alert_transition_lock:
        if desired == alert_active:
            return True

        if startup:
            set_alert_state(desired, f"startup:{source}")
            if desired:
                recent_alert_messages.clear()
                for channel_name in ALL_CONTENT_CHANNELS:
                    buffers[channel_name].clear()
                discard_pending_normal_messages(f"startup_alert:{source}")
                await send_to_owner(
                    "⚠️ <b>ALERT state restored after restart</b>\n"
                    "The worker entered ALERT without duplicating the public start message."
                )
            return True

        if not desired:
            await drain_alert_delivery_tasks()
        message = public_message or (ALERT_START_MESSAGE if desired else build_all_clear_message())
        delivered = await send_to_alert_channel(message)
        if not delivered:
            await send_to_owner(
                "🚨 <b>Alert transition delivery failed</b>\n"
                f"Requested state: {'ALERT' if desired else 'NORMAL'}; source: {html.escape(source)}. "
                "The transition was not committed and will be retried on the next trigger."
            )
            return False

        set_alert_state(desired, source)
        if desired:
            recent_alert_messages.clear()
            for channel_name in ALL_CONTENT_CHANNELS:
                buffers[channel_name].clear()
            discard_pending_normal_messages(f"alert_started:{source}")
        elif production_client and content_source_entities:
            await advance_normal_cursors_to_latest(
                production_client, content_source_entities, f"alert_ended:{source}"
            )
        print(f"[ALERT TRANSITION] committed={'ALERT' if desired else 'NORMAL'} source={source}")
        return True


async def translate_message(text):
    try:
        async with translation_slots:
            r = await http_client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": MODEL, "max_tokens": 1000, "messages": [{"role": "user", "content": (
                "Translate this Ukrainian/Russian military-alert message into English. "
                "Output ONLY the translation. Never add notes, disclaimers, explanations, alternative "
                "readings, or comments about OCR/ambiguity. If a word is a place name, keep it as the "
                "place name. Known place names include: Brovary, Bucha, Irpin, Vyshhorod, Boryspil, "
                "Obukhiv, Fastiv, Bila Tserkva, Kharkiv, Dnipro, Odesa, Lviv, Zaporizhzhia. "
                "Remove promo/subscribe/LIVE tags. Keep locations, times, quantities, and uncertainty. "
                "Translation only:\n\n" + text)}]},
            timeout=httpx.Timeout(15.0, connect=5.0)
            )
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"Translation error: {e}")
        return None

def initialize_stats_db():
    """Create the persistent statistics and NORMAL-message store."""
    try:
        db_dir = os.path.dirname(CATEGORY_STATS_DB_PATH)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        with sqlite3.connect(CATEGORY_STATS_DB_PATH) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS hourly_category_stats (
                    run_at TEXT NOT NULL,
                    category TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    received INTEGER NOT NULL,
                    valid INTEGER NOT NULL,
                    PRIMARY KEY (run_at, category, channel)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS hourly_classifications (
                    run_at TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    category TEXT NOT NULL,
                    preview TEXT NOT NULL,
                    PRIMARY KEY (run_at, message_id, category)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS normal_messages (
                    channel TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    message_at TEXT NOT NULL,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    processed_at TEXT,
                    PRIMARY KEY (channel, message_id)
                )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS normal_messages_status_time
                   ON normal_messages (status, message_at)"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS source_cursors (
                    channel TEXT PRIMARY KEY,
                    last_message_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS operational_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
        print(f"[STATS DB] ready path={CATEGORY_STATS_DB_PATH}")
        return True
    except Exception as exc:
        print(f"[STATS DB ERROR] persistence unavailable: {type(exc).__name__}: {exc}")
        return False


def utc_iso(value=None):
    """Return a stable UTC timestamp for Telegram messages and database state."""
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def persist_operational_state(key, value):
    """Persist small restart-sensitive state without storing credentials."""
    if not stats_db_ready:
        return False
    try:
        with sqlite3.connect(CATEGORY_STATS_DB_PATH, timeout=5) as connection:
            connection.execute(
                """INSERT INTO operational_state (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value, updated_at = excluded.updated_at""",
                (key, str(value), utc_iso()),
            )
        return True
    except Exception as exc:
        print(f"[STATE DB ERROR] key={key}: {type(exc).__name__}: {exc}")
        return False


def load_operational_state(key):
    if not stats_db_ready:
        return None
    try:
        with sqlite3.connect(CATEGORY_STATS_DB_PATH, timeout=5) as connection:
            row = connection.execute(
                "SELECT value FROM operational_state WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None
    except Exception as exc:
        print(f"[STATE DB ERROR] read key={key}: {type(exc).__name__}: {exc}")
        return None


def persist_trigger_observation(state, message_id, message_at):
    """Record the canonical trigger event used to reconstruct the effective mode."""
    return all((
        persist_operational_state("telegram_alert_state", "1" if state else "0"),
        persist_operational_state("telegram_alert_message_id", message_id),
        persist_operational_state("telegram_alert_message_at", utc_iso(message_at)),
    ))


def alert_feed_cursor_key(channel):
    return f"alert_feed_cursor:{channel}"


def get_alert_feed_cursor(channel):
    value = load_operational_state(alert_feed_cursor_key(channel))
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def set_alert_feed_cursor(channel, message_id):
    return persist_operational_state(alert_feed_cursor_key(channel), int(message_id))


async def ensure_alert_feed_membership(client, source_entities):
    """Join every ALERT feed; resolving a public channel alone does not deliver live updates."""
    for channel in ALERT_FEED_CHANNELS:
        try:
            await client(JoinChannelRequest(source_entities[channel]))
            print(f"[ALERT SOURCE MEMBERSHIP] joined=@{channel}")
        except Exception as exc:
            if type(exc).__name__ == "UserAlreadyParticipantError":
                print(f"[ALERT SOURCE MEMBERSHIP] already_joined=@{channel}")
                continue
            raise RuntimeError(f"Cannot join required ALERT feed @{channel}") from exc


async def backfill_alert_feed(client, source_entities):
    """Recover tactical posts emitted after the trigger but before listener readiness."""
    if not alert_active:
        return 0
    trigger_at_raw = load_operational_state("telegram_alert_message_at")
    trigger_at = None
    if trigger_at_raw:
        try:
            trigger_at = datetime.fromisoformat(trigger_at_raw)
        except ValueError:
            print(f"[ALERT BACKFILL] invalid trigger timestamp={trigger_at_raw!r}")

    delivered = 0
    for channel in ALERT_FEED_CHANNELS:
        cursor = get_alert_feed_cursor(channel)
        messages = await client.get_messages(source_entities[channel], limit=50)
        pending = [message for message in reversed(messages) if message.id > cursor]
        if len(pending) > ALERT_RECOVERY_MAX_MESSAGES:
            skipped = pending[:-ALERT_RECOVERY_MAX_MESSAGES]
            set_alert_feed_cursor(channel, skipped[-1].id)
            print(
                f"[ALERT BACKFILL COLLAPSED] @{channel} skipped={len(skipped)} "
                f"through_id={skipped[-1].id}"
            )
            pending = pending[-ALERT_RECOVERY_MAX_MESSAGES:]
        for message in pending:
            if not alert_active:
                print(f"[ALERT BACKFILL STOPPED] @{channel} reason=clear")
                return delivered
            if message.id <= cursor:
                continue
            if trigger_at and message.date and message.date < trigger_at:
                continue
            clean = clean_alert_source_text(message.text or "")
            if (
                len(clean) < 5
                or is_non_operational_alert_message(clean)
                or is_pure_ad(clean)
                or not is_actionable_alert_message(clean)
            ):
                set_alert_feed_cursor(channel, message.id)
                print(f"[ALERT BACKFILL FILTERED] @{channel} id={message.id}")
                continue
            if should_publish_alert(clean, channel):
                await handle_alert_message(clean, source=channel, generation=alert_generation)
                delivered += 1
            set_alert_feed_cursor(channel, message.id)
            print(f"[ALERT BACKFILL PROCESSED] @{channel} id={message.id}")
    print(f"[ALERT BACKFILL COMPLETE] delivered={delivered}")
    return delivered


async def alert_feed_poll_loop(client, source_entities):
    """Poll trigger and ALERT history so missed push updates cannot create a blind spot."""
    while True:
        try:
            global telegram_alert_state
            latest_trigger = await client.get_messages(source_entities[BACKUP_TRIGGER_CHANNEL], limit=1)
            if latest_trigger:
                trigger = latest_trigger[0]
                observed = classify_telegram_alert(trigger.text or "")
                if observed is not None and observed != telegram_alert_state:
                    telegram_alert_state = observed
                    persist_trigger_observation(observed, trigger.id, trigger.date)
                    print(f"[TRIGGER POLL] {'ACTIVE' if observed else 'CLEAR'} id={trigger.id}")
                    await reconcile_alert_state(f"poll:@{BACKUP_TRIGGER_CHANNEL}")
            if alert_active:
                delivered = await backfill_alert_feed(client, source_entities)
                if delivered:
                    print(f"[ALERT POLL RECOVERED] delivered={delivered}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[ALERT POLL ERROR] {type(exc).__name__}: {exc}")
        await asyncio.sleep(ALERT_FEED_POLL_INTERVAL)


def persist_normal_message(channel, message_id, message_at, text):
    """Persist one live NORMAL message idempotently; return False only on storage failure."""
    if not stats_db_ready:
        return False
    try:
        with sqlite3.connect(CATEGORY_STATS_DB_PATH) as connection:
            connection.execute(
                """INSERT OR IGNORE INTO normal_messages
                   (channel, message_id, message_at, text, status)
                   VALUES (?, ?, ?, ?, 'pending')""",
                (channel, int(message_id), utc_iso(message_at), text[:800]),
            )
        return True
    except Exception as exc:
        print(f"[NORMAL DB ERROR] live insert failed @{channel}: {type(exc).__name__}: {exc}")
        return False


def get_source_cursor(channel):
    if not stats_db_ready:
        return None
    try:
        with sqlite3.connect(CATEGORY_STATS_DB_PATH) as connection:
            row = connection.execute(
                "SELECT last_message_id FROM source_cursors WHERE channel = ?",
                (channel,),
            ).fetchone()
        return int(row[0]) if row else None
    except Exception as exc:
        print(f"[NORMAL DB ERROR] cursor read failed @{channel}: {type(exc).__name__}: {exc}")
        return None


def persist_history_batch(channel, messages, last_message_id):
    """Atomically persist a history batch and its source cursor."""
    if not stats_db_ready:
        return False
    try:
        now = utc_iso()
        with sqlite3.connect(CATEGORY_STATS_DB_PATH) as connection:
            for message in messages:
                connection.execute(
                    """INSERT OR IGNORE INTO normal_messages
                       (channel, message_id, message_at, text, status)
                       VALUES (?, ?, ?, ?, 'pending')""",
                    (
                        channel,
                        int(message["message_id"]),
                        message["message_at"],
                        message["text"][:800],
                    ),
                )
            if last_message_id is not None:
                connection.execute(
                    """INSERT INTO source_cursors (channel, last_message_id, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(channel) DO UPDATE SET
                           last_message_id = excluded.last_message_id,
                           updated_at = excluded.updated_at""",
                    (channel, int(last_message_id), now),
                )
        return True
    except Exception as exc:
        print(f"[NORMAL DB ERROR] history batch failed @{channel}: {type(exc).__name__}: {exc}")
        return False


def load_pending_normal_messages():
    """Load the durable NORMAL queue in chronological order."""
    if not stats_db_ready:
        return []
    try:
        with sqlite3.connect(CATEGORY_STATS_DB_PATH) as connection:
            rows = connection.execute(
                """SELECT channel, message_id, message_at, text
                   FROM normal_messages
                   WHERE status = 'pending'
                   ORDER BY message_at, channel, message_id"""
            ).fetchall()
        return [
            {
                "channel": channel,
                "message_id": int(message_id),
                "message_at": message_at,
                "text": text,
            }
            for channel, message_id, message_at, text in rows
        ]
    except Exception as exc:
        print(f"[NORMAL DB ERROR] pending read failed: {type(exc).__name__}: {exc}")
        return []


def mark_normal_messages_processed(message_keys):
    """Mark exactly the delivered snapshot as processed, leaving later arrivals pending."""
    if not stats_db_ready or not message_keys:
        return
    try:
        processed_at = utc_iso()
        with sqlite3.connect(CATEGORY_STATS_DB_PATH) as connection:
            connection.executemany(
                """UPDATE normal_messages
                   SET status = 'processed', processed_at = ?
                   WHERE channel = ? AND message_id = ? AND status = 'pending'""",
                [(processed_at, channel, int(message_id)) for channel, message_id in message_keys],
            )
            cutoff = utc_iso(datetime.now(timezone.utc) - timedelta(days=NORMAL_MESSAGE_RETENTION_DAYS))
            connection.execute(
                """DELETE FROM normal_messages
                   WHERE status != 'pending' AND processed_at < ?""",
                (cutoff,),
            )
    except Exception as exc:
        print(f"[NORMAL DB ERROR] completion write failed: {type(exc).__name__}: {exc}")


def discard_pending_normal_messages(reason):
    """Preserve the original rule that an alert discards accumulated NORMAL material."""
    if not stats_db_ready:
        return
    try:
        processed_at = utc_iso()
        with sqlite3.connect(CATEGORY_STATS_DB_PATH) as connection:
            changed = connection.execute(
                """UPDATE normal_messages
                   SET status = 'discarded', processed_at = ?
                   WHERE status = 'pending'""",
                (processed_at,),
            ).rowcount
        print(f"[NORMAL DB] discarded={changed} reason={reason}")
    except Exception as exc:
        print(f"[NORMAL DB ERROR] discard failed: {type(exc).__name__}: {exc}")


async def sync_normal_history(client, source_entities):
    """Backfill messages missed by live events and advance each cursor atomically."""
    global last_message_time
    if TEST_MODE or not stats_db_ready:
        return True
    all_ok = True
    bootstrap_cutoff = datetime.now(timezone.utc) - timedelta(hours=NORMAL_HISTORY_BOOTSTRAP_HOURS)
    for channel in ALL_CONTENT_CHANNELS:
        cursor = get_source_cursor(channel)
        try:
            if cursor is None:
                bootstrap_messages = list(
                    await client.get_messages(source_entities[channel], limit=500)
                )
                latest_id = max(
                    (int(message.id) for message in bootstrap_messages), default=None
                )
                fetched = [
                    message for message in reversed(bootstrap_messages)
                    if getattr(message, "date", bootstrap_cutoff) >= bootstrap_cutoff
                ]
            else:
                fetched = [
                    message async for message in client.iter_messages(
                        source_entities[channel], min_id=cursor, reverse=True
                    )
                ]

                latest_id = cursor
            rows = []
            for message in fetched:
                latest_id = max(latest_id or 0, int(message.id))
                raw_text = message.text or ""
                if not raw_text or len(raw_text.strip()) < 5:
                    continue
                clean = clean_text(raw_text)
                if is_pure_ad(clean):
                    print(f"[HISTORY FILTERED AD] @{channel}: {clean[:80]}")
                    continue
                rows.append({
                    "message_id": int(message.id),
                    "message_at": utc_iso(message.date),
                    "text": clean[:800],
                })

            if not persist_history_batch(channel, rows, latest_id):
                all_ok = False
                continue
            if fetched:
                last_message_time = time.time()
            print(
                f"[HISTORY SYNC] @{channel}: fetched={len(fetched)} "
                f"stored_candidates={len(rows)} cursor={latest_id}"
            )
        except Exception as exc:
            all_ok = False
            print(f"[HISTORY SYNC ERROR] @{channel}: {type(exc).__name__}: {exc}")
    return all_ok


async def advance_normal_cursors_to_latest(client, source_entities, reason):
    """Skip material produced while NORMAL mode is intentionally paused."""
    if TEST_MODE or not stats_db_ready:
        return
    for channel in ALL_CONTENT_CHANNELS:
        try:
            latest = await client.get_messages(source_entities[channel], limit=1)
            latest_id = int(latest[0].id) if latest else get_source_cursor(channel)
            if latest_id is not None:
                persist_history_batch(channel, [], latest_id)
                print(f"[HISTORY CURSOR] @{channel}: cursor={latest_id} reason={reason}")
        except Exception as exc:
            print(f"[HISTORY CURSOR ERROR] @{channel}: {type(exc).__name__}: {exc}")


def persist_category_stats(run_at, snapshots, category_results):
    if not stats_db_ready:
        return
    by_id = {item["id"]: item for item in snapshots}
    received_by_channel = {
        channel: sum(1 for item in snapshots if item["channel"] == channel)
        for channel in ALL_CONTENT_CHANNELS
    }
    try:
        with sqlite3.connect(CATEGORY_STATS_DB_PATH) as connection:
            for category_key, category_data in category_results.items():
                selected_ids = set(category_data.get("selected_ids", []))
                for channel in ALL_CONTENT_CHANNELS:
                    valid = sum(
                        1 for message_id in selected_ids
                        if message_id in by_id and by_id[message_id]["channel"] == channel
                    )
                    connection.execute(
                        """INSERT OR REPLACE INTO hourly_category_stats
                           (run_at, category, channel, received, valid)
                           VALUES (?, ?, ?, ?, ?)""",
                        (run_at, category_key, channel, received_by_channel[channel], valid),
                    )
                for message_id in selected_ids:
                    item = by_id.get(message_id)
                    if item:
                        connection.execute(
                            """INSERT OR REPLACE INTO hourly_classifications
                               (run_at, message_id, channel, category, preview)
                               VALUES (?, ?, ?, ?, ?)""",
                            (run_at, message_id, item["channel"], category_key, item["text"][:300]),
                        )
    except Exception as exc:
        print(f"[STATS DB ERROR] write failed: {type(exc).__name__}: {exc}")


def parse_first_json_object(raw):
    """Legacy fallback: decode the first JSON object and report trailing anomalies."""
    cleaned = raw.strip()
    cleaned = re.sub(r"^\x60\x60\x60(?:json)?\\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\\s*\x60\x60\x60$", "", cleaned)
    object_start = cleaned.find("{")
    if object_start < 0:
        raise json.JSONDecodeError("No JSON object found", cleaned, 0)
    parsed, end_index = json.JSONDecoder().raw_decode(cleaned[object_start:])
    trailing = cleaned[object_start + end_index:].strip()
    print(
        "[STRUCTURED OUTPUT LEGACY PARSER] primary json.loads failed; "
        f"first object extracted trailing_data={bool(trailing)}"
    )
    if not isinstance(parsed, dict):
        raise ValueError("AI response root must be a JSON object")
    return parsed


def normalize_category_result(parsed, messages):
    """Strictly validate categories, value types and selected message IDs."""
    if set(parsed.keys()) != {"categories"}:
        raise ValueError("structured result must contain only categories")
    supplied = parsed["categories"]
    if not isinstance(supplied, dict) or set(supplied.keys()) != set(CATEGORIES.keys()):
        raise ValueError("structured result must contain every configured category exactly once")

    valid_ids = {item["id"] for item in messages}
    normalized = {}
    for category_key in CATEGORIES:
        category_data = supplied[category_key]
        if not isinstance(category_data, dict):
            raise TypeError(f"{category_key} must be an object")
        if set(category_data.keys()) != {"selected_ids", "bullets"}:
            raise ValueError(f"{category_key} must contain only selected_ids and bullets")

        selected_ids = category_data["selected_ids"]
        bullets = category_data["bullets"]
        if not isinstance(selected_ids, list) or not all(
            isinstance(message_id, str) for message_id in selected_ids
        ):
            raise TypeError(f"{category_key}.selected_ids must be an array of strings")
        if not isinstance(bullets, list) or not all(
            isinstance(bullet, str) for bullet in bullets
        ):
            raise TypeError(f"{category_key}.bullets must be an array of strings")

        unknown_ids = [message_id for message_id in selected_ids if message_id not in valid_ids]
        if unknown_ids:
            raise ValueError(f"{category_key} returned unknown IDs: {unknown_ids[:5]}")

        normalized[category_key] = {
            "selected_ids": list(dict.fromkeys(selected_ids)),
            "bullets": [
                bullet.strip().lstrip("•- ").strip()[:180]
                for bullet in bullets
                if bullet.strip()
            ][:5],
        }
    return normalized


def retry_after_seconds(response, default):
    """Honor a numeric Retry-After header, bounded to protect the worker."""
    raw = response.headers.get("Retry-After")
    try:
        return max(1, min(int(raw), 60))
    except (TypeError, ValueError):
        return default


async def analyze_hourly_matrix(messages):
    """Use Anthropic Structured Outputs with error-specific retry policies."""
    if not messages:
        return {
            category_key: {"selected_ids": [], "bullets": []}
            for category_key in CATEGORIES
        }

    category_text = "\n".join(
        f"- {key}: {meta['criteria']}" for key, meta in CATEGORIES.items()
    )
    message_text = "\n\n".join(
        f"ID={item['id']} SOURCE=@{item['channel']} TIME={item['time']}\n{item['text']}"
        for item in messages
    )
    prompt = (
        "Classify Ukrainian news messages. The source is provenance only: evaluate EVERY message against "
        "EVERY category below. A message may belong to multiple categories when genuinely relevant. "
        "Do not favor or exclude a message because of its source channel. Reject advertising, clickbait, "
        "routine statements without a concrete development, and unrelated material.\n\n"
        f"Categories:\n{category_text}\n\n"
        "Use the required structured output schema. selected_ids must contain exact message IDs that qualify. "
        "bullets must contain at most five concise English summary strings per category. Within each category, "
        "order bullets chronologically from the earliest event/message time to the latest. Preserve stated "
        "locations, uncertainty, times and quantities.\n\nMessages:\n" + message_text
    )

    token_budgets = (4000, 6000, 8000)
    for token_attempt, max_tokens in enumerate(token_budgets, start=1):
        reached_token_limit = False
        for transient_attempt in range(1, 4):
            try:
                response = await http_client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": MODEL,
                        "max_tokens": max_tokens,
                        "messages": [{"role": "user", "content": prompt}],
                        "output_config": {
                            "format": {
                                "type": "json_schema",
                                "schema": CATEGORY_RESULT_SCHEMA,
                            }
                        },
                    },
                    timeout=httpx.Timeout(60.0, connect=5.0),
                )
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                if transient_attempt < 3:
                    wait_seconds = 2 ** (transient_attempt - 1)
                    print(
                        f"[CATEGORY API RETRY] transport attempt={transient_attempt}/3 "
                        f"wait={wait_seconds}s {type(exc).__name__}: {exc}"
                    )
                    await asyncio.sleep(wait_seconds)
                    continue
                print(f"[CATEGORY ANALYSIS ERROR] transport exhausted: {type(exc).__name__}: {exc}")
                return None

            if response.status_code == 429 or response.status_code >= 500:
                if transient_attempt < 3:
                    wait_seconds = retry_after_seconds(
                        response, min(2 ** (transient_attempt - 1), 30)
                    )
                    print(
                        f"[CATEGORY API RETRY] status={response.status_code} "
                        f"attempt={transient_attempt}/3 wait={wait_seconds}s"
                    )
                    await asyncio.sleep(wait_seconds)
                    continue
                print(
                    f"[CATEGORY ANALYSIS ERROR] transient HTTP {response.status_code} exhausted"
                )
                return None

            if 400 <= response.status_code < 500:
                print(
                    f"[CATEGORY ANALYSIS ERROR] non-retryable HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )
                return None

            try:
                response_data = response.json()
                stop_reason = response_data.get("stop_reason")

                if stop_reason == "max_tokens":
                    reached_token_limit = True
                    print(
                        f"[CATEGORY TOKEN RETRY] max_tokens reached at {max_tokens}; "
                        f"next_budget={token_budgets[token_attempt] if token_attempt < len(token_budgets) else 'none'}"
                    )
                    break

                if stop_reason == "refusal":
                    print("[CATEGORY ANALYSIS ERROR] non-retryable refusal")
                    return None

                text_blocks = [
                    block.get("text")
                    for block in response_data.get("content", [])
                    if block.get("type") == "text" and isinstance(block.get("text"), str)
                ]
                if not text_blocks:
                    raise ValueError("structured response contains no text block")
                raw = text_blocks[0].strip()

                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = parse_first_json_object(raw)

                return normalize_category_result(parsed, messages)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                print(
                    f"[CATEGORY ANALYSIS ERROR] non-retryable structured output "
                    f"{type(exc).__name__}: {exc}"
                )
                return None

        if reached_token_limit:
            continue
        return None

    print("[CATEGORY ANALYSIS ERROR] max_tokens exhausted at 8000")
    return None


def build_emergency_category_result(messages):
    """Last resort: publish only original source excerpts, without AI synthesis."""
    result = {
        category_key: {"selected_ids": [], "bullets": []}
        for category_key in CATEGORIES
    }
    for item in messages:
        lowered = item["text"].lower()
        if contains_any(lowered, SECURITY_KEYWORDS):
            category_key = "air_defence"
        elif item["channel"] == KYIV_INFO_CHANNEL:
            category_key = "kyiv_city"
        elif item["channel"] == UKRAINE_NEWS_CHANNEL:
            category_key = "ukraine_national"
        else:
            category_key = "military"
        result[category_key]["selected_ids"].append(item["id"])
        if len(result[category_key]["bullets"]) < 5:
            original_excerpt = re.sub(r"\\s+", " ", item["text"]).strip()[:160]
            result[category_key]["bullets"].append(
                f"Original source excerpt from @{item['channel']}: {original_excerpt}"
            )
    return result


async def telegram_request(method, payload):
    """Telegram Bot API request with bounded retries that honor flood control."""
    for attempt in range(4):
        try:
            r = await http_client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                json=payload,
                timeout=httpx.Timeout(15.0, connect=3.0),
            )
            data = r.json()
            if data.get("ok"):
                return data.get("result")
            if r.status_code == 429 and attempt < 3:
                retry_after = max(1, min(int(data.get("parameters", {}).get("retry_after", 1)), 60))
                print(f"Telegram {method} flood control: waiting {retry_after}s")
                await asyncio.sleep(retry_after)
                continue
            print(f"Telegram {method} error: {data}")
            return None
        except Exception as exc:
            if attempt < 3:
                await asyncio.sleep(attempt + 1)
                continue
            print(f"Telegram {method} error: {exc}")
            return None
    return None

async def send_message(chat_id, text):
    return await telegram_request("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })

async def edit_message(chat_id, message_id, text):
    return await telegram_request("editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })

async def send_to_alert_channel(text):
    """Send alert lifecycle and real-time messages to the alert-only channel."""
    result = await send_message(ALERT_OUTPUT_CHAT_ID, text)
    if TEST_MODE and result and result.get("message_id"):
        bot_output_message_ids.add(result["message_id"])
    return result


async def send_to_summary_group(text):
    """Send scheduled analysis only to the linked summary group."""
    result = await send_message(SUMMARY_OUTPUT_CHAT_ID, text)
    if TEST_MODE and result and result.get("message_id"):
        bot_output_message_ids.add(result["message_id"])
    return result

async def send_to_owner(text):
    """Send actionable operational alerts to the dedicated private Ops chat."""
    return await send_message(OPS_CHAT_ID, text)


async def startup_self_check():
    """Validate persistent storage and Bot API destinations before going operational."""
    failures = []
    if not stats_db_ready:
        failures.append("persistent SQLite database is unavailable")
    else:
        marker = f"startup-{time.time_ns()}"
        if not persist_operational_state("startup_self_check", marker):
            failures.append("persistent SQLite database is not writable")
        elif load_operational_state("startup_self_check") != marker:
            failures.append("persistent SQLite database read-after-write failed")

    destinations = {
        "alert": ALERT_OUTPUT_CHAT_ID,
        "summary": SUMMARY_OUTPUT_CHAT_ID,
        "ops": OPS_CHAT_ID,
    }
    for name, chat_id in destinations.items():
        if not await telegram_request("getChat", {"chat_id": chat_id}):
            failures.append(f"Bot API cannot access the {name} destination")

    if failures:
        message = "🚨 <b>Startup self-check failed</b>\n" + "\n".join(
            f"• {html.escape(failure)}" for failure in failures
        )
        print("[STARTUP CHECK] " + "; ".join(failures))
        await send_to_owner(message)
        return False
    print("[STARTUP CHECK] storage and Bot API destinations are ready")
    return True


async def safe_send(text):
    global last_send_time
    async with send_lock:
        now = time.monotonic()
        wait = MIN_SEND_INTERVAL - (now - last_send_time)
        if wait > 0:
            await asyncio.sleep(wait)
        result = await send_to_alert_channel(text)
        last_send_time = time.monotonic()
        return result


async def build_summary(night_recap=False, trigger="scheduled"):
    """Build and publish one summary; retain durable input until delivery is confirmed."""
    global last_summary_success_time, summary_watchdog_attempts
    async with summary_lock:
        if not TEST_MODE and stats_db_ready and production_client and content_source_entities:
            await sync_normal_history(production_client, content_source_entities)

        snapshots = []
        snapshot_lengths = {}
        snapshot_message_keys = []
        received_by_channel = {}

        if not TEST_MODE and stats_db_ready:
            pending_rows = load_pending_normal_messages()
            for channel in ALL_CONTENT_CHANNELS:
                channel_rows = [row for row in pending_rows if row["channel"] == channel]
                received_by_channel[channel] = len(channel_rows)
                print(f"[SUMMARY INPUT] @{channel}: messages={len(channel_rows)} trigger={trigger}")
            for row in pending_rows:
                message_at = datetime.fromisoformat(row["message_at"])
                message_key = (row["channel"], row["message_id"])
                snapshot_message_keys.append(message_key)
                snapshots.append({
                    "id": f"{row['channel']}:{row['message_id']}",
                    "channel": row["channel"],
                    "time": message_at.astimezone(TZ).strftime("%H:%M"),
                    "text": row["text"],
                })
        else:
            for channel in ALL_CONTENT_CHANNELS:
                pending = list(buffers[channel])
                snapshot_lengths[channel] = len(pending)
                received_by_channel[channel] = len(pending)
                print(f"[SUMMARY INPUT] @{channel}: messages={len(pending)} trigger={trigger}")
                for index, message in enumerate(pending):
                    snapshots.append({
                        "id": f"{channel}:{index}",
                        "channel": channel,
                        "time": message["time"],
                        "text": message["text"],
                    })

        category_results = await analyze_hourly_matrix(snapshots)
        used_emergency_fallback = category_results is None
        if used_emergency_fallback:
            category_results = build_emergency_category_result(snapshots)
            await send_to_owner(
                "⚠️ <b>Hourly summary AI fallback used</b>\n"
                "All AI retries failed; a deterministic source-text summary was generated."
            )

        by_id = {item["id"]: item for item in snapshots}
        run_at = datetime.now(TZ).isoformat(timespec="seconds")
        sections = []

        print(
            f"[HOURLY CATEGORY STATS] run_at={run_at} trigger={trigger} "
            f"emergency_fallback={used_emergency_fallback}"
        )
        for category_key, category_meta in CATEGORIES.items():
            category_data = category_results[category_key]
            selected_ids = set(category_data["selected_ids"])
            for channel in ALL_CONTENT_CHANNELS:
                valid = sum(
                    1 for message_id in selected_ids
                    if message_id in by_id and by_id[message_id]["channel"] == channel
                )
                print(
                    f"[CATEGORY STATS] category={category_key} @{channel}: "
                    f"valid={valid} received={received_by_channel[channel]}"
                )
            for message_id in category_data["selected_ids"]:
                item = by_id.get(message_id)
                if item:
                    print(
                        f"[CATEGORY ITEM] category={category_key} @{item['channel']} "
                        f"id={message_id} text={item['text'][:100]}"
                    )
            if category_data["bullets"]:
                bullets = "\n".join(
                    f"• {html.escape(bullet)}" for bullet in category_data["bullets"]
                )
                sections.append(
                    f"{category_meta['icon']} <b>{category_meta['name']}</b>\n{bullets}"
                )

        if used_emergency_fallback:
            sections.insert(
                0,
                "⚠️ <b>AI synthesis unavailable</b>\n"
                "The entries below are unchanged original-source excerpts, not an AI summary.",
            )

        time_label = datetime.now(TZ).strftime("%H:%M %Z")
        if sections:
            title = "🌙 <b>Overnight Recap" if night_recap else "📋 <b>Hourly Update"
            result = await send_to_summary_group(
                f"{title} — {time_label}</b>\n\n" + "\n\n".join(sections)
            )
        else:
            if night_recap:
                heartbeat = (
                    f"🌙 <b>Overnight Recap — {time_label}</b>\n\n"
                    "No relevant updates during the overnight period."
                )
            else:
                heartbeat = (
                    f"📋 <b>Hourly Update — {time_label}</b>\n\n"
                    "No relevant updates in the last hour."
                )
            heartbeat_sender = send_to_summary_group if TEST_MODE else send_to_owner
            result = await heartbeat_sender(heartbeat)
            print(
                f"[SUMMARY OPS HEARTBEAT] trigger={trigger}; "
                "no relevant updates, production chat kept silent"
            )

        if not result:
            print(f"[SUMMARY SEND ERROR] trigger={trigger}; buffers retained")
            await send_to_owner(
                "🚨 <b>Hourly summary delivery failed</b>\n"
                "Telegram did not confirm delivery. Buffers were retained for retry."
            )
            return False

        persist_category_stats(run_at, snapshots, category_results)
        if not TEST_MODE and stats_db_ready:
            mark_normal_messages_processed(snapshot_message_keys)
        else:
            for channel, length in snapshot_lengths.items():
                del buffers[channel][:length]

        last_summary_success_time = time.monotonic()
        summary_watchdog_attempts.clear()
        outcome = "delivered" if sections else "ops_heartbeat"
        print(
            f"[SUMMARY COMPLETED] trigger={trigger} "
            f"outcome={outcome} messages={len(snapshots)}"
        )
        return True


async def summary_loop():
    was_night = False
    first_delay = SUMMARY_INTERVAL if TEST_MODE else seconds_until_next_hour()
    next_run = time.monotonic() + first_delay
    print(
        f"[SUMMARY SCHEDULE] first_run_in={first_delay:.1f}s "
        f"interval={SUMMARY_INTERVAL}s timezone={TZ.key}"
    )
    while True:
        await asyncio.sleep(max(0, next_run - time.monotonic()))
        next_run += SUMMARY_INTERVAL
        try:
            if alert_active:
                continue

            night_now = is_night()
            if was_night and not night_now:
                await build_summary(night_recap=True, trigger="night_recap")
                was_night = False
                continue

            was_night = night_now
            if night_now:
                continue

            await build_summary(night_recap=False, trigger="scheduled")
        except Exception as exc:
            print(f"[SUMMARY LOOP ERROR] {type(exc).__name__}: {exc}")
            await send_to_owner(
                f"🚨 <b>Summary loop recovered</b>\n{html.escape(type(exc).__name__ + ': ' + str(exc))}"
            )


async def summary_watchdog_loop():
    """Retry missing production summaries at 62, 65, 67 and 70 minutes."""
    thresholds = (62, 65, 67, 70)
    while True:
        await asyncio.sleep(20 if TEST_MODE else 30)
        if alert_active or is_night():
            continue
        age_minutes = (time.monotonic() - last_summary_success_time) / 60
        for threshold in thresholds:
            if age_minutes >= threshold and threshold not in summary_watchdog_attempts:
                summary_watchdog_attempts.add(threshold)
                print(f"[SUMMARY WATCHDOG] no confirmed summary for {age_minutes:.1f}m; retry={threshold}m")
                await send_to_owner(
                    f"⚠️ <b>Summary watchdog retry</b>\n"
                    f"No confirmed delivery for {age_minutes:.1f} minutes. Running retry {threshold}m."
                )
                try:
                    success = await build_summary(trigger=f"watchdog_{threshold}m")
                except Exception as exc:
                    success = False
                    print(f"[SUMMARY WATCHDOG ERROR] {type(exc).__name__}: {exc}")
                if success:
                    break
                if threshold == 70:
                    await send_to_owner(
                        "🚨 <b>Summary watchdog critical</b>\n"
                        "Retries at 62, 65, 67 and 70 minutes failed; buffers remain retained."
                    )


async def health_loop():
    while True:
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)
        silence = time.time() - last_message_time
        if silence > SILENCE_THRESHOLD:
            hours = int(silence // 3600)
            await send_to_owner(f"⚠️ <b>Kyiv Monitor warning</b>\nNo messages received from any channel in ~{hours}h. Connection may be down — check Railway.")



def schedule_alert_delivery(clean, source):
    """Track background delivery so transitions and shutdown can drain it safely."""
    generation = alert_generation
    task = asyncio.create_task(handle_alert_message(clean, source=source, generation=generation))
    alert_delivery_tasks.add(task)
    task.add_done_callback(alert_delivery_tasks.discard)
    return task


async def handle_alert_message(clean, source=ALERT_FEED_CHANNEL, generation=None):
    """Deliver low-latency alerts; TEST_MODE uses one Bot API write to avoid group flood limits."""
    generation = alert_generation if generation is None else generation
    if generation != alert_generation or not alert_active:
        print(f"[ALERT STALE] skipped source=@{source} generation={generation}")
        return
    print(f"[ALERT ACCEPTED] @{source}: {clean[:100]}")
    if TEST_MODE:
        translation = await translate_message(clean[:1500])
        if generation != alert_generation or not alert_active:
            print(f"[ALERT STALE] test translation discarded source=@{source}")
            return
        if translation:
            await safe_send(f"🔴 {html.escape(translation)}")
        else:
            await safe_send(f"🔴 ⚠️ Translation unavailable\n\n{html.escape(clean[:1000])}")
        return

    original = html.escape(clean[:1500])
    sent = await safe_send(f"🔴 <b>Incoming alert — translating…</b>\n\n{original}")
    if not sent:
        return

    try:
        translation = await translate_message(clean[:1500])
    except asyncio.CancelledError:
        await edit_message(
            ALERT_OUTPUT_CHAT_ID,
            sent["message_id"],
            f"🔴 ⚠️ Translation interrupted at alert end\n\n{html.escape(clean[:1000])}",
        )
        raise
    if generation != alert_generation or not alert_active:
        print(f"[ALERT STALE] translation discarded source=@{source} generation={generation}")
        return
    if translation:
        await edit_message(ALERT_OUTPUT_CHAT_ID, sent["message_id"], f"🔴 {html.escape(translation)}")
    else:
        await edit_message(
            ALERT_OUTPUT_CHAT_ID,
            sent["message_id"],
            f"🔴 ⚠️ Translation unavailable\n\n{html.escape(clean[:1000])}",
        )


async def process_test_source_text(clean):
    global last_message_time
    clean = clean_text(clean)
    clean = re.sub(r"^\[burst\s+\d+/\d+\]\s*", "", clean, flags=re.IGNORECASE)
    if not clean:
        return
    last_message_time = time.time()

    if alert_active and is_non_operational_alert_message(clean):
        print(f"[TEST FILTERED NON-OPERATIONAL] {clean[:80]}")
        return
    if alert_active and not is_actionable_alert_message(clean):
        print(f"[TEST FILTERED NON-TACTICAL] {clean[:80]}")
        return

    if is_pure_ad(clean):
        print(f"[TEST FILTERED AD] {clean[:80]}")
        return

    if alert_active:
        if should_publish_alert(clean, "test"):
            schedule_alert_delivery(clean, source="test")
        return

    time_str = datetime.now(TZ).strftime("%H:%M")
    buffers[TEST_BUFFER_CHANNEL].append({"time": time_str, "text": clean[:800]})


async def publish_test_source(client, text):
    sent = await client.send_message(int(TEST_CHAT_ID), f"{TEST_SOURCE_PREFIX}\n{text}")
    simulator_processed_message_ids.add(sent.id)
    await process_test_source_text(text)
    return sent


async def main():
    global http_client, send_lock, test_command_lock, translation_slots, summary_lock
    global alert_transition_lock
    global telegram_alert_state, stats_db_ready, production_client, content_source_entities
    http_client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=30, max_keepalive_connections=15),
        http2=False,
    )
    send_lock = asyncio.Lock()
    test_command_lock = asyncio.Lock()
    translation_slots = asyncio.Semaphore(8)
    summary_lock = asyncio.Lock()
    alert_transition_lock = asyncio.Lock()
    stats_db_ready = initialize_stats_db()

    client = None
    if not TEST_MODE or TEST_TELEGRAM_SESSION:
        session_value = TEST_TELEGRAM_SESSION if TEST_MODE else TELEGRAM_SESSION
        client = TelegramClient(StringSession(session_value), TELEGRAM_API_ID, TELEGRAM_API_HASH)
        await client.start()

    if not await startup_self_check():
        raise RuntimeError("Startup self-check failed")

    if TEST_MODE and not TEST_TELEGRAM_SESSION:
        print(
            f"🧪 TEST_MODE enabled without a Telegram listener. Exclusive output chat: {TEST_CHAT_ID}; "
            "the production Telethon session is not opened."
        )
        await send_to_alert_channel(
            "🧪 <b>Kyiv Monitor started in isolated TEST_MODE</b>\n"
            "Real Telegram sources and interactive test commands are disabled."
        )
        asyncio.create_task(summary_loop())
        asyncio.create_task(summary_watchdog_loop())
        try:
            await asyncio.Event().wait()
        finally:
            await drain_alert_delivery_tasks(timeout=10.0)
            await http_client.aclose()
        return

    if TEST_MODE:
        print(f"🧪 TEST_MODE enabled. Exclusive chat: {TEST_CHAT_ID}; real Telegram sources are NOT registered.")
        await send_to_alert_channel(
            "🧪 <b>Kyiv Monitor started in TEST_MODE</b>\n"
            "Exclusive source/output chat enabled. Real Telegram channels are disabled.\n"
            "Commands: /test_start, /test_message, /test_burst N, /test_end, /test_summary"
        )

        @client.on(events.NewMessage(chats=int(TEST_CHAT_ID)))
        async def test_command_handler(event):
            global last_message_time
            raw = (event.message.text or "").strip()
            command = raw.lower()
            if event.sender_id == BOT_USER_ID or event.message.id in bot_output_message_ids:
                return

            if command == "/test_start":
                async with test_command_lock:
                    await apply_alert_state(
                        True,
                        "test_command",
                        public_message="🚨 <b>TEST AIR ALERT — KYIV</b>\n\n⚡ REAL-TIME test mode active",
                    )
                return

            if command == "/test_message":
                async with test_command_lock:
                    await publish_test_source(client, TEST_SAMPLE_MESSAGES[0])
                return

            burst_match = re.fullmatch(r"/test_burst(?:\s+(\d+))?", command)
            if burst_match:
                async with test_command_lock:
                    count = int(burst_match.group(1) or "1")
                    count = max(1, min(count, 100))
                    for index in range(count):
                        sample = TEST_SAMPLE_MESSAGES[index % len(TEST_SAMPLE_MESSAGES)]
                        await publish_test_source(client, f"[burst {index + 1}/{count}] {sample}")
                return

            if command == "/test_end":
                async with test_command_lock:
                    await apply_alert_state(
                        False,
                        "test_command",
                        public_message="✅ <b>TEST ALL CLEAR — KYIV</b>\n\n📋 Back to NORMAL test mode",
                    )
                return

            if command == "/test_summary":
                async with test_command_lock:
                    if alert_active:
                        await send_to_alert_channel("⚠️ End the test alert with /test_end before requesting a summary.")
                    else:
                        await build_summary()
                return

        @client.on(events.NewMessage(chats=int(TEST_CHAT_ID)))
        async def test_source_handler(event):
            global last_message_time
            raw_text = (event.message.text or "").strip()
            if event.sender_id == BOT_USER_ID or event.message.id in bot_output_message_ids:
                return
            if not raw_text.startswith(TEST_SOURCE_PREFIX):
                return

            if event.message.id in simulator_processed_message_ids:
                return
            clean = raw_text[len(TEST_SOURCE_PREFIX):].lstrip()
            await process_test_source_text(clean)

    else:
        source_entities = {}
        channel_by_chat_id = {}
        production_channels = list(dict.fromkeys(
            ALL_CONTENT_CHANNELS + ALERT_FEED_CHANNELS + [BACKUP_TRIGGER_CHANNEL]
        ))
        for channel_name in production_channels:
            entity = await client.get_entity(channel_name)
            source_entities[channel_name] = entity
            channel_by_chat_id[utils.get_peer_id(entity)] = channel_name
        production_client = client
        content_source_entities = {
            channel: source_entities[channel] for channel in ALL_CONTENT_CHANNELS
        }
        await ensure_alert_feed_membership(client, source_entities)

        # Establish the Telegram trigger state immediately from its latest explicit event.
        recent_trigger_messages = await client.get_messages(source_entities[BACKUP_TRIGGER_CHANNEL], limit=20)
        for recent in recent_trigger_messages:
            state = classify_telegram_alert(recent.text or "")
            if state is not None:
                telegram_alert_state = state
                persist_trigger_observation(state, recent.id, recent.date)
                print(f"✅ Telegram alert state loaded: {'ACTIVE' if state else 'CLEAR'}")
                break

        if telegram_alert_state is None:
            await send_to_owner(
                "🚨 <b>Startup self-check failed</b>\n"
                f"No explicit Kyiv state found in the latest @{BACKUP_TRIGGER_CHANNEL} messages. "
                "The worker stopped instead of assuming NORMAL."
            )
            raise RuntimeError("Cannot establish initial Kyiv alert state")

        await apply_alert_state(telegram_alert_state, f"@{BACKUP_TRIGGER_CHANNEL}", startup=True)

        print(
            f"✅ Connected in production. Alert trigger: @{BACKUP_TRIGGER_CHANNEL}; "
            f"content sources: {ALL_CONTENT_CHANNELS}; alert feeds: {ALERT_FEED_CHANNELS}"
        )
        await send_to_owner(
            "🟢 <b>Kyiv Monitor started</b>\n"
            f"Mode: {'ALERT' if alert_active else 'NORMAL'}\n"
            "Night pause: 01:00–07:00 EET/EEST"
        )

        @client.on(events.NewMessage(chats=int(ALERT_CHANNEL_ID)))
        async def production_command_handler(event):
            text = (event.message.text or "").strip().lower()
            if text not in {"/alert", "/normal"}:
                return
            if not is_authorized_admin(event.sender_id):
                print(f"[COMMAND DENIED] sender_id={event.sender_id} command={text}")
                await send_to_owner(
                    "⚠️ <b>Unauthorized monitor command rejected</b>\n"
                    f"Sender ID: <code>{event.sender_id}</code>; command: <code>{html.escape(text)}</code>"
                )
                return
            if text == "/alert":
                await apply_alert_state(
                    True,
                    f"manual:{event.sender_id}",
                    public_message="🚨 <b>MANUAL OVERRIDE</b>\n⚡ Switched to REAL-TIME mode",
                )
            elif text == "/normal":
                await apply_alert_state(
                    False,
                    f"manual:{event.sender_id}",
                    public_message="✅ <b>MANUAL OVERRIDE</b>\n📋 Back to NORMAL mode",
                )

        @client.on(events.NewMessage(chats=list(source_entities.values())))
        async def production_source_handler(event):
            global alert_active, alert_started_at, last_message_time, telegram_alert_state
            last_message_time = time.time()

            raw_text = event.message.text or ""
            if not raw_text or len(raw_text.strip()) < 5:
                return

            channel = channel_by_chat_id.get(event.chat_id)
            if channel is None:
                print(f"[IGNORED UNKNOWN CHAT] chat_id={event.chat_id}")
                return
            clean = clean_alert_source_text(raw_text) if channel in ALERT_FEED_CHANNELS else clean_text(raw_text)

            if channel == BACKUP_TRIGGER_CHANNEL:
                state = classify_telegram_alert(clean)
                if state is not None:
                    telegram_alert_state = state
                    persist_trigger_observation(state, event.message.id, event.message.date)
                    print(f"Telegram trigger update: {'ACTIVE' if state else 'CLEAR'}")
                    await reconcile_alert_state(f"@{BACKUP_TRIGGER_CHANNEL}")
                return

            if alert_active and channel in ALERT_FEED_CHANNELS and is_non_operational_alert_message(clean):
                print(f"[FILTERED NON-OPERATIONAL ALERT] @{channel}: {clean[:80]}")
                return
            if alert_active and channel in ALERT_FEED_CHANNELS and not is_actionable_alert_message(clean):
                print(f"[FILTERED NON-TACTICAL ALERT] @{channel}: {clean[:80]}")
                return

            if is_pure_ad(clean):
                print(f"[FILTERED AD] @{channel}: {clean[:80]}")
                return

            if alert_active:
                if channel in ALERT_FEED_CHANNELS:
                    if should_publish_alert(clean, channel):
                        schedule_alert_delivery(clean, source=channel)
                    set_alert_feed_cursor(channel, event.message.id)
                return

            if channel == ALERT_FEED_CHANNEL:
                return

            time_str = datetime.now(TZ).strftime("%H:%M")
            channel_buffer = buffers.get(channel)
            if channel_buffer is None:
                print(f"[IGNORED NON-CONTENT CHAT] channel={channel} chat_id={event.chat_id}")
                return
            if stats_db_ready and persist_normal_message(
                channel, event.message.id, event.message.date, clean
            ):
                print(f"[PERSISTED LIVE] @{channel}: id={event.message.id} text={clean[:80]}")
            else:
                channel_buffer.append({"time": time_str, "text": clean[:800]})
                print(f"[BUFFERED FALLBACK] @{channel}: pending={len(channel_buffer)} text={clean[:80]}")

        if alert_active:
            await backfill_alert_feed(client, source_entities)

        # Telethon push events are best-effort. The cursor-based poller is the
        # production safety net and recovers any post the live listener misses.
        asyncio.create_task(alert_feed_poll_loop(client, source_entities))

    asyncio.create_task(summary_loop())
    asyncio.create_task(summary_watchdog_loop())
    if not TEST_MODE:
        asyncio.create_task(health_loop())

    try:
        await client.run_until_disconnected()
    finally:
        await drain_alert_delivery_tasks(timeout=10.0)
        await http_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
