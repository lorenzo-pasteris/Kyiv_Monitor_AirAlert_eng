"""Pure, side-effect-free rules used by the real-time alert pipeline."""

from collections.abc import Iterable


KYIV_NAMES = ("kyiv", "kiev", "київ", "києв", "киев")
CLEAR_PHRASES = ("all clear", "clear", "cancelled", "canceled", "ended", "відбій")
ALERT_PHRASES = ("повітряна тривога", "тривога")


def contains_any(text: str, keywords: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def classify_telegram_alert(text: str) -> bool | None:
    """Return True/False for an explicit Kyiv alert/clear message, otherwise None."""
    lowered = text.lower()
    if not contains_any(lowered, KYIV_NAMES):
        return None
    if contains_any(lowered, CLEAR_PHRASES):
        return False
    if (
        "air" in lowered
        and contains_any(lowered, ("siren", "raid", "alert"))
    ) or contains_any(lowered, ALERT_PHRASES):
        return True
    return None
