import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from test_routing import FakeTelegramMessage, monitor
from text_processing import clean_news_attributions


class NewsAttributionTests(unittest.TestCase):
    def test_channel_attribution_becomes_neutral_without_changing_facts(self):
        examples = (
            ("AMK reports that 2 facilities were possibly damaged.", "Reported: 2 facilities were possibly damaged."),
            ("Kyiv Oblast reports 3 injuries in Bucha district.", "Reported: 3 injuries in Bucha district."),
            ("AMK Mapping claims a missile was intercepted.", "Claim: a missile was intercepted."),
            ("According to AMK, the cause is unconfirmed.", "According to reports, the cause is unconfirmed."),
            ("Damage in Kyiv Oblast, according to Kyiv Oblast.", "Damage in Kyiv Oblast, according to reports."),
            ("A fire was reported by AMK.", "A fire was reported."),
            ("• war_monitor reports no bomber activity.", "• Reported: no bomber activity."),
            ("• @unknown_channel reports possible disruption.", "• Reported: possible disruption."),
            ("Power restored.\nSource: @ukrenergo", "Power restored."),
        )
        for original, expected in examples:
            with self.subTest(original=original):
                self.assertEqual(clean_news_attributions(original, monitor.ALL_CONTENT_CHANNELS + ["war_monitor"]), expected)

    def test_locations_and_named_official_authorities_are_preserved(self):
        for text in (
            "A fire in Kyiv Oblast damaged 2 warehouses.",
            "Emergency services in Kyiv Oblast reported 3 injuries.",
            "Kyiv Oblast authorities report 3 injuries.",
            "Ukrenergo reports scheduled power cuts.",
            "According to the Ukrainian Air Force, 12 drones were intercepted.",
            "According to Kyiv Oblast authorities, the road is closed.",
        ):
            self.assertEqual(clean_news_attributions(text, monitor.ALL_CONTENT_CHANNELS), text)

    def test_news_parser_cleans_public_text_but_keeps_source_ids(self):
        message = {"id": "AMK_Mapping:123", "channel": "AMK_Mapping", "text": "source text"}
        categories = {key: {"selected_ids": [], "items": []} for key in monitor.CATEGORIES}
        category = next(iter(categories))
        categories[category] = {"selected_ids": [message["id"]], "items": [{
            "event_key": "2026-11-03|kyiv|fire", "is_update": False,
            "text": "AMK reports a possible fire in Kyiv Oblast.",
        }]}
        result = monitor.normalize_category_result({"categories": categories}, [message])
        self.assertEqual(result[category]["items"][0]["text"], "Reported: a possible fire in Kyiv Oblast.")
        self.assertEqual(result[category]["selected_ids"], ["AMK_Mapping:123"])


class NightAssessmentAttributionTests(unittest.IsolatedAsyncioTestCase):
    async def test_nightly_publication_applies_the_same_rule(self):
        now = datetime.now(timezone.utc)
        raw = f"📡 Обстановка станом на 00:00\n{now:%d.%m.%y}\n#обстановка@war_monitor"
        with patch.object(monitor, "is_daily_war_monitor_report", return_value=True), \
             patch.object(monitor.state_store, "claim_alert_feed_delivery", return_value=True), \
             patch.object(monitor, "collect_preparation_signals", new=AsyncMock(return_value=[])), \
             patch.object(monitor, "translate_war_monitor_report", new=AsyncMock(return_value="• War Monitor reports limited, unconfirmed activity.")), \
             patch.object(monitor, "send_to_summary_group", new=AsyncMock(return_value=True)) as send:
            self.assertTrue(await monitor.process_war_monitor_report(FakeTelegramMessage(1, raw, now)))
            send.assert_awaited_once_with("• Reported: limited, unconfirmed activity.")

