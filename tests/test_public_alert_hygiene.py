import json
import unittest

from text_processing import (
    clean_alert_public_text, clean_alert_source_text, is_commentary_alert_message,
    is_general_siren_announcement, parse_alert_gate_output,
)


class PublicAlertHygieneTests(unittest.TestCase):
    def test_real_edited_blast_report_has_no_decorations_or_promo(self):
        text = "🟡 Loud blasts 💥💥 — shot down. Kyiv is clear for now ✈️"
        self.assertEqual(clean_alert_public_text(text),
                         "Loud blasts — shot down. Kyiv is clear for now")
        source = "Гучно💥💥 є збиття. Київ поки чисто ✈️\n**Надсилайте новини в анонімний бот **➡️@novosty_kiev_bot"
        self.assertEqual(clean_alert_source_text(source), "Гучно💥💥 є збиття. Київ поки чисто ✈️")

    def test_real_radar_promo_and_double_indicator(self):
        text = "🟡 Boryspil, Brovary district — air alert, yellow level: drone threat (yellow level)\nDetailed radar 👉🏽 @kievinform_ua1"
        self.assertEqual(clean_alert_public_text(text),
                         "Boryspil, Brovary district — air alert, yellow level: drone threat (yellow level)")
        source = "🟡 **Бориспільський, Броварський район** — повітряна тривога, жовтий рівень: Дронова загроза (жовтий рівень)\n**Детальний радар** 👉🏽 @kievinform_ua1"
        self.assertTrue(is_general_siren_announcement(clean_alert_source_text(source)))
        self.assertTrue(is_general_siren_announcement("Відбій."))
        self.assertFalse(is_general_siren_announcement("Дрони на Бровари, повітряна тривога"))

    def test_urls_mentions_and_emoji_do_not_remove_numbers_or_directions(self):
        text = "2️⃣ Kh-101 → Kyiv, 2-3 minutes. [Source](https://t.me/example/1) @example ✈️"
        result = clean_alert_public_text(text)
        self.assertEqual(result, "2 Kh-101 → Kyiv, 2-3 minutes. Source")

    def test_real_all_clear_speculation_is_not_rescued_by_priority_words(self):
        source = "В теорії має бути відбій, але Міхалич по базі може ще 2 години чекати поки підлетять нові дрони"
        self.assertTrue(is_commentary_alert_message(source))
        for decision in ("PUBLISH", "DROP"):
            raw = json.dumps({"decision": decision, "block_category": "EDITORIAL_COMMENTARY",
                              "translation": "In theory the all clear should come.", "evidence": source})
            self.assertEqual(parse_alert_gate_output(raw, source), ("DROP", "", "EDITORIAL_COMMENTARY"))

    def test_real_threats_and_uncertain_outcomes_remain_allowed(self):
        for source in ("Схоже другий раз вже не попали", "Вибух, але дрон наче вижив",
                       "Перші ракети Х101 залітають на Сумщину", "Відбій по балістиці",
                       "БпЛА можуть підлетіти за 2 години"):
            self.assertFalse(is_commentary_alert_message(source), source)
            self.assertFalse(is_general_siren_announcement(source), source)
