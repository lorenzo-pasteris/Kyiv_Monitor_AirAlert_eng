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
        "OWNER_CHAT_ID": "392256147",
        "ADMIN_USER_IDS": "392256147",
        "TEST_MODE": "false",
    })
    os.environ.pop("UKRAINE_ALARM_API_KEY", None)

    httpx_stub = types.ModuleType("httpx")
    telethon_stub = types.ModuleType("telethon")
    telethon_sessions_stub = types.ModuleType("telethon.sessions")
    telethon_tl_stub = types.ModuleType("telethon.tl")
    telethon_functions_stub = types.ModuleType("telethon.tl.functions")
    telethon_channels_stub = types.ModuleType("telethon.tl.functions.channels")
    telethon_channels_stub.JoinChannelRequest = lambda entity: entity
    telethon_stub.TelegramClient = object
    telethon_stub.events = types.SimpleNamespace()
    telethon_stub.utils = types.SimpleNamespace()
    telethon_sessions_stub.StringSession = object
    telethon_errors_stub = types.ModuleType("telethon.errors")
    telethon_errors_stub.AuthKeyDuplicatedError = type("AuthKeyDuplicatedError", (Exception,), {})
    telethon_stub.errors = telethon_errors_stub
    sys.modules.setdefault("httpx", httpx_stub)
    sys.modules.setdefault("telethon", telethon_stub)
    sys.modules.setdefault("telethon.errors", telethon_errors_stub)
    sys.modules.setdefault("telethon.sessions", telethon_sessions_stub)
    sys.modules.setdefault("telethon.tl", telethon_tl_stub)
    sys.modules.setdefault("telethon.tl.functions", telethon_functions_stub)
    sys.modules.setdefault("telethon.tl.functions.channels", telethon_channels_stub)

    path = Path(__file__).parents[1] / "monitor.py"
    spec = importlib.util.spec_from_file_location("monitor_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


monitor = load_monitor()


class FakeTelegramMessage:
    def __init__(self, message_id, text, date, edit_date=None, photo=None):
        self.id = message_id
        self.text = text
        self.date = date
        self.edit_date = edit_date
        self.photo = photo


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
        self.original_db_path = monitor.state_store.CATEGORY_STATS_DB_PATH
        self.original_stats_ready = monitor.state_store.stats_db_ready
        self.original_client = monitor.production_client
        self.original_entities = monitor.content_source_entities
        self.original_analyzer = monitor.analyze_hourly_matrix
        self.original_summary_sender = monitor.send_to_summary_group
        self.original_owner_sender = monitor.send_to_owner
        monitor.state_store.CATEGORY_STATS_DB_PATH = str(Path(self.temp_dir.name) / "monitor.sqlite3")
        monitor.state_store.stats_db_ready = monitor.state_store.initialize_stats_db()
        monitor.production_client = None
        monitor.content_source_entities = {}
        monitor.summary_lock = asyncio.Lock()

    async def asyncTearDown(self):
        monitor.state_store.CATEGORY_STATS_DB_PATH = self.original_db_path
        monitor.state_store.stats_db_ready = self.original_stats_ready
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

        self.assertTrue(monitor.state_store.persist_normal_message(
            monitor.KYIV_INFO_CHANNEL, 102, messages[1].date, messages[1].text
        ))
        self.assertTrue(await monitor.sync_normal_history(client, entities))

        pending = monitor.state_store.load_pending_normal_messages()
        self.assertEqual([row["message_id"] for row in pending], [101, 102])
        self.assertEqual(monitor.state_store.get_source_cursor(monitor.KYIV_INFO_CHANNEL), 102)

        messages.append(FakeTelegramMessage(
            103, "A new road closure was announced in Kyiv.", now
        ))
        self.assertTrue(await monitor.sync_normal_history(client, entities))
        self.assertEqual(
            [row["message_id"] for row in monitor.state_store.load_pending_normal_messages()],
            [101, 102, 103],
        )
        self.assertEqual(monitor.state_store.get_source_cursor(monitor.KYIV_INFO_CHANNEL), 103)

    async def test_bootstrap_advances_cursor_when_all_messages_are_old(self):
        old = datetime.now(timezone.utc) - timedelta(hours=3)
        channel = monitor.KYIV_INFO_CHANNEL
        entities = {name: name for name in monitor.ALL_CONTENT_CHANNELS}
        client = FakeHistoryClient({name: [] for name in monitor.ALL_CONTENT_CHANNELS})
        client.messages_by_channel[channel] = [
            FakeTelegramMessage(901, "Old but valid Kyiv message.", old)
        ]

        self.assertTrue(await monitor.sync_normal_history(client, entities))
        self.assertEqual(monitor.state_store.load_pending_normal_messages(), [])
        self.assertEqual(monitor.state_store.get_source_cursor(channel), 901)

    async def test_failed_delivery_retains_pending_and_success_marks_processed(self):
        channel = monitor.KYIV_INFO_CHANNEL
        message_id = 501
        monitor.state_store.persist_normal_message(
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
            result["kyiv_region"] = {
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
        self.assertEqual(len(monitor.state_store.load_pending_normal_messages()), 1)

        self.assertTrue(await monitor.build_summary(trigger="success-test"))
        self.assertEqual(monitor.state_store.load_pending_normal_messages(), [])
        with sqlite3.connect(monitor.state_store.CATEGORY_STATS_DB_PATH) as connection:
            status = connection.execute(
                "SELECT status FROM normal_messages WHERE channel = ? AND message_id = ?",
                (channel, message_id),
            ).fetchone()[0]
        self.assertEqual(status, "processed")

    async def test_alert_discards_pending_and_clear_advances_all_cursors(self):
        now = datetime.now(timezone.utc)
        monitor.state_store.persist_normal_message(
            monitor.KYIV_INFO_CHANNEL,
            700,
            now,
            "A pending NORMAL message that must not cross into ALERT.",
        )
        monitor.state_store.discard_pending_normal_messages("unit-test-alert")
        self.assertEqual(monitor.state_store.load_pending_normal_messages(), [])

        entities = {channel: channel for channel in monitor.ALL_CONTENT_CHANNELS}
        messages_by_channel = {
            channel: [FakeTelegramMessage(800 + index, "latest source message", now)]
            for index, channel in enumerate(monitor.ALL_CONTENT_CHANNELS)
        }
        client = FakeHistoryClient(messages_by_channel)
        await monitor.advance_normal_cursors_to_latest(client, entities, "unit-test-clear")

        self.assertEqual(
            [monitor.state_store.get_source_cursor(channel) for channel in monitor.ALL_CONTENT_CHANNELS],
            [800 + index for index, _ in enumerate(monitor.ALL_CONTENT_CHANNELS)],
        )

    async def test_trigger_observation_is_persisted_as_one_state_snapshot(self):
        observed_at = datetime.now(timezone.utc)
        self.assertTrue(monitor.state_store.persist_trigger_observation(True, 991, observed_at))
        self.assertEqual(monitor.state_store.load_operational_state("telegram_alert_state"), "1")
        self.assertEqual(monitor.state_store.load_operational_state("telegram_alert_message_id"), "991")
        self.assertEqual(
            monitor.state_store.load_operational_state("telegram_alert_message_at"),
            monitor.utc_iso(observed_at),
        )

    async def test_alert_delivery_claim_survives_restart_and_releases_on_failure(self):
        text = "Балістична загроза зі сходу"
        self.assertTrue(monitor.state_store.claim_alert_feed_delivery("kyiv_alerts", 123, text))
        self.assertFalse(monitor.state_store.claim_alert_feed_delivery("kyiv_alerts", 123, text))
        monitor.state_store.release_alert_feed_delivery("kyiv_alerts", 123, text)
        self.assertTrue(monitor.state_store.claim_alert_feed_delivery("kyiv_alerts", 123, text))
        self.assertTrue(monitor.state_store.claim_alert_feed_delivery("kyiv_alerts", 123, text + "! Нова інформація"))

    async def test_telethon_session_lock_allows_only_one_worker(self):
        path = str(Path(self.temp_dir.name) / "telethon.lock")
        first = monitor.state_store.acquire_telethon_session_lock(path)
        try:
            with self.assertRaises(BlockingIOError):
                monitor.state_store.acquire_telethon_session_lock(path, blocking=False)
        finally:
            first.close()
        replacement = monitor.state_store.acquire_telethon_session_lock(path, blocking=False)
        replacement.close()


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_ukraine_alarm_parser_selects_kyiv_city_air_only(self):
        response = [
            {
                "regionName": "Київська область",
                "regionEngName": "Kyiv region",
                "activeAlerts": [{"type": "AIR"}],
            },
            {
                "regionName": "м. Київ",
                "regionEngName": "Kyiv",
                "activeAlerts": [{"type": "INFO"}],
            },
        ]
        self.assertFalse(monitor.parse_ukraine_alarm_kyiv_state(response))
        response[1]["activeAlerts"].append({"type": "AIR"})
        self.assertTrue(monitor.parse_ukraine_alarm_kyiv_state(response))

        with self.assertRaisesRegex(ValueError, "Kyiv City"):
            monitor.parse_ukraine_alarm_kyiv_state(response[:1])

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
        for gratitude in (
            "Оболонь, подякував❤️",
            "Оболонь, дякую ❤️",
            "Оболонь, спасибо ❤️",
            "Obolon, thank you ❤️",
            "Obolon, thanks ❤️",
            "Всіх бачу, найкращі підписники❤️🥹",
            "I can see everyone, best subscribers❤️🥹",
        ):
            self.assertTrue(monitor.is_non_operational_alert_message(gratitude))
        self.assertFalse(monitor.is_non_operational_alert_message(operational))
        self.assertFalse(monitor.is_pure_ad(donation))  # security words no longer bypass the ALERT-only filter
        self.assertFalse(monitor.is_actionable_alert_message(unrelated))
        self.assertTrue(monitor.is_actionable_alert_message(terse_follow_up))

    async def test_commentary_filter_hides_opinion_in_public_and_sends_it_to_ops(self):
        commentary = (
            "If anything, the all clear should have been given half an hour ago. "
            "I don't know why they're keeping the alert on. Maybe the guy on the button fell asleep."
        )
        rhetorical = (
            "Минуло більше години, біля Києва чисто, то чому не можуть дати відбій?"
        )
        operational = "2 БПЛА біля Броварів, працює ППО"
        shelter_commentary = "Ще може бути залп, всі спустились на перший поверх, на вулицю не йдемо"
        kellogg_commentary = "Келлог менш ефективний ніж Петя, всю балістику пропускає"
        engagement_commentary = "У Києві є важко поранені, віримо в медиків"
        glossary_commentary = "Бандероль - крилата ракета"
        hope_commentary = "Сподіваємось більше нічого не прилетить і буде відбій"
        camera_speculation = "Ці просто кружляють над Києвом. Не здивуюсь, якщо на них камери"
        petya_commentary = "Нарешті у Києві вперше за 2 місяці Петя відпрацював на повну"
        debris_warning = "ППО працює добре, але уламки ніхто не скасовував, тому на вулицю не виходимо"

        self.assertTrue(monitor.is_commentary_alert_message(commentary))
        self.assertTrue(monitor.is_commentary_alert_message(rhetorical))
        self.assertTrue(monitor.is_commentary_alert_message(shelter_commentary))
        self.assertTrue(monitor.is_commentary_alert_message(kellogg_commentary))
        self.assertTrue(monitor.is_commentary_alert_message(engagement_commentary))
        self.assertTrue(monitor.is_commentary_alert_message(glossary_commentary))
        self.assertTrue(monitor.is_commentary_alert_message(hope_commentary))
        self.assertTrue(monitor.is_commentary_alert_message(camera_speculation))
        self.assertTrue(monitor.is_commentary_alert_message(petya_commentary))
        self.assertFalse(monitor.is_commentary_alert_message(debris_warning))
        self.assertFalse(monitor.is_commentary_alert_message(operational))
        self.assertEqual(
            monitor.strip_mixed_alert_commentary(
                "Літають як у себе вдома. Русанівка, Троєщина, Дарниця"
            ),
            "Русанівка, Троєщина, Дарниця",
        )

        original_active = monitor.alert_active
        try:
            monitor.alert_active = True
            self.assertTrue(await monitor.handle_alert_message(commentary))
        finally:
            monitor.alert_active = original_active

        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], monitor.OPS_CHAT_ID)
        self.assertIn("<b>COMMENTO</b>", self.sent[0][1])
        self.assertNotEqual(self.sent[0][0], monitor.ALERT_OUTPUT_CHAT_ID)

    async def test_alert_source_promotional_footer_is_removed(self):
        raw = (
            "🔴 Ballistic ballistics from the east\n"
            "💙 Dnipro Alerts • 💛 Kyiv Alerts\n"
            "ℹ️ Alerts Live • 🤙 Feedback\n\nㅤ\n"
            "Надіслати новину @novosti_kieva_bot\n👉ПІДПИСАТИСЯ"
        )
        self.assertEqual(
            monitor.clean_alert_source_text(raw),
            "🔴 Ballistic ballistics from the east",
        )

    async def test_alert_source_keeps_linked_tactical_text(self):
        raw = (
            "Вишгород жовтогарячий 🟧, дорозвідка.\n\n"
            "[Троєщина](https://t.me/kyiv_alerts) та Бровари червоний 🟥."
        )
        self.assertEqual(
            monitor.clean_alert_source_text(raw),
            "Вишгород жовтогарячий 🟧, дорозвідка.\nТроєщина та Бровари червоний 🟥.",
        )

    async def test_kyiv_nebo_monitoring_is_the_only_alert_feed(self):
        self.assertEqual(monitor.ALERT_FEED_CHANNELS, ["kyivnebomonitoring"])
        self.assertNotIn("kyivnebomonitoring", monitor.ALL_CONTENT_CHANNELS)
        self.assertNotIn("KyivDeZagrozaCHAT", monitor.ALERT_FEED_CHANNELS)
        self.assertNotIn("kyiv_alerts", monitor.ALL_CONTENT_CHANNELS)
        self.assertNotIn("kyiv_alerts", monitor.ALERT_FEED_CHANNELS)
        self.assertNotIn("kievreal1", monitor.ALERT_FEED_CHANNELS)
        self.assertNotIn("nebo_raketa", monitor.ALERT_FEED_CHANNELS)
        self.assertNotIn("Nashee_PPO", monitor.ALL_CONTENT_CHANNELS)

    async def test_insider_ua_is_an_hourly_content_source_only(self):
        self.assertIn("insiderukr", monitor.ALL_CONTENT_CHANNELS)
        self.assertNotIn("insiderukr", monitor.ALERT_FEED_CHANNELS)

    async def test_selected_official_sources_are_content_and_war_monitor_is_special(self):
        self.assertNotIn("agentstvonews", monitor.ALL_CONTENT_CHANNELS)
        for channel in ("KyivCityOfficial", "suspilne_kyiv", "ukrenergo", "UkrzalInfo"):
            self.assertIn(channel, monitor.ALL_CONTENT_CHANNELS)
        self.assertNotIn("war_monitor", monitor.ALL_CONTENT_CHANNELS)
        self.assertNotIn("war_monitor", monitor.ALERT_FEED_CHANNELS)

    async def test_war_monitor_accepts_only_todays_tagged_daily_report(self):
        today = datetime(2026, 8, 31).date()
        valid = (
            "📡 Обстановка станом на 00:00\n"
            "31.08.26\n\n— Стратегічна авіація:\nНе активна;\n\n"
            "#обстановка@war_monitor"
        )
        self.assertTrue(monitor.is_daily_war_monitor_report(valid, today))
        self.assertFalse(monitor.is_daily_war_monitor_report(valid.replace("31.08.26", "30.08.26"), today))
        self.assertFalse(monitor.is_daily_war_monitor_report(valid.replace("📡 Обстановка", "Оновлення"), today))
        self.assertFalse(monitor.is_daily_war_monitor_report(valid.replace("#обстановка@war_monitor", ""), today))

    async def test_summary_schedule_uses_requested_kyiv_hours(self):
        self.assertEqual(monitor.SUMMARY_HOURS, (1, 7, 9, 11, 13, 15, 17, 19, 21, 23))
        now = datetime(2026, 8, 31, 10, 30, tzinfo=monitor.TZ)
        self.assertEqual(monitor.seconds_until_next_summary(now), 0.5 * 3600)
        after_last = datetime(2026, 8, 31, 22, 30, tzinfo=monitor.TZ)
        self.assertEqual(monitor.seconds_until_next_summary(after_last), 0.5 * 3600)

    async def test_manual_override_admin_allowlist(self):
        self.assertTrue(monitor.is_authorized_admin(392256147))
        self.assertFalse(monitor.is_authorized_admin(999999999))

    async def test_alert_feed_real_message_shapes_are_actionable(self):
        # Terse course/direction updates observed on the operational alert feed.
        alert = "5 БПЛА з Чернігівщини на Київщину"
        movement = "Реактивний БпЛА курсом на Бровари"
        defence = "2 БПЛА біля Броварів, працює ППО"
        clear = "❎ М. КИЇВ - ВІДБІЙ ТРИВОГИ"
        terse_dymer = "Один в бік Димера через водосховище"
        terse_brovary = "3 підлітають до Броварів"
        terse_boryspil = "На Бориспіль один від Броварів"
        terse_rembaza = "На Рембазу!"
        terse_tec = "На ТЕЦ-6 знову"
        terse_oseshtyna = "На Осещину поворот"
        terse_direction = "На Тарасівку від Боярки"
        terse_direction_two = "Ще на Яготин з південного напрямку"
        # Real shapes observed on @kyiv_alerts.
        kyiv_alerts_alert = "тривога на сході області на мопеди. Поки без загроз."
        kyiv_alerts_clear = "Відбій балістичної загрози по всій Україні."
        ordinary_news = "В УЗ повідомили про затримки в русі низки приміських поїздів на Київщині"

        for message in (
            alert, movement, defence, clear, terse_dymer, terse_brovary,
            terse_boryspil, terse_rembaza, terse_tec, terse_oseshtyna,
            terse_direction, terse_direction_two,
            kyiv_alerts_alert, kyiv_alerts_clear,
        ):
            self.assertTrue(monitor.is_actionable_alert_message(message))
            self.assertFalse(monitor.is_non_operational_alert_message(message))
        self.assertFalse(monitor.is_actionable_alert_message(ordinary_news))

    async def test_translation_prompt_covers_observed_alert_jargon(self):
        source = "Без загроз по бандеролям, тривогу дали на реактивний в бік Броварів"
        prompt = monitor.build_alert_translation_prompt(source)
        self.assertIn("jet-powered UAV(s)", prompt)
        self.assertIn("NEVER cruise missile(s) unless", prompt)
        self.assertIn("no high-speed targets", prompt)
        self.assertIn("Бандероль = Banderol", prompt)
        self.assertIn("бандеролі = Banderols", prompt)
        self.assertIn("NEVER infer a weapon type", prompt)
        self.assertIn("No threat from Banderols", prompt)
        self.assertNotIn("Бандероль / бандеролі / бандеролям = cruise missile(s)", prompt)
        self.assertIn("never 'minus'", prompt)
        self.assertIn("never translate them literally as gifts or parcels", prompt)
        self.assertIn("never good or friendly drones", prompt)
        self.assertIn("air defence may become active", prompt)
        self.assertTrue(prompt.endswith(source))

    async def test_known_terse_alert_fragments_never_need_model_context(self):
        self.assertEqual(monitor.translate_known_terse_fragment("Дарниця"), "Darnytsia")
        self.assertEqual(monitor.translate_known_terse_fragment("ТЕЦ-5"), "CHP-5")
        self.assertEqual(monitor.translate_known_terse_fragment("Збили"), "Shot down")
        self.assertIsNone(
            monitor.translate_known_terse_fragment("По балістиці очікуємо на відбій")
        )
        self.assertEqual(
            monitor.translate_known_terse_fragment("Балістика на Київ!"),
            "Ballistic threat to Kyiv!",
        )
        self.assertEqual(await monitor.translate_message("Збили"), "Shot down")
        prompt = monitor.build_alert_translation_prompt("Дарниця")
        self.assertIn("Never ask for more context", prompt)
        self.assertIn("'Дарниця' = 'Darnytsia'", prompt)

    async def test_semantic_alert_gate_allows_only_evidenced_closed_category_drops(self):
        prompt = monitor.build_alert_translation_prompt(
            "Боярка, Вишневе",
            ["2 реактивних БпЛА наближаються до Київщини"],
        )
        self.assertIn("default decision is PUBLISH", prompt)
        self.assertIn("closed categories", prompt)
        self.assertIn("When uncertain, PUBLISH", prompt)
        self.assertIn("A bare Kyiv district or immediate-approach place is always tactical", prompt)
        self.assertIn("Always provide a valid English translation", prompt)
        self.assertIn("2 реактивних БпЛА наближаються до Київщини", prompt)
        self.assertTrue(prompt.endswith("Боярка, Вишневе"))

        self.assertEqual(
            monitor.parse_alert_gate_output(
                '{"decision":"DROP","block_category":"EDITORIAL_COMMENTARY",'
                '"translation":"Editorial complaint.","evidence":"чому","reason":"commentary"}',
                "чому досі тривога",
            ),
            ("DROP", "", "EDITORIAL_COMMENTARY"),
        )
        self.assertEqual(
            monitor.parse_alert_gate_output(
                '{"decision":"PUBLISH","block_category":null,'
                '"translation":"UAV heading toward Kyiv.","evidence":"","reason":"current_threat"}',
                "БпЛА на Київ",
            ),
            ("PUBLISH", "UAV heading toward Kyiv.", "current_threat"),
        )
        with self.assertRaises(ValueError):
            monitor.parse_alert_gate_output('{"translation":"Maybe publish this"}')

    async def test_operational_locations_and_unapproved_drops_publish(self):
        real_tactical_updates = (
            "Нивки",
            "Шулявка",
            "Жуляни",
            "Підлітають до Броварів",
            "Новий на Вишгород",
            "Далі Чабани",
            "Вилітає в район Василькова",
            "Жуляни, Вишневе",
            "Другий Нивки, Солома",
        )
        for text in real_tactical_updates:
            self.assertTrue(monitor.contains_operational_location(text), text)
            self.assertEqual(
                monitor.parse_alert_gate_output(
                    '{"decision":"DROP","block_category":"EDITORIAL_COMMENTARY",'
                    '"translation":"Operational location update.",'
                    '"evidence":"' + text + '","reason":"vague"}',
                    text,
                ),
                (
                    "PUBLISH",
                    "Operational location update.",
                    "override_unapproved_drop:EDITORIAL_COMMENTARY",
                ),
            )

        self.assertEqual(
            monitor.parse_alert_gate_output(
                '{"decision":"DROP","block_category":"VAGUE",'
                '"translation":"Four of them there.","evidence":"Їх там 4шт","reason":"ambiguous"}',
                "Їх там 4шт",
            ),
            ("PUBLISH", "Four of them there.", "override_unapproved_drop:VAGUE"),
        )

    async def test_distant_update_without_kyiv_trajectory_can_be_dropped(self):
        text = "Нові залітають на Чернігівщину"
        self.assertFalse(monitor.contains_operational_location(text))
        self.assertEqual(
            monitor.parse_alert_gate_output(
                '{"decision":"DROP","block_category":"DISTANT_WITHOUT_KYIV_TRAJECTORY",'
                '"translation":"New threats entering Chernihiv region.",'
                '"evidence":"Чернігівщину","reason":"distant"}',
                text,
            ),
            ("DROP", "", "DISTANT_WITHOUT_KYIV_TRAJECTORY"),
        )

    async def test_semantic_drop_is_checkpointed_instead_of_retried(self):
        original_active = monitor.alert_active
        original_translate = monitor.translate_message

        async def semantic_drop(text, context=()):
            return False

        try:
            monitor.alert_active = True
            monitor.translate_message = semantic_drop
            self.assertTrue(
                await monitor.handle_alert_message(
                    "2 нових на Чернігівщині",
                    generation=monitor.alert_generation,
                )
            )
        finally:
            monitor.alert_active = original_active
            monitor.translate_message = original_translate

    async def test_alert_source_context_resolves_terse_followups_and_expires(self):
        monitor.recent_alert_source_context.clear()
        start = datetime(2026, 8, 28, 2, 20, tzinfo=timezone.utc)
        self.assertEqual(
            monitor.remember_alert_source_message(
                "kyivnebomonitoring", 1, "2 реактивних на Київщині", start
            ),
            [],
        )
        self.assertEqual(
            monitor.remember_alert_source_message(
                "kyivnebomonitoring", 2, "Боярка, Вишневе", start + timedelta(minutes=1)
            ),
            ["2 реактивних на Київщині"],
        )
        self.assertEqual(
            monitor.remember_alert_source_message(
                "kyivnebomonitoring", 3, "Новий епізод", start + timedelta(minutes=11)
            ),
            ["Боярка, Вишневе"],
        )
        self.assertEqual(
            monitor.remember_alert_source_message(
                "kyivnebomonitoring", 3, "Новий епізод виправлено", start + timedelta(minutes=11)
            ),
            ["Боярка, Вишневе"],
        )
        monitor.recent_alert_source_context.clear()

    async def test_model_meta_commentary_can_never_be_published_as_translation(self):
        bad_outputs = (
            'No translation provided. The source message contains only "Darnytsia".',
            "I'm ready to translate, but the source message appears incomplete.",
            'I need the source message to translate. You\'ve provided only "Збили".',
            "Please provide the complete Ukrainian/Russian message.",
        )
        for output in bad_outputs:
            self.assertTrue(monitor.is_translation_meta_output(output))
        self.assertFalse(monitor.is_translation_meta_output("2 UAVs heading toward Brovary"))
        self.assertFalse(monitor.is_valid_alert_translation("Уся інформація з Київ Де Загроза"))
        self.assertTrue(monitor.is_valid_alert_translation("2 missiles heading toward Brovary"))

    async def test_alert_source_attribution_is_removed(self):
        self.assertEqual(
            monitor.clean_alert_source_text(
                "2 ракети\nУся інформація з Київ Де Загроза"
            ),
            "2 ракети",
        )

    async def test_alert_feed_publishes_operational_posts_without_keyword_allowlist(self):
        channel = monitor.ALERT_FEED_CHANNEL
        now = datetime.now(timezone.utc)
        original_active = monitor.alert_active
        original_get_cursor = monitor.state_store.get_alert_feed_cursor
        original_set_cursor = monitor.state_store.set_alert_feed_cursor
        original_claim = monitor.state_store.claim_alert_feed_delivery
        original_schedule = monitor.schedule_alert_delivery
        delivered = []

        async def fake_delivery(text, source):
            delivered.append((text, source))
            return True

        try:
            monitor.alert_active = True
            monitor.state_store.get_alert_feed_cursor = lambda name: 0
            monitor.state_store.set_alert_feed_cursor = lambda name, value: True
            monitor.state_store.claim_alert_feed_delivery = lambda name, message_id, text: True
            monitor.schedule_alert_delivery = lambda text, source, context=(): asyncio.create_task(
                fake_delivery(text, source)
            )
            monitor.recent_alert_messages.clear()

            bomber_count = FakeTelegramMessage(
                20001, "В повітрі 7 бортів Ту-95мс та 2 борти Ту-160", now
            )
            missile_estimate = FakeTelegramMessage(
                20002, "Розвідка пише, що буде ~45 крилатих ракет", now
            )

            self.assertTrue(await monitor.process_alert_feed_message(bomber_count, channel))
            self.assertTrue(await monitor.process_alert_feed_message(missile_estimate, channel))
        finally:
            monitor.alert_active = original_active
            monitor.state_store.get_alert_feed_cursor = original_get_cursor
            monitor.state_store.set_alert_feed_cursor = original_set_cursor
            monitor.state_store.claim_alert_feed_delivery = original_claim
            monitor.schedule_alert_delivery = original_schedule
            monitor.recent_alert_messages.clear()

        self.assertEqual(
            delivered,
            [
                ("В повітрі 7 бортів Ту-95мс та 2 борти Ту-160", channel),
                ("Розвідка пише, що буде ~45 крилатих ракет", channel),
            ],
        )

    async def test_alert_image_is_filtered_in_background(self):
        channel = monitor.ALERT_FEED_CHANNEL
        gate = asyncio.Event()
        cursors = {channel: 30000}
        original_image_check = monitor.is_blocked_alert_image
        original_get_cursor = monitor.state_store.get_alert_feed_cursor
        original_set_cursor = monitor.state_store.set_alert_feed_cursor

        async def blocked_after_delay(message):
            await gate.wait()
            return True

        try:
            monitor.is_blocked_alert_image = blocked_after_delay
            monitor.state_store.get_alert_feed_cursor = lambda name: cursors[name]
            monitor.state_store.set_alert_feed_cursor = lambda name, value: cursors.__setitem__(name, value) or True
            message = FakeTelegramMessage(
                30001, "Оперативне оновлення", datetime.now(timezone.utc), photo=object()
            )
            task = monitor.schedule_alert_image_processing(
                message, channel, message.text, False
            )
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            cursors[channel] = 30002
            gate.set()
            self.assertFalse(await task)
            self.assertEqual(cursors[channel], 30002)
        finally:
            monitor.is_blocked_alert_image = original_image_check
            monitor.state_store.get_alert_feed_cursor = original_get_cursor
            monitor.state_store.set_alert_feed_cursor = original_set_cursor

    async def test_alert_poller_recovers_messages_after_cursor(self):
        now = datetime.now(timezone.utc)
        channel = monitor.ALERT_FEED_CHANNEL
        entities = {channel: channel}
        client = FakeHistoryClient({channel: [
            FakeTelegramMessage(125071, "Залісся – уважно по БпЛА", now - timedelta(minutes=2)),
            FakeTelegramMessage(125072, "1 на Троєщину! 1 на Бровари", now - timedelta(minutes=1)),
            FakeTelegramMessage(125073, "На Осещину поворот", now),
        ]})
        original_active = monitor.alert_active
        original_generation = monitor.alert_generation
        original_handler = monitor.handle_alert_message
        original_get_cursor = monitor.state_store.get_alert_feed_cursor
        original_set_cursor = monitor.state_store.set_alert_feed_cursor
        delivered = []
        cursors = {channel: 125071}

        async def fake_handler(text, source, generation, context=()):
            delivered.append((text, source, generation))
            return True

        try:
            monitor.alert_active = True
            monitor.alert_generation = 7
            monitor.handle_alert_message = fake_handler
            monitor.state_store.get_alert_feed_cursor = lambda name: cursors.get(name, 0)
            monitor.state_store.set_alert_feed_cursor = lambda name, value: cursors.__setitem__(name, value) or True
            count = await monitor.backfill_alert_feed(client, entities)
        finally:
            monitor.alert_active = original_active
            monitor.alert_generation = original_generation
            monitor.handle_alert_message = original_handler
            monitor.state_store.get_alert_feed_cursor = original_get_cursor
            monitor.state_store.set_alert_feed_cursor = original_set_cursor

        self.assertEqual(count, 2)
        self.assertEqual([item[0] for item in delivered], [
            "1 на Троєщину! 1 на Бровари",
            "На Осещину поворот",
        ])
        self.assertEqual(cursors[channel], 125073)

    async def test_filtered_message_edit_delivery_cursor_replay_and_retry(self):
        now = datetime.now(timezone.utc)
        channel = monitor.ALERT_FEED_CHANNEL
        original_active = monitor.alert_active
        original_generation = monitor.alert_generation
        original_get_cursor = monitor.state_store.get_alert_feed_cursor
        original_set_cursor = monitor.state_store.set_alert_feed_cursor
        original_claim = monitor.state_store.claim_alert_feed_delivery
        original_release = monitor.state_store.release_alert_feed_delivery
        original_schedule = monitor.schedule_alert_delivery
        cursors = {channel: 18368}
        claims = set()
        delivered = []
        delivery_results = iter((True, False, True))

        async def fake_delivery(text, source):
            delivered.append(text)
            return next(delivery_results)

        try:
            monitor.alert_active = True
            monitor.alert_generation = 3
            monitor.state_store.get_alert_feed_cursor = lambda name: cursors.get(name, 0)
            monitor.state_store.set_alert_feed_cursor = lambda name, value: cursors.__setitem__(name, value) or True
            monitor.state_store.claim_alert_feed_delivery = lambda name, message_id, text: (
                False if (name, message_id, monitor.normalize_alert_for_dedup(text)) in claims
                else not claims.add((name, message_id, monitor.normalize_alert_for_dedup(text)))
            )
            monitor.state_store.release_alert_feed_delivery = lambda name, message_id, text: claims.discard(
                (name, message_id, monitor.normalize_alert_for_dedup(text))
            )
            monitor.schedule_alert_delivery = lambda text, source, context=(): asyncio.create_task(
                fake_delivery(text, source)
            )

            original = FakeTelegramMessage(
                18369,
                "Підтримайте збір на евакуаційний автомобіль.",
                now,
            )
            self.assertFalse(await monitor.process_alert_feed_message(original, channel))
            self.assertEqual(cursors[channel], 18369)

            edited = FakeTelegramMessage(
                18369,
                "Вишгород жовтогарячий. Троєщина та Бровари червоний.",
                now,
                edit_date=now,
            )
            self.assertTrue(await monitor.process_alert_feed_message(edited, channel, edited=True))
            monitor.recent_alert_messages.clear()  # simulate a new worker after deploy
            self.assertFalse(await monitor.process_alert_feed_message(edited, channel, edited=True))

            failed = FakeTelegramMessage(18370, "БпЛА рухається на Київ.", now)
            self.assertFalse(await monitor.process_alert_feed_message(failed, channel))
            self.assertEqual(cursors[channel], 18369)
            self.assertTrue(await monitor.process_alert_feed_message(failed, channel))
        finally:
            monitor.alert_active = original_active
            monitor.alert_generation = original_generation
            monitor.state_store.get_alert_feed_cursor = original_get_cursor
            monitor.state_store.set_alert_feed_cursor = original_set_cursor
            monitor.state_store.claim_alert_feed_delivery = original_claim
            monitor.state_store.release_alert_feed_delivery = original_release
            monitor.schedule_alert_delivery = original_schedule

        self.assertEqual(cursors[channel], 18370)
        self.assertEqual(len(delivered), 3)

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


class AlertLiveCursorGuardTests(unittest.TestCase):
    """Late Telethon replays must not republish what the poller already covered."""

    def setUp(self):
        self.original_get_cursor = monitor.state_store.get_alert_feed_cursor
        self.cursors = {monitor.ALERT_FEED_CHANNEL: 125073}
        monitor.state_store.get_alert_feed_cursor = lambda name: self.cursors.get(name, 0)

    def tearDown(self):
        monitor.state_store.get_alert_feed_cursor = self.original_get_cursor

    def test_message_at_or_before_cursor_is_stale(self):
        self.assertTrue(monitor.state_store.is_stale_alert_feed_message(monitor.ALERT_FEED_CHANNEL, 125073))
        self.assertTrue(monitor.state_store.is_stale_alert_feed_message(monitor.ALERT_FEED_CHANNEL, 125000))

    def test_message_after_cursor_is_fresh(self):
        self.assertFalse(monitor.state_store.is_stale_alert_feed_message(monitor.ALERT_FEED_CHANNEL, 125074))

    def test_without_cursor_nothing_is_stale(self):
        self.cursors.clear()
        self.assertFalse(monitor.state_store.is_stale_alert_feed_message(monitor.ALERT_FEED_CHANNEL, 5))


if __name__ == "__main__":
    unittest.main()
