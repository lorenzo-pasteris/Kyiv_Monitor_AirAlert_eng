"""
 Kyiv Alert Monitor v6 — low-latency async pipeline
- Production trigger: official UkraineAlarm API (Kyiv City)
- Normal mode: hourly analysis of 3 channels with per-channel filters
- Alert mode (24/7): only @monitorwarr in real-time, with translation fallback
- Night pause: no hourly summaries 01:00-06:00 CET, one big recap at 06:00
- Health check every 12h: private warning to owner if channels go silent
"""
import asyncio
import html
import os
import re
import time
import httpx
from datetime import datetime
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
PRODUCTION_CHAT_ID = os.environ["TARGET_CHAT_ID"]
TEST_CHAT_ID = os.environ.get("TEST_CHAT_ID")
if TEST_MODE and not TEST_CHAT_ID:
    raise RuntimeError("TEST_CHAT_ID is required when TEST_MODE=true")
OUTPUT_CHAT_ID = TEST_CHAT_ID if TEST_MODE else PRODUCTION_CHAT_ID
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "392256147")  # private warnings go here
UKRAINE_ALARM_API_KEY = os.environ.get("UKRAINE_ALARM_API_KEY")
UKRAINE_ALARM_REGION_ID = os.environ.get("UKRAINE_ALARM_REGION_ID", "31")  # Kyiv City
UKRAINE_ALARM_POLL_INTERVAL = max(5, int(os.environ.get("UKRAINE_ALARM_POLL_INTERVAL", "10")))
if not TEST_MODE and not UKRAINE_ALARM_API_KEY:
    raise RuntimeError("UKRAINE_ALARM_API_KEY is required when TEST_MODE=false")

# --- Channels ---
KYIV_INFO_CHANNEL = "kievinfo_kyiv"
AMK_CHANNEL = "AMK_Mapping"
MONITOR_CHANNEL = "monitorwarr"

ALL_CONTENT_CHANNELS = [KYIV_INFO_CHANNEL, AMK_CHANNEL, MONITOR_CHANNEL]

SUMMARY_INTERVAL = 180 if TEST_MODE else 3600  # 3 minutes in test, 1 hour in production
HEALTH_CHECK_INTERVAL = 43200  # 12 hours
SILENCE_THRESHOLD = 4 * 3600  # 4 hours of total silence = warning

# --- Timezone / night pause ---
TZ = ZoneInfo("Europe/Rome")  # CET/CEST auto
NIGHT_START = 1   # 01:00 CET
NIGHT_END = 6     # 06:00 CET

MODEL = "claude-haiku-4-5"

# --- Display ---
CHANNEL_NAMES = {KYIV_INFO_CHANNEL: "Kyiv City", AMK_CHANNEL: "Military Analysis", MONITOR_CHANNEL: "Russian Monitoring"}
CHANNEL_ICONS = {KYIV_INFO_CHANNEL: "🏙️", AMK_CHANNEL: "🗺️", MONITOR_CHANNEL: "⚔️"}

# --- Prompts (normal mode) ---
CHANNEL_PROMPTS = {
    KYIV_INFO_CHANNEL: (
        "You analyze messages from a Kyiv city information channel. Extract ONLY concrete disruptions "
        "to city life: road/bridge closures, traffic, metro problems, power/water/heating outages, fires, "
        "accidents, police operations, curfews, evacuations. Exclude general news, national politics, and "
        "military updates that don't affect city infrastructure. "
        "Format: max 5 bullets, each starting with '•', one line each, English. No preamble or closing. "
        "If nothing qualifies, reply exactly 'NO_RELEVANT_INFO'."
    ),
    AMK_CHANNEL: (
        "You analyze war-analysis messages. Extract ONLY developments in the Russia-Ukraine war: frontline "
        "changes, offensives, operations, notable losses, strategic shifts. Hard exclude: Israel, Iran, Gaza, "
        "Palestine, Lebanon, Yemen, and any non-Ukraine Middle East content. "
        "Format: max 5 bullets, each '•', one line, English. No preamble. "
        "If nothing about Ukraine, reply exactly 'NO_RELEVANT_INFO'."
    ),
    MONITOR_CHANNEL: (
        "You analyze Russian military-monitoring messages. Extract ONLY: troop movements/concentrations, "
        "equipment transfers, offensive preparations, missile/drone launch activity, border or Belarus military "
        "activity, fortification work. "
        "Format: max 5 bullets, each '•', one line, English. No preamble. "
        "If nothing qualifies, reply exactly 'NO_RELEVANT_INFO'."
    )
}

# --- Pre-filters ---
KYIV_CITY_KEYWORDS = ["kyiv","kiev","київ","киев","street","road","traffic","metro","subway","protest","demonstration","rally","power","water","heating","emergency","accident","fire","police","curfew","shelter","evacuation","bridge","district","avenue","closure","closed","block","disruption","delay","cancelled","train","bus","tram"]
MIDDLE_EAST_KEYWORDS = ["israel","iran","gaza","palestine","lebanon","hamas","hezbollah","yemen","houthi","idf","tehran","jerusalem","beirut","middle east","west bank"]
MILITARY_KEYWORDS = ["troop","movement","concentration","deployment","preparation","offensive","attack","shelling","drone","missile","launch","russian","belarus","border","military","convoy","equipment","tank","armored","artillery","mlrs","iskander","kinzhal","calibr","oniks","zircon","bomb","glide","fab","umpb","lancet","orlan","zala","reconnaissance","satellite","fortification","trenches","dragon teeth","digging","barracks","railway","ukraine","ukrainian"]
AD_INDICATORS = ["#реклама","реклама","знижк","розпродаж","промокод","купуй","придбай","акція","магазин","замовляй","доставка"]
SECURITY_KEYWORDS = ["тривога","відбій","балістика","ракета","шахед","шахеди","бпла","вибух","вибухи","ппо","повітряна ціль","укриття","загроза","обстріл","приліт","дрон","дрони","mig","міг","siren","raid","missile","drone","explosion","alert","attack","ballistic","shahed","interception","strike"]
ALERT_START_UA = ["тривога"]
ALERT_END_UA = ["відбій"]

# --- State ---
buffers = {ch: [] for ch in ALL_CONTENT_CHANNELS}
alert_active = False
alert_started_at = None
last_send_time = 0
last_message_time = time.time()
MIN_SEND_INTERVAL = 1.0 if TEST_MODE else 0.2

# Created in main(); one shared connection pool avoids a new TLS handshake per message.
http_client = None
send_lock = None
test_command_lock = None
translation_slots = None
bot_output_message_ids = set()
simulator_processed_message_ids = set()
TEST_SOURCE_PREFIX = "[TEST_SOURCE:monitorwarr]"
TEST_SAMPLE_MESSAGES = [
    "⚠️ Київщина: зафіксовано рух ударних БпЛА Shahed drone у напрямку Києва.",
    "Ракетна небезпека: missile launch activity зафіксована з північного напрямку.",
    "Група БпЛА продовжує рух; air-defense monitoring reports drone activity near Kyiv region.",
]


def contains_any(text, keywords):
    return any(k.lower() in text.lower() for k in keywords)

def is_pure_ad(text):
    if contains_any(text, SECURITY_KEYWORDS):
        return False
    return contains_any(text, AD_INDICATORS)

def clean_text(text):
    text = re.sub(r'^\s*#\w+\s*', '', text)
    text = re.sub(r'\s*#\w+\s*', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def pre_filter(channel, text):
    if channel == KYIV_INFO_CHANNEL:
        return contains_any(text, KYIV_CITY_KEYWORDS)
    elif channel == AMK_CHANNEL:
        if contains_any(text, MIDDLE_EAST_KEYWORDS):
            return False
        return contains_any(text, MILITARY_KEYWORDS)
    elif channel == MONITOR_CHANNEL:
        return contains_any(text, MILITARY_KEYWORDS)
    return True

def is_night():
    h = datetime.now(TZ).hour
    return NIGHT_START <= h < NIGHT_END

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


def ukraine_alarm_air_active(payload):
    """Return the Kyiv City air-alert state from one regional API response."""
    regions = payload if isinstance(payload, list) else [payload]
    if not regions or not isinstance(regions[0], dict):
        raise ValueError("unexpected UkraineAlarm response shape")
    active_alerts = regions[0].get("activeAlerts") or []
    return any(
        isinstance(item, dict) and str(item.get("type", "")).strip().lower() == "air"
        for item in active_alerts
    )


async def ukraine_alarm_loop():
    """Drive production alert mode exclusively from the official UkraineAlarm API."""
    global alert_active, alert_started_at
    initialized = False
    endpoint = f"https://api.ukrainealarm.com/api/v3/alerts/{UKRAINE_ALARM_REGION_ID}"
    while True:
        try:
            response = await http_client.get(
                endpoint,
                headers={"Authorization": UKRAINE_ALARM_API_KEY},
                timeout=httpx.Timeout(10.0, connect=4.0),
            )
            response.raise_for_status()
            current_active = ukraine_alarm_air_active(response.json())

            if not initialized:
                alert_active = current_active
                alert_started_at = time.monotonic() if current_active else None
                initialized = True
                print(
                    f"✅ UkraineAlarm API connected: Kyiv City region={UKRAINE_ALARM_REGION_ID}, "
                    f"air_alert={'ACTIVE' if current_active else 'CLEAR'}, poll={UKRAINE_ALARM_POLL_INTERVAL}s"
                )
            elif current_active != alert_active:
                alert_active = current_active
                if current_active:
                    alert_started_at = time.monotonic()
                    for channel_name in ALL_CONTENT_CHANNELS:
                        buffers[channel_name].clear()
                    await send_to_channel(
                        "🚨 <b>AIR ALERT — KYIV</b>\n\n"
                        f"Official UkraineAlarm API signal.\n\n⚡ REAL-TIME mode — only @{MONITOR_CHANNEL} forwarded."
                    )
                    print("🚨 UkraineAlarm transition: Kyiv City AIR ALERT started")
                else:
                    elapsed = time.monotonic() - alert_started_at if alert_started_at else None
                    duration = f"{int(elapsed // 60)} min" if elapsed is not None else "unknown"
                    alert_started_at = None
                    await send_to_channel(
                        f"✅ <b>ALL CLEAR — KYIV</b>\n\nDuration observed: {duration}\n\n📋 Back to NORMAL mode"
                    )
                    print("✅ UkraineAlarm transition: Kyiv City AIR ALERT ended")
        except Exception as exc:
            # Preserve the last known state: an API outage must never create a false transition.
            print(f"UkraineAlarm API error (state preserved): {type(exc).__name__}: {exc}")

        await asyncio.sleep(UKRAINE_ALARM_POLL_INTERVAL)


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

async def analyze_channel(messages, prompt):
    if not messages:
        return None
    try:
        msgs_text = "\n\n---\n\n".join([f"[{m['time']}] {m['text']}" for m in messages])
        r = await http_client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": MODEL, "max_tokens": 1500, "messages": [{"role": "user", "content": prompt + "\n\nMessages:\n\n" + msgs_text}]},
            timeout=httpx.Timeout(45.0, connect=5.0)
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"Analysis error: {e}")
        return None


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

async def send_to_channel(text):
    result = await send_message(OUTPUT_CHAT_ID, text)
    if TEST_MODE and result and result.get("message_id"):
        bot_output_message_ids.add(result["message_id"])
    return result

async def send_to_owner(text):
    return await send_message(OWNER_CHAT_ID, text)


async def safe_send(text):
    global last_send_time
    async with send_lock:
        now = time.monotonic()
        wait = MIN_SEND_INTERVAL - (now - last_send_time)
        if wait > 0:
            await asyncio.sleep(wait)
        result = await send_to_channel(text)
        last_send_time = time.monotonic()
        return result


async def build_summary(night_recap=False):
    sections = []
    for channel in ALL_CONTENT_CHANNELS:
        msgs = buffers[channel]
        if not msgs:
            continue
        buffers[channel] = []
        result = await analyze_channel(msgs, CHANNEL_PROMPTS[channel])
        if result and "NO_RELEVANT_INFO" not in result:
            sections.append(f"{CHANNEL_ICONS[channel]} <b>{CHANNEL_NAMES[channel]}</b>\n{result}")
    if sections:
        now = datetime.now(TZ).strftime("%H:%M")
        title = "🌙 <b>Overnight Recap" if night_recap else "📋 <b>Hourly Update"
        header = f"{title} — {now} CET</b>\n\n"
        await send_to_channel(header + "\n\n".join(sections))
    elif not night_recap:
        now = datetime.now(TZ).strftime("%H:%M")
        await send_to_channel(f"📋 <b>Hourly Update — {now} CET</b>\n\nNo relevant updates in the last hour.")


async def summary_loop():
    was_night = False
    next_run = time.monotonic() + SUMMARY_INTERVAL
    while True:
        await asyncio.sleep(max(0, next_run - time.monotonic()))
        next_run += SUMMARY_INTERVAL
        if alert_active:
            continue

        night_now = is_night()

        # Just exited the night window -> big recap
        if was_night and not night_now:
            await build_summary(night_recap=True)
            was_night = False
            continue

        was_night = night_now

        # Skip hourly summaries during the night
        if night_now:
            continue

        await build_summary(night_recap=False)


async def health_loop():
    while True:
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)
        silence = time.time() - last_message_time
        if silence > SILENCE_THRESHOLD:
            hours = int(silence // 3600)
            await send_to_owner(f"⚠️ <b>Kyiv Monitor warning</b>\nNo messages received from any channel in ~{hours}h. Connection may be down — check Railway.")



async def handle_alert_message(clean):
    """Deliver low-latency alerts; TEST_MODE uses one Bot API write to avoid group flood limits."""
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
        await edit_message(OUTPUT_CHAT_ID, sent["message_id"], f"🔴 {html.escape(translation)}")
    else:
        await edit_message(
            OUTPUT_CHAT_ID,
            sent["message_id"],
            f"🔴 ⚠️ Translation unavailable\n\n{html.escape(clean[:1000])}",
        )


async def process_test_source_text(clean):
    global last_message_time
    clean = clean_text(clean)
    if not clean:
        return
    last_message_time = time.time()

    if is_pure_ad(clean):
        print(f"[TEST FILTERED AD] {clean[:80]}")
        return

    if alert_active:
        asyncio.create_task(handle_alert_message(clean))
        return

    if not pre_filter(MONITOR_CHANNEL, clean):
        print(f"[TEST FILTERED] @{MONITOR_CHANNEL}: {clean[:60]}")
        return

    time_str = datetime.now(TZ).strftime("%H:%M")
    buffers[MONITOR_CHANNEL].append({"time": time_str, "text": clean[:800]})


async def publish_test_source(client, text):
    sent = await client.send_message(int(TEST_CHAT_ID), f"{TEST_SOURCE_PREFIX}\n{text}")
    simulator_processed_message_ids.add(sent.id)
    await process_test_source_text(text)
    return sent


async def main():
    global http_client, send_lock, test_command_lock, translation_slots
    http_client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=30, max_keepalive_connections=15),
        http2=False,
    )
    send_lock = asyncio.Lock()
    test_command_lock = asyncio.Lock()
    translation_slots = asyncio.Semaphore(8)

    client = TelegramClient(StringSession(TELEGRAM_SESSION), TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await client.start()

    if TEST_MODE:
        print(f"🧪 TEST_MODE enabled. Exclusive chat: {TEST_CHAT_ID}; real Telegram sources are NOT registered.")
        await send_to_channel(
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
                    await send_to_channel("🚨 <b>TEST AIR ALERT — KYIV</b>\n\n⚡ REAL-TIME test mode active")
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
                    await send_to_channel("✅ <b>TEST ALL CLEAR — KYIV</b>\n\n📋 Back to NORMAL test mode")
                return

            if command == "/test_summary":
                async with test_command_lock:
                    if alert_active:
                        await send_to_channel("⚠️ End the test alert with /test_end before requesting a summary.")
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
        for channel_name in ALL_CONTENT_CHANNELS:
            entity = await client.get_entity(channel_name)
            source_entities[channel_name] = entity
            channel_by_chat_id[utils.get_peer_id(entity)] = channel_name

        print(f"✅ Connected in production. Trigger: UkraineAlarm API; Telegram sources: {ALL_CONTENT_CHANNELS}")
        await send_to_channel(
            "🟢 <b>Kyiv Monitor started</b>\n"
            "Mode: NORMAL (hourly summaries)\n"
            "Night pause: 01:00–06:00 CET\n"
            "Alert trigger: official UkraineAlarm API (Kyiv City)\n"
            "Alert mode active 24/7"
        )

        @client.on(events.NewMessage(chats=int(PRODUCTION_CHAT_ID)))
        async def production_command_handler(event):
            global alert_active
            text = (event.message.text or "").strip().lower()
            if text == "/alert":
                alert_active = True
                for channel_name in ALL_CONTENT_CHANNELS:
                    buffers[channel_name].clear()
                await send_to_channel("🚨 <b>MANUAL OVERRIDE</b>\n⚡ Switched to REAL-TIME mode")
            elif text == "/normal":
                alert_active = False
                await send_to_channel("✅ <b>MANUAL OVERRIDE</b>\n📋 Back to NORMAL mode")

        @client.on(events.NewMessage(chats=list(source_entities.values())))
        async def production_source_handler(event):
            global alert_active, last_message_time
            last_message_time = time.time()

            raw_text = event.message.text or ""
            if not raw_text or len(raw_text.strip()) < 5:
                return

            channel = channel_by_chat_id.get(event.chat_id)
            if channel is None:
                print(f"[IGNORED UNKNOWN CHAT] chat_id={event.chat_id}")
                return
            clean = clean_text(raw_text)

            if is_pure_ad(clean):
                print(f"[FILTERED AD] @{channel}: {clean[:80]}")
                return

            if alert_active:
                if channel == MONITOR_CHANNEL:
                    asyncio.create_task(handle_alert_message(clean))
                return

            if not pre_filter(channel, clean):
                print(f"[FILTERED] @{channel}: {clean[:60]}")
                return

            time_str = datetime.now(TZ).strftime("%H:%M")
            channel_buffer = buffers.get(channel)
            if channel_buffer is None:
                print(f"[IGNORED NON-CONTENT CHAT] channel={channel} chat_id={event.chat_id}")
                return
            channel_buffer.append({"time": time_str, "text": clean[:800]})

    asyncio.create_task(summary_loop())
    if not TEST_MODE:
        asyncio.create_task(health_loop())
        asyncio.create_task(ukraine_alarm_loop())

    try:
        await client.run_until_disconnected()
    finally:
        await http_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())

