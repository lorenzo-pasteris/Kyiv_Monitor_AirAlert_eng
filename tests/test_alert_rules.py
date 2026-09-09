import unittest

from alert_rules import classify_kyiv_city_official_alert


class KyivCityOfficialAlertTests(unittest.TestCase):
    def test_classifies_yellow_red_and_clear_templates(self):
        self.assertTrue(classify_kyiv_city_official_alert(
            "🟡 УВАГА! У Києві оголошена дронова небезпека!"
        ))
        self.assertTrue(classify_kyiv_city_official_alert(
            "‼️УВАГА! У Києві оголошена повітряна тривога!"
        ))
        self.assertFalse(classify_kyiv_city_official_alert(
            "❕Відбій повітряної тривоги!"
        ))

    def test_ignores_other_official_news(self):
        self.assertIsNone(classify_kyiv_city_official_alert(
            "У Києві відновили рух автобусів після ремонту."
        ))


if __name__ == "__main__":
    unittest.main()
