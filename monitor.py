"""
 Kyiv Alert Monitor v6 — low-latency async pipeline
- Production trigger: @kyiv_airraid_alert
- Normal mode: scheduled analysis of news channels published in the news group
- Alert mode (24/7): only @kyivnebomonitoring in the alert-only channel
- Daily situation report: only the strict #обстановка post from @war_monitor
- Health check every 12h: private warning to owner if channels go silent
"""
import asyncio
import base64
import html
import json
import os
import re
import time
import httpx
from collections import deque
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.errors import AuthKeyDuplicatedError
import state_store
from alert_rules import classify_telegram_alert
from predeploy_check import validate_environment
from text_processing import (
    SECURITY_KEYWORDS,
    build_alert_translation_prompt,
    clean_alert_source_text,
    clean_text,
    contains_operational_location,  # noqa: F401 -- re-exported for routing regressions
    contains_any,
    is_actionable_alert_message,
    is_commentary_alert_message,
    is_non_operational_alert_message,
    is_pure_ad,
    is_translation_meta_output,  # noqa: F401 -- re-exported for tests/test_routing.py
    is_valid_alert_translation,  # noqa: F401 -- re-exported for tests/test_routing.py
    normalize_alert_for_dedup,
    parse_alert_gate_output,
    parse_first_json_object,
    parse_ukraine_alarm_kyiv_state,
    strip_mixed_alert_commentary,
    translate_known_terse_fragment,
    utc_iso,
)

# --- Credentials ---
TELEGRAM_API_ID = int(os.environ["TELEGRAM_API_ID"])
TELEGRAM_API_HASH = os.environ["TELEGRAM_API_HASH"]
TELEGRAM_SESSION = os.environ["TELEGRAM_SESSION"]
TEST_TELEGRAM_SESSION = os.environ.get("TEST_TELEGRAM_SESSION")
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
UKRAINE_ALARM_API_KEY = os.environ.get("UKRAINE_ALARM_API_KEY", "").strip()
if UKRAINE_ALARM_API_KEY == "disabled":
    UKRAINE_ALARM_API_KEY = ""
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
INSIDER_UA_CHANNEL = "insiderukr"
KYIV_CITY_OFFICIAL_CHANNEL = "KyivCityOfficial"
SUSPILNE_KYIV_CHANNEL = "suspilne_kyiv"
UKRENERGO_CHANNEL = "ukrenergo"
UKRZALINFO_CHANNEL = "UkrzalInfo"
WAR_MONITOR_CHANNEL = "war_monitor"
ALERT_FEED_CHANNEL = "kyivnebomonitoring"
UKRAINE_NEWS_CHANNEL = "shv_ukr"
BACKUP_TRIGGER_CHANNEL = "kyiv_airraid_alert"
ALL_CONTENT_CHANNELS = [
    KYIV_INFO_CHANNEL,
    UKRAINE_NEWS_CHANNEL,
    AMK_CHANNEL,
    INSIDER_UA_CHANNEL,
    KYIV_CITY_OFFICIAL_CHANNEL,
    SUSPILNE_KYIV_CHANNEL,
    UKRENERGO_CHANNEL,
    UKRZALINFO_CHANNEL,
]
ALERT_FEED_CHANNELS = [ALERT_FEED_CHANNEL]

SUMMARY_HOURS = (1, 7, 9, 11, 13, 15, 17, 19, 21, 23)
SUMMARY_INTERVAL = 180 if TEST_MODE else 2 * 3600
HEALTH_CHECK_INTERVAL = 43200  # 12 hours
SILENCE_THRESHOLD = 4 * 3600  # 4 hours of total silence = warning
ALERT_FEED_POLL_INTERVAL = float(os.environ.get("ALERT_FEED_POLL_INTERVAL", "5"))
ALERT_RECOVERY_MAX_MESSAGES = int(os.environ.get("ALERT_RECOVERY_MAX_MESSAGES", "3"))
TELETHON_HANDOFF_DELAY = float(os.environ.get("TELETHON_HANDOFF_DELAY", "15.0"))
UKRAINE_ALARM_POLL_INTERVAL = float(os.environ.get("UKRAINE_ALARM_POLL_INTERVAL", "30"))
UKRAINE_ALARM_REGION_ID = os.environ.get("UKRAINE_ALARM_REGION_ID", "31").strip()
UKRAINE_ALARM_URL = (
    f"https://api.ukrainealarm.com/api/v3/alerts/{UKRAINE_ALARM_REGION_ID}"
)

# --- Timezone ---
TZ = ZoneInfo("Europe/Kyiv")  # EET/EEST auto

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
    "ukraine_key_developments": {
        "name": "Ukraine — Key Developments",
        "icon": "🇺🇦",
        "criteria": (
            "Major Ukrainian developments that materially affect the population: government and parliamentary "
            "decisions, laws, mobilization, economy, health, diplomacy, international support, sanctions, "
            "corruption, major frontline changes, and strategically important war developments."
        ),
    },
    "kyiv_region": {
        "name": "Kyiv & Region",
        "icon": "🏙️",
        "criteria": (
            "Concrete developments affecting Kyiv city or Kyiv Oblast: local decisions, roads, closures, "
            "schools, evacuations, public safety, accidents, fires, police operations, and other changes that "
            "matter to residents. Put transport and utility disruptions in transport_essential_services."
        ),
    },
    "security_consequences": {
        "name": "Security & Attack Consequences",
        "icon": "🛡️",
        "criteria": (
            "Concrete outcomes of attacks: confirmed impacts, damage, fires, casualties, service disruption, "
            "or substantial quantified attack and interception recaps. Exclude routine alert/all-clear notices, "
            "shelter instructions, live trajectories or locations, isolated launches, and bare interception "
            "claims without a concrete result. Preserve all stated counts and never invent or combine "
            "incompatible quantities."
        ),
    },
    "transport_essential_services": {
        "name": "Transport & Essential Services",
        "icon": "🚇",
        "criteria": (
            "Material changes to metro, buses, trams, railways, roads, electricity, water, heating, mobile or "
            "internet service in Kyiv or elsewhere in Ukraine. Keep interruptions, restorations, major delays, "
            "closures and actionable schedule changes; exclude routine corporate publicity and minor delays."
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


NORMAL_HISTORY_BOOTSTRAP_HOURS = 1

# --- Pre-filters ---
# (keyword/pattern constants live in text_processing.py, imported above)

# --- State ---
buffers = {ch: [] for ch in ALL_CONTENT_CHANNELS}
alert_active = False
alert_started_at = None
telegram_alert_state = None
last_send_time = 0
last_message_time = time.time()
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
recent_alert_source_context = deque(maxlen=12)
alert_delivery_tasks = set()
alert_generation = 0
ALERT_DEDUP_WINDOW = 180
TEST_SOURCE_PREFIX = "[TEST_SOURCE:kyivnebomonitoring]"
TEST_BUFFER_CHANNEL = AMK_CHANNEL
TEST_SAMPLE_MESSAGES = [
    "⚠️ Київщина: зафіксовано рух ударних БпЛА Shahed drone у напрямку Києва.",
    "Ракетна небезпека: missile launch activity зафіксована з північного напрямку.",
    "Група БпЛА продовжує рух; air-defense monitoring reports drone activity near Kyiv region.",
]


def is_authorized_admin(sender_id):
    return sender_id in ADMIN_USER_IDS


async def is_blocked_alert_image(message):
    """Reject fundraising, advertising and engagement-only alert images."""
    if not getattr(message, "photo", None) or production_client is None:
        return False
    try:
        image_bytes = await production_client.download_media(message, bytes, thumb=-1)
        if not image_bytes:
            return False
        response = await http_client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": 10,
                "messages": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": base64.b64encode(image_bytes).decode("ascii"),
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "Reply BLOCK only if this image prominently requests donations or money, "
                                "shows payment details, a fundraising QR/card/jar, advertising, thanks "
                                "supporters, or is purely social engagement. Otherwise reply ALLOW."
                            ),
                        },
                    ],
                }],
            },
            timeout=20,
        )
        response.raise_for_status()
        return response.json()["content"][0]["text"].strip().upper().startswith("BLOCK")
    except Exception as exc:
        print(f"[ALERT IMAGE CHECK ERROR] {type(exc).__name__}: {exc}")
        return False

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


def forget_failed_alert(text, source):
    """Allow a failed delivery to be retried instead of deduplicated."""
    normalized = normalize_alert_for_dedup(text)
    for item in reversed(recent_alert_messages):
        if item[1] == source and item[2] == normalized:
            recent_alert_messages.remove(item)
            break


def remember_alert_source_message(source, message_id, text, message_at=None):
    """Return recent same-source context, then remember this tactical source message."""
    timestamp = message_at.timestamp() if message_at else time.time()
    cutoff = timestamp - 10 * 60
    retained = [
        item for item in recent_alert_source_context
        if item[0] >= cutoff and not (item[1] == source and item[2] == message_id)
    ]
    recent_alert_source_context.clear()
    recent_alert_source_context.extend(retained)
    context = [item[3] for item in retained if item[1] == source][-3:]
    recent_alert_source_context.append((timestamp, source, message_id, text[:1000]))
    return context

def seconds_until_next_summary(now=None) -> float:
    """Return the delay to the next configured Europe/Kyiv summary slot."""
    now = now or datetime.now(TZ)
    for day_offset in (0, 1):
        day = now.date() + timedelta(days=day_offset)
        for hour in SUMMARY_HOURS:
            candidate = datetime.combine(day, datetime.min.time(), TZ).replace(hour=hour)
            if candidate > now:
                return max(0.1, (candidate - now).total_seconds())
    raise RuntimeError("No summary slot configured")

async def reconcile_alert_state(source):
    """Apply the explicit trigger state and retry when public delivery fails."""
    return await apply_alert_state(telegram_alert_state, source)


async def ukraine_alarm_shadow_loop():
    """Report official API observations to OPS without controlling public state."""
    previous = None
    previous_mismatch = None
    error_streak = 0
    headers = {"Authorization": UKRAINE_ALARM_API_KEY, "Accept": "application/json"}
    while True:
        try:
            response = await http_client.get(UKRAINE_ALARM_URL, headers=headers, timeout=10.0)
            if response.status_code != 200:
                error_streak += 1
                if error_streak in {1, 5, 20}:
                    await send_to_owner(
                        "🛰️ <b>UkraineAlarm API test error</b>\n"
                        f"HTTP {response.status_code}; attempt {error_streak}. "
                        "Telegram and the public alert state are unaffected."
                    )
                await asyncio.sleep(UKRAINE_ALARM_POLL_INTERVAL)
                continue
            error_streak = 0
            observed = parse_ukraine_alarm_kyiv_state(response.json())
            if observed != previous:
                print(f"[UKRAINEALARM SHADOW] Kyiv={'ACTIVE' if observed else 'CLEAR'}")
                await send_to_owner(
                    "🛰️ <b>UkraineAlarm API test</b>\n"
                    f"Kyiv City: <b>{'ALERT' if observed else 'NORMAL'}</b>. "
                    "Shadow mode: no public state change."
                )
                previous = observed
            mismatch = telegram_alert_state is not None and observed != telegram_alert_state
            if mismatch and mismatch != previous_mismatch:
                print(
                    "[UKRAINEALARM SHADOW MISMATCH] "
                    f"api={'ACTIVE' if observed else 'CLEAR'} "
                    f"telegram={'ACTIVE' if telegram_alert_state else 'CLEAR'}"
                )
                await send_to_owner(
                    "⚠️ <b>UkraineAlarm shadow mismatch</b>\n"
                    f"API: <b>{'ALERT' if observed else 'NORMAL'}</b>; "
                    f"Telegram: <b>{'ALERT' if telegram_alert_state else 'NORMAL'}</b>. "
                    "No public state change."
                )
            previous_mismatch = mismatch
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_streak += 1
            if error_streak in {1, 5, 20}:
                print(f"[UKRAINEALARM SHADOW ERROR] {type(exc).__name__}: {exc}")
        await asyncio.sleep(UKRAINE_ALARM_POLL_INTERVAL)


def set_alert_state(new_state, reason):
    """The only function allowed to mutate the effective alert state."""
    global alert_active, alert_started_at, alert_generation
    if new_state == alert_active:
        return False
    previous = alert_active
    alert_active = new_state
    alert_started_at = time.monotonic() if new_state else None
    alert_generation += 1
    recent_alert_source_context.clear()
    state_store.persist_operational_state("alert_active", "1" if new_state else "0")
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
                state_store.discard_pending_normal_messages(f"startup_alert:{source}")
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
            state_store.discard_pending_normal_messages(f"alert_started:{source}")
        elif production_client and content_source_entities:
            await advance_normal_cursors_to_latest(
                production_client, content_source_entities, f"alert_ended:{source}"
            )
        print(f"[ALERT TRANSITION] committed={'ALERT' if desired else 'NORMAL'} source={source}")
        return True


async def translate_message(text, context=()):
    known_fragment = translate_known_terse_fragment(text)
    if known_fragment:
        return known_fragment
    try:
        async with translation_slots:
            r = await http_client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": MODEL, "max_tokens": 200, "temperature": 0, "messages": [{
                "role": "user", "content": build_alert_translation_prompt(text, context)
            }]},
            timeout=httpx.Timeout(15.0, connect=5.0)
            )
        r.raise_for_status()
        result = r.json()["content"][0]["text"].strip()
        decision, translation, reason = parse_alert_gate_output(result, text)
        if decision == "DROP":
            print(f"[ALERT SEMANTIC DROP] reason={reason!r} input={text[:120]!r}")
            return False
        if reason.startswith("override_unapproved_drop:"):
            print(f"[ALERT DROP OVERRIDDEN] reason={reason!r} input={text[:120]!r}")
        print(f"[TRANSLATION OK] input={text[:120]!r} output={translation[:120]!r}")
        return translation
    except Exception as e:
        print(f"Translation error: {e}")
        try:
            await send_to_owner(
                f"Ops: translation request failed; nothing published.\nInput: {text[:300]}"
            )
        except Exception as ops_err:
            print(f"Ops notify failed: {ops_err}")
        return None


WAR_MONITOR_REPORT_RE = re.compile(
    r"\A📡\s*Обстановка станом на (?:[01]\d|2[0-3]):[0-5]\d\s*\n"
    r"(?P<date>\d{2}\.\d{2}\.\d{2,4})\b[\s\S]*#обстановка@war_monitor\b"
)


def is_daily_war_monitor_report(text, today=None):
    """Accept only today's explicitly tagged daily situation report."""
    match = WAR_MONITOR_REPORT_RE.search((text or "").strip())
    if not match:
        return False
    date_format = "%d.%m.%y" if len(match.group("date")) == 8 else "%d.%m.%Y"
    try:
        report_date = datetime.strptime(match.group("date"), date_format).date()
    except ValueError:
        return False
    return report_date == (today or datetime.now(TZ).date())


async def translate_war_monitor_report(text):
    """Translate the one accepted daily report without the tactical alert gate."""
    try:
        async with translation_slots:
            response = await http_client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": MODEL,
                    "max_tokens": 600,
                    "temperature": 0,
                    "messages": [{
                        "role": "user",
                        "content": (
                            "Translate this Ukrainian daily military situation report into concise, natural "
                            "English. Preserve its heading, date, section structure, facts, uncertainty and "
                            "punctuation. Do not add analysis or commentary. Omit only the final source hashtag. "
                            "Return only the translated report.\n\n" + text[:2500]
                        ),
                    }],
                },
                timeout=httpx.Timeout(30.0, connect=5.0),
            )
        response.raise_for_status()
        return response.json()["content"][0]["text"].strip()
    except Exception as exc:
        print(f"[WAR MONITOR TRANSLATION ERROR] {type(exc).__name__}: {exc}")
        await send_to_owner(
            "Ops: @war_monitor daily report translation failed; nothing published."
        )
        return None


async def process_war_monitor_report(message):
    """Publish only today's tagged daily report, once, to the requested destinations."""
    raw = (message.text or "").strip()
    if not is_daily_war_monitor_report(raw):
        print(f"[WAR MONITOR IGNORED] id={message.id}")
        return False
    if not state_store.claim_alert_feed_delivery(WAR_MONITOR_CHANNEL, message.id, raw):
        print(f"[WAR MONITOR DUPLICATE] id={message.id}")
        return False

    translated = await translate_war_monitor_report(raw)
    if not translated:
        state_store.release_alert_feed_delivery(WAR_MONITOR_CHANNEL, message.id, raw)
        return False

    rendered = html.escape(translated)
    if not await send_to_summary_group(rendered):
        state_store.release_alert_feed_delivery(WAR_MONITOR_CHANNEL, message.id, raw)
        await send_to_owner("Ops: @war_monitor daily report failed to publish in the news channel.")
        return False
    if not alert_active and not await send_to_alert_channel(rendered):
        await send_to_owner(
            "Ops: @war_monitor daily report reached the news channel but failed in the alert channel."
        )
    print(f"[WAR MONITOR PUBLISHED] id={message.id} alert_copy={not alert_active}")
    return True

async def ensure_live_source_membership(client, source_entities):
    """Join sources that depend on Telegram push updates."""
    for channel in ALERT_FEED_CHANNELS + [WAR_MONITOR_CHANNEL]:
        try:
            await client(JoinChannelRequest(source_entities[channel]))
            print(f"[LIVE SOURCE MEMBERSHIP] joined=@{channel}")
        except Exception as exc:
            if type(exc).__name__ == "UserAlreadyParticipantError":
                print(f"[LIVE SOURCE MEMBERSHIP] already_joined=@{channel}")
                continue
            raise RuntimeError(f"Cannot join required live source @{channel}") from exc


async def recover_war_monitor_report(client, source_entities):
    """Recover today's recent report if a Telegram push update was missed."""
    messages = await client.get_messages(source_entities[WAR_MONITOR_CHANNEL], limit=5)
    for message in messages:
        age = max(0, time.time() - message.date.timestamp())
        if is_daily_war_monitor_report(message.text or "") and age <= 3 * 3600:
            return await process_war_monitor_report(message)
    return False


async def war_monitor_poll_loop(client, source_entities):
    while True:
        try:
            await recover_war_monitor_report(client, source_entities)
        except Exception as exc:
            print(f"[WAR MONITOR POLL ERROR] {type(exc).__name__}: {exc}")
        await asyncio.sleep(60)


async def backfill_alert_feed(client, source_entities):
    """Recover tactical posts emitted after the trigger but before listener readiness."""
    if not alert_active:
        return 0
    trigger_at_raw = state_store.load_operational_state("telegram_alert_message_at")
    trigger_at = None
    if trigger_at_raw:
        try:
            trigger_at = datetime.fromisoformat(trigger_at_raw)
        except ValueError:
            print(f"[ALERT BACKFILL] invalid trigger timestamp={trigger_at_raw!r}")

    delivered = 0
    for channel in ALERT_FEED_CHANNELS:
        cursor = state_store.get_alert_feed_cursor(channel)
        messages = await client.get_messages(source_entities[channel], limit=50)
        pending = [message for message in reversed(messages) if message.id > cursor]
        if len(pending) > ALERT_RECOVERY_MAX_MESSAGES:
            skipped = pending[:-ALERT_RECOVERY_MAX_MESSAGES]
            state_store.set_alert_feed_cursor(channel, skipped[-1].id)
            print(
                f"[ALERT BACKFILL COLLAPSED] @{channel} skipped={len(skipped)} "
                f"through_id={skipped[-1].id}"
            )
            pending = pending[-ALERT_RECOVERY_MAX_MESSAGES:]
        for message in pending:
            if not alert_active:
                print(f"[ALERT BACKFILL STOPPED] @{channel} reason=clear")
                return delivered
            if trigger_at and message.date and message.date < trigger_at:
                continue
            if await process_alert_feed_message(message, channel):
                delivered += 1
    if delivered:
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
                    state_store.persist_trigger_observation(observed, trigger.id, trigger.date)
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


async def sync_normal_history(client, source_entities):
    """Backfill messages missed by live events and advance each cursor atomically."""
    global last_message_time
    if TEST_MODE or not state_store.stats_db_ready:
        return True
    all_ok = True
    bootstrap_cutoff = datetime.now(timezone.utc) - timedelta(hours=NORMAL_HISTORY_BOOTSTRAP_HOURS)
    for channel in ALL_CONTENT_CHANNELS:
        cursor = state_store.get_source_cursor(channel)
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

            if not state_store.persist_history_batch(channel, rows, latest_id):
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
    if TEST_MODE or not state_store.stats_db_ready:
        return
    for channel in ALL_CONTENT_CHANNELS:
        try:
            latest = await client.get_messages(source_entities[channel], limit=1)
            latest_id = int(latest[0].id) if latest else state_store.get_source_cursor(channel)
            if latest_id is not None:
                state_store.persist_history_batch(channel, [], latest_id)
                print(f"[HISTORY CURSOR] @{channel}: cursor={latest_id} reason={reason}")
        except Exception as exc:
            print(f"[HISTORY CURSOR ERROR] @{channel}: {type(exc).__name__}: {exc}")


def normalize_category_result(parsed, messages):
    """Strictly validate categories, value types and selected message IDs."""
    if set(parsed.keys()) != {"categories"}:
        raise ValueError("structured result must contain only categories")
    supplied = parsed["categories"]
    if not isinstance(supplied, dict) or set(supplied.keys()) != set(CATEGORIES.keys()):
        raise ValueError("structured result must contain every configured category exactly once")

    valid_ids = {item["id"] for item in messages}
    normalized = {}
    assigned_ids = set()
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
        duplicate_ids = [message_id for message_id in selected_ids if message_id in assigned_ids]
        if duplicate_ids:
            raise ValueError(f"message IDs assigned to multiple categories: {duplicate_ids[:5]}")
        assigned_ids.update(selected_ids)

        normalized[category_key] = {
            "selected_ids": list(dict.fromkeys(selected_ids)),
            "bullets": [
                bullet.strip().lstrip("•- ").strip()
                for bullet in bullets
                if bullet.strip()
            ][:3],
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
        f"ID={item['id']} SOURCE=@{item['channel']} TIME={item['time']}\n{item['text'][:500]}"
        for item in messages
    )
    prompt = (
        "Classify Ukrainian news messages. The source is provenance only: evaluate EVERY message against "
        "EVERY category below. Assign each qualifying message to exactly one best primary category; the same "
        "message ID must never appear in multiple categories. "
        "Do not favor or exclude a message because of its source channel. Reject advertising, clickbait, "
        "routine statements without a concrete development, and unrelated material. The news channel must "
        "not repeat the real-time alert feed: reject alert and all-clear announcements, generic shelter "
        "instructions, live missile/UAV/object headings or locations, and bare interception claims. Keep an "
        "attack item only when it reports a concrete consequence or a substantial quantified recap.\n\n"
        f"Categories:\n{category_text}\n\n"
        "Use the required structured output schema. selected_ids must contain exact message IDs that qualify. "
        "bullets must contain at most three concise English summary strings per category. Within each category, "
        "Write complete bullets; never truncate a sentence or word. "
        "order bullets chronologically from the earliest event/message time to the latest. Preserve stated "
        "locations, uncertainty, times and quantities.\n\nMessages:\n" + message_text
    )

    token_budgets = (2000,)
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
            category_key = "security_consequences"
        elif item["channel"] == KYIV_INFO_CHANNEL:
            category_key = "kyiv_region"
        else:
            category_key = "ukraine_key_developments"
        result[category_key]["selected_ids"].append(item["id"])
        if len(result[category_key]["bullets"]) < 3:
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
    """Send news output to the linked summary group."""
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
    if not state_store.stats_db_ready:
        failures.append("persistent SQLite database is unavailable")
    else:
        marker = f"startup-{time.time_ns()}"
        if not state_store.persist_operational_state("startup_self_check", marker):
            failures.append("persistent SQLite database is not writable")
        elif state_store.load_operational_state("startup_self_check") != marker:
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
    async with summary_lock:
        if not TEST_MODE and state_store.stats_db_ready and production_client and content_source_entities:
            await sync_normal_history(production_client, content_source_entities)

        snapshots = []
        snapshot_lengths = {}
        snapshot_message_keys = []
        received_by_channel = {}

        if not TEST_MODE and state_store.stats_db_ready:
            pending_rows = state_store.load_pending_normal_messages()
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

        state_store.persist_category_stats(run_at, snapshots, category_results, ALL_CONTENT_CHANNELS)
        if not TEST_MODE and state_store.stats_db_ready:
            state_store.mark_normal_messages_processed(snapshot_message_keys)
        else:
            for channel, length in snapshot_lengths.items():
                del buffers[channel][:length]

        outcome = "delivered" if sections else "ops_heartbeat"
        print(
            f"[SUMMARY COMPLETED] trigger={trigger} "
            f"outcome={outcome} messages={len(snapshots)}"
        )
        return True


async def summary_loop():
    first_delay = SUMMARY_INTERVAL if TEST_MODE else seconds_until_next_summary()
    print(
        f"[SUMMARY SCHEDULE] first_run_in={first_delay:.1f}s "
        f"hours={SUMMARY_HOURS} timezone={TZ.key}"
    )
    await asyncio.sleep(first_delay)
    while True:
        try:
            if not alert_active:
                hour = datetime.now(TZ).hour
                await build_summary(
                    night_recap=not TEST_MODE and hour == 7,
                    trigger="night_recap" if not TEST_MODE and hour == 7 else "scheduled",
                )
        except Exception as exc:
            print(f"[SUMMARY LOOP ERROR] {type(exc).__name__}: {exc}")
            await send_to_owner(
                f"🚨 <b>Summary loop recovered</b>\n{html.escape(type(exc).__name__ + ': ' + str(exc))}"
            )
        await asyncio.sleep(SUMMARY_INTERVAL if TEST_MODE else seconds_until_next_summary())


async def health_loop():
    while True:
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)
        silence = time.time() - last_message_time
        if silence > SILENCE_THRESHOLD:
            hours = int(silence // 3600)
            await send_to_owner(f"⚠️ <b>Kyiv Monitor warning</b>\nNo messages received from any channel in ~{hours}h. Connection may be down — check Railway.")



def schedule_alert_delivery(clean, source, context=()):
    """Track background delivery so transitions and shutdown can drain it safely."""
    generation = alert_generation
    task = asyncio.create_task(
        handle_alert_message(clean, source=source, generation=generation, context=context)
    )
    alert_delivery_tasks.add(task)
    task.add_done_callback(alert_delivery_tasks.discard)
    return task


def schedule_alert_image_processing(message, channel, clean, edited, context=()):
    """Analyze an image without holding up later alert-feed messages."""
    task = asyncio.create_task(
        process_alert_image_message(message, channel, clean, edited, context)
    )
    alert_delivery_tasks.add(task)
    task.add_done_callback(alert_delivery_tasks.discard)
    return task


async def process_alert_image_message(message, channel, clean, edited, context=()):
    try:
        if await is_blocked_alert_image(message):
            state_store.set_alert_feed_cursor(
                channel, max(state_store.get_alert_feed_cursor(channel), message.id)
            )
            print(f"[ALERT FILTERED IMAGE] @{channel} id={message.id}")
            return False
        delivered = bool(await schedule_alert_delivery(clean, source=channel, context=context))
        if delivered:
            state_store.set_alert_feed_cursor(
                channel, max(state_store.get_alert_feed_cursor(channel), message.id)
            )
            print(f"[ALERT PROCESSED] @{channel} id={message.id} edited={edited}")
        else:
            state_store.release_alert_feed_delivery(channel, message.id, clean)
            forget_failed_alert(clean, channel)
        return delivered
    except asyncio.CancelledError:
        state_store.release_alert_feed_delivery(channel, message.id, clean)
        raise


async def handle_alert_message(clean, source=ALERT_FEED_CHANNEL, generation=None, context=()):
    """Translate first, then publish one final English alert if it is still current."""
    generation = alert_generation if generation is None else generation
    if generation != alert_generation or not alert_active:
        print(f"[ALERT STALE] skipped source=@{source} generation={generation}")
        return False

    original = clean
    clean = strip_mixed_alert_commentary(clean)
    if clean != original:
        await send_to_owner(
            "💬 <b>COMMENTO PARZIALE</b> — rimosso prima della pubblicazione\n"
            f"Source: @{html.escape(source)}\n"
            f"Original: {html.escape(original[:1000])}\n"
            f"Pubblicato: {html.escape(clean[:1000])}"
        )

    if is_commentary_alert_message(clean):
        print(f"[ALERT COMMENTARY] hidden from public source=@{source}: {clean[:100]}")
        return bool(await send_to_owner(
            "💬 <b>COMMENTO</b> — non pubblicato nel canale\n"
            f"Source: @{html.escape(source)}\n"
            f"Original: {html.escape(clean[:1000])}"
        ))

    print(f"[ALERT ACCEPTED] @{source}: {clean[:100]}")
    try:
        translation = await translate_message(clean[:1500], context=context)
    except asyncio.CancelledError:
        print(f"[ALERT CANCELLED] translation interrupted source=@{source} generation={generation}")
        raise

    if generation != alert_generation or not alert_active:
        print(f"[ALERT STALE] translation discarded source=@{source} generation={generation}")
        return False
    if translation is False:
        # A semantic DROP is successfully handled. Returning True checkpoints the
        # source cursor so the poller cannot bill the same classification forever.
        return True
    if not translation:
        print(f"[ALERT NOT PUBLISHED] translation unavailable source=@{source}")
        try:
            await send_to_owner(
                f"Ops: alert not published because translation was unavailable.\n"
                f"Source: @{source}\nInput: {clean[:500]}"
            )
        except Exception as ops_err:
            print(f"Ops notify failed: {ops_err}")
        return False

    return bool(await safe_send(f"🔴 {html.escape(translation)}"))


async def process_alert_feed_message(message, channel, *, edited=False):
    """Filter, deliver and checkpoint one ALERT-feed message."""
    if not alert_active:
        return False
    if not edited and state_store.is_stale_alert_feed_message(channel, message.id):
        print(f"[ALERT SKIPPED STALE] @{channel} id={message.id}")
        return False

    clean = clean_alert_source_text(message.text or "")
    cursor = state_store.get_alert_feed_cursor(channel)
    if (
        len(clean) < 5
        or is_non_operational_alert_message(clean)
        or is_pure_ad(clean)
    ):
        state_store.set_alert_feed_cursor(channel, max(cursor, message.id))
        print(f"[ALERT FILTERED] @{channel} id={message.id}")
        return False
    if not state_store.claim_alert_feed_delivery(channel, message.id, clean):
        print(f"[ALERT SKIPPED DELIVERED] @{channel} id={message.id} edited={edited}")
        return False
    if not should_publish_alert(clean, channel):
        state_store.set_alert_feed_cursor(channel, max(cursor, message.id))
        return False

    context = remember_alert_source_message(
        channel,
        message.id,
        strip_mixed_alert_commentary(clean),
        getattr(message, "date", None),
    )

    if getattr(message, "photo", None):
        schedule_alert_image_processing(message, channel, clean, edited, context)
        print(f"[ALERT IMAGE QUEUED] @{channel} id={message.id}")
        return True

    delivered = bool(await schedule_alert_delivery(clean, source=channel, context=context))
    if delivered:
        state_store.set_alert_feed_cursor(channel, max(cursor, message.id))
        print(f"[ALERT PROCESSED] @{channel} id={message.id} edited={edited}")
    else:
        state_store.release_alert_feed_delivery(channel, message.id, clean)
        forget_failed_alert(clean, channel)
    return delivered


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
    global telegram_alert_state, production_client, content_source_entities
    http_client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=30, max_keepalive_connections=15),
        http2=False,
    )
    send_lock = asyncio.Lock()
    test_command_lock = asyncio.Lock()
    translation_slots = asyncio.Semaphore(8)
    summary_lock = asyncio.Lock()
    alert_transition_lock = asyncio.Lock()
    state_store.stats_db_ready = state_store.initialize_stats_db()

    client = None
    session_lock = None
    if not TEST_MODE or TEST_TELEGRAM_SESSION:
        if not TEST_MODE:
            # Keep a reference for the life of this coroutine: the returned handle
            # holds the flock, and if nothing referenced it, it could be garbage
            # collected (closing the file and releasing the lock) before shutdown.
            session_lock = state_store.acquire_telethon_session_lock()  # noqa: F841
            if TELETHON_HANDOFF_DELAY:
                print(
                    f"[TELETHON HANDOFF] waiting {TELETHON_HANDOFF_DELAY:g}s "
                    "for the previous Railway deployment to stop"
                )
                await asyncio.sleep(TELETHON_HANDOFF_DELAY)
        session_value = TEST_TELEGRAM_SESSION if TEST_MODE else TELEGRAM_SESSION
        client = TelegramClient(StringSession(session_value), TELEGRAM_API_ID, TELEGRAM_API_HASH)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                raise RuntimeError(
                    "TELEGRAM_SESSION is not authorized; regenerate it instead of using interactive login"
                )
        except AuthKeyDuplicatedError:
            await send_to_owner(
                "WARNING Telegram session invalidated (AuthKeyDuplicatedError). "
                "The worker cannot start. Regenerate TELEGRAM_SESSION and redeploy. "
                "Ensure the session runs from only one place."
            )
            raise

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
            ALL_CONTENT_CHANNELS
            + ALERT_FEED_CHANNELS
            + [BACKUP_TRIGGER_CHANNEL, WAR_MONITOR_CHANNEL]
        ))
        for channel_name in production_channels:
            entity = await client.get_entity(channel_name)
            source_entities[channel_name] = entity
            channel_by_chat_id[utils.get_peer_id(entity)] = channel_name
        production_client = client
        content_source_entities = {
            channel: source_entities[channel] for channel in ALL_CONTENT_CHANNELS
        }
        await ensure_live_source_membership(client, source_entities)

        # Establish the Telegram trigger state immediately from its latest explicit event.
        recent_trigger_messages = await client.get_messages(source_entities[BACKUP_TRIGGER_CHANNEL], limit=20)
        for recent in recent_trigger_messages:
            state = classify_telegram_alert(recent.text or "")
            if state is not None:
                telegram_alert_state = state
                state_store.persist_trigger_observation(state, recent.id, recent.date)
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

        await recover_war_monitor_report(client, source_entities)

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
        @client.on(events.MessageEdited(chats=[source_entities[channel] for channel in ALERT_FEED_CHANNELS]))
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
                    state_store.persist_trigger_observation(state, event.message.id, event.message.date)
                    print(f"Telegram trigger update: {'ACTIVE' if state else 'CLEAR'}")
                    await reconcile_alert_state(f"@{BACKUP_TRIGGER_CHANNEL}")
                return

            if channel == WAR_MONITOR_CHANNEL:
                await process_war_monitor_report(event.message)
                return

            if channel in ALERT_FEED_CHANNELS:
                if alert_active:
                    await process_alert_feed_message(
                        event.message,
                        channel,
                        edited=bool(getattr(event.message, "edit_date", None)),
                    )
                return

            if is_pure_ad(clean):
                print(f"[FILTERED AD] @{channel}: {clean[:80]}")
                return

            if alert_active:
                return

            time_str = datetime.now(TZ).strftime("%H:%M")
            channel_buffer = buffers.get(channel)
            if channel_buffer is None:
                print(f"[IGNORED NON-CONTENT CHAT] channel={channel} chat_id={event.chat_id}")
                return
            if state_store.stats_db_ready and state_store.persist_normal_message(
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
        asyncio.create_task(war_monitor_poll_loop(client, source_entities))

    asyncio.create_task(summary_loop())
    if not TEST_MODE:
        asyncio.create_task(health_loop())
        if UKRAINE_ALARM_API_KEY:
            asyncio.create_task(ukraine_alarm_shadow_loop())
            print("✅ UkraineAlarm API enabled in shadow mode (cannot change public alert state)")
        else:
            print("ℹ️ UkraineAlarm API shadow mode disabled: UKRAINE_ALARM_API_KEY is not set")

    try:
        await client.run_until_disconnected()
    finally:
        await drain_alert_delivery_tasks(timeout=10.0)
        await http_client.aclose()


if __name__ == "__main__":
    validate_environment(os.environ)
    asyncio.run(main())
