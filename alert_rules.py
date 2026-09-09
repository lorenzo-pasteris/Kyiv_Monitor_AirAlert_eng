"""Pure, side-effect-free rules used by the real-time alert pipeline."""


def classify_kyiv_city_official_alert(text: str) -> bool | None:
    """Classify only the explicit alert templates used by Kyiv City."""
    lowered = text.lower()
    if "відбій повітряної тривоги" in lowered or "air siren all clear" in lowered:
        return False
    if any(
        phrase in lowered
        for phrase in (
            "у києві оголошена повітряна тривога",
            "у києві оголошена дронова небезпека",
            "у києві оголошена ракетна небезпека",
            "air raid sirens in kyiv",
            "drone threat in kyiv",
            "missile threat in kyiv",
        )
    ):
        return True
    return None
