"""One-shot isolated replay of the 22 August alert feed; never allowed in production."""

import asyncio
import html

import httpx

import monitor


async def main():
    if not monitor.TEST_MODE or not monitor.TEST_CHAT_ID:
        raise RuntimeError("Replay refused: TEST_MODE=true and TEST_CHAT_ID are required")

    monitor.http_client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5)
    )
    monitor.send_lock = asyncio.Lock()
    monitor.translation_slots = asyncio.Semaphore(1)
    monitor.alert_active = True
    try:
        await monitor.send_to_alert_channel(
            f"🧪 <b>Translation replay — 22 August</b>\n"
            f"{len(monitor.TEST_ALERT_REPLAY_20260822)} real source messages; production disabled."
        )
        for index, source in enumerate(monitor.TEST_ALERT_REPLAY_20260822, 1):
            await monitor.send_to_alert_channel(
                f"<b>SOURCE {index}/{len(monitor.TEST_ALERT_REPLAY_20260822)}</b>\n"
                f"{html.escape(source)}"
            )
            await monitor.handle_alert_message(source, source="replay-20260822")
        await monitor.send_to_alert_channel("🧪 <b>Replay complete</b>")
    finally:
        await monitor.http_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
