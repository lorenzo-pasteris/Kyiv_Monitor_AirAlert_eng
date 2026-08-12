import asyncio
import unittest

from test_routing import monitor


class AlertStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_send_alert = monitor.send_to_alert_channel
        self.original_send_owner = monitor.send_to_owner
        self.original_stats_ready = monitor.stats_db_ready
        self.original_client = monitor.production_client
        self.original_entities = monitor.content_source_entities
        monitor.alert_transition_lock = asyncio.Lock()
        monitor.alert_active = False
        monitor.alert_started_at = None
        monitor.alert_generation = 0
        monitor.telegram_alert_state = None
        monitor.stats_db_ready = False
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
        monitor.stats_db_ready = self.original_stats_ready
        monitor.production_client = self.original_client
        monitor.content_source_entities = self.original_entities

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


if __name__ == "__main__":
    unittest.main()
