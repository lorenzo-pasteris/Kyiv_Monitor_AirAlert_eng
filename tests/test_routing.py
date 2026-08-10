import importlib.util
import os
import random
import sys
import types
import unittest

from pathlib import Path


ALERT_CHAT_ID = "-1001111111111"
SUMMARY_CHAT_ID = "-1002222222222"
SUMMARY_CHAT_LINK = "https://t.me/+random-test-link"


def load_monitor():
    os.environ.update({
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH": "test-hash",
        "TELEGRAM_SESSION": "test-session",
        "ANTHROPIC_API_KEY": "test-key",
        "BOT_TOKEN": "123456:test-token",
        "TARGET_CHAT_ID": ALERT_CHAT_ID,
        "SUMMARY_CHAT_ID": SUMMARY_CHAT_ID,
        "SUMMARY_CHAT_LINK": SUMMARY_CHAT_LINK,
        "TEST_MODE": "false",
    })

    httpx_stub = types.ModuleType("httpx")
    telethon_stub = types.ModuleType("telethon")
    telethon_sessions_stub = types.ModuleType("telethon.sessions")
    telethon_stub.TelegramClient = object
    telethon_stub.events = types.SimpleNamespace()
    telethon_stub.utils = types.SimpleNamespace()
    telethon_sessions_stub.StringSession = object
    sys.modules.setdefault("httpx", httpx_stub)
    sys.modules.setdefault("telethon", telethon_stub)
    sys.modules.setdefault("telethon.sessions", telethon_sessions_stub)

    path = Path(__file__).parents[1] / "monitor.py"
    spec = importlib.util.spec_from_file_location("monitor_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


monitor = load_monitor()


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent = []
        monitor.recent_alert_messages.clear()

        async def fake_send_message(chat_id, text):
            self.sent.append((chat_id, text))
            return {"message_id": len(self.sent)}

        monitor.send_message = fake_send_message

    async def test_random_outputs_never_cross_destinations(self):
        random.seed(4485942761)
        expected = []
        for index in range(500):
            route = random.choice(("alert", "summary"))
            text = f"random-message-{index}-{random.getrandbits(64)}"
            if route == "alert":
                await monitor.send_to_alert_channel(text)
                expected.append((ALERT_CHAT_ID, text))
            else:
                await monitor.send_to_summary_group(text)
                expected.append((SUMMARY_CHAT_ID, text))

        self.assertEqual(self.sent, expected)
        self.assertEqual({chat_id for chat_id, _ in self.sent}, {ALERT_CHAT_ID, SUMMARY_CHAT_ID})

    async def test_alert_lifecycle_uses_minimal_public_copy(self):
        monitor.telegram_alert_state = True
        monitor.alert_active = False
        for channel in monitor.ALL_CONTENT_CHANNELS:
            monitor.buffers[channel].append({"time": "12:00", "text": "discard me"})

        await monitor.reconcile_alert_state("random-test")
        self.assertEqual(self.sent[-1], (ALERT_CHAT_ID, "🚨 <b>AIR ALERT — KYIV</b>"))
        self.assertNotIn("REAL-TIME mode", self.sent[-1][1])
        self.assertTrue(all(not items for items in monitor.buffers.values()))

        monitor.telegram_alert_state = False
        await monitor.reconcile_alert_state("random-test")
        clear_text = self.sent[-1][1]
        self.assertEqual(self.sent[-1][0], ALERT_CHAT_ID)
        self.assertIn("✅ <b>ALL CLEAR — KYIV</b>", clear_text)
        self.assertIn(f'<a href="{SUMMARY_CHAT_LINK}">Join Kyiv News →</a>', clear_text)
        self.assertNotIn("Back to NORMAL mode", clear_text)

    async def test_fundraising_and_engagement_posts_are_rejected(self):
        donation = (
            "Через постійну загрозу балістики ці дні проводжу без сну. "
            "При бажані можете підтримати кавою. Моно: 4441111045774118"
        )
        thanks = (
            "Ми щиро вдячні кожному вашому донату та сподіваємося на вашу "
            "підтримку за нашу роботу."
        )
        operational = "⚠️ 2 шахеди рухаються через Бровари у напрямку Києва."
        unrelated = "Оновлений графік відключень електроенергії на завтра."
        terse_follow_up = "Київщина чисто."

        self.assertTrue(monitor.is_non_operational_alert_message(donation))
        self.assertTrue(monitor.is_non_operational_alert_message(thanks))
        self.assertFalse(monitor.is_non_operational_alert_message(operational))
        self.assertFalse(monitor.is_pure_ad(donation))  # security words no longer bypass the ALERT-only filter
        self.assertFalse(monitor.is_actionable_alert_message(unrelated))
        self.assertTrue(monitor.is_actionable_alert_message(terse_follow_up))

    async def test_second_alert_feed_is_alert_only(self):
        self.assertIn("Nashee_PPO", monitor.ALERT_FEED_CHANNELS)
        self.assertIn("nebo_raketa", monitor.ALERT_FEED_CHANNELS)
        self.assertNotIn("nebo_raketa", monitor.ALL_CONTENT_CHANNELS)

    async def test_cross_source_dedup_keeps_first_and_new_information(self):
        first = "⚠️ 2 шахеди рухаються через Бровари у напрямку Києва"
        duplicate = "⚠️ 2 шахеди рухаються через Бровари у напрямку Києва!"
        update = "⚠️ 3 шахеди рухаються через Васильків у напрямку Києва"

        self.assertTrue(monitor.should_publish_alert(first, "Nashee_PPO", now=100.0))
        self.assertFalse(monitor.should_publish_alert(duplicate, "nebo_raketa", now=130.0))
        self.assertTrue(monitor.should_publish_alert(update, "nebo_raketa", now=140.0))

    async def test_duplicate_window_expires(self):
        message = "Балістична ціль рухається у напрямку Києва"
        self.assertTrue(monitor.should_publish_alert(message, "Nashee_PPO", now=100.0))
        self.assertTrue(monitor.should_publish_alert(message, "nebo_raketa", now=281.0))


if __name__ == "__main__":
    unittest.main()
