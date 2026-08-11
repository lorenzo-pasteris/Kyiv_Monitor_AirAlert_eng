import asyncio
import importlib.util
import os
import random
import sqlite3
import sys
import tempfile
import types
import unittest

from datetime import datetime, timedelta, timezone
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


class FakeTelegramMessage:
    def __init__(self, message_id, text, date):
        self.id = message_id
        self.text = text
        self.date = date


class FakeHistoryClient:
    def __init__(self, messages_by_channel):
        self.messages_by_channel = messages_by_channel

    async def get_messages(self, entity, limit):
        return list(reversed(self.messages_by_channel.get(entity, [])))[0:limit]

    def iter_messages(self, entity, min_id, reverse=True):
        async def generate():
            messages = [
                message for message in self.messages_by_channel.get(entity, [])
                if message.id > min_id
            ]
            for message in messages if reverse else reversed(messages):
                yield message
        return generate()


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = monitor.CATEGORY_STATS_DB_PATH
        self.original_stats_ready = monitor.stats_db_ready
        self.original_client = monitor.production_client
        self.original_entities = monitor.content_source_entities
        self.original_analyzer = monitor.analyze_hourly_matrix
        self.original_summary_sender = monitor.send_to_summary_group
        self.original_owner_sender = monitor.send_to_owner
        monitor.CATEGORY_STATS_DB_PATH = str(Path(self.temp_dir.name) / "monitor.sqlite3")
        monitor.stats_db_ready = monitor.initialize_stats_db()
        monitor.production_client = None
        monitor.content_source_entities = {}
        monitor.summary_lock = asyncio.Lock()
        monitor.summary_watchdog_attempts.clear()

    async def asyncTearDown(self):
        monitor.CATEGORY_STATS_DB_PATH = self.original_db_path
        monitor.stats_db_ready = self.original_stats_ready
        monitor.production_client = self.original_client
        monitor.content_source_entities = self.original_entities
        monitor.analyze_hourly_matrix = self.original_analyzer
        monitor.send_to_summary_group = self.original_summary_sender
        monitor.send_to_owner = self.original_owner_sender
        self.temp_dir.cleanup()

    async def test_live_and_history_ingestion_are_idempotent_and_cursor_based(self):
        now = datetime.now(timezone.utc)
        messages = [
            FakeTelegramMessage(101, "Kyiv metro service changed this morning.", now - timedelta(minutes=30)),
            FakeTelegramMessage(102, "Ukraine approved a new national measure.", now - timedelta(minutes=10)),
        ]
        entities = {channel: channel for channel in monitor.ALL_CONTENT_CHANNELS}
        client = FakeHistoryClient({channel: [] for channel in monitor.ALL_CONTENT_CHANNELS})
        client.messages_by_channel[monitor.KYIV_INFO_CHANNEL] = messages

        self.assertTrue(monitor.persist_normal_message(
            monitor.KYIV_INFO_CHANNEL, 102, messages[1].date, messages[1].text
        ))
        self.assertTrue(await monitor.sync_normal_history(client, entities))

        pending = monitor.load_pending_normal_messages()
        self.assertEqual([row["message_id"] for row in pending], [101, 102])
        self.assertEqual(monitor.get_source_cursor(monitor.KYIV_INFO_CHANNEL), 102)

        messages.append(FakeTelegramMessage(
            103, "A new road closure was announced in Kyiv.", now
        ))
        self.assertTrue(await monitor.sync_normal_history(client, entities))
        self.assertEqual(
            [row["message_id"] for row in monitor.load_pending_normal_messages()],
            [101, 102, 103],
        )
        self.assertEqual(monitor.get_source_cursor(monitor.KYIV_INFO_CHANNEL), 103)

    async def test_failed_delivery_retains_pending_and_success_marks_processed(self):
        channel = monitor.KYIV_INFO_CHANNEL
        message_id = 501
        monitor.persist_normal_message(
            channel,
            message_id,
            datetime.now(timezone.utc),
            "Kyiv metro announced a concrete service disruption.",
        )

        async def fake_analyzer(messages):
            result = {
                key: {"selected_ids": [], "bullets": []}
                for key in monitor.CATEGORIES
            }
            stable_id = f"{channel}:{message_id}"
            result["kyiv_city"] = {
                "selected_ids": [stable_id],
                "bullets": ["Kyiv metro announced a service disruption."],
            }
            return result

        delivery_results = [None, {"message_id": 9001}]

        async def fake_summary_sender(text):
            return delivery_results.pop(0)

        async def fake_owner_sender(text):
            return {"message_id": 1}

        monitor.analyze_hourly_matrix = fake_analyzer
        monitor.send_to_summary_group = fake_summary_sender
        monitor.send_to_owner = fake_owner_sender

        self.assertFalse(await monitor.build_summary(trigger="failure-test"))
        self.assertEqual(len(monitor.load_pending_normal_messages()), 1)

        self.assertTrue(await monitor.build_summary(trigger="success-test"))
        self.assertEqual(monitor.load_pending_normal_messages(), [])
        with sqlite3.connect(monitor.CATEGORY_STATS_DB_PATH) as connection:
            status = connection.execute(
                "SELECT status FROM normal_messages WHERE channel = ? AND message_id = ?",
                (channel, message_id),
            ).fetchone()[0]
        self.assertEqual(status, "processed")

    async def test_alert_discards_pending_and_clear_advances_all_cursors(self):
        now = datetime.now(timezone.utc)
        monitor.persist_normal_message(
            monitor.KYIV_INFO_CHANNEL,
            700,
            now,
            "A pending NORMAL message that must not cross into ALERT.",
        )
        monitor.discard_pending_normal_messages("unit-test-alert")
        self.assertEqual(monitor.load_pending_normal_messages(), [])

        entities = {channel: channel for channel in monitor.ALL_CONTENT_CHANNELS}
        messages_by_channel = {
            channel: [FakeTelegramMessage(800 + index, "latest source message", now)]
            for index, channel in enumerate(monitor.ALL_CONTENT_CHANNELS)
        }
        client = FakeHistoryClient(messages_by_channel)
        await monitor.advance_normal_cursors_to_latest(client, entities, "unit-test-clear")

        self.assertEqual(
            [monitor.get_source_cursor(channel) for channel in monitor.ALL_CONTENT_CHANNELS],
            [800, 801, 802],
        )


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

    async def test_kyiv_alerts_is_the_only_alert_feed(self):
        self.assertEqual(monitor.ALERT_FEED_CHANNELS, ["kyiv_alerts"])
        self.assertNotIn("kyiv_alerts", monitor.ALL_CONTENT_CHANNELS)
        self.assertNotIn("nebo_raketa", monitor.ALERT_FEED_CHANNELS)
        self.assertNotIn("Nashee_PPO", monitor.ALL_CONTENT_CHANNELS)

    async def test_kyiv_alerts_real_message_shapes_are_actionable(self):
        alert = "тривога на сході області на мопеди. Поки без загроз."
        clear = "Відбій балістичної загрози по всій Україні."

        self.assertTrue(monitor.is_actionable_alert_message(alert))
        self.assertTrue(monitor.is_actionable_alert_message(clear))
        self.assertFalse(monitor.is_non_operational_alert_message(alert))
        self.assertFalse(monitor.is_non_operational_alert_message(clear))

    async def test_alert_dedup_keeps_first_and_new_information(self):
        first = "⚠️ 2 шахеди рухаються через Бровари у напрямку Києва"
        duplicate = "⚠️ 2 шахеди рухаються через Бровари у напрямку Києва!"
        update = "⚠️ 3 шахеди рухаються через Васильків у напрямку Києва"

        self.assertTrue(monitor.should_publish_alert(first, "kyiv_alerts", now=100.0))
        self.assertFalse(monitor.should_publish_alert(duplicate, "kyiv_alerts", now=130.0))
        self.assertTrue(monitor.should_publish_alert(update, "kyiv_alerts", now=140.0))

    async def test_duplicate_window_expires(self):
        message = "Балістична ціль рухається у напрямку Києва"
        self.assertTrue(monitor.should_publish_alert(message, "kyiv_alerts", now=100.0))
        self.assertTrue(monitor.should_publish_alert(message, "kyiv_alerts", now=281.0))


if __name__ == "__main__":
    unittest.main()
