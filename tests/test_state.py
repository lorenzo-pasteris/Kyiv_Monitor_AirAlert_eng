import asyncio
import sqlite3
import tempfile
import threading
import unittest

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_routing import monitor


class AlertStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_send_alert = monitor.send_to_alert_channel
        self.original_send_owner = monitor.send_to_owner
        self.original_stats_ready = monitor.state_store.stats_db_ready
        self.original_client = monitor.production_client
        self.original_entities = monitor.content_source_entities
        monitor.alert_transition_lock = asyncio.Lock()
        monitor.alert_active = False
        monitor.alert_started_at = None
        monitor.alert_generation = 0
        monitor.telegram_alert_state = None
        monitor.telegram_alert_level = None
        monitor.telegram_alert_message_id = 0
        monitor.telegram_alert_message_at = None
        monitor.api_alert_level = None
        monitor.api_alert_event_at = None
        monitor.alert_source_mismatch_since = None
        monitor.last_source_reconcile_request_at = None
        monitor.last_unknown_alert_message_id = 0
        monitor.webhook_silence_notified = False
        monitor.last_alert_source_mismatch = None
        monitor.last_alert_mismatch_notice_at = None
        monitor.alert_level = "GREEN"
        monitor.state_store.stats_db_ready = False
        monitor.production_client = None
        monitor.content_source_entities = {}
        monitor.alert_delivery_tasks.clear()
        self.owner_messages = []

        async def owner_sender(text):
            self.owner_messages.append(text)
            return {"message_id": 1}

        monitor.send_to_owner = owner_sender

    async def asyncTearDown(self):
        monitor.send_to_alert_channel = self.original_send_alert
        monitor.send_to_owner = self.original_send_owner
        monitor.state_store.stats_db_ready = self.original_stats_ready
        monitor.production_client = self.original_client
        monitor.content_source_entities = self.original_entities

    def test_structured_schema_avoids_unsupported_limit_keywords(self):
        schema_text = repr(monitor.CATEGORY_RESULT_SCHEMA)
        self.assertNotIn("maxItems", schema_text)
        self.assertNotIn("maxLength", schema_text)

    def test_normalizer_caps_bullet_count_without_truncating_text(self):
        parsed = {
            "categories": {
                key: {"selected_ids": [], "items": []}
                for key in monitor.CATEGORIES
            }
        }
        bullets = [
            "Ratcliffe warned Putin of planned US-Ukraine operations to block southern ports "
            "and proposed a Donbas compromise; Putin threatened threefold stronger strikes "
            "and demanded that Ukraine cease its attacks.",
            "Belarus is constructing a new military training ground near the Ukrainian border "
            "at Yakimivka station, with a railway branch for rapid unloading of heavy wheeled "
            "and tracked military vehicles.",
        ]
        parsed["categories"]["kyiv_region"]["items"] = [
            {
                "event_key": f"2026-09-01|kyiv|event-{index}",
                "text": bullet,
                "is_update": False,
            }
            for index, bullet in enumerate(bullets * 4)
        ]

        normalized = monitor.normalize_category_result(parsed, [])

        self.assertEqual(len(normalized["kyiv_region"]["items"]), 3)
        self.assertEqual(
            [item["text"] for item in normalized["kyiv_region"]["items"][:2]],
            bullets,
        )

    def test_normalizer_keeps_one_message_in_only_one_category(self):
        message_id = "source:1"
        parsed = {
            "categories": {
                key: {"selected_ids": [], "items": []}
                for key in monitor.CATEGORIES
            }
        }
        parsed["categories"]["ukraine_key_developments"]["selected_ids"] = [message_id]
        parsed["categories"]["kyiv_region"]["selected_ids"] = [message_id]

        normalized = monitor.normalize_category_result(parsed, [{"id": message_id}])

        self.assertEqual(
            normalized["ukraine_key_developments"]["selected_ids"],
            [message_id],
        )
        self.assertEqual(normalized["kyiv_region"]["selected_ids"], [])

    async def test_failed_public_start_does_not_commit_state_and_can_retry(self):
        results = [None, {"message_id": 42}]

        async def alert_sender(text):
            return results.pop(0)

        monitor.send_to_alert_channel = alert_sender
        monitor.telegram_alert_state = True

        self.assertFalse(await monitor.reconcile_alert_state("unit-test"))
        self.assertFalse(monitor.alert_active)
        self.assertTrue(self.owner_messages)

        self.assertTrue(await monitor.reconcile_alert_state("unit-test"))
        self.assertTrue(monitor.alert_active)
        self.assertEqual(monitor.alert_generation, 1)

    async def test_startup_active_restores_state_without_public_duplicate(self):
        public_messages = []

        async def alert_sender(text):
            public_messages.append(text)
            return {"message_id": 2}

        monitor.send_to_alert_channel = alert_sender
        self.assertTrue(await monitor.apply_alert_state(True, "startup-test", startup=True))

        self.assertTrue(monitor.alert_active)
        self.assertEqual(public_messages, [])
        self.assertIn("restored after restart", self.owner_messages[-1])

    async def test_active_level_changes_publish_without_restarting_alert(self):
        public_messages = []

        async def alert_sender(text):
            public_messages.append(text)
            return {"message_id": len(public_messages)}

        monitor.send_to_alert_channel = alert_sender
        self.assertTrue(await monitor.apply_alert_state(True, "yellow", level="YELLOW"))
        generation = monitor.alert_generation
        self.assertTrue(await monitor.apply_alert_state(True, "red", level="RED"))
        self.assertTrue(await monitor.apply_alert_state(True, "yellow-again", level="YELLOW"))

        self.assertEqual(public_messages, [
            monitor.ALERT_LEVEL_MESSAGES["YELLOW"],
            monitor.ALERT_LEVEL_MESSAGES["RED"],
            monitor.ALERT_LEVEL_MESSAGES["YELLOW"],
        ])
        self.assertTrue(monitor.alert_active)
        self.assertEqual(monitor.alert_level, "YELLOW")
        self.assertEqual(monitor.alert_generation, generation)

    async def test_failed_level_change_does_not_commit_and_can_retry(self):
        results = [None, {"message_id": 2}]

        async def alert_sender(text):
            return results.pop(0)

        monitor.alert_active = True
        monitor.alert_level = "YELLOW"
        monitor.send_to_alert_channel = alert_sender

        self.assertFalse(await monitor.apply_alert_state(True, "red", level="RED"))
        self.assertEqual(monitor.alert_level, "YELLOW")
        self.assertTrue(await monitor.apply_alert_state(True, "red", level="RED"))
        self.assertEqual(monitor.alert_level, "RED")

    async def test_unknown_startup_state_is_rejected(self):
        self.assertFalse(await monitor.apply_alert_state(None, "startup-test", startup=True))
        self.assertFalse(monitor.alert_active)

    async def test_october_10_incident_and_delayed_events(self):
        monitor.send_to_alert_channel = AsyncMock(return_value={"message_id": 1})
        monitor.api_alert_level = "GREEN"
        for level, message_id, at in (
            ("GREEN", 21566, "2026-10-10T09:59:02+00:00"),
            ("RED", 21571, "2026-10-10T10:57:26+00:00"),
        ):
            message = SimpleNamespace(id=message_id, date=datetime.fromisoformat(at))
            self.assertTrue(monitor.record_telegram_alert_level(level, message))
            await monitor.reconcile_alert_state("incident replay")
        self.assertEqual(monitor.alert_level, "RED")
        self.assertTrue(monitor.alert_active)
        await monitor.reconcile_alert_state("unchanged stale API green")
        self.assertEqual(monitor.send_to_alert_channel.await_count, 1)

        clear = SimpleNamespace(id=21575, date=datetime.fromisoformat("2026-10-10T11:36:22+00:00"))
        self.assertTrue(monitor.record_telegram_alert_level("GREEN", clear))
        await monitor.reconcile_alert_state("official all clear")
        self.assertFalse(monitor.record_telegram_alert_level("RED", message))
        monitor.api_alert_level = "RED"
        await monitor.reconcile_alert_state("late undated webhook RED")
        self.assertFalse(monitor.alert_active)
        self.assertEqual(monitor.alert_level, "GREEN")
        self.assertEqual(
            [call.args[0] for call in monitor.send_to_alert_channel.await_args_list],
            [monitor.ALERT_LEVEL_MESSAGES["RED"], monitor.build_all_clear_message()],
        )

    async def test_yellow_red_yellow_clear_with_disagreeing_webhook(self):
        monitor.send_to_alert_channel = AsyncMock(return_value={"message_id": 1})
        monitor.api_alert_level = "RED"
        now = datetime.now(timezone.utc)
        for index, level in enumerate(("YELLOW", "RED", "YELLOW", "GREEN"), 1):
            message = SimpleNamespace(id=index, date=now + timedelta(seconds=index))
            self.assertTrue(monitor.record_telegram_alert_level(level, message))
            await monitor.reconcile_alert_state("cycle replay")
            self.assertEqual(monitor.alert_level, level)
        self.assertEqual(monitor.send_to_alert_channel.await_count, 4)
        self.assertEqual(len(self.owner_messages), 1)

    async def test_public_alert_precedes_discrepancy_notice(self):
        monitor.telegram_alert_state = True
        monitor.telegram_alert_level = "RED"
        monitor.api_alert_level = "GREEN"
        monitor.send_to_alert_channel = AsyncMock(return_value={"message_id": 1})

        async def owner_sender(text):
            self.assertEqual(monitor.send_to_alert_channel.await_count, 1)
            self.assertTrue(monitor.alert_active)
            self.owner_messages.append(text)

        monitor.send_to_owner = owner_sender
        for _ in range(3):
            await monitor.reconcile_alert_state("retry")
        self.assertEqual(len(self.owner_messages), 1)

    def test_same_level_new_message_advances_cursor_and_rejects_older_clear(self):
        now = datetime.now(timezone.utc)
        self.assertTrue(monitor.record_telegram_alert_level("RED", SimpleNamespace(id=10, date=now)))
        later = now + timedelta(seconds=20)
        self.assertTrue(monitor.record_telegram_alert_level("RED", SimpleNamespace(id=12, date=later)))
        self.assertFalse(monitor.record_telegram_alert_level("GREEN", SimpleNamespace(id=11, date=now)))
        self.assertFalse(monitor.record_telegram_alert_level("GREEN", SimpleNamespace(id=13, date=now)))
        self.assertEqual(monitor.telegram_alert_message_id, 12)
        self.assertEqual(monitor.telegram_alert_message_at, later)

    async def test_poll_recovers_red_despite_green_api(self):
        monitor.api_alert_level = "GREEN"
        monitor.telegram_alert_state = False
        monitor.telegram_alert_level = "GREEN"
        message = SimpleNamespace(
            id=21571, date=datetime.now(timezone.utc),
            text="🔴 УВАГА! У Києві оголошена ракетна загроза!",
        )
        client = SimpleNamespace(get_messages=AsyncMock(return_value=[message]))
        monitor.send_to_alert_channel = AsyncMock(return_value={"message_id": 1})
        with patch.object(monitor, "backfill_alert_feed", new=AsyncMock(return_value=0)), \
             patch.object(monitor.asyncio, "sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await monitor.alert_feed_poll_loop(client, {monitor.ALERT_TRIGGER_CHANNEL: "trigger"})
        self.assertTrue(monitor.alert_active)
        self.assertEqual(monitor.alert_level, "RED")

    async def test_api_remains_fallback_without_telegram_observation(self):
        monitor.api_alert_level = "YELLOW"
        monitor.send_to_alert_channel = AsyncMock(return_value={"message_id": 1})
        self.assertTrue(await monitor.reconcile_alert_state("API only"))
        self.assertEqual(monitor.alert_level, "YELLOW")

    async def test_newer_dated_webhook_escalates_before_telegram(self):
        monitor.send_to_alert_channel = AsyncMock(return_value={"message_id": 1})
        start = datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc)
        self.assertTrue(monitor.record_telegram_alert_level("YELLOW", SimpleNamespace(id=1, date=start)))
        await monitor.reconcile_alert_state("official yellow")
        monitor.api_alert_level = "RED"
        monitor.api_alert_event_at = start + timedelta(minutes=5)
        await monitor.reconcile_alert_state("webhook red")
        self.assertEqual(monitor.alert_level, "RED")
        # The official RED that follows is the same level: no second publication.
        red = SimpleNamespace(id=2, date=start + timedelta(minutes=5, seconds=10))
        self.assertTrue(monitor.record_telegram_alert_level("RED", red))
        await monitor.reconcile_alert_state("official red")
        self.assertEqual(monitor.send_to_alert_channel.await_count, 2)

    async def test_october_10_replay_with_dated_webhook(self):
        monitor.send_to_alert_channel = AsyncMock(return_value={"message_id": 1})
        monitor.api_alert_level = "GREEN"
        monitor.api_alert_event_at = datetime.fromisoformat("2026-10-10T09:58:52+00:00")
        for level, message_id, at in (
            ("GREEN", 21566, "2026-10-10T09:59:02+00:00"),
            ("RED", 21571, "2026-10-10T10:57:26+00:00"),
        ):
            message = SimpleNamespace(id=message_id, date=datetime.fromisoformat(at))
            self.assertTrue(monitor.record_telegram_alert_level(level, message))
            await monitor.reconcile_alert_state("incident replay")
        self.assertEqual((monitor.alert_active, monitor.alert_level), (True, "RED"))
        # The provider's later dated all-clear ends the alert even before Telegram.
        monitor.api_alert_event_at = datetime.fromisoformat("2026-10-10T11:36:20+00:00")
        await monitor.reconcile_alert_state("webhook all clear")
        self.assertEqual((monitor.alert_active, monitor.alert_level), (False, "GREEN"))
        # An older webhook event cannot reopen it.
        monitor.api_alert_level = "RED"
        monitor.api_alert_event_at = datetime.fromisoformat("2026-10-10T10:57:05+00:00")
        clear = SimpleNamespace(id=21575, date=datetime.fromisoformat("2026-10-10T11:36:22+00:00"))
        self.assertTrue(monitor.record_telegram_alert_level("GREEN", clear))
        await monitor.reconcile_alert_state("late older webhook")
        self.assertFalse(monitor.alert_active)

    async def test_telegram_wins_ties_and_undated_snapshots(self):
        at = datetime(2026, 10, 10, 10, 57, tzinfo=timezone.utc)
        self.assertTrue(monitor.record_telegram_alert_level("RED", SimpleNamespace(id=5, date=at)))
        monitor.api_alert_level = "GREEN"
        self.assertEqual(monitor.select_alert_source(), "telegram")
        monitor.api_alert_event_at = at
        self.assertEqual(monitor.select_alert_source(), "telegram")
        monitor.api_alert_event_at = at + timedelta(seconds=1)
        self.assertEqual(monitor.select_alert_source(), "webhook")

    async def test_unknown_receiver_state_clears_stale_webhook_level(self):
        monitor.api_alert_level = "GREEN"
        monitor.api_alert_event_at = datetime.now(timezone.utc)
        response = SimpleNamespace(status_code=200, json=lambda: {"known": False})
        client = SimpleNamespace(get=AsyncMock(return_value=response))
        with patch.object(monitor, "http_client", client), \
             patch.object(monitor.asyncio, "sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await monitor.ukraine_alarm_state_loop()
        self.assertIsNone(monitor.api_alert_level)
        self.assertIsNone(monitor.api_alert_event_at)

    async def test_state_loop_reads_dated_receiver_level(self):
        monitor.send_to_alert_channel = AsyncMock(return_value={"message_id": 1})
        payload = {
            "known": True, "level": "YELLOW", "event_at": "2026-10-10T12:42:52.252324+00:00",
            "last_webhook_at": datetime.now(timezone.utc).isoformat(),
        }
        response = SimpleNamespace(status_code=200, json=lambda: payload)
        client = SimpleNamespace(get=AsyncMock(return_value=response))
        with patch.object(monitor, "http_client", client), \
             patch.object(monitor.asyncio, "sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await monitor.ukraine_alarm_state_loop()
        self.assertEqual(monitor.api_alert_event_at, datetime(2026, 10, 10, 12, 42, 52, 252324, tzinfo=timezone.utc))
        self.assertEqual(monitor.alert_level, "YELLOW")

    async def test_persistent_disagreement_requests_throttled_rest_repair(self):
        monitor.telegram_alert_level = "RED"
        monitor.api_alert_level = "GREEN"
        response = SimpleNamespace(status_code=200, json=lambda: {"decision": "applied"})
        client = SimpleNamespace(post=AsyncMock(return_value=response))
        with patch.object(monitor, "http_client", client), \
             patch.object(monitor, "UKRAINE_ALARM_RECONCILE_URL", "http://receiver/reconcile"):
            self.assertFalse(await monitor.request_source_reconcile_if_needed(now=100))
            self.assertFalse(await monitor.request_source_reconcile_if_needed(now=130))
            self.assertTrue(await monitor.request_source_reconcile_if_needed(now=146))
            self.assertFalse(await monitor.request_source_reconcile_if_needed(now=180))
            self.assertTrue(await monitor.request_source_reconcile_if_needed(now=207))
            monitor.api_alert_level = "RED"
            self.assertFalse(await monitor.request_source_reconcile_if_needed(now=400))
        self.assertEqual(client.post.await_count, 2)
        self.assertIsNone(monitor.alert_source_mismatch_since)

    async def test_matching_yellow_still_checks_rest_every_minute(self):
        monitor.alert_active = True
        monitor.telegram_alert_level = monitor.api_alert_level = "YELLOW"
        response = SimpleNamespace(status_code=200, json=lambda: {"decision": "applied"})
        client = SimpleNamespace(post=AsyncMock(return_value=response))
        with patch.object(monitor, "http_client", client), \
             patch.object(monitor, "UKRAINE_ALARM_RECONCILE_URL", "http://receiver/reconcile"):
            self.assertTrue(await monitor.request_source_reconcile_if_needed(now=100))
            self.assertFalse(await monitor.request_source_reconcile_if_needed(now=159))
            self.assertTrue(await monitor.request_source_reconcile_if_needed(now=160))
        self.assertEqual(client.post.await_count, 2)

    async def test_rest_timeout_is_throttled_without_changing_level(self):
        monitor.alert_active = True
        monitor.alert_level = "YELLOW"
        client = SimpleNamespace(post=AsyncMock(side_effect=TimeoutError))
        with patch.object(monitor, "http_client", client), \
             patch.object(monitor, "UKRAINE_ALARM_RECONCILE_URL", "http://receiver/reconcile"):
            self.assertTrue(await monitor.request_source_reconcile_if_needed(now=100))
            self.assertFalse(await monitor.request_source_reconcile_if_needed(now=120))
        self.assertEqual(monitor.alert_level, "YELLOW")

    async def test_repaired_api_red_overrides_stale_telegram_yellow(self):
        monitor.alert_active = True
        monitor.alert_level = monitor.telegram_alert_level = "YELLOW"
        monitor.telegram_alert_state = True
        monitor.telegram_alert_message_at = datetime(2026, 10, 18, 19, 32, tzinfo=timezone.utc)
        monitor.send_to_alert_channel = AsyncMock(return_value={"message_id": 1})
        payload = {
            "known": True, "level": "RED", "event_at": "2026-10-18T19:57:23+00:00",
            "last_webhook_at": datetime.now(timezone.utc).isoformat(),
        }
        client = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: payload)))
        with patch.object(monitor, "http_client", client), \
             patch.object(monitor, "request_source_reconcile_if_needed", new=AsyncMock()), \
             patch.object(monitor.asyncio, "sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await monitor.ukraine_alarm_state_loop()
        self.assertEqual(monitor.alert_level, "RED")
        monitor.send_to_alert_channel.assert_awaited_once()

    async def test_unrecognized_official_announcement_requests_verification_once(self):
        message = SimpleNamespace(id=21825, text="🔴 УВАГА! Новий формат повідомлення")
        with patch.object(monitor, "request_source_reconcile_if_needed", new=AsyncMock()) as repair:
            await monitor.report_unknown_alert_announcement(message)
            await monitor.report_unknown_alert_announcement(message)
        repair.assert_awaited_once_with(urgent=True)
        self.assertEqual(len(self.owner_messages), 1)

    async def test_new_template_upgrades_yellow_and_can_downgrade_to_yellow(self):
        monitor.alert_active = True
        monitor.alert_level = "YELLOW"
        monitor.send_to_alert_channel = AsyncMock(return_value={"message_id": 1})
        for offset, threat, expected in ((0, "ракетна загроза", "RED"), (1, "дронова небезпека", "YELLOW")):
            message = SimpleNamespace(id=21825 + offset, date=datetime(2026, 10, 18, 19, 57 + offset, tzinfo=timezone.utc))
            level = monitor.classify_kyiv_city_official_level(f"🔴 УВАГА! У Києві оголошена нова загроза – {threat}!")
            monitor.record_telegram_alert_level(level, message)
            await monitor.reconcile_alert_state("test official announcement")
            self.assertEqual(monitor.alert_level, expected)
        self.assertEqual(monitor.send_to_alert_channel.await_count, 2)

    async def test_webhook_silence_is_reported_once_and_recovery_once(self):
        now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
        quiet = {
            "last_webhook_at": now - timedelta(seconds=monitor.WEBHOOK_SILENCE_ALERT_SECONDS + 60),
            "started_at": now - timedelta(days=1),
        }
        self.assertTrue(await monitor.check_webhook_liveness(quiet, now=now))
        self.assertTrue(await monitor.check_webhook_liveness(quiet, now=now))
        self.assertEqual(len(self.owner_messages), 1)
        self.assertIn("webhook silent", self.owner_messages[0])
        alive = {"last_webhook_at": now, "started_at": now - timedelta(days=1)}
        self.assertFalse(await monitor.check_webhook_liveness(alive, now=now))
        self.assertFalse(await monitor.check_webhook_liveness(alive, now=now))
        self.assertEqual(len(self.owner_messages), 2)
        # Old receivers expose no liveness fields: no false alarm.
        self.assertFalse(await monitor.check_webhook_liveness({"last_webhook_at": None, "started_at": None}, now=now))

    async def test_unknown_receiver_retries_bootstrap_after_delay(self):
        monitor.api_alert_level = None
        monitor.telegram_alert_level = None
        response = SimpleNamespace(status_code=200, json=lambda: {"decision": "error"})
        client = SimpleNamespace(post=AsyncMock(return_value=response))
        with patch.object(monitor, "http_client", client), \
             patch.object(monitor, "UKRAINE_ALARM_RECONCILE_URL", "http://receiver/reconcile"):
            self.assertFalse(await monitor.request_source_reconcile_if_needed(now=100))
            self.assertTrue(await monitor.request_source_reconcile_if_needed(now=146))
            self.assertFalse(await monitor.request_source_reconcile_if_needed(now=160))
            self.assertTrue(await monitor.request_source_reconcile_if_needed(now=207))
        self.assertEqual(client.post.await_count, 2)

    async def test_unknown_receiver_reapplies_telegram_before_private_liveness_notice(self):
        monitor.telegram_alert_state = True
        monitor.telegram_alert_level = "RED"
        monitor.api_alert_level = "GREEN"
        response = SimpleNamespace(status_code=200, json=lambda: {"known": False})
        events = []

        async def reconcile(source):
            events.append("public")
            return True

        async def liveness(state):
            events.append("private")

        with patch.object(monitor, "http_client", SimpleNamespace(get=AsyncMock(return_value=response))), \
             patch.object(monitor, "reconcile_alert_state", side_effect=reconcile), \
             patch.object(monitor, "check_webhook_liveness", side_effect=liveness), \
             patch.object(monitor, "request_source_reconcile_if_needed", new=AsyncMock()), \
             patch.object(monitor.asyncio, "sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await monitor.ukraine_alarm_state_loop()
        self.assertEqual(events, ["public", "private"])

    async def test_stale_alert_delivery_is_discarded(self):
        sent = []

        async def alert_sender(text):
            sent.append(text)
            return {"message_id": 3}

        monitor.send_to_alert_channel = alert_sender
        monitor.alert_active = True
        monitor.alert_generation = 5
        await monitor.handle_alert_message("test", generation=4)
        self.assertEqual(sent, [])


class StatsDbDisabledTests(unittest.IsolatedAsyncioTestCase):
    """When stats_db_ready is False, every persistence helper must short-circuit safely
    instead of touching sqlite or raising."""

    async def asyncSetUp(self):
        self.original_db_path = monitor.state_store.CATEGORY_STATS_DB_PATH
        self.original_stats_ready = monitor.state_store.stats_db_ready
        self.temp_dir = tempfile.TemporaryDirectory()
        # Point at a path that is never created, so any accidental DB access would
        # either fail loudly or silently create a file we can detect.
        monitor.state_store.CATEGORY_STATS_DB_PATH = str(Path(self.temp_dir.name) / "unused.sqlite3")
        monitor.state_store.stats_db_ready = False

    async def asyncTearDown(self):
        monitor.state_store.CATEGORY_STATS_DB_PATH = self.original_db_path
        monitor.state_store.stats_db_ready = self.original_stats_ready
        self.temp_dir.cleanup()

    def test_persist_operational_state_short_circuits(self):
        self.assertFalse(monitor.state_store.persist_operational_state("key", "value"))
        self.assertFalse(Path(monitor.state_store.CATEGORY_STATS_DB_PATH).exists())

    def test_load_operational_state_short_circuits(self):
        self.assertIsNone(monitor.state_store.load_operational_state("key"))

    def test_persist_trigger_observation_short_circuits(self):
        self.assertFalse(
            monitor.state_store.persist_trigger_observation(True, 1, datetime.now(timezone.utc))
        )

    def test_alert_feed_cursor_helpers_short_circuit(self):
        self.assertEqual(monitor.state_store.get_alert_feed_cursor("kyiv_alerts"), 0)
        self.assertFalse(monitor.state_store.set_alert_feed_cursor("kyiv_alerts", 42))

    def test_claim_alert_feed_delivery_fails_open_when_db_unavailable(self):
        # Without durable dedup, the caller must be allowed to proceed rather than
        # silently drop a tactical alert.
        self.assertTrue(monitor.state_store.claim_alert_feed_delivery("kyiv_alerts", 1, "text"))
        self.assertTrue(monitor.state_store.claim_alert_feed_delivery("kyiv_alerts", 1, "text"))

    def test_release_alert_feed_delivery_is_a_safe_noop(self):
        self.assertIsNone(monitor.state_store.release_alert_feed_delivery("kyiv_alerts", 1, "text"))

    def test_is_stale_alert_feed_message_defaults_to_not_stale(self):
        self.assertFalse(monitor.state_store.is_stale_alert_feed_message("kyiv_alerts", 999))

    def test_persist_normal_message_short_circuits(self):
        self.assertFalse(
            monitor.state_store.persist_normal_message(
                "channel", 1, datetime.now(timezone.utc), "text"
            )
        )

    def test_get_source_cursor_short_circuits(self):
        self.assertIsNone(monitor.state_store.get_source_cursor("channel"))

    def test_persist_history_batch_short_circuits(self):
        self.assertFalse(monitor.state_store.persist_history_batch("channel", [], 5))

    def test_load_pending_normal_messages_short_circuits(self):
        self.assertEqual(monitor.state_store.load_pending_normal_messages(), [])

    def test_mark_normal_messages_processed_is_a_safe_noop(self):
        # Must not raise even though there is nothing to write to.
        self.assertIsNone(monitor.state_store.mark_normal_messages_processed([("channel", 1)]))

    def test_discard_pending_normal_messages_is_a_safe_noop(self):
        self.assertIsNone(monitor.state_store.discard_pending_normal_messages("unit-test"))

    def test_persist_category_stats_is_a_safe_noop(self):
        self.assertIsNone(
            monitor.state_store.persist_category_stats(
                "2026-01-01T00:00:00+00:00", [], {}, monitor.ALL_CONTENT_CHANNELS
            )
        )

    def test_initialize_stats_db_still_works_when_flag_is_false(self):
        # The flag only gates the helpers above; initialize_stats_db itself does the
        # actual bootstrap and should still succeed independently of it.
        self.assertTrue(monitor.state_store.initialize_stats_db())
        self.assertTrue(Path(monitor.state_store.CATEGORY_STATS_DB_PATH).exists())


class PersistenceEdgeCaseTests(unittest.IsolatedAsyncioTestCase):
    """Deeper coverage of the sqlite-backed persistence layer: restarts, malformed
    state, concurrent claims, and retention cleanup."""

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = monitor.state_store.CATEGORY_STATS_DB_PATH
        self.original_stats_ready = monitor.state_store.stats_db_ready
        monitor.state_store.CATEGORY_STATS_DB_PATH = str(Path(self.temp_dir.name) / "monitor.sqlite3")
        monitor.state_store.stats_db_ready = monitor.state_store.initialize_stats_db()

    async def asyncTearDown(self):
        monitor.state_store.CATEGORY_STATS_DB_PATH = self.original_db_path
        monitor.state_store.stats_db_ready = self.original_stats_ready
        self.temp_dir.cleanup()

    # -- operational state / cursors surviving a simulated restart -----------------

    def test_operational_state_survives_simulated_restart(self):
        self.assertTrue(monitor.state_store.persist_operational_state("some_key", "some_value"))

        # Simulate a process restart by opening a brand new sqlite3 connection to the
        # same file path, independent of anything monitor.py has cached in memory.
        with sqlite3.connect(monitor.state_store.CATEGORY_STATS_DB_PATH) as fresh_connection:
            row = fresh_connection.execute(
                "SELECT value FROM operational_state WHERE key = ?", ("some_key",)
            ).fetchone()
        self.assertEqual(row[0], "some_value")

        # And the normal read path picks it back up too.
        self.assertEqual(monitor.state_store.load_operational_state("some_key"), "some_value")

    def test_alert_feed_cursor_survives_simulated_restart(self):
        self.assertTrue(monitor.state_store.set_alert_feed_cursor("kyiv_alerts", 555))

        with sqlite3.connect(monitor.state_store.CATEGORY_STATS_DB_PATH) as fresh_connection:
            row = fresh_connection.execute(
                "SELECT value FROM operational_state WHERE key = ?",
                (monitor.state_store.alert_feed_cursor_key("kyiv_alerts"),),
            ).fetchone()
        self.assertEqual(row[0], "555")
        self.assertEqual(monitor.state_store.get_alert_feed_cursor("kyiv_alerts"), 555)

    # -- malformed / missing operational_state values ------------------------------

    def test_get_alert_feed_cursor_handles_missing_value(self):
        self.assertEqual(monitor.state_store.get_alert_feed_cursor("never-seen-channel"), 0)

    def test_get_alert_feed_cursor_handles_non_integer_stored_value(self):
        monitor.state_store.persist_operational_state(
            monitor.state_store.alert_feed_cursor_key("kyiv_alerts"), "not-an-int"
        )
        # Must not raise; falls back to the safe default of 0.
        self.assertEqual(monitor.state_store.get_alert_feed_cursor("kyiv_alerts"), 0)

    def test_is_stale_alert_feed_message_with_malformed_cursor(self):
        monitor.state_store.persist_operational_state(
            monitor.state_store.alert_feed_cursor_key("kyiv_alerts"), "garbage"
        )
        # A malformed cursor is treated as 0 (missing), so nothing is considered stale.
        self.assertFalse(monitor.state_store.is_stale_alert_feed_message("kyiv_alerts", 1))

    # -- claim/release: atomic reservation ------------------------------------------

    def test_claim_alert_feed_delivery_only_one_concurrent_caller_wins(self):
        text = "Повітряна тривога в Києві"
        worker_count = 5
        barrier = threading.Barrier(worker_count)

        def worker():
            # Block every thread here so they all call claim_alert_feed_delivery as
            # close to simultaneously as possible, instead of one-after-another.
            barrier.wait(timeout=5)
            return monitor.state_store.claim_alert_feed_delivery("kyiv_alerts", 42, text)

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            results = list(pool.map(lambda _: worker(), range(worker_count)))

        # Exactly one of five truly concurrent claims for the same message/version wins;
        # sqlite's own locking (not application logic) is what must make this atomic.
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 4)

    def test_release_allows_retry_after_failed_claim(self):
        text = "Загроза застосування балістичної зброї"
        self.assertTrue(monitor.state_store.claim_alert_feed_delivery("kyiv_alerts", 7, text))
        self.assertFalse(monitor.state_store.claim_alert_feed_delivery("kyiv_alerts", 7, text))

        monitor.state_store.release_alert_feed_delivery("kyiv_alerts", 7, text)

        # After release, the same version can be claimed again (i.e. delivery retry
        # after a failed send is possible).
        self.assertTrue(monitor.state_store.claim_alert_feed_delivery("kyiv_alerts", 7, text))

    def test_release_of_unclaimed_delivery_is_a_harmless_noop(self):
        # Releasing something that was never claimed must not raise or corrupt state.
        monitor.state_store.release_alert_feed_delivery("kyiv_alerts", 999, "never claimed")
        self.assertTrue(monitor.state_store.claim_alert_feed_delivery("kyiv_alerts", 999, "never claimed"))

    def test_is_stale_alert_feed_message_reflects_cursor(self):
        monitor.state_store.set_alert_feed_cursor("kyiv_alerts", 100)
        self.assertTrue(monitor.state_store.is_stale_alert_feed_message("kyiv_alerts", 99))
        self.assertTrue(monitor.state_store.is_stale_alert_feed_message("kyiv_alerts", 100))
        self.assertFalse(monitor.state_store.is_stale_alert_feed_message("kyiv_alerts", 101))

    # -- mark_normal_messages_processed: exact-key targeting + retention -----------

    def test_mark_normal_messages_processed_only_marks_exact_keys(self):
        now = datetime.now(timezone.utc)
        monitor.state_store.persist_normal_message("chan_a", 1, now, "first message body text")
        monitor.state_store.persist_normal_message("chan_a", 2, now, "second message body text")
        monitor.state_store.persist_normal_message("chan_b", 1, now, "other channel message text")

        monitor.state_store.mark_normal_messages_processed([("chan_a", 1)])

        with sqlite3.connect(monitor.state_store.CATEGORY_STATS_DB_PATH) as connection:
            rows = dict(
                (f"{channel}:{message_id}", status)
                for channel, message_id, status in connection.execute(
                    "SELECT channel, message_id, status FROM normal_messages"
                ).fetchall()
            )
        self.assertEqual(rows["chan_a:1"], "processed")
        self.assertEqual(rows["chan_a:2"], "pending")
        self.assertEqual(rows["chan_b:1"], "pending")

        # The still-pending rows remain visible to the normal queue reader.
        pending_keys = {
            (row["channel"], row["message_id"])
            for row in monitor.state_store.load_pending_normal_messages()
        }
        self.assertEqual(pending_keys, {("chan_a", 2), ("chan_b", 1)})

    def test_mark_normal_messages_processed_ignores_unknown_keys(self):
        now = datetime.now(timezone.utc)
        monitor.state_store.persist_normal_message("chan_a", 1, now, "known message body text")

        # A key that does not exist in the table must not raise or affect anything.
        monitor.state_store.mark_normal_messages_processed([("chan_a", 999), ("chan_z", 1)])

        self.assertEqual(len(monitor.state_store.load_pending_normal_messages()), 1)

    def test_mark_normal_messages_processed_with_empty_list_is_noop(self):
        now = datetime.now(timezone.utc)
        monitor.state_store.persist_normal_message("chan_a", 1, now, "message body text here")
        monitor.state_store.mark_normal_messages_processed([])
        self.assertEqual(len(monitor.state_store.load_pending_normal_messages()), 1)

    def test_mark_normal_messages_processed_deletes_rows_past_retention(self):
        old_processed_at = monitor.utc_iso(
            datetime.now(timezone.utc)
            - timedelta(days=monitor.state_store.NORMAL_MESSAGE_RETENTION_DAYS + 1)
        )
        recent_processed_at = monitor.utc_iso(datetime.now(timezone.utc))

        with sqlite3.connect(monitor.state_store.CATEGORY_STATS_DB_PATH) as connection:
            connection.execute(
                """INSERT INTO normal_messages
                   (channel, message_id, message_at, text, status, processed_at)
                   VALUES (?, ?, ?, ?, 'processed', ?)""",
                ("chan_old", 1, old_processed_at, "an old already-processed message", old_processed_at),
            )
            connection.execute(
                """INSERT INTO normal_messages
                   (channel, message_id, message_at, text, status, processed_at)
                   VALUES (?, ?, ?, ?, 'processed', ?)""",
                ("chan_recent", 2, recent_processed_at, "a recently processed message", recent_processed_at),
            )
            connection.execute(
                """INSERT INTO normal_messages
                   (channel, message_id, message_at, text, status)
                   VALUES (?, ?, ?, ?, 'pending')""",
                ("chan_pending", 3, recent_processed_at, "a still-pending message"),
            )

        # Any call to mark_normal_messages_processed runs the retention sweep, even
        # when the keys passed in don't relate to the old rows.
        monitor.state_store.mark_normal_messages_processed([("chan_pending", 3)])

        with sqlite3.connect(monitor.state_store.CATEGORY_STATS_DB_PATH) as connection:
            remaining = {
                (channel, message_id)
                for channel, message_id in connection.execute(
                    "SELECT channel, message_id FROM normal_messages"
                ).fetchall()
            }
        self.assertNotIn(("chan_old", 1), remaining)
        self.assertIn(("chan_recent", 2), remaining)
        self.assertIn(("chan_pending", 3), remaining)

    # -- discard_pending_normal_messages ---------------------------------------------

    def test_discard_pending_normal_messages_leaves_processed_rows_untouched(self):
        now = datetime.now(timezone.utc)
        monitor.state_store.persist_normal_message("chan_a", 1, now, "message that will be discarded")
        monitor.state_store.persist_normal_message("chan_a", 2, now, "message that gets processed")
        monitor.state_store.mark_normal_messages_processed([("chan_a", 2)])

        monitor.state_store.discard_pending_normal_messages("unit-test")

        with sqlite3.connect(monitor.state_store.CATEGORY_STATS_DB_PATH) as connection:
            statuses = dict(
                connection.execute(
                    "SELECT message_id, status FROM normal_messages WHERE channel = 'chan_a'"
                ).fetchall()
            )
        self.assertEqual(statuses[1], "discarded")
        self.assertEqual(statuses[2], "processed")
        self.assertEqual(monitor.state_store.load_pending_normal_messages(), [])

    # -- persist_category_stats -------------------------------------------------------

    def test_persist_category_stats_writes_rows_for_each_channel(self):
        run_at = monitor.utc_iso()
        channel = monitor.ALL_CONTENT_CHANNELS[0]
        snapshots = [{"id": "a:1", "channel": channel, "text": "some classified text"}]
        category_key = next(iter(monitor.CATEGORIES))
        category_results = {
            key: {"selected_ids": [], "items": []} for key in monitor.CATEGORIES
        }
        category_results[category_key] = {
            "selected_ids": ["a:1"],
            "items": [{"event_key": "test|event", "text": "x", "is_update": False}],
        }

        monitor.state_store.persist_category_stats(
            run_at, snapshots, category_results, monitor.ALL_CONTENT_CHANNELS
        )

        with sqlite3.connect(monitor.state_store.CATEGORY_STATS_DB_PATH) as connection:
            valid = connection.execute(
                """SELECT valid FROM hourly_category_stats
                   WHERE run_at = ? AND category = ? AND channel = ?""",
                (run_at, category_key, channel),
            ).fetchone()[0]
            classification = connection.execute(
                """SELECT preview FROM hourly_classifications
                   WHERE run_at = ? AND message_id = ? AND category = ?""",
                (run_at, "a:1", category_key),
            ).fetchone()
        self.assertEqual(valid, 1)
        self.assertEqual(classification[0], "some classified text")
        recent = monitor.state_store.load_recent_published_classifications()
        self.assertEqual(recent[0]["category"], category_key)
        self.assertEqual(recent[0]["preview"], "some classified text")

    # -- acquire_telethon_session_lock -------------------------------------------------

    def test_telethon_session_lock_blocks_second_non_blocking_acquire(self):
        path = str(Path(self.temp_dir.name) / "telethon.lock")
        holder = monitor.state_store.acquire_telethon_session_lock(path)
        try:
            with self.assertRaises(BlockingIOError):
                monitor.state_store.acquire_telethon_session_lock(path, blocking=False)
        finally:
            holder.close()

        # Once released, a fresh non-blocking acquire succeeds immediately.
        second_holder = monitor.state_store.acquire_telethon_session_lock(path, blocking=False)
        second_holder.close()

    def test_telethon_session_lock_creates_parent_directory(self):
        path = str(Path(self.temp_dir.name) / "nested" / "dir" / "telethon.lock")
        holder = monitor.state_store.acquire_telethon_session_lock(path, blocking=False)
        try:
            self.assertTrue(Path(path).exists())
        finally:
            holder.close()


if __name__ == "__main__":
    unittest.main()
