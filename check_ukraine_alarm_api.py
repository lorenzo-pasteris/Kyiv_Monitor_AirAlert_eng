"""One-shot, read-only UkraineAlarm credential and schema check."""

import json
import os
import urllib.request

from text_processing import parse_ukraine_alarm_kyiv_state


key = os.environ["UKRAINE_ALARM_API_KEY"].strip()
request = urllib.request.Request(
    "https://api.ukrainealarm.com/api/v3/alerts/31",
    headers={"Authorization": key, "Accept": "application/json"},
)
with urllib.request.urlopen(request, timeout=15) as response:
    regions = json.load(response)

active = parse_ukraine_alarm_kyiv_state(regions)
print(f"UkraineAlarm API check passed: Kyiv City AIR={'ACTIVE' if active else 'CLEAR'}")
