import unittest

from alert_rules import classify_kyiv_city_official_level


class KyivCityOfficialAlertTests(unittest.TestCase):
    def test_new_threat_templates_and_formatting(self):
        for text, expected in (
            ("🔴 УВАГА! У Києві оголошена нова загроза – ракетна загроза!", "RED"),
            ("🟡 УВАГА! У Києві оголошена нова загроза – дронова небезпека!", "YELLOW"),
            ("🔴 ATTENTION! New threat in Kyiv: Missile threat!", "RED"),
            ("🟡 ATTENTION! New threat in Kyiv: Drone threat!", "YELLOW"),
            ("УВАГА! У Києві\nоголошено **нову загрозу: ракетна небезпека**!", "RED"),
            ("УВАГА! У Києві оголошена балістична загроза!", "RED"),
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_kyiv_city_official_level(text), expected)

    def test_no_level_from_generic_news_or_negated_new_threat(self):
        for text in (
            "У Києві працюють рятувальники після ракетної атаки.",
            "ATTENTION! New threat in Kyiv: no missile threat!",
            "УВАГА! У Києві оголошена нова загроза – не ракетна загроза!",
            "Ракетна загроза у Броварах.",
        ):
            with self.subTest(text=text):
                self.assertIsNone(classify_kyiv_city_official_level(text))

    def test_classifies_yellow_red_and_clear_templates(self):
        self.assertEqual(classify_kyiv_city_official_level(
            "🟡 УВАГА! У Києві оголошена дронова небезпека!"
        ), "YELLOW")
        self.assertEqual(classify_kyiv_city_official_level(
            "‼️УВАГА! У Києві оголошена повітряна тривога!"
        ), "RED")
        self.assertEqual(classify_kyiv_city_official_level(
            "❕Відбій повітряної тривоги!"
        ), "GREEN")

    def test_ignores_other_official_news(self):
        self.assertIsNone(classify_kyiv_city_official_level(
            "У Києві відновили рух автобусів після ремонту."
        ))

    def test_october_10_ukrainian_threat_templates_do_not_need_english_translation(self):
        self.assertEqual(classify_kyiv_city_official_level(
            "🔴 УВАГА! У Києві оголошена ракетна загроза!"
        ), "RED")
        self.assertEqual(classify_kyiv_city_official_level(
            "🟡 УВАГА! У Києві оголошена дронова загроза!"
        ), "YELLOW")


if __name__ == "__main__":
    unittest.main()
