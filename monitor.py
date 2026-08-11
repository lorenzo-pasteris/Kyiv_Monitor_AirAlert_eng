"""
 Kyiv Alert Monitor v6 — low-latency async pipeline
- Production trigger: @kyiv_airraid_alert
- Normal mode: hourly analysis of 3 channels published in the news group
- Alert mode (24/7): only @nebo_raketa in the alert-only channel
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
from datetime import datetime
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession

# --- Credentials ---
TELEGRAM_API_ID = int(os.environ["TELEGRAM_API_ID"])
TELEGRAM_API_HASH = os.environ["TELEGRAM_API_HASH"]
TELEGRAM_SESSION = os.environ["TELEGRAM_SESSION"]
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
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "392256147")
OPS_CHAT_ID = os.environ.get("OPS_CHAT_ID", OWNER_CHAT_ID)  # operational alerts; legacy fallback
# --- Channels ---
KYIV_INFO_CHANNEL = "kievinfo_kyiv"
AMK_CHANNEL = "AMK_Mapping"
ALERT_FEED_CHANNEL = "nebo_raketa"
UKRAINE_NEWS_CHANNEL = "shv_ukr"
BACKUP_TRIGGER_CHANNEL = "kyiv_airraid_alert"
ALL_CONTENT_CHANNELS = [KYIV_INFO_CHANNEL, UKRAINE_NEWS_CHANNEL, AMK_CHANNEL]
ALERT_FEED_CHANNELS = [ALERT_FEED_CHANNEL]

SUMMARY_INTERVAL = 180 if TEST_MODE else 3600  # 3 minutes in test, 1 hour in production
HEALTH_CHECK_INTERVAL = 43200  # 12 hours
SILENCE_THRESHOLD = 4 * 3600  # 4 hours of total silence = warning

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

# --- Pre-filters ---
KYIV_CITY_KEYWORDS = ["kyiv","kiev","київ","киев","києві","києва","street","road","traffic","metro","subway","protest","demonstration","rally","power","water","heating","emergency","accident","fire","police","curfew","shelter","evacuation","bridge","district","avenue","closure","closed","block","disruption","delay","cancelled","train","bus","tram","вулиця","дорога","рух","затор","затори","метро","протест","мітинг","світло","електроенергія","відключення","вода","опалення","аварія","пожежа","поліція","комендантська","укриття","евакуація","міст","перекриття","перекрито","закрито","район","проспект","затримка","скасовано","поїзд","автобус","трамвай"]
MIDDLE_EAST_KEYWORDS = ["israel","iran","gaza","palestine","lebanon","hamas","hezbollah","yemen","houthi","idf","tehran","jerusalem","beirut","middle east","west bank"]
MILITARY_KEYWORDS = ["troop","movement","concentration","deployment","preparation","offensive","attack","shelling","drone","missile","launch","russian","belarus","border","military","convoy","equipment","tank","armored","artillery","mlrs","iskander","kinzhal","calibr","oniks","zircon","bomb","glide","fab","umpb","lancet","orlan","zala","reconnaissance","satellite","fortification","trenches","dragon teeth","digging","barracks","railway","ukraine","ukrainian"]
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
ALERT_START_UA = ["тривога"]
ALERT_END_UA = ["відбій"]

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
MIN_SEND_INTERVAL = 1.0 if TEST_MODE else 0.2

# Created in main(); one shared connection pool avoids a new TLS handshake per message.
http_client = None
send_lock = None
test_command_lock = None
translation_slots = None
summary_lock = None
bot_output_message_ids = set()
simulator_processed_message_ids = set()
recent_alert_messages = deque()
ALERT_DEDUP_WINDOW = 180
TEST_SOURCE_PREFIX = "[TEST_SOURCE:nebo_raketa]"
TEST_BUFFER_CHANNEL = AMK_CHANNEL
TEST_SAMPLE_MESSAGES = [
    "⚠️ Київщина: зафіксовано рух ударних БпЛА Shahed drone у напрямку Києва.",
    "Ракетна небезпека: missile launch activity зафіксована з північного напрямку.",
    "Група БпЛА продовжує рух; air-defense monitoring reports drone activity near Kyiv region.",
]


def contains_any(text, keywords):
    return any(k.lower() in text.lower() for k in keywords)

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
    """Allow tactical alerts and terse follow-ups; reject unrelated feed posts during ALERT."""
    return contains_any(text, ALERT_TACTICAL_KEYWORDS)

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

def is_night():
    h = datetime.now(TZ).hour
    return NIGHT_START <= h < NIGHT_END


def seconds_until_next_hour():
    """Return the delay to the next exact Europe/Kyiv clock hour."""
    now = datetime.now(TZ)
    elapsed = now.minute * 60 + now.second + now.microsecond / 1_000_000
    return max(0.1, 3600 - elapsed)

def check_alert_trigger(text):
    """Classify Kyiv alert-channel messages without depending on one exact phrase."""
    global alert_active
    t = text.lower()
    mentions_kyiv = contains_any(t, ["kyiv", "kiev", "київ", "києв", "киев"])
    is_clear = contains_any(t, ["all clear", "clear", "cancelled", "canceled", "ended", "відбій"])
    is_alert = (
        ("air" in t and contains_any(t, ["siren", "raid", "alert"]))
        or contains_any(t, ["повітряна тривога", "тривога"])
    )

    if mentions_kyiv and is_clear:
        if alert_active:
            alert_active = False
            m = re.search(r'Duration:\s*(.+)', text, re.IGNORECASE)
            return "end", (m.group(1).strip() if m else "unknown")
    elif mentions_kyiv and is_alert:
        if not alert_active:
            alert_active = True
            return "start", None
    return None, None

def check_ua_trigger(text):
    """Backup trigger, accepted only when the message explicitly mentions Kyiv."""
    global alert_active
    if not contains_any(text, ["київ", "києв", "киев", "kyiv", "kiev"]):
        return None
    if contains_any(text, ALERT_END_UA):
        if alert_active:
            alert_active = False
            return "end"
    elif contains_any(text, ALERT_START_UA):
        if not alert_active:
            alert_active = True
            return "start"
    return None


def classify_telegram_alert(text):
    """Return True/False for an explicit Kyiv alert/clear message, otherwise None."""
    t = text.lower()
    if not contains_any(t, ["kyiv", "kiev", "київ", "києв", "киев"]):
        return None
    if contains_any(t, ["all clear", "clear", "cancelled", "canceled", "ended", "відбій"]):
        return False
    if (("air" in t and contains_any(t, ["siren", "raid", "alert"]))
            or contains_any(t, ["повітряна тривога", "тривога"])):
        return True
    return None


async def reconcile_alert_state(source):
    """Use the explicit Kyiv state published by @kyiv_airraid_alert."""
    global alert_active, alert_started_at
    if telegram_alert_state is None:
        print("⚠️ No valid Telegram alert state; preserving last known state")
        return

    desired = telegram_alert_state
    if desired == alert_active:
        return

    alert_active = desired
    if desired:
        alert_started_at = time.monotonic()
        recent_alert_messages.clear()
        for channel_name in ALL_CONTENT_CHANNELS:
            buffers[channel_name].clear()
        await send_to_alert_channel(ALERT_START_MESSAGE)
        print(f"🚨 Effective alert state started via {source}")
    else:
        alert_started_at = None
        await send_to_alert_channel(build_all_clear_message())
        print(f"✅ Effective alert state ended via {source}")


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
    """Create the persistent hourly statistics store when the configured path is writable."""
    try:
        db_dir = os.path.dirname(CATEGORY_STATS_DB_PATH)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        with sqlite3.connect(CATEGORY_STATS_DB_PATH) as connection:
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
        print(f"[STATS DB] ready path={CATEGORY_STATS_DB_PATH}")
        return True
    except Exception as exc:
        print(f"[STATS DB ERROR] persistence unavailable: {type(exc).__name__}: {exc}")
        return False


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
                bullet.strip().lstrip("•- ").strip()
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
    """Build and publish one summary; retain buffers until Telegram confirms delivery."""
    global last_summary_success_time, summary_watchdog_attempts
    async with summary_lock:
        snapshots = []
        snapshot_lengths = {}
        received_by_channel = {}

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



async def handle_alert_message(clean, source=ALERT_FEED_CHANNEL):
    """Deliver low-latency alerts; TEST_MODE uses one Bot API write to avoid group flood limits."""
    print(f"[ALERT ACCEPTED] @{source}: {clean[:100]}")
    if TEST_MODE:
        translation = await translate_message(clean[:1500])
        if translation:
            await safe_send(f"🔴 {html.escape(translation)}")
        else:
            await safe_send(f"🔴 ⚠️ Translation unavailable\n\n{html.escape(clean[:1000])}")
        return

    original = html.escape(clean[:1500])
    sent = await safe_send(f"🔴 <b>Incoming alert — translating…</b>\n\n{original}")
    if not sent:
        return

    translation = await translate_message(clean[:1500])
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
            asyncio.create_task(handle_alert_message(clean, source="test"))
        return

    time_str = datetime.now(TZ).strftime("%H:%M")
    buffers[TEST_BUFFER_CHANNEL].append({"time": time_str, "text": clean[:800]})


async def publish_test_source(client, text):
    sent = await client.send_message(int(TEST_CHAT_ID), f"{TEST_SOURCE_PREFIX}\n{text}")
    simulator_processed_message_ids.add(sent.id)
    await process_test_source_text(text)
    return sent


async def main():
    global http_client, send_lock, test_command_lock, translation_slots, summary_lock, telegram_alert_state, stats_db_ready
    http_client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=30, max_keepalive_connections=15),
        http2=False,
    )
    send_lock = asyncio.Lock()
    test_command_lock = asyncio.Lock()
    translation_slots = asyncio.Semaphore(8)
    summary_lock = asyncio.Lock()
    stats_db_ready = initialize_stats_db()

    client = TelegramClient(StringSession(TELEGRAM_SESSION), TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await client.start()

    if TEST_MODE:
        print(f"🧪 TEST_MODE enabled. Exclusive chat: {TEST_CHAT_ID}; real Telegram sources are NOT registered.")
        await send_to_alert_channel(
            "🧪 <b>Kyiv Monitor started in TEST_MODE</b>\n"
            "Exclusive source/output chat enabled. Real Telegram channels are disabled.\n"
            "Commands: /test_start, /test_message, /test_burst N, /test_end, /test_summary"
        )

        @client.on(events.NewMessage(chats=int(TEST_CHAT_ID)))
        async def test_command_handler(event):
            global alert_active, last_message_time
            raw = (event.message.text or "").strip()
            command = raw.lower()
            if event.sender_id == BOT_USER_ID or event.message.id in bot_output_message_ids:
                return

            if command == "/test_start":
                async with test_command_lock:
                    alert_active = True
                    for channel_name in ALL_CONTENT_CHANNELS:
                        buffers[channel_name].clear()
                    await send_to_alert_channel("🚨 <b>TEST AIR ALERT — KYIV</b>\n\n⚡ REAL-TIME test mode active")
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
                    alert_active = False
                    await send_to_alert_channel("✅ <b>TEST ALL CLEAR — KYIV</b>\n\n📋 Back to NORMAL test mode")
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

        # Establish the Telegram trigger state immediately from its latest explicit event.
        recent_trigger_messages = await client.get_messages(source_entities[BACKUP_TRIGGER_CHANNEL], limit=20)
        for recent in recent_trigger_messages:
            state = classify_telegram_alert(recent.text or "")
            if state is not None:
                telegram_alert_state = state
                print(f"✅ Telegram alert state loaded: {'ACTIVE' if state else 'CLEAR'}")
                break

        print(
            f"✅ Connected in production. Alert trigger: @{BACKUP_TRIGGER_CHANNEL}; "
            f"content sources: {ALL_CONTENT_CHANNELS}; alert feeds: {ALERT_FEED_CHANNELS}"
        )
        await send_to_owner(
            "🟢 <b>Kyiv Normal Monitor started</b>\n"
            "Mode: NORMAL (hourly summaries)\n"
            "Night pause: 01:00–07:00 EET/EEST"
        )

        @client.on(events.NewMessage(chats=int(ALERT_CHANNEL_ID)))
        async def production_command_handler(event):
            global alert_active
            text = (event.message.text or "").strip().lower()
            if text == "/alert":
                alert_active = True
                recent_alert_messages.clear()
                for channel_name in ALL_CONTENT_CHANNELS:
                    buffers[channel_name].clear()
                await send_to_alert_channel("🚨 <b>MANUAL OVERRIDE</b>\n⚡ Switched to REAL-TIME mode")
            elif text == "/normal":
                alert_active = False
                await send_to_alert_channel("✅ <b>MANUAL OVERRIDE</b>\n📋 Back to NORMAL mode")

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
            clean = clean_text(raw_text)

            if channel == BACKUP_TRIGGER_CHANNEL:
                state = classify_telegram_alert(clean)
                if state is not None:
                    telegram_alert_state = state
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
                if channel in ALERT_FEED_CHANNELS and should_publish_alert(clean, channel):
                    asyncio.create_task(handle_alert_message(clean, source=channel))
                return

            if channel == ALERT_FEED_CHANNEL:
                return

            time_str = datetime.now(TZ).strftime("%H:%M")
            channel_buffer = buffers.get(channel)
            if channel_buffer is None:
                print(f"[IGNORED NON-CONTENT CHAT] channel={channel} chat_id={event.chat_id}")
                return
            channel_buffer.append({"time": time_str, "text": clean[:800]})
            print(f"[BUFFERED] @{channel}: pending={len(channel_buffer)} text={clean[:80]}")

    asyncio.create_task(summary_loop())
    asyncio.create_task(summary_watchdog_loop())
    if not TEST_MODE:
        asyncio.create_task(health_loop())

    try:
        await client.run_until_disconnected()
    finally:
        await http_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
