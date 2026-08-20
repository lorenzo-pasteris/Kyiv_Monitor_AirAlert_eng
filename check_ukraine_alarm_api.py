"""One-shot, read-only UkraineAlarm credential and schema check."""

import json
import os
import urllib.request


key = os.environ["UKRAINE_ALARM_API_KEY"].strip()
request = urllib.request.Request(
    "https://api.ukrainealarm.com/api/v3/alerts",
    headers={"Authorization": key, "Accept": "application/json"},
)
with urllib.request.urlopen(request, timeout=15) as response:
    regions = json.load(response)

kyiv = next(
    (
        region
        for region in regions
        if {
            str(region.get("regionName", "")).strip().casefold(),
            str(region.get("regionEngName", "")).strip().casefold(),
        }
        & {"київ", "м. київ", "kyiv", "kyiv city"}
    ),
    None,
)
if kyiv is None:
    raise RuntimeError("API response is valid JSON but Kyiv City is missing")

active = any(
    str(alert.get("type", "")).upper() == "AIR"
    for alert in kyiv.get("activeAlerts") or []
    if isinstance(alert, dict)
)
print(f"UkraineAlarm API check passed: Kyiv City AIR={'ACTIVE' if active else 'CLEAR'}")
