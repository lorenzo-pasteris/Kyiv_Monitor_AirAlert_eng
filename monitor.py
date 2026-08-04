"""
Kyiv Alert Monitor v5 — low-latency async pipeline
- Trigger: @kyiv_airraid_alert (English) + Ukrainian ТРИВОГА/ВІДБІЙ as backup
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
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# --- Credentials ---
TELEGRAM_API_ID = int(os.environ["TELEGRAM_API_ID"])
TELEGRAM_API_HASH = os.environ["TELEGRAM_API_HASH"]
TELEGRAM_SESSION = os.environ["TELEGRAM_SESSION"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
TARGET_CHAT_ID = os.environ["TARGET_CHAT_ID"]
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "392256147")  # private warnings go here

# --- Channels ---
TRIGGER_CHANNEL = "kyiv_airraid_alert"
KYIV_INFO_CHANNEL = "kievinfo_kyiv"
AMK_CHANNEL = "AMK_Mapping"
MONITOR_CHANNEL = "monitorwarr"

ALL_CONTENT_CHANNELS = [KYIV_INFO_CHANNEL, AMK_CHANNEL, MONITOR_CHANNEL]
ALL_CHANNELS = [TRIGGER_CHANNEL] + ALL_CONTENT_CHANNELS

SUMMARY_INTERVAL = 3600  # 1 hour
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
last_send_time = 0
last_message_time = time.time()
MIN_SEND_INTERVAL = 0.2

# Created in main(); one shared connection pool avoids a new TLS handshake per message.
http_client = None
send_lock = None
translation_slots = None


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
    """English trigger from @kyiv_airraid_alert."""
    global alert_active
    t = text.lower()
    if re.search(r'air\s+siren\s+in\s+kyiv\s+clear', t):
        if alert_active:
            alert_active = False
            m = re.search(r'Duration:\s*(.+)', text, re.IGNORECASE)
            return "end", (m.group(1).strip() if m else "unknown")
    elif re.search(r'air\s+raid\s+sirens?\s+in\s+kyiv', t):
        if not alert_active:
            alert_active = True
            return "start", None
    return None, None

def check_ua_trigger(text):
    """Backup Ukrainian triggers from any channel."""
    global alert_active
    if contains_any(text, ALERT_END_UA):
        if alert_active:
            alert_active = False
            return "end"
    elif contains_any(text, ALERT_START_UA):
        if not alert_active:
            alert_active = True
            return "start"
    return None


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
    """Telegram Bot API request with one retry for flood control."""
    try:
        r = await http_client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            json=payload,
            timeout=httpx.Timeout(10.0, connect=3.0),
        )
        data = r.json()
        if r.status_code == 429:
            retry_after = min(data.get("parameters", {}).get("retry_after", 1), 5)
            await asyncio.sleep(retry_after)
            r = await http_client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                json=payload,
                timeout=httpx.Timeout(10.0, connect=3.0),
            )
            data = r.json()
        if not data.get("ok"):
            print(f"Telegram {method} error: {data}")
            return None
        return data.get("result")
    except Exception as e:
        print(f"Telegram {method} error: {e}")
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
    return await send_message(TARGET_CHAT_ID, text)

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


async def summary_loop():
    was_night = False
    while True:
        await asyncio.sleep(SUMMARY_INTERVAL)
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
    """Publish instantly, then replace the same post with its translation."""
    original = html.escape(clean[:1500])
    sent = await safe_send(f"🔴 <b>Incoming alert — translating…</b>\n\n{original}")
    if not sent:
        return

    translation = await translate_message(clean[:1500])
    if translation:
        await edit_message(TARGET_CHAT_ID, sent["message_id"], f"🔴 {html.escape(translation)}")
    else:
        await edit_message(
            TARGET_CHAT_ID,
            sent["message_id"],
            f"🔴 ⚠️ Translation unavailable\n\n{html.escape(clean[:1000])}",
        )


async def main():
    global http_client, send_lock, translation_slots
    http_client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=30, max_keepalive_connections=15),
        http2=False,
    )
    send_lock = asyncio.Lock()
    translation_slots = asyncio.Semaphore(8)

    client = TelegramClient(StringSession(TELEGRAM_SESSION), TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await client.start()

    print(f"✅ Connected. Trigger: @{TRIGGER_CHANNEL}, sources: {ALL_CONTENT_CHANNELS}")
    await send_to_channel(
        f"🟢 <b>Kyiv Monitor started</b>\n"
        f"Mode: NORMAL (hourly summaries)\n"
        f"Night pause: 01:00–06:00 CET\n"
        f"Alert mode active 24/7"
    )

    @client.on(events.NewMessage(chats=int(TARGET_CHAT_ID)))
    async def command_handler(event):
        global alert_active
        text = (event.message.text or "").strip().lower()
        if text == "/alert":
            alert_active = True
            for c in ALL_CONTENT_CHANNELS:
                buffers[c].clear()
            await send_to_channel("🚨 <b>MANUAL OVERRIDE</b>\n⚡ Switched to REAL-TIME mode")
        elif text == "/normal":
            alert_active = False
            await send_to_channel("✅ <b>MANUAL OVERRIDE</b>\n📋 Back to NORMAL mode")

    @client.on(events.NewMessage(chats=ALL_CHANNELS))
    async def handler(event):
        global alert_active, last_message_time
        last_message_time = time.time()

        raw_text = event.message.text or ""
        if not raw_text or len(raw_text.strip()) < 5:
            return

        channel = event.chat.username if event.chat and event.chat.username else "?"
        clean = clean_text(raw_text)

        # ===== TRIGGER CHANNEL (English) =====
        if channel == TRIGGER_CHANNEL:
            action, extra = check_alert_trigger(clean)
            if action == "start":
                for c in ALL_CONTENT_CHANNELS:
                    buffers[c].clear()
                await send_to_channel(f"🚨 <b>AIR ALERT — KYIV</b>\n\n{html.escape(clean)}\n\n⚡ REAL-TIME mode — only @{MONITOR_CHANNEL} forwarded.")
                return
            elif action == "end":
                await send_to_channel(f"✅ <b>ALL CLEAR — KYIV</b>\n\nDuration: {html.escape(str(extra))}\n\n📋 Back to NORMAL mode")
                return
            return

        # ===== Ukrainian backup triggers (any content channel) =====
        ua = check_ua_trigger(clean)
        if ua == "start":
            for c in ALL_CONTENT_CHANNELS:
                buffers[c].clear()
            await send_to_channel(f"🚨 <b>AIR ALERT — KYIV</b>\n\n⚡ REAL-TIME mode — only @{MONITOR_CHANNEL} forwarded.")
            # fall through: this same message may also be content
        elif ua == "end":
            await send_to_channel("✅ <b>ALL CLEAR — KYIV</b>\n\n📋 Back to NORMAL mode")
            return

        # ===== AD FILTER =====
        if is_pure_ad(clean):
            print(f"[FILTERED AD] @{channel}: {clean[:80]}")
            return

        # ===== ALERT MODE =====
        if alert_active:
            if channel != MONITOR_CHANNEL:
                return
            # Translate + send in parallel so a burst of messages doesn't queue up.
            asyncio.create_task(handle_alert_message(clean))
            return

        # ===== NORMAL MODE =====
        if not pre_filter(channel, clean):
            print(f"[FILTERED] @{channel}: {clean[:60]}")
            return

        time_str = datetime.now(TZ).strftime("%H:%M")
        buffers[channel].append({"time": time_str, "text": clean[:800]})

    asyncio.create_task(summary_loop())
    asyncio.create_task(health_loop())
    try:
        await client.run_until_disconnected()
    finally:
        await http_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
