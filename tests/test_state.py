import asyncio
import sqlite3
import tempfile
import threading
import unittest

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
                key: {"selected_ids": [], "bullets": []}
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
        parsed["categories"]["kyiv_region"]["bullets"] = bullets * 4

        normalized = monitor.normalize_category_result(parsed, [])

        self.assertEqual(len(normalized["kyiv_region"]["bullets"]), 3)
        self.assertEqual(normalized["kyiv_region"]["bullets"][:2], bullets)

    def test_normalizer_rejects_one_message_in_multiple_categories(self):
        message_id = "source:1"
        parsed = {
            "categories": {
                key: {"selected_ids": [], "bullets": []}
                for key in monitor.CATEGORIES
            }
        }
        parsed["categories"]["ukraine_key_developments"]["selected_ids"] = [message_id]
        parsed["categories"]["kyiv_region"]["selected_ids"] = [message_id]

        with self.assertRaisesRegex(ValueError, "multiple categories"):
            monitor.normalize_category_result(parsed, [{"id": message_id}])

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

    async def test_unknown_startup_state_is_rejected(self):
        self.assertFalse(await monitor.apply_alert_state(None, "startup-test", startup=True))
        self.assertFalse(monitor.alert_active)

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
            key: {"selected_ids": [], "bullets": []} for key in monitor.CATEGORIES
        }
        category_results[category_key] = {"selected_ids": ["a:1"], "bullets": ["x"]}

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
