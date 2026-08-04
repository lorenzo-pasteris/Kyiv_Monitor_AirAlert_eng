"""
Kyiv Alert Monitor
- Trigger-based: ТРИВОГА starts alert mode, ВІДБІЙ ends it
- Alert mode: every message translated to English and sent immediately
- Normal mode: messages buffered, summary every 2 hours
- Ad filter: pure ads removed, promo tails cleaned, security messages always pass
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

CHANNELS = [c.strip() for c in os.environ.get("CHANNELS", "kyiv_alerts,monitorwarr,kievinfo_kyiv").split(",")]

SUMMARY_INTERVAL = int(os.environ.get("SUMMARY_INTERVAL", "7200"))  # 2 hours

# --- Trigger words ---
ALERT_START_TRIGGERS = ["ТРИВОГА", "тривога"]
ALERT_END_TRIGGERS = ["ВІДБІЙ", "відбій"]

# --- Security keywords: messages containing these ALWAYS pass the ad filter ---
SECURITY_KEYWORDS = [
    "тривога", "відбій", "балістика", "ракета", "шахед", "шахеди",
    "бпла", "вибух", "вибухи", "ппо", "повітряна ціль", "укриття",
    "загроза", "обстріл", "приліт", "дрон", "дрони", "mig", "міг"
]

# --- Ad indicators: messages with these (and no security keywords) are dropped ---
AD_INDICATORS = [
    "#реклама", "реклама", "знижк", "розпродаж", "промокод",
    "купуй", "придбай", "акція", "магазин", "замовляй", "доставка"
]

# --- State ---
message_buffer = []
alert_active = False


def contains_any(text, keywords):
    text_lower = text.lower()
    return any(k.lower() in text_lower for k in keywords)


def is_pure_ad(text):
    """True if message is advertising and contains no security content."""
    if contains_any(text, SECURITY_KEYWORDS):
        return False
    return contains_any(text, AD_INDICATORS)


def check_triggers(text):
    """Update alert state based on trigger words. Returns 'start', 'end' or None."""
    global alert_active
    if contains_any(text, ALERT_END_TRIGGERS):
        if alert_active:
            alert_active = False
            return "end"
    elif contains_any(text, ALERT_START_TRIGGERS):
        if not alert_active:
            alert_active = True
            return "start"
    return None


def translate_message(text):
    """Translate to English and remove promo tails via Claude Haiku."""
    try:
        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-haiku-4-5",
                "max_tokens": 1000,
                "messages": [{
                    "role": "user",
                    "content": (
                        "Translate the following Ukrainian message into clear and concise English. "
                        "Preserve locations, times, quantities and uncertainty. "
                        "Do not add information. "
                        "REMOVE any promotional content (subscribe links, 'send news to bot', channel ads, LIVE tags). "
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
    """Summarize buffered messages in English."""
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
                "model": "claude-haiku-4-5",
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

    print(f"✅ Connected. Monitoring: {CHANNELS}")
    send_to_group(f"🟢 <b>Kyiv Monitor started</b>\nChannels: {', '.join('@' + c for c in CHANNELS)}\nMode: NORMAL (2h summaries)")

    @client.on(events.NewMessage(chats=CHANNELS))
    async def handler(event):
        global message_buffer
        text = event.message.text
        if not text or len(text.strip()) < 10:
            return

        time_str = datetime.now().strftime("%H:%M")
        channel = event.chat.username if event.chat and event.chat.username else "?"

        # --- Check alert triggers first ---
        trigger = check_triggers(text)

        if trigger == "start":
            translation = translate_message(text[:1500])
            send_to_group(
                f"🚨 <b>AIR ALERT — KYIV</b>\n\n"
                f"{translation or text}\n\n"
                f"⚡ Switching to REAL-TIME mode"
            )
            return

        if trigger == "end":
            translation = translate_message(text[:1500])
            send_to_group(
                f"✅ <b>ALERT OVER — KYIV</b>\n\n"
                f"{translation or text}\n\n"
                f"📋 Back to NORMAL mode (2h summaries)"
            )
            return

        # --- Ad filter ---
        if is_pure_ad(text):
            print(f"[FILTERED AD] @{channel}: {text[:100]}")
            return

        # --- Route by mode ---
        if alert_active:
            translation = translate_message(text[:1500])
            if translation:
                send_to_group(
                    f"🔔 <b>[{time_str}] @{channel}</b>\n\n"
                    f"{translation}"
                )
        else:
            message_buffer.append({"time": time_str, "text": text[:500]})

    asyncio.create_task(summary_loop())
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
