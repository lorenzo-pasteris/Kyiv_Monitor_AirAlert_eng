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
from text_processing import (
    build_alert_translation_prompt, correct_known_translation_errors,
    is_general_siren_announcement, normalize_alert_revision, parse_alert_gate_output,
    translate_known_terse_fragment,
)


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

    async def test_publication_adds_only_one_state_dot_after_final_cleanup(self):
        monitor.translate_message.return_value = "🟡 Loud blasts 💥💥 — shot down. Kyiv is clear for now ✈️\nDetailed radar 👉🏽 @kievinform_ua1"
        self.assertTrue(await monitor.handle_alert_message("Гучно є збиття", self.backup))
        monitor.safe_send.assert_awaited_once_with("🟡 Loud blasts — shot down. Kyiv is clear for now")

    async def test_all_clear_speculation_never_calls_model_or_public_sender(self):
        text = "В теорії має бути відбій, але Міхалич по базі може ще 2 години чекати поки підлетять нові дрони"
        self.assertTrue(await monitor.handle_alert_message(text, self.primary))
        monitor.translate_message.assert_not_awaited()
        monitor.safe_send.assert_not_awaited()

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
        async def translate(text, context=(), **kwargs):
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

        async def translate(text, context=(), **kwargs):
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

    async def test_no_longer_observed_is_not_general_all_clear(self):
        for text in ("Не спостерігається", "Не спостережень", "Не спостерігаються"):
            self.assertEqual(translate_known_terse_fragment(text), "No longer observed.")
        self.assertEqual(
            correct_known_translation_errors("Не спостерігається біля Чабанів", "No threats currently observed near Chabany."),
            "No longer observed near Chabany.",
        )
        self.assertIn("NEVER 'no threats currently observed'", build_alert_translation_prompt("Не спостерігається"))

    async def test_equivalent_edit_has_one_durable_claim_even_after_restart(self):
        self.assertTrue(monitor.state_store.claim_alert_feed_delivery(self.primary, 21234, "Не спостережень"))
        monitor.recent_alert_messages.clear()
        self.assertFalse(monitor.state_store.claim_alert_feed_delivery(self.primary, 21234, "Не спостерігається!"))
        self.assertTrue(monitor.state_store.claim_alert_feed_delivery(self.primary, 21234, "Знову спостерігається"))
        self.assertTrue(monitor.state_store.claim_alert_feed_delivery(self.primary, 21235, "Не спостерігається"))

    async def test_equivalent_edit_can_retry_after_failed_delivery(self):
        self.assertTrue(monitor.state_store.claim_alert_feed_delivery(self.primary, 1, "Не спостережень"))
        monitor.state_store.release_alert_feed_delivery(self.primary, 1, "Не спостерігається")
        self.assertTrue(monitor.state_store.claim_alert_feed_delivery(self.primary, 1, "Не спостерігається"))

    async def test_equivalent_edit_publishes_once_but_operational_edit_passes(self):
        for text in ("Не спостережень", "Не спостерігається", "Знову спостерігається біля Чабанів"):
            msg = FakeTelegramMessage(21234, text, datetime.now(timezone.utc))
            await monitor.process_alert_feed_message(msg, self.primary, edited=True)
        self.assertEqual(monitor.safe_send.await_count, 2)

    async def test_revision_key_preserves_tactical_changes(self):
        pairs = [
            ("2 дрони на Бровари", "3 дрони на Бровари"),
            ("Дрон на Бровари", "Дрон від Броварів"),
            ("Збили", "Не збили"),
            ("Не спостерігається", "Не спостерігаються"),
            ("Не спостерігається", "Схоже не спостерігається"),
        ]
        for before, after in pairs:
            self.assertNotEqual(normalize_alert_revision(before), normalize_alert_revision(after))

    async def test_general_siren_notices_are_official_only(self):
        for source in (self.primary, self.backup):
            for index, text in enumerate(("✅ВІДБІЙ ПОВІТРЯНОЇ ТРИВОГИ ✅", "Київ: повітряна тривога!", "У Києві оголошена ракетна загроза!"), 1):
                self.assertTrue(is_general_siren_announcement(text))
                msg = FakeTelegramMessage(index, text, datetime.now(timezone.utc))
                await monitor.process_alert_feed_message(msg, source)
        monitor.safe_send.assert_not_awaited()
        monitor.translate_message.assert_not_awaited()

    async def test_tactical_threat_all_clear_and_mixed_warning_are_not_blocked(self):
        for text in ("Відбій по балістиці", "Без загроз по бандеролям", "Повітряна тривога! Дрон на Бровари", "Загроза з Брянська!!", "Перші ракети Х101 залітають на Сумщину"):
            self.assertFalse(is_general_siren_announcement(text))
            await monitor.handle_alert_message(text, self.backup)
        self.assertEqual(monitor.safe_send.await_count, 5)

    async def test_backup_gets_news_rules_without_changing_primary_prompt(self):
        await monitor.handle_alert_message(self.text, self.backup)
        monitor.translate_message.assert_awaited_once_with(self.text, context=(), mixed_news_source=True)
        primary = build_alert_translation_prompt(self.text)
        backup = build_alert_translation_prompt(self.text, mixed_news_source=True)
        self.assertNotIn("NON_OPERATIONAL_NEWS", primary)
        self.assertIn("NON_OPERATIONAL_NEWS", backup)

    async def test_evidenced_ordinary_news_drop_is_scoped_to_backup(self):
        text = "Київ витратив 16 млрд грн на підготовку до зими, — директор Центру досліджень енергетики Харченко"
        raw = json.dumps(dict(decision="DROP", block_category="NON_OPERATIONAL_NEWS",
                              evidence="витратив 16 млрд грн", translation="Kyiv spent UAH 16 billion on winter preparations."))
        self.assertEqual(parse_alert_gate_output(raw, text, mixed_news_source=True)[0], "DROP")
        self.assertEqual(parse_alert_gate_output(raw, text)[0], "PUBLISH")
        with self.assertRaises(ValueError):
            parse_alert_gate_output(raw, "Новий дрон на Бровари", mixed_news_source=True)

    async def test_news_drop_cannot_remove_terse_operational_updates(self):
        for text in ("Нивки", "Жуляни", "2 дрони на Чабани", "Збили", "Перші ракети Х101 залітають на Сумщину"):
            raw = json.dumps(dict(decision="DROP", block_category="NON_OPERATIONAL_NEWS", evidence=text, translation="Operational update",
                                  operational_updates=[dict(source=text, translation="Operational update")]))
            self.assertEqual(parse_alert_gate_output(raw, text, mixed_news_source=True)[0], "PUBLISH", text)

    async def test_mixed_district_notice_is_removed_before_translation(self):
        text = "🟡 Вишгородський район — повітряна тривога, жовтий рівень: Дронова загроза (жовтий рівень)\n3х рБпЛА з Чернігівщини\n✈️ радар 👉🏽 @kievinform_ua1"
        await monitor.handle_alert_message(text, self.backup)
        monitor.translate_message.assert_awaited_once_with(
            "3х рБпЛА з Чернігівщини", context=(), mixed_news_source=True)

    async def test_altitude_equivalents_merge_but_new_facts_and_intervening_events_do_not(self):
        first = "З 4к скинув висоту до 2, і продовжує поступово знижуватись"
        second = "Скинув висоту з 4 км до 2 км і продовжує поступово знижуватись"
        await monitor.handle_alert_message(first, self.primary)
        await monitor.handle_alert_message(second, self.primary)
        self.assertEqual(monitor.safe_send.await_count, 1)
        for changed in (second.replace("до 2", "до 1"), "Схоже " + second,
                        "Не " + second, second + " на Бровари", "Ще один дрон на Київ", second):
            await monitor.handle_alert_message(changed, self.backup)
        self.assertEqual(monitor.safe_send.await_count, 7)

    async def test_altitude_cosmetic_edit_has_one_durable_claim(self):
        before = "З 4к скинув висоту до 2, і продовжує поступово знижуватись"
        self.assertTrue(monitor.state_store.claim_alert_feed_delivery(self.primary, 21263, before))
        self.assertFalse(monitor.state_store.claim_alert_feed_delivery(self.primary, 21263, before.replace("4к", "4км")))

    async def test_generic_and_old_clear_notices_never_reach_model(self):
        for text in ("🔴 ТРИВОГА В КИЄВІ та області", "Відбій, відпочивайте"):
            await monitor.handle_alert_message(text, self.backup)
        monitor.translate_message.assert_not_awaited()
        monitor.safe_send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
