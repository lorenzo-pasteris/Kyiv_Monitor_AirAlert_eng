import unittest

from predeploy_check import validate_environment


def valid_environment():
    return {
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH": "hash",
        "TELEGRAM_SESSION": "valid-session",
        "ANTHROPIC_API_KEY": "key",
        "BOT_TOKEN": "token",
        "TARGET_CHAT_ID": "-1001",
        "SUMMARY_CHAT_ID": "-1002",
        "SUMMARY_CHAT_LINK": "https://t.me/example",
        "OWNER_CHAT_ID": "123",
    }


class PredeployCheckTests(unittest.TestCase):
    def test_valid_environment_passes(self):
        validate_environment(valid_environment(), lambda value: object())

    def test_missing_variable_fails(self):
        env = valid_environment()
        env["BOT_TOKEN"] = ""
        with self.assertRaisesRegex(ValueError, "BOT_TOKEN"):
            validate_environment(env, lambda value: object())

    def test_non_integer_chat_id_fails(self):
        env = valid_environment()
        env["TARGET_CHAT_ID"] = "alerts"
        with self.assertRaisesRegex(ValueError, "TARGET_CHAT_ID must be an integer"):
            validate_environment(env, lambda value: object())

    def test_same_target_and_summary_fails(self):
        env = valid_environment()
        env["SUMMARY_CHAT_ID"] = env["TARGET_CHAT_ID"]
        with self.assertRaisesRegex(ValueError, "must be different"):
            validate_environment(env, lambda value: object())

    def test_session_whitespace_fails(self):
        env = valid_environment()
        env["TELEGRAM_SESSION"] += "\n"
        with self.assertRaisesRegex(ValueError, "whitespace"):
            validate_environment(env, lambda value: object())

    def test_malformed_session_fails_without_exposing_value(self):
        env = valid_environment()
        env["TELEGRAM_SESSION"] = "secret-malformed-value"

        def reject(value):
            raise RuntimeError("bad session")

        with self.assertRaises(ValueError) as caught:
            validate_environment(env, reject)
        self.assertNotIn(env["TELEGRAM_SESSION"], str(caught.exception))
        self.assertIn("RuntimeError", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
