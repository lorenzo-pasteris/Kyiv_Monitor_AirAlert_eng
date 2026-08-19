"""Fail a Railway deployment before promotion when production config is invalid."""

import os
from collections.abc import Callable, Mapping


REQUIRED_VARIABLES = (
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELEGRAM_SESSION",
    "ANTHROPIC_API_KEY",
    "BOT_TOKEN",
    "TARGET_CHAT_ID",
    "SUMMARY_CHAT_ID",
    "SUMMARY_CHAT_LINK",
    "OWNER_CHAT_ID",
)

INTEGER_VARIABLES = (
    "TELEGRAM_API_ID",
    "TARGET_CHAT_ID",
    "SUMMARY_CHAT_ID",
    "OWNER_CHAT_ID",
)


def validate_environment(
    env: Mapping[str, str],
    session_factory: Callable[[str], object] | None = None,
) -> None:
    missing = [name for name in REQUIRED_VARIABLES if not env.get(name, "").strip()]
    if missing:
        raise ValueError(f"missing required variables: {', '.join(missing)}")

    for name in INTEGER_VARIABLES:
        try:
            int(env[name])
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer") from exc

    if env["TARGET_CHAT_ID"] == env["SUMMARY_CHAT_ID"]:
        raise ValueError("TARGET_CHAT_ID and SUMMARY_CHAT_ID must be different")

    session = env["TELEGRAM_SESSION"]
    if session != session.strip():
        raise ValueError("TELEGRAM_SESSION contains leading or trailing whitespace")

    if session_factory is None:
        from telethon.sessions import StringSession

        session_factory = StringSession

    try:
        session_factory(session)
    except Exception as exc:
        raise ValueError(
            f"TELEGRAM_SESSION has an invalid StringSession format ({type(exc).__name__})"
        ) from exc


if __name__ == "__main__":
    validate_environment(os.environ)
    print("Pre-deploy configuration check passed")
