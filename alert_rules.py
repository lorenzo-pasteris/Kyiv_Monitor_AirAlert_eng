"""Pure, side-effect-free rules used by the real-time alert pipeline."""


def classify_kyiv_city_official_level(text: str) -> str | None:
    """Return GREEN/YELLOW/RED for explicit Kyiv City alert templates."""
    lowered = text.lower()
    if "відбій повітряної тривоги" in lowered or "air siren all clear" in lowered:
        return "GREEN"
    if "у києві оголошена дронова небезпека" in lowered or "drone threat in kyiv" in lowered:
        return "YELLOW"
    if any(
        phrase in lowered
        for phrase in (
            "у києві оголошена повітряна тривога",
            "у києві оголошена ракетна небезпека",
            "air raid sirens in kyiv",
            "missile threat in kyiv",
        )
    ):
        return "RED"
    return None
