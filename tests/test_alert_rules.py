import unittest

from alert_rules import classify_kyiv_city_official_level


class KyivCityOfficialAlertTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
