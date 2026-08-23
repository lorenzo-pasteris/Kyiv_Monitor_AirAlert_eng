"""Pure text filtering, cleaning and parsing helpers used by monitor.py.

Nothing here touches the network, the database, or module-level mutable
state — every function is a deterministic transform of its arguments, which
keeps this module trivial to unit test in isolation from the Telegram/DB
wiring in monitor.py.
"""
import json
import re
from datetime import datetime, timezone

# --- Pre-filters ---
AD_INDICATORS = ["#реклама","реклама","знижк","розпродаж","промокод","купуй","придбай","акція","магазин","замовляй","доставка"]
DONATION_INDICATORS = [
    "донат", "донатів", "підтримати", "підтримайте", "підтримку", "підтримкою",
    "кавою", "на каву", "mono", "моно", "monobank", "приват", "privat",
    "банка", "банку", "збір", "збору", "картка", "карта", "реквізит",
]
ENGAGEMENT_INDICATORS = [
    "щиро вдяч", "дуже вдяч", "вдячн", "дяку", "подяку", "спасиб",
    "thank you", "thanks", "ви найкращі", "найкращі підписники",
    "всіх бачу", "best subscribers", "i can see everyone", "тримаємось",
    "з днем", "вітаємо", "вітаю", "найкращі були та будете",
]
SECURITY_KEYWORDS = ["тривога","відбій","балістика","ракета","шахед","шахеди","бпла","вибух","вибухи","ппо","повітряна ціль","укриття","загроза","обстріл","приліт","дрон","дрони","mig","міг","siren","raid","missile","drone","explosion","alert","attack","ballistic","shahed","interception","strike"]
ALERT_TACTICAL_KEYWORDS = SECURITY_KEYWORDS + [
    "ціль", "цілі", "рух", "рухається", "рухаються", "курс", "напрям", "летить",
    "летять", "чисто", "знищено", "знищена", "знищений", "збито", "увага",
    "target", "heading", "moving", "destroyed", "shot down", "area clear",
]
ALERT_TERSE_FOLLOWUP_KEYWORDS = [
    "підліта", "в бік", "у бік", "на бориспіль", "на бровари", "від броварів",
    "через водосховище", "уважно", "гучно", "димер", "згурів", "троєщин",
    "рембаз", "осещин", "тец-6", "березан", "українк",
]
COMMENTARY_PATTERNS = (
    r"\b(?:не знаю|я думаю|мені здається|не розумію|чому|навіщо)\b",
    r"\b(?:не знаю|я думаю|мне кажется|не понимаю|почему|зачем)\b",
    r"\b(?:i don['’]t know|i think|in my opinion|why|what are they waiting for)\b",
    r"\b(?:повинні були|мали б|should have|can['’]t they|keeping the alert on)\b",
    r"\b(?:заснув|уснул|fell asleep)\b",
    r"\b(?:всі спустились|все спустились|на вулицю не йдемо|на улицу не ид[её]м)\b",
    r"\b(?:менш\s+ефектив\w*|менее\s+эффектив\w*|less\s+effective)\b.*\b(?:ніж|чем|than)\b",
    r"\b(?:віримо в медиків|верим в медиков|we believe in the medics)\b",
    r"^\s*бандероль\s*[-—:=]\s*крилата ракета\s*[.!]?\s*$",
)


def contains_any(text, keywords):
    return any(k.lower() in text.lower() for k in keywords)


def clean_text(text):
    text = re.sub(r'^\s*#\w+\s*', '', text)
    text = re.sub(r'\s*#\w+\s*', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def clean_alert_source_text(text):
    """Remove promotional lines from alert-source text before filtering and translation."""
    clean = clean_text(text)
    promo_line_patterns = (
        r"(?i)\b(?:надіслати новину|написати нам|підписатися|підпишись|підписуйся)\b",
        r"(?i)\b(?:send news|report news|subscribe|watch live|live stream|join us)\b",
        r"(?i)^\s*(?:💙\s*)?dnipro alerts\s*•\s*(?:💛\s*)?kyiv alerts\s*$",
        r"(?i)^\s*(?:ℹ️\s*)?alerts live\s*•\s*(?:🤙\s*)?feedback\s*$",
        r"(?i)^\s*(?:👉\s*)?live\s*[:!—-]*\s*$",
        r"(?i)^\s*(?:уся|вся|вся інформація|all information)\s+(?:інформація\s+)?(?:з|из|from)\s+київ\s*[|—:-]*\s*де\s+загроза\s*$",
        r"^\s*ㅤ\s*$",
    )
    kept_lines = []
    for line in clean.splitlines():
        line = re.sub(
            r"(?i)\[([^\]]+)\]\((?:https?://|www\.|(?:t|telegram)\.me/)\S+\)",
            r"\1",
            line,
        )
        line = re.sub(
            r"(?i)(?:https?://|www\.|(?:t|telegram)\.me/)\S+",
            "",
            line,
        ).strip()
        if line and not any(re.search(pattern, line) for pattern in promo_line_patterns):
            kept_lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept_lines)).strip()


def is_non_operational_alert_message(text):
    """Reject fundraising, payment details, thanks and greeting posts even if they mention attacks."""
    compact_digits = re.sub(r"[\s-]", "", text)
    has_payment_number = bool(re.search(r"(?<!\d)\d{13,19}(?!\d)", compact_digits))
    return (
        has_payment_number
        or contains_any(text, DONATION_INDICATORS)
        or contains_any(text, ENGAGEMENT_INDICATORS)
    )


def is_actionable_alert_message(text):
    """Allow explicit threats plus the terse location/direction follow-ups used by the alert feed."""
    if contains_any(text, ALERT_TACTICAL_KEYWORDS):
        return True
    clean = clean_text(text)
    if len(clean) > 500:
        return False
    if contains_any(clean, ALERT_TERSE_FOLLOWUP_KEYWORDS):
        return True
    # The alert feed frequently posts terse course updates such as
    # "На Тарасівку від Боярки" without repeating "БпЛА".
    return bool(re.match(r"^(?:ще\s+)?на\s+.{2,80}\s+(?:від|з|із|зі)\s+.{2,80}[.!]?\s*$", clean, re.I))


def is_commentary_alert_message(text):
    """Identify opinions, rhetorical complaints and speculation in an alert feed."""
    return any(re.search(pattern, text, re.I) for pattern in COMMENTARY_PATTERNS)


def is_pure_ad(text):
    if contains_any(text, SECURITY_KEYWORDS):
        return False
    return contains_any(text, AD_INDICATORS)


def normalize_alert_for_dedup(text):
    """Normalize formatting noise while retaining locations, targets and quantities."""
    normalized = clean_text(text).lower()
    normalized = re.sub(r"https?://\S+|t\.me/\S+|@\w+", " ", normalized)
    normalized = re.sub(r"[^\w\s]", " ", normalized, flags=re.UNICODE)
    return re.sub(r"\s+", " ", normalized).strip()


def parse_ukraine_alarm_kyiv_state(regions):
    """Return Kyiv City's AIR state; never confuse it with Kyiv Oblast."""
    kyiv_names = {"київ", "м. київ", "kyiv", "kyiv city"}
    for region in regions if isinstance(regions, list) else ():
        if not isinstance(region, dict):
            continue
        names = {
            str(region.get("regionName", "")).strip().casefold(),
            str(region.get("regionEngName", "")).strip().casefold(),
        }
        if names.isdisjoint(kyiv_names):
            continue
        alerts = region.get("activeAlerts") or []
        return any(
            isinstance(alert, dict) and str(alert.get("type", "")).upper() == "AIR"
            for alert in alerts
        )
    raise ValueError("Kyiv City is missing from UkraineAlarm response")


def utc_iso(value=None):
    """Return a stable UTC timestamp for Telegram messages and database state."""
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def alert_feed_cursor_key(channel):
    return f"alert_feed_cursor:{channel}"


def build_alert_translation_prompt(text):
    """Build a concise, domain-aware translation request for Ukrainian alert jargon."""
    return (
        "Translate this Ukrainian/Russian air-defence update into concise, natural English for "
        "civilians in Kyiv. Use only facts explicitly present in the source. Never add a weapon type, "
        "destination, attribution, channel name, explanation, or missing context. Output ONLY English "
        "translation; no notes, disclaimers, alternatives, labels, quotation marks, or Cyrillic. Preserve every "
        "location, direction, quantity, time, uncertainty marker, and distinction between observed, "
        "reported, probable, intercepted, and confirmed events. Do not invent a weapon or destination.\n\n"
        "Mandatory alert glossary:\n"
        "- ракета / ракети = missile(s), NEVER cruise missile(s) unless the source says крилата or Бандероль\n"
        "- крилата ракета / крилаті / крилатих = cruise missile(s), never 'winged'\n"
        "- циркон / циркони = Zircon missile(s), never Circon or Circone\n"
        "- реактивний БпЛА / реактивні БпЛА / реактив = jet-powered UAV(s), never aircraft or reactor\n"
        "- БпЛА / шахед / мопед = UAV / Shahed drone as context permits\n"
        "- мінус = intercepted or neutralized threat, never 'minus'\n"
        "- чисто / не спостерігається = clear / no threats currently observed\n"
        "- відбій = all clear\n"
        "- відбій по балістиці = ballistic threat all clear, not all clear on ballistic missiles\n"
        "- локаційно втрачено = no longer tracked, never location lost\n"
        "- зникла = no longer observed/tracked, never lost\n"
        "- відвернула = turned away, never diverted or intercepted\n"
        "- без швидкісних = no high-speed targets, never no jet-powered UAVs\n"
        "- є влучання = an impact is reported; preserve singular and never add 'direct'\n"
        "- ППО працює = air defence is engaging\n"
        "- пуск / повторні пуски = launch / repeated launches\n"
        "- курсом на / в напрямку = heading toward\n"
        "- Бандероль / бандеролі / бандеролям = cruise missile(s), never S8000, parcel, package, or UAV\n"
        "- подарунки / посилки can be alert-channel euphemisms for incoming threats; never translate "
        "them literally as gifts or parcels. Name UAVs or missiles only when the source establishes it.\n\n"
        "Example: 'Без загроз по бандеролям, тривогу дали на реактивний в бік Броварів' means "
        "'No threat from cruise missiles. The alert was issued for a jet-powered UAV "
        "heading toward Brovary.'\n\n"
        "Never ask for more context. A one-word place, target, or outcome is intentional and must be "
        "translated as a one-word fragment. Examples: 'Дарниця' = 'Darnytsia'; 'ТЕЦ-5' = "
        "'CHP-5'; 'Збили' = 'Shot down'. Keep terse fragments terse. Retain Ukrainian place "
        "names using standard transliteration. Remove "
        "promo, subscribe, and LIVE tags. Known spellings include Kyiv, Brovary, Boryspil, Obukhiv, "
        "Vyshhorod, Bucha, Irpin, Fastiv, Bila Tserkva, Berezniaky, Osokorky, Pozniaky, Troieshchyna, "
        "Vyshneve, Dymer, Boyarka, Yahotyn, Hostomel, Rembaza, and Koncha-Zaspa.\n\n"
        "Source message:\n" + text
    )


def translate_known_terse_fragment(text):
    """Translate common one-line tactical fragments without model interpretation."""
    normalized = re.sub(r"[.!…\s]+$", "", text.strip()).casefold()
    return {
        "дарниця": "Darnytsia",
        "тец-5": "CHP-5",
        "тец 5": "CHP-5",
        "збили": "Shot down",
        "збито": "Shot down",
        "балістика на київ": "Ballistic threat to Kyiv!",
        "відбій по балістиці": "The ballistic threat has been lifted.",
        "по балістиці очікуємо на відбій": "The ballistic threat is expected to be lifted shortly.",
        "поки просто чекаємо на відбої по балістиці": "We are waiting for the ballistic threat to be lifted.",
    }.get(normalized)


def is_translation_meta_output(text):
    """Reject model commentary, refusals, and requests for more source context."""
    lowered = text.strip().casefold()
    forbidden = (
        "translation",
        "translate",
        "source message",
        "source text",
        "please provide",
        "need the full",
        "need more context",
        "no operational",
        "cannot provide",
        "unable to provide",
        "appears to be incomplete",
        "appears to be a partial",
        "appears to be a fragment",
        "i can only see",
        "i need the",
        "i'm ready",
        "you've provided",
    )
    return not lowered or any(marker in lowered for marker in forbidden)


def is_valid_alert_translation(text):
    """Only publish a non-meta English result; Ukrainian/Russian belongs in Ops, never public."""
    return not re.search(r"[А-Яа-яІіЇїЄєҐґ]", text or "") and not is_translation_meta_output(text)


def parse_first_json_object(raw):
    """Legacy fallback: decode the first JSON object and report trailing anomalies."""
    cleaned = raw.strip()
    cleaned = re.sub(r"^\x60\x60\x60(?:json)?\\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\\s*\x60\x60\x60$", "", cleaned)
    object_start = cleaned.find("{")
    if object_start < 0:
        raise json.JSONDecodeError("No JSON object found", cleaned, 0)
    parsed, end_index = json.JSONDecoder().raw_decode(cleaned[object_start:])
    trailing = cleaned[object_start + end_index:].strip()
    print(
        "[STRUCTURED OUTPUT LEGACY PARSER] primary json.loads failed; "
        f"first object extracted trailing_data={bool(trailing)}"
    )
    if not isinstance(parsed, dict):
        raise ValueError("AI response root must be a JSON object")
    return parsed
