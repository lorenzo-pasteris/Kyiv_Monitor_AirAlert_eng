"""Pure, side-effect-free rules used by the real-time alert pipeline."""


def classify_kyiv_city_official_level(text: str) -> str | None:
    """Return GREEN/YELLOW/RED for explicit Kyiv City alert templates."""
    lowered = text.lower()
    if "відбій повітряної тривоги" in lowered or "air siren all clear" in lowered:
        return "GREEN"
    if any(phrase in lowered for phrase in (
        "у києві оголошена дронова небезпека",
        "у києві оголошена дронова загроза",
        "drone threat in kyiv",
    )):
        return "YELLOW"
    if any(
        phrase in lowered
        for phrase in (
            "у києві оголошена повітряна тривога",
            "у києві оголошена ракетна небезпека",
            "у києві оголошена ракетна загроза",
            "air raid sirens in kyiv",
            "missile threat in kyiv",
        )
    ):
        return "RED"
    return None
