"""Pure, side-effect-free rules used by the real-time alert pipeline."""

import re


def is_official_alert_announcement(text: str) -> bool:
    """Recognize the announcement header, not arbitrary news about threats."""
    header = re.sub(r"[\s*_]+", " ", text.lower()).strip()
    return bool(re.match(r"^[^\w]*(?:увага|attention)\s*[!:—–-]", header))


def classify_kyiv_city_official_level(text: str) -> str | None:
    """Return GREEN/YELLOW/RED for explicit Kyiv City alert templates."""
    lowered = re.sub(r"[\s*_]+", " ", text.lower())
    if "відбій повітряної тривоги" in lowered or "air siren all clear" in lowered:
        return "GREEN"
    # Read the announcement clause only: shelter instructions/news are not state.
    clauses = re.findall(
        r"(?:у києві оголошен[ао]|new threat in kyiv\s*:)\s*([^!.?\n]{1,100})",
        lowered if is_official_alert_announcement(text) else "",
    )
    for clause in clauses:
        if re.search(r"\b(?:не|немає|not|no)\b", clause):
            continue
        if re.search(r"(?:ракетна|балістична)\s+(?:загроза|небезпека)|missile threat", clause):
            return "RED"
        if re.search(r"дронова\s+(?:загроза|небезпека)|drone threat", clause):
            return "YELLOW"
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
