"""Offline regressions from the Nov 1–2 incidents; no live APIs or Telegram."""
import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from weakref import WeakValueDictionary

from test_routing import FakeHistoryClient, FakeTelegramMessage, monitor
from alert_rules import classify_kyiv_city_official_level
from text_processing import (
    build_alert_translation_prompt, caption_requires_image, parse_alert_gate_output,
    protected_place_tokens, restore_alert_place_tokens,
)

REAL_TRANSLATE = monitor.translate_message


def publish(source, translation):
    return json.dumps({"decision": "PUBLISH", "operational_updates": [
        {"source": source, "translation": translation}]})


class TranslationSafetyTests(unittest.TestCase):
    def test_real_wrong_places_and_takeoff_are_rejected(self):
        for source, wrong in (
            ("Шахед повз Кийлів західним курсом, через нього тривога",
             "Shahed drone passing Kyiv on a western course; it triggered the alert."),
            ("Вилітає на Чайки західним курсом", "Taking off toward Chabany on a western heading."),
            ("Вилітає на Чайки західним курсом", "Taking off toward Chaiky on a western heading."),
            ("Дрон на Київщині", "Drone in Kyiv"),
        ):
            with self.subTest(source=source, wrong=wrong), self.assertRaises(ValueError):
                restore_alert_place_tokens(publish(source, wrong), source)

    def test_protected_names_roundtrip_without_fuzzy_matching(self):
        source = "Кийлів, Київ, Чайки, Чабани, Київщина"
        tokens = protected_place_tokens(source)
        self.assertEqual(set(tokens.values()), {"Kyiliv", "Kyiv", "Chaiky", "Chabany", "Kyiv region"})
        raw = restore_alert_place_tokens(publish(source, ", ".join(tokens)), source)
        output = parse_alert_gate_output(raw, source, require_excerpts=True)[1]
        self.assertEqual(output, ", ".join(tokens.values()))
        self.assertEqual(protected_place_tokens("Кийловом"), {"PLACE_0": "Kyiliv"})
        self.assertEqual(protected_place_tokens("Кийлівка"), {})
        for source, output in (("Дрон на Київщині", "Drone in PLACE_0"),
                               ("Дрон у Бучанському районі", "Drone in Bucha district"),
                               ("Дрон на Київ", "Drone toward Kyiv")):
            restore_alert_place_tokens(publish(source, output), source)

    def test_numbers_negation_direction_uncertainty_and_missing_names(self):
        for source, output in (
            ("3 дрони на Бровари", "2 drones toward Brovary"),
            ("Дрон західним курсом", "Drone heading east"),
            ("Схоже збили", "Shot down"),
            ("Не збили", "Shot down"),
            ("Дрон на Бровари", "Drone approaching"),
            ("Дрон на Бровари", "Drone toward Brovary via Kyiv"),
            ("Дрон на Бровари", "Drone toward PLACE_9"),
            ("Дві ракети на Київ", "3 missiles toward Kyiv"),
            ("Дрон з Броварів до Києва", "Drone from Kyiv toward Brovary"),
            ("Шахед повз Кийлів", "Shahed toward Kyiliv"),
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                restore_alert_place_tokens(publish(source, output), source)
        for source, output in (
            ("Схоже другий раз вже не попали", "It seems they missed again."),
            ("Вилітає на Чайки західним курсом", "Flying toward Chaiky on a western course."),
            ("Вибух, але дрон наче вижив", "An explosion, but the drone seems to have survived."),
            ("Дві ракети на Київ", "Two missiles toward Kyiv"),
            ("Дрон з Броварів до Києва", "Drone from Brovary toward Kyiv"),
        ):
            restore_alert_place_tokens(publish(source, output), source)

    def test_empty_evidenced_drop_is_valid_but_shelter_order_requires_translation(self):
        source = "Київ витратив кошти на підготовку до зими"
        raw = json.dumps({"decision": "DROP", "block_category": "NON_OPERATIONAL_NEWS", "evidence": source})
        self.assertEqual(parse_alert_gate_output(raw, source, require_excerpts=True)[0], "DROP")
        source = "Весь Київ сховалися! Особливо на вулицях кудись нирнули"
        result = {"decision": "DROP", "block_category": "NON_OPERATIONAL_NEWS", "evidence": source}
        with self.assertRaises(ValueError):
            parse_alert_gate_output(json.dumps(result), source, require_excerpts=True)
        result["translation"] = "Everyone in Kyiv, take cover! Especially if you are outside."
        self.assertEqual(parse_alert_gate_output(json.dumps(result), source, require_excerpts=True)[0], "PUBLISH")

    def test_photo_only_caption_is_distinct_from_operational_caption(self):
        self.assertTrue(caption_requires_image("Приблизно по шахедам"))
        self.assertFalse(caption_requires_image("Бровари"))
        self.assertFalse(caption_requires_image("Приблизно 3 шахеди на Київ"))

    def test_official_mass_drone_announcement_is_red(self):
        for text in (
            "🔴 УВАГА! У Києві оголошена масована дронова загроза!",
            "УВАГА! У Києві оголошена нова загроза — масована дронова небезпека!",
            "ATTENTION! New threat in Kyiv: Massive drone threat!",
            "ATTENTION! Mass drone threat in Kyiv!",
        ):
            self.assertEqual(classify_kyiv_city_official_level(text), "RED", text)
        self.assertEqual(classify_kyiv_city_official_level(
            "🟡 УВАГА! У Києві оголошена дронова загроза!"), "YELLOW")
        for text in ("ATTENTION! No mass drone threat in Kyiv!",
                     "Вчора у Києві оголошена масована дронова загроза",
                     "УВАГА! У Києві оголошена не масована дронова загроза!"):
            self.assertIsNone(classify_kyiv_city_official_level(text))


class AlertDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.set(monitor.state_store, "CATEGORY_STATS_DB_PATH", str(Path(directory.name) / "state.sqlite"))
        self.set(monitor.state_store, "stats_db_ready", True)
        monitor.state_store.initialize_stats_db()
        self.source = monitor.ALERT_FEED_CHANNEL
        self.now = datetime.now(timezone.utc)
        monitor.state_store.persist_operational_state("alert_episode_started_at", (self.now - timedelta(seconds=10)).isoformat())
        for name, value in {
            "alert_active": True, "alert_level": "YELLOW", "alert_generation": 30,
            "alert_publication_lock": asyncio.Lock(), "alert_delivery_tasks": set(),
            "alert_message_locks": WeakValueDictionary(), "translation_slots": asyncio.Semaphore(2),
            "translate_message": AsyncMock(return_value="Moving toward Kyiv."),
            "safe_send": AsyncMock(return_value={"message_id": 500}),
            "edit_message": AsyncMock(return_value={"message_id": 500}),
            "send_to_owner": AsyncMock(return_value=True),
        }.items():
            self.set(monitor, name, value)
        monitor.recent_alert_messages.clear()
        monitor.recent_alert_source_context.clear()
        self.addCleanup(monitor.recent_alert_messages.clear)
        self.addCleanup(monitor.recent_alert_source_context.clear)

    def set(self, obj, name, value):
        item = patch.object(obj, name, value, create=True)
        item.start()
        self.addCleanup(item.stop)

    def msg(self, message_id=1, text="Дрон на Київ", **kwargs):
        return FakeTelegramMessage(message_id, text, self.now, **kwargs)

    async def process(self, message=None, **kwargs):
        return await monitor.process_alert_feed_message(message or self.msg(), self.source, **kwargs)

    def record(self, message_id=1):
        return monitor.state_store.load_alert_message(self.source, message_id)

    def make_due(self, message_id=1, **changes):
        state = self.record(message_id)
        state.update(next_retry=0, **changes)
        monitor.state_store.save_alert_message(self.source, message_id, state)

    async def test_same_id_concurrent_calls_and_cosmetic_edit_publish_once_after_restart(self):
        await asyncio.gather(self.process(), self.process())
        monitor.safe_send.assert_awaited_once()
        monitor.translate_message.assert_awaited_once()
        monitor.recent_alert_messages.clear()
        monitor.state_store.initialize_stats_db()
        edited = self.msg(text="Дрон летить на Київ", edit_date=self.now + timedelta(seconds=2))
        self.assertTrue(await self.process(edited, edited=True))
        self.assertEqual(self.record()["status"], "duplicate_edit")
        monitor.safe_send.assert_awaited_once()
        revision = monitor.translate_message.call_args.kwargs["previous_revision"]
        self.assertEqual(revision["source"], "Дрон на Київ")
        self.assertIn("EDIT OF THE SAME SOURCE MESSAGE", build_alert_translation_prompt(edited.text, previous_revision=revision))
        self.assertFalse(await self.process(edited, edited=True))
        self.assertEqual(monitor.translate_message.await_count, 2)

    async def test_meaningful_edits_and_separate_message_ids_are_preserved(self):
        outputs = ["2 drones toward Brovary", "3 drones toward Brovary", "3 drones toward Kyiv",
                   "3 drones heading west", "Apparently 3 drones heading west", "3 drones are not heading west"]
        sources = ["2 дрони на Бровари", "3 дрони на Бровари", "3 дрони на Київ",
                   "3 дрони західним курсом", "Схоже 3 дрони західним курсом", "3 дрони не західним курсом"]
        monitor.translate_message.side_effect = outputs
        for second, text in enumerate(sources):
            await self.process(self.msg(text=text, edit_date=self.now + timedelta(seconds=second)), edited=True)
        self.assertEqual(monitor.safe_send.await_count, len(sources))
        self.assertIn("UPDATE — ", monitor.safe_send.call_args.args[0])
        monitor.translate_message.side_effect = None
        monitor.translate_message.return_value = "Brovary"
        await self.process(self.msg(2, "Бровари"))
        await self.process(self.msg(3, "Бровари"))
        self.assertEqual(monitor.safe_send.await_count, len(sources) + 2)

    async def test_explicit_correction_edits_only_original_destination(self):
        await self.process()
        monitor.translate_message.return_value = "Correction: moving toward Brovary."
        await self.process(self.msg(text="Уточнення: дрон на Бровари", edit_date=self.now + timedelta(seconds=2)), edited=True)
        monitor.safe_send.assert_awaited_once()
        monitor.edit_message.assert_awaited_once()
        self.assertEqual(monitor.edit_message.call_args.args[:2], (monitor.ALERT_OUTPUT_CHAT_ID, 500))
        self.assertEqual(self.record()["translation"], "Correction: moving toward Brovary.")

    async def test_previous_context_excludes_future_other_source_and_later_edit(self):
        remember = monitor.remember_alert_source_message
        remember(self.source, 1, "older", self.now - timedelta(minutes=11))
        remember(self.source, 8, "future id", self.now)
        remember("other", 3, "other source", self.now)
        remember(self.source, 4, "later edit", self.now, self.now + timedelta(minutes=2))
        remember(self.source, 5, "valid", self.now)
        self.assertEqual(remember(self.source, 6, "current", self.now + timedelta(seconds=10)), ["valid"])

    async def test_reply_context_is_bounded_same_chat_and_not_edited_after_current(self):
        current = self.msg(8)
        current.chat_id, current.reply_to_msg_id = 42, 5
        reply = self.msg(5, "Дрон на Бровари")
        reply.chat_id, reply.date = 42, self.now - timedelta(seconds=30)
        current.get_reply_message = AsyncMock(return_value=reply)
        context = await monitor.alert_reply_context(current, self.source, ["a", "b", "c"])
        self.assertEqual(len(context), 3)
        self.assertTrue(context[0].startswith("Reply to earlier"))
        for field, value in (("chat_id", 99), ("edit_date", self.now + timedelta(seconds=1)),
                             ("date", self.now - timedelta(minutes=11))):
            original = getattr(reply, field)
            setattr(reply, field, value)
            self.assertEqual(await monitor.alert_reply_context(current, self.source, ["a"]), ["a"])
            setattr(reply, field, original)
        current.get_reply_message.side_effect = TimeoutError
        self.assertEqual(await monitor.alert_reply_context(current, self.source, ["a"]), ["a"])

    async def test_failed_translation_is_recovered_even_after_later_cursor_and_restart(self):
        monitor.translate_message.side_effect = [None, "Drone toward Brovary", "Drone toward Kyiv"]
        self.assertFalse(await self.process())
        self.assertEqual(self.record()["status"], "translation_error")
        await self.process(self.msg(2, "Дрон на Бровари"))
        self.assertEqual(monitor.state_store.get_alert_feed_cursor(self.source), 2)
        self.make_due()
        monitor.state_store.initialize_stats_db()
        await monitor.retry_failed_alerts()
        self.assertEqual(self.record()["status"], "published")
        self.assertEqual(self.record()["attempts"], 2)
        self.assertEqual(monitor.state_store.get_alert_feed_cursor(self.source), 2)
        self.assertEqual(monitor.safe_send.await_count, 2)

    async def test_bounded_model_failure_is_not_rebilled_by_polling(self):
        response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"content": [{"text": "{}"}]})
        self.set(monitor, "http_client", SimpleNamespace(post=AsyncMock(return_value=response)))
        self.set(monitor.httpx, "Timeout", lambda *args, **kwargs: None)
        self.set(monitor, "translate_message", REAL_TRANSLATE)
        await self.process()
        self.make_due()
        await monitor.retry_failed_alerts()
        for _ in range(3):
            await monitor.retry_failed_alerts()
            await self.process(edited=True)
        self.assertEqual(monitor.http_client.post.await_count, 4)
        monitor.safe_send.assert_not_awaited()

    async def test_expiry_all_clear_or_new_episode_prevents_recovery(self):
        monitor.translate_message.return_value = None
        for message_id in (1, 2, 3):
            await self.process(self.msg(message_id))
        self.make_due(1, deadline=time.time() - 1)
        self.make_due(2, episode="previous episode")
        self.make_due(3)
        monitor.alert_active = False
        await monitor.retry_failed_alerts()
        monitor.alert_active = True
        self.make_due(3, deadline=time.time() - 1)
        await monitor.retry_failed_alerts()
        self.assertEqual(monitor.translate_message.await_count, 3)

    async def test_new_source_edit_does_not_wait_for_failed_old_revision(self):
        monitor.translate_message.side_effect = [None, "Now moving toward Brovary"]
        await self.process()
        await self.process(self.msg(text="Зараз дрон на Бровари", edit_date=self.now + timedelta(seconds=2)), edited=True)
        self.assertEqual(self.record()["status"], "published")
        self.assertEqual(self.record()["attempts"], 1)

    async def test_unknown_delivery_is_not_resent_and_storage_failure_does_not_send(self):
        monitor.safe_send.return_value = None
        await self.process()
        self.assertEqual(self.record()["status"], "delivery_unknown")
        self.make_due()
        await monitor.retry_failed_alerts()
        await self.process(edited=True)
        monitor.safe_send.assert_awaited_once()
        monitor.translate_message.assert_awaited_once()
        with patch.object(monitor.state_store, "load_alert_message", side_effect=sqlite3.OperationalError):
            with self.assertRaises(sqlite3.OperationalError):
                await self.process(self.msg(2))
        monitor.safe_send.assert_awaited_once()

    async def test_telegram_send_timeout_never_retries_blindly(self):
        self.set(monitor, "http_client", SimpleNamespace(post=AsyncMock(side_effect=TimeoutError)))
        self.set(monitor.httpx, "Timeout", lambda *args, **kwargs: None)
        self.assertIsNone(await monitor.telegram_request("sendMessage", {"text": "test"}))
        monitor.http_client.post.assert_awaited_once()

    async def test_photo_caption_is_dropped_before_spend_and_other_messages_do_not_wait(self):
        gate = asyncio.Event()
        self.set(monitor, "is_blocked_alert_image", AsyncMock(side_effect=lambda message: None))
        await self.process(self.msg(text="Приблизно по шахедам", photo=object()))
        monitor.is_blocked_alert_image.assert_not_awaited()
        monitor.translate_message.assert_not_awaited()
        async def delayed(message):
            await gate.wait()
            return True
        monitor.is_blocked_alert_image.side_effect = delayed
        task = asyncio.create_task(self.process(self.msg(2, "Дрон на Бровари", photo=object())))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(self.record(2)["status"], "image_pending")
        self.make_due(2)
        self.assertEqual(monitor.state_store.pending_alert_retries(self.record(2)["episode"]), [])
        await self.process(self.msg(3))
        self.assertFalse(task.done())
        gate.set()
        await task
        self.assertEqual(monitor.state_store.get_alert_feed_cursor(self.source), 3)

    async def test_all_clear_cancels_delivery_without_killing_the_poller(self):
        started = asyncio.Event()
        async def slow_translation(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        monitor.translate_message.side_effect = slow_translation
        continued = []
        async def poller():
            result = await self.process()
            continued.append(result)
        parent = asyncio.create_task(poller())
        await started.wait()
        await monitor.drain_alert_delivery_tasks(timeout=0)
        await parent
        self.assertEqual(continued, [False])
        monitor.safe_send.assert_not_awaited()

    async def test_poll_backfill_does_not_wait_for_slow_image(self):
        gate = asyncio.Event()
        async def delayed(message):
            await gate.wait()
            return True
        self.set(monitor, "is_blocked_alert_image", delayed)
        client = FakeHistoryClient({self.source: [self.msg(1, photo=object()), self.msg(2)]})
        await asyncio.wait_for(monitor.backfill_alert_feed(client, {self.source: self.source}), timeout=1)
        monitor.safe_send.assert_awaited_once()
        self.assertEqual(self.record(1)["status"], "image_pending")
        gate.set()
        await monitor.drain_alert_delivery_tasks(timeout=1)
        self.assertEqual(self.record(1)["status"], "dropped")
        self.assertEqual(monitor.state_store.get_alert_feed_cursor(self.source), 2)

    async def test_bad_place_response_is_repaired_once_with_full_parser(self):
        source = "Шахед повз Кийлів західним курсом"
        responses = [publish(source, "Shahed passing Kyiv on a western course"),
                     publish(source, "Shahed passing PLACE_0 on a western course")]
        client = SimpleNamespace(post=AsyncMock(side_effect=[
            SimpleNamespace(raise_for_status=lambda: None, json=lambda raw=raw: {
                "stop_reason": "end_turn", "content": [{"text": raw}]}) for raw in responses]))
        self.set(monitor, "http_client", client)
        self.set(monitor.httpx, "Timeout", lambda *args, **kwargs: None)
        output = await REAL_TRANSLATE(source)
        self.assertEqual(output, "Shahed passing Kyiliv on a western course")
        self.assertNotIn("PLACE_", output.raw)
        self.assertEqual(client.post.await_count, 2)
        self.assertIn("Validation failed", client.post.call_args.kwargs["json"]["messages"][0]["content"])

