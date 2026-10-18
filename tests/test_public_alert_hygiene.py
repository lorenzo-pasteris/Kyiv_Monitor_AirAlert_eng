import json
import unittest

from text_processing import (
    clean_alert_public_text, clean_alert_source_text, is_commentary_alert_message,
    is_general_siren_announcement, parse_alert_gate_output,
    strip_general_siren_notices,
    strip_non_operational_news, translate_known_terse_fragment, build_alert_translation_prompt,
    parse_backup_novelty,
    strip_alert_questions, is_valid_alert_translation,
)


class PublicAlertHygieneTests(unittest.TestCase):
    def test_editorial_drops_are_not_rescued_by_threat_keywords(self):
        cases = (
            ("Міхалич з перепугу дав жовту тривогу, хоча дронів немає", "EDITORIAL_COMMENTARY"),
            ("Трохи уточню, США нам не перестали давати розвіддані як деякі пишуть, але в цей момент той хто був відповідальний за відстеження пусків ДУЖЕ СИЛЬНО затупив", "EDITORIAL_COMMENTARY"),
            ("О 22.10 ворог завдав удару по столиці попередньо 6-ма балістичними ракетами по кількох районах Києва. Інформації щодо пусків з території противника не поступало. Причини встановлюються.", "NON_OPERATIONAL_NEWS"),
        )
        for source, category in cases:
            raw = json.dumps(dict(decision="DROP", block_category=category,
                                  evidence=source, translation="Non-operational report."))
            for backup in (False, True):
                self.assertEqual(parse_alert_gate_output(raw, source, mixed_news_source=backup,
                                                        require_excerpts=True)[0], "DROP")

    def test_both_feeds_require_grounded_operational_extraction(self):
        source = "Новини політики. Два дрони на Бровари"
        excerpt = "Два дрони на Бровари"
        output = "Two drones towards Brovary."
        for backup in (False, True):
            payload = dict(decision="PUBLISH", translation="Unwanted editorial introduction.")
            with self.assertRaises(ValueError):
                parse_alert_gate_output(json.dumps(payload), source, mixed_news_source=backup,
                                        require_excerpts=True)
            payload["operational_updates"] = [dict(source=excerpt, translation=output)]
            self.assertEqual(parse_alert_gate_output(json.dumps(payload), source,
                             mixed_news_source=backup, require_excerpts=True)[1], output)
            payload["operational_updates"][0]["source"] = "Invented context"
            with self.assertRaises(ValueError):
                parse_alert_gate_output(json.dumps(payload), source, mixed_news_source=backup,
                                        require_excerpts=True)

    def test_comment_on_warning_issuer_cannot_be_salvaged_as_threat(self):
        for source in (
            "Саме цікаве що США тільки що дали загрозу з Брянська",
            "Найцікавіше що вони оголосили тривогу",
            "Exactly what's most interesting is that the US just issued a threat from Bryansk.",
        ):
            self.assertTrue(is_commentary_alert_message(source))
            raw = json.dumps(dict(decision="PUBLISH", operational_updates=[dict(
                source=source, translation="Threat from Bryansk.")]))
            for backup in (False, True):
                self.assertEqual(parse_alert_gate_output(raw, source, mixed_news_source=backup,
                                 require_excerpts=True), ("DROP", "", "EDITORIAL_COMMENTARY"))
        for source in ("Загроза з Брянська!!", "Нові пуски ракет з Брянська"):
            self.assertFalse(is_commentary_alert_message(source))
            raw = json.dumps(dict(decision="PUBLISH", operational_updates=[dict(
                source=source, translation="Threat from Bryansk.")]))
            for backup in (False, True):
                self.assertEqual(parse_alert_gate_output(raw, source, mixed_news_source=backup,
                                 require_excerpts=True)[0], "PUBLISH")

    def test_protected_primary_drop_does_not_require_publish_schema(self):
        source = "Перші ракети Х101 залітають на Сумщину"
        raw = json.dumps(dict(decision="DROP", block_category="DISTANT_WITHOUT_KYIV_TRAJECTORY",
                              evidence=source, translation="The first Kh-101 missiles are entering Sumy region."))
        self.assertEqual(parse_alert_gate_output(raw, source, require_excerpts=True)[0], "PUBLISH")

    def test_questions_are_removed_as_clauses_never_as_punctuation(self):
        self.assertEqual(strip_alert_questions("Де дрон? Біля Києва чисто."), "Біля Києва чисто.")
        self.assertEqual(strip_alert_questions("Ракета?"), "")
        self.assertEqual(strip_alert_questions("2.5 km. Not shot down."), "2.5 km. Not shot down.")
        for question in ("Where is it?", "Shot down？", "Shot down❓", "Missiles⁉"):
            self.assertFalse(is_valid_alert_translation(question))
            self.assertEqual(clean_alert_public_text(question), "")
        raw = json.dumps(dict(decision="PUBLISH", translation="Where is the other drone?"))
        with self.assertRaises(ValueError):
            parse_alert_gate_output(raw, "Летів дрон на Вишгород")

    def test_fell_is_not_shot_down_or_impact(self):
        for source in ("Впав", "Впав!", "Впав 💥"):
            self.assertEqual(translate_known_terse_fragment(source), "It fell.")
        self.assertIsNone(translate_known_terse_fragment("Впав?"))
        self.assertIsNone(translate_known_terse_fragment("Не впав"))

    def test_shadow_duplicate_annotation_never_suppresses_translation(self):
        source = "Два реактивних на Козин"
        publications = [{"id": "p1", "text": "2 jet-powered UAVs towards Kozyn"}]
        payload = dict(decision="PUBLISH", operational_updates=[dict(
            source=source, translation="Two jet-powered UAVs towards Kozyn",
            novelty=dict(status="DUPLICATE", duplicate_of=["p1"], reason="same event"))])
        raw = json.dumps(payload)
        self.assertEqual(parse_backup_novelty(raw, source, publications)[0]["status"], "DUPLICATE")
        self.assertEqual(parse_alert_gate_output(raw, source, mixed_news_source=True)[0], "PUBLISH")
        for refs in (["invented"], [], None, [{}]):
            payload["operational_updates"][0]["novelty"]["duplicate_of"] = refs
            self.assertEqual(parse_backup_novelty(json.dumps(payload), source, publications)[0]["status"], "UNCERTAIN")
            self.assertEqual(parse_alert_gate_output(json.dumps(payload), source, mixed_news_source=True)[0], "PUBLISH")

    def test_novelty_prompt_covers_both_sources_and_protects_new_threats(self):
        publications = [{"id": "p1", "text": "2 drones towards Kozyn"}]
        primary = build_alert_translation_prompt("Козин", published_context=publications)
        backup = build_alert_translation_prompt("Козин", mixed_news_source=True, published_context=publications)
        self.assertIn("OBSERVATION ONLY", primary)
        self.assertIn('"p1"', primary)
        self.assertIn("OBSERVATION ONLY", backup)
        self.assertIn("A new missile launch", backup)
        self.assertIn("negation or uncertainty", backup)
        self.assertIn('"p1"', backup)
        self.assertEqual(parse_backup_novelty("broken", "Козин", publications), [])

    def test_ballistic_warning_keeps_threat_and_shelter_not_sharing_requests(self):
        text = "🔴 Ballistic threat due to OTRK activity by the enemy\nTake shelter!\nWarn your loved ones!\nShare with friends, many people don't know where to find threat information!"
        self.assertEqual(clean_alert_public_text(text),
                         "Ballistic threat due to OTRK activity by the enemy\nTake shelter!")
        source = "Загроза балістики через активність ОТРК ворога! В укриття! Попередьте близьких! Поділіться з друзями!"
        self.assertEqual(clean_alert_source_text(source),
                         "Загроза балістики через активність ОТРК ворога! В укриття!")
        self.assertEqual(clean_alert_source_text("Share with friends! New missiles towards Kyiv!"),
                         "New missiles towards Kyiv!")

    def test_pokh_is_places_not_ballistics(self):
        for text in ("ПОХ - увага ⚠️ ✈️", "ПОХ — увага!", "ПОХ: увага"):
            self.assertEqual(translate_known_terse_fragment(text),
                             "Pozniaky, Osokorky, Kharkivskyi — stay alert.")
        self.assertIsNone(translate_known_terse_fragment("ПОХ — увага, 2 дрони з півдня"))
        self.assertIn("NEVER a ballistic warning", build_alert_translation_prompt("ПОХ"))

    def test_mixed_news_preserves_live_tactical_update(self):
        text = "Пожежу рятувальники локалізували.\n2 дрони на Бровари"
        self.assertEqual(strip_non_operational_news(text), "2 дрони на Бровари")
        for live in ("Вибух, але дрон наче вижив", "Загроза балістики з Курська",
                     "Перші ракети Х101 залітають на Сумщину", "Щойно вибух, поруч триває ліквідація пожежі",
                     "Дрон на Бровари, пожежу локалізували"):
            self.assertEqual(strip_non_operational_news(live), live)

    def test_retrospective_excerpt_is_not_allowed_by_parser(self):
        source = "Пожежу рятувальники локалізували."
        raw = json.dumps(dict(decision="PUBLISH", operational_updates=[
            dict(source=source, translation="Firefighters contained the fire.")]))
        with self.assertRaises(ValueError):
            parse_alert_gate_output(raw, source, mixed_news_source=True)

    def test_mixed_trump_post_loses_siren_and_radar_and_news_drop_stays_drop(self):
        text = "Тим часом Трамп…\nЯ можу говорити найтупіші речі, — Трамп\n00:05 🟡 Білоцерківський район — повітряна тривога, жовтий рівень: Дронова загроза (жовтий рівень)\n✈️ радар 👉🏽 @kievinform_ua1"
        clean = strip_general_siren_notices(clean_alert_source_text(text))
        self.assertNotIn("тривога", clean)
        self.assertNotIn("радар", clean)
        raw = json.dumps(dict(decision="DROP", block_category="NON_OPERATIONAL_NEWS",
                              evidence="Трамп", translation="Meanwhile Trump"))
        self.assertEqual(parse_alert_gate_output(raw, clean, mixed_news_source=True)[0], "DROP")

    def test_only_extracted_tactical_translation_is_published(self):
        source = "Політичні новини. 3х рБпЛА з Чернігівщини"
        result = dict(decision="PUBLISH", translation="Politics. Three UAVs.",
                      operational_updates=[dict(source="3х рБпЛА з Чернігівщини",
                                                translation="3 jet-powered UAVs from Chernihiv region.")])
        self.assertEqual(parse_alert_gate_output(json.dumps(result), source, mixed_news_source=True)[1],
                         "3 jet-powered UAVs from Chernihiv region.")
        for updates in (None, [], [dict(source="Дрон на Київ", translation="Drone towards Kyiv")]):
            result["operational_updates"] = updates
            with self.assertRaises(ValueError):
                parse_alert_gate_output(json.dumps(result), source, mixed_news_source=True)

    def test_siren_excerpt_cannot_be_used_as_operational_evidence(self):
        source = "ТРИВОГА В КИЄВІ та області"
        raw = json.dumps(dict(decision="PUBLISH", translation="Alert in Kyiv",
                              operational_updates=[dict(source=source, translation="Alert in Kyiv")]))
        with self.assertRaises(ValueError):
            parse_alert_gate_output(raw, source, mixed_news_source=True)

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
