"""Offline regressions: no Telegram sessions or paid model calls."""
import ast
import asyncio
import json
import sqlite3
import tempfile
import time
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_routing import FakeTelegramMessage, monitor


class BackupAlertTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        values = {
            "alert_active": True,
            "alert_level": "YELLOW",
            "alert_publication_lock": asyncio.Lock(),
            "translate_message": AsyncMock(return_value="A drone is heading towards Kyiv"),
            "safe_send": AsyncMock(return_value=True),
            "send_to_owner": AsyncMock(return_value=True),
        }
        for name, value in values.items():
            self.patch(monitor, name, value)
        self.patch(monitor.state_store, "CATEGORY_STATS_DB_PATH", str(Path(self.directory.name) / "test.sqlite"))
        self.patch(monitor.state_store, "stats_db_ready", True)
        monitor.state_store.initialize_stats_db()
        monitor.recent_alert_messages.clear()
        monitor.recent_alert_source_context.clear()
        self.primary = monitor.ALERT_FEED_CHANNEL
        self.backup = monitor.KYIV_INFO_CHANNEL
        self.text = "2 дрони рухаються через Бровари у напрямку Києва"

    def patch(self, obj, name, value):
        p = patch.object(obj, name, value)
        p.start()
        self.addCleanup(p.stop)

    async def test_primary_then_backup_duplicate_skips_second_model_call(self):
        await monitor.handle_alert_message(self.text, self.primary)
        await monitor.handle_alert_message("Увага! " + self.text + "!", self.backup)
        self.assertEqual(monitor.safe_send.await_count, 1)
        self.assertEqual(monitor.translate_message.await_count, 1)

    async def test_backup_first_then_primary_duplicate(self):
        await monitor.handle_alert_message(self.text, self.backup)
        await monitor.handle_alert_message(self.text, self.primary)
        self.assertEqual(monitor.safe_send.await_count, 1)

    async def test_compare_all_recent_publications_not_only_last(self):
        await monitor.handle_alert_message(self.text, self.primary)
        await monitor.handle_alert_message("Ракети рухаються через Суми до Києва", self.primary)
        await monitor.handle_alert_message(self.text, self.backup)
        self.assertEqual(monitor.safe_send.await_count, 2)

    async def test_updates_preserve_quantity_location_direction_negation_uncertainty(self):
        await monitor.handle_alert_message(self.text, self.primary)
        updates = [
            self.text.replace("2", "3"),
            self.text.replace("Бровари", "Васильків"),
            "2 дрони рухаються через Київ у напрямку Броварів",
            self.text.replace("рухаються", "не рухаються"),
            "Ймовірно " + self.text,
            self.text + ", один збили",
            self.text + ", вибух",
        ]
        for update in updates:
            await monitor.handle_alert_message(update, self.backup)
        self.assertEqual(monitor.safe_send.await_count, 1 + len(updates))

    async def test_ambiguous_fragments_and_another_target_are_not_cross_deduplicated(self):
        for text in ("Жуляни", "Ще один дрон рухається до Києва", "Схоже другий раз вже не попали"):
            await monitor.handle_alert_message(text, self.primary)
            await monitor.handle_alert_message(text, self.backup)
        self.assertEqual(monitor.safe_send.await_count, 6)

    async def test_failed_send_does_not_suppress_backup(self):
        monitor.safe_send.side_effect = [False, True]
        self.assertFalse(await monitor.handle_alert_message(self.text, self.primary))
        self.assertFalse(monitor.recent_alert_messages)
        self.assertTrue(await monitor.handle_alert_message(self.text, self.backup))
        self.assertEqual(monitor.safe_send.await_count, 2)

    async def test_semantic_drop_is_not_a_publication(self):
        monitor.translate_message.side_effect = [False, "A drone is approaching Kyiv"]
        await monitor.handle_alert_message(self.text, self.primary)
        self.assertFalse(monitor.recent_alert_messages)
        await monitor.handle_alert_message(self.text, self.backup)
        self.assertEqual(monitor.safe_send.await_count, 1)

    async def test_translation_failure_is_not_a_publication(self):
        monitor.translate_message.side_effect = [None, "A drone is approaching Kyiv"]
        await monitor.handle_alert_message(self.text, self.primary)
        self.assertFalse(monitor.recent_alert_messages)
        await monitor.handle_alert_message(self.text, self.backup)
        self.assertEqual(monitor.safe_send.await_count, 1)

    async def test_concurrent_sources_publish_once(self):
        async def translate(text, context=()):
            await asyncio.sleep(0)
            return "Drones heading towards Kyiv"

        async def send(text):
            await asyncio.sleep(0)
            return True

        monitor.translate_message.side_effect = translate
        monitor.safe_send.side_effect = send
        await asyncio.gather(*[
            monitor.handle_alert_message(self.text, source)
            for source in (self.primary, self.backup) * 10
        ])
        self.assertEqual(monitor.safe_send.await_count, 1)

    async def test_slow_backup_classification_does_not_delay_primary(self):
        gate = asyncio.Event()

        async def translate(text, context=()):
            if text.startswith("3"):
                await gate.wait()
            return text

        monitor.translate_message.side_effect = translate
        backup = asyncio.create_task(monitor.handle_alert_message(self.text.replace("2", "3"), self.backup))
        try:
            await asyncio.sleep(0)
            await asyncio.wait_for(monitor.handle_alert_message(self.text, self.primary), 1)
            self.assertFalse(backup.done())
            self.assertEqual(monitor.safe_send.await_count, 1)
        finally:
            gate.set()
            await backup

    async def test_confirmed_history_survives_restart_and_expires(self):
        await monitor.handle_alert_message(self.text, self.primary)
        monitor.recent_alert_messages.clear()
        monitor.restore_published_alerts()
        self.assertFalse(monitor.should_publish_alert(self.text, self.backup))
        self.assertTrue(monitor.should_publish_alert(self.text, self.backup, now=time.time() + 181))

    async def test_invalid_or_future_history_fails_open(self):
        for raw in ("broken", "null", '["invalid"]', json.dumps([[time.time() + 500, self.primary, self.text]])):
            monitor.state_store.persist_operational_state(monitor.ALERT_PUBLICATION_HISTORY_KEY, raw)
            monitor.restore_published_alerts()
            self.assertTrue(monitor.should_publish_alert(self.text, self.backup))

    async def test_backup_uses_existing_ad_commentary_and_semantic_filters(self):
        for index, text in enumerate((
            "Обов'язково підписуйтесь на наш ракетний канал сьогодні!!",
            "Наче другий залп пережили, ви як там?",
        ), 1):
            message = FakeTelegramMessage(index, text, datetime.now(timezone.utc))
            await monitor.process_alert_feed_message(message, self.backup)
        self.assertEqual(monitor.safe_send.await_count, 0)
        self.assertFalse(monitor.recent_alert_messages)

    async def test_backup_uses_image_filter(self):
        self.patch(monitor, "is_blocked_alert_image", AsyncMock(return_value=True))
        message = FakeTelegramMessage(1, self.text, datetime.now(timezone.utc), photo=object())
        await monitor.process_alert_feed_message(message, self.backup)
        await asyncio.gather(*list(monitor.alert_delivery_tasks))
        monitor.is_blocked_alert_image.assert_awaited_once()
        monitor.safe_send.assert_not_awaited()

    async def test_primary_read_failure_does_not_disable_backup_recovery(self):
        client = AsyncMock()
        message = FakeTelegramMessage(1, self.text, datetime.now(timezone.utc))
        client.get_messages.side_effect = [RuntimeError("primary unavailable"), [message]]
        entities = {channel: channel for channel in monitor.ALERT_FEED_CHANNELS}
        self.assertEqual(await monitor.backfill_alert_feed(client, entities), 1)
        monitor.safe_send.assert_awaited_once()

    async def test_backup_is_not_processed_outside_alert(self):
        monitor.alert_active = False
        message = FakeTelegramMessage(1, self.text, datetime.now(timezone.utc))
        self.assertFalse(await monitor.process_alert_feed_message(message, self.backup))
        monitor.translate_message.assert_not_awaited()

    async def test_actual_live_handler_keeps_backup_news_in_both_modes(self):
        # Exercise the actual nested callback without starting main/Telethon.
        tree = ast.parse(Path(monitor.__file__).read_text())
        callback = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)
                        and node.name == "production_source_handler")
        callback.decorator_list = []
        code = compile(ast.Module(body=[callback], type_ignores=[]), monitor.__file__, "exec")
        for active in (True, False):
            monitor.alert_active = active
            namespace = dict(vars(monitor), channel_by_chat_id={42: self.backup})
            exec(code, namespace)
            message = FakeTelegramMessage(100 + int(active), self.text, datetime.now(timezone.utc))
            event = types.SimpleNamespace(chat_id=42, message=message)
            await namespace["production_source_handler"](event)
        self.assertEqual(monitor.safe_send.await_count, 1)
        with sqlite3.connect(monitor.state_store.CATEGORY_STATS_DB_PATH) as connection:
            rows = connection.execute("SELECT channel, message_id FROM normal_messages ORDER BY message_id").fetchall()
        self.assertEqual(rows, [(self.backup, 100), (self.backup, 101)])


if __name__ == "__main__":
    unittest.main()
