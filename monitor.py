"""
Kyiv Alert Monitor v2
- Trigger: @kyiv_airraid_alert (reliable English alerts)
- Alert mode: instant translation, clean output
- Normal mode: 2h summaries, Kyiv-filtered for @monitorwarr
- Manual override: /alert and /normal
"""
import asyncio
import os
import re
import httpx
from datetime import datetime
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# --- Credentials ---
TELEGRAM_API_ID = int(os.environ["TELEGRAM_API_ID"])
TELEGRAM_API_HASH = os.environ["TELEGRAM_API_HASH"]
TELEGRAM_SESSION = os.environ["TELEGRAM_SESSION"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
TARGET_CHAT_ID = os.environ["TARGET_CHAT_ID"]

# --- Channels ---
TRIGGER_CHANNEL = "kyiv_airraid_alert"
CONTENT_CHANNELS = [c.strip() for c in os.environ.get("CHANNELS", "kievinfo_kyiv,monitorwarr").split(",")]
ALL_CHANNELS = [TRIGGER_CHANNEL] + CONTENT_CHANNELS

SUMMARY_INTERVAL = int(os.environ.get("SUMMARY_INTERVAL", "7200"))  # 2 hours

# --- Filters ---
SECURITY_KEYWORDS = [
    "тривога", "відбій", "балістика", "ракета", "шахед", "шахеди",
    "бпла", "вибух", "вибухи", "ппо", "повітряна ціль", "укриття",
    "загроза", "обстріл", "приліт", "дрон", "дрони", "mig", "міг",
    "siren", "raid", "missile", "drone", "explosion", "alert", "attack",
    "ballistic", "shahed", "interception", "strike"
]

AD_INDICATORS = [
    "#реклама", "реклама", "знижк", "розпродаж", "промокод",
    "купуй", "придбай", "акція", "магазин", "замовляй", "доставка"
]

KYIV_KEYWORDS = [
    "київ", "kyiv", "киев", "києва", "київський", "київська",
    "kyivskyi", "kyivsky", "kiev", "kyeva", "podil", "obolon",
    "pechersk", "solomyanka", "holosiiv", "dnipro", "darnytsia"
]

# --- State ---
message_buffer = []
alert_active = False


def contains_any(text, keywords):
    return any(k.lower() in text.lower() for k in keywords)


def is_pure_ad(text):
    if contains_any(text, SECURITY_KEYWORDS):
        return False
    return contains_any(text, AD_INDICATORS)


def mentions_kyiv(text):
    return any(k in text.lower() for k in KYIV_KEYWORDS)


def clean_text(text):
    """Remove channel hashtags, promo tags, fix spacing."""
    # Strip leading hashtags like #theywrote
    text = re.sub(r'^\s*#\w+\s*', '', text)
    # Remove any other hashtags mid-text
    text = re.sub(r'\s*#\w+\s*', ' ', text)
    # Collapse excessive newlines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def check_alert_trigger(text):
    """Check @kyiv_airraid_alert messages. Returns (action, extra_info)."""
    global alert_active
    t = text.lower()

    # END: "🟢 Air siren in Kyiv clear 🟢 Duration: 47 minutes"
    if re.search(r'air\s+siren\s+in\s+kyiv\s+clear', t):
        if alert_active:
            alert_active = False
            m = re.search(r'Duration:\s*(.+)', text, re.IGNORECASE)
            duration = m.group(1).strip() if m else "unknown"
            return "end", duration

    # START: "🔴 ATTENTION! 🔴 Air raid sirens in Kyiv!"
    elif re.search(r'air\s+raid\s+sirens?\s+in\s+kyiv', t):
        if not alert_active:
            alert_active = True
            return "start", None

    return None, None


def translate_message(text):
    try:
        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-3-5-haiku-20241022",
                "max_tokens": 1000,
                "messages": [{
                    "role": "user",
                    "content": (
                        "Translate the following Ukrainian/Russian message into clear English. "
                        "Preserve locations, times, quantities and uncertainty. "
                        "Do not add information. "
                        "REMOVE any promotional content, subscribe links, channel ads, LIVE tags. "
                        "Reply ONLY with the cleaned translation:\n\n" + text
                    )
                }]
            },
            timeout=30
        )
        return response.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"Translation error: {e}")
        return None


def summarize_messages(messages):
    if not messages:
        return None
    try:
        messages_text = "\n\n---\n\n".join([f"[{m['time']}] {m['text']}" for m in messages])
        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-3-5-haiku-20241022",
                "max_tokens": 1500,
                "messages": [{
                    "role": "user",
                    "content": (
                        "Summarize in English these messages from Ukrainian military monitoring channels. "
                        "Be concise, report only relevant facts (attacks, alerts, threats, movements). "
                        "Ignore promotional content. "
                        "If nothing relevant, reply only 'No relevant events.':\n\n" + messages_text
                    )
                }]
            },
            timeout=60
        )
        return response.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"Summary error: {e}")
        return None


def send_to_group(text):
    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TARGET_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=30
        )
        if response.status_code != 200:
            print(f"Send error: {response.text}")
    except Exception as e:
        print(f"Telegram send error: {e}")


async def summary_loop():
    global message_buffer
    while True:
        await asyncio.sleep(SUMMARY_INTERVAL)
        if not alert_active and message_buffer:
            msgs = message_buffer.copy()
            message_buffer.clear()
            summary = summarize_messages(msgs)
            if summary and "No relevant events" not in summary:
                now = datetime.now().strftime("%H:%M")
                send_to_group(f"📋 <b>Summary — last 2 hours ({now} UTC)</b>\n\n{summary}")


async def main():
    client = TelegramClient(StringSession(TELEGRAM_SESSION), TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await client.start()

    print(f"✅ Connected. Trigger: @{TRIGGER_CHANNEL} | Sources: {CONTENT_CHANNELS}")
    send_to_group(
        f"🟢 <b>Kyiv Monitor started</b>\n"
        f"Trigger: @{TRIGGER_CHANNEL}\n"
        f"Sources: {', '.join('@' + c for c in CONTENT_CHANNELS)}\n"
        f"Mode: NORMAL (2h summaries)"
    )

    @client.on(events.NewMessage(chats=int(TARGET_CHAT_ID)))
    async def command_handler(event):
        global alert_active
        text = (event.message.text or "").strip().lower()
        if text == "/alert":
            alert_active = True
            send_to_group("🚨 <b>MANUAL OVERRIDE</b>\n⚡ Switched to REAL-TIME mode")
        elif text == "/normal":
            alert_active = False
            send_to_group("✅ <b>MANUAL OVERRIDE</b>\n📋 Back to NORMAL mode (2h summaries)")

    @client.on(events.NewMessage(chats=ALL_CHANNELS))
    async def handler(event):
        global message_buffer
        raw_text = event.message.text or ""
        if not raw_text or len(raw_text.strip()) < 5:
            return

        channel = event.chat.username if event.chat and event.chat.username else "?"
        clean = clean_text(raw_text)

        # ========== TRIGGER CHANNEL ==========
        if channel == TRIGGER_CHANNEL:
            action, extra = check_alert_trigger(clean)
            if action == "start":
                send_to_group(
                    f"🚨 <b>AIR ALERT — KYIV</b>\n\n"
                    f"{clean}\n\n"
                    f"⚡ Switching to REAL-TIME mode"
                )
                return
            elif action == "end":
                send_to_group(
                    f"✅ <b>ALL CLEAR — KYIV</b>\n\n"
                    f"Duration: {extra}\n\n"
                    f"📋 Back to NORMAL mode (2h summaries)"
                )
                return
            # Non-trigger messages from this channel are ignored
            return

        # ========== CONTENT CHANNELS ==========
        # Ad filter
        if is_pure_ad(clean):
            print(f"[FILTERED AD] @{channel}: {clean[:100]}")
            return

        # --- ALERT MODE ---
        if alert_active:
            # monitorwarr filter: skip non-Kyiv noise unless security-related
            if channel == "monitorwarr" and not mentions_kyiv(clean) and not contains_any(clean, SECURITY_KEYWORDS):
                print(f"[FILTERED NON-KYIV] @{channel}: {clean[:80]}")
                return

            translation = translate_message(clean[:1500])
            if translation:
                # CLEAN OUTPUT: just the text, no header, no timestamp, no @channel
                send_to_group(translation)
            return

        # --- NORMAL MODE ---
        # monitorwarr: only Kyiv-related
        if channel == "monitorwarr" and not mentions_kyiv(clean):
            print(f"[FILTERED NON-KYIV] @{channel}: {clean[:80]}")
            return

        time_str = datetime.now().strftime("%H:%M")
        message_buffer.append({"time": time_str, "text": clean[:500]})

    asyncio.create_task(summary_loop())
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
