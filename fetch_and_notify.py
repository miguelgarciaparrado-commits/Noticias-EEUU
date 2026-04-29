#!/usr/bin/env python3
"""
Daily Economic Calendar WhatsApp Notifier
Fetches high-impact events from ForexFactory and sends via WhatsApp (CallMeBot).

Key behaviors:
- Send window: 07:30 - 08:30 local time (tolerates GitHub Actions cron lag).
- Anti-duplicate: writes a marker file 'last_sent.txt' so the workflow can
  detect "already sent today" via an artifact and skip subsequent runs.
- Includes High impact (3-star) events for all countries plus Medium impact
  events for USD (which Investing often shows as 3-star).
"""

import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ---- Config ----
LOCAL_TZ = ZoneInfo("Europe/Madrid")
TARGET_HOUR = 8                # Send around 08:00 local time
WINDOW_START_MIN = 7 * 60  # 07:30
WINDOW_END_MIN = 12 * 60   # 08:30
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"
WEEKDAYS_ONLY = True

# Marker file used by the workflow's artifact mechanism for anti-duplicate.
MARKER_FILE = Path("last_sent.txt")

# Countries where we also include Medium impact (not just High)
EXTRA_MEDIUM_COUNTRIES = {"USD"}

FLAGS = {
    "USD": "🇺🇸", "EUR": "🇪🇺", "GBP": "🇬🇧", "JPY": "🇯🇵",
    "CAD": "🇨🇦", "AUD": "🇦🇺", "NZD": "🇳🇿", "CHF": "🇨🇭",
    "CNY": "🇨🇳",
}

WEEKDAYS_ES = ["lunes", "martes", "miércoles", "jueves",
               "viernes", "sábado", "domingo"]


def log(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}")


def already_sent_today() -> bool:
    """Check the marker file to see if today's send already happened."""
    if not MARKER_FILE.exists():
        return False
    try:
        last = MARKER_FILE.read_text().strip()
    except OSError:
        return False
    today_str = datetime.now(LOCAL_TZ).date().isoformat()
    return last == today_str


def write_marker() -> None:
    today_str = datetime.now(LOCAL_TZ).date().isoformat()
    MARKER_FILE.write_text(today_str)
    log(f"Wrote marker for {today_str}")


def should_run_now() -> bool:
    """Decide whether to proceed with sending."""
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        log("Manual trigger — skipping time/dedup checks.")
        return True

    now_local = datetime.now(LOCAL_TZ)

    if WEEKDAYS_ONLY and now_local.weekday() >= 5:
        log(f"Weekend ({WEEKDAYS_ES[now_local.weekday()]}). Skipping.")
        return False

    minutes = now_local.hour * 60 + now_local.minute
    if not (WINDOW_START_MIN <= minutes <= WINDOW_END_MIN):
        log(f"Local time {now_local:%H:%M %Z} outside window 07:30-08:30. Skipping.")
        return False

    if already_sent_today():
        log("Already sent today (marker file present). Skipping.")
        return False

    return True


def fetch_events() -> list[dict]:
    log(f"Fetching {FF_URL}")
    r = requests.get(
        FF_URL,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0 (compatible; DailyEconNotifier/1.0)"},
    )
    r.raise_for_status()
    return r.json()


def should_include(impact: str, country: str) -> bool:
    if impact == "High":
        return True
    if impact == "Medium" and country in EXTRA_MEDIUM_COUNTRIES:
        return True
    return False


def filter_today_events(events: list[dict]) -> list[dict]:
    today = datetime.now(LOCAL_TZ).date()
    result = []
    for e in events:
        impact = e.get("impact", "")
        country = e.get("country", "")
        if not should_include(impact, country):
            continue
        try:
            dt = datetime.fromisoformat(e["date"]).astimezone(LOCAL_TZ)
        except (KeyError, ValueError, TypeError):
            continue
        if dt.date() != today:
            continue
        result.append({
            "time": dt.strftime("%H:%M"),
            "country": country or "?",
            "title": e.get("title", "?"),
            "forecast": (e.get("forecast") or "").strip(),
            "previous": (e.get("previous") or "").strip(),
            "impact": impact,
        })
    result.sort(key=lambda x: x["time"])
    return result


def format_message(events: list[dict]) -> str:
    now = datetime.now(LOCAL_TZ)
    header_date = f"{WEEKDAYS_ES[now.weekday()].capitalize()} {now:%d/%m/%Y}"
    if not events:
        return f"📅 *{header_date}*\n\n✅ No hay eventos de alto impacto hoy."

    high_count = sum(1 for e in events if e["impact"] == "High")
    med_count = len(events) - high_count
    subtitle = f"_{high_count} evento(s) 3⭐"
    if med_count:
        subtitle += f" + {med_count} de 2⭐ USD"
    subtitle += "_"

    lines = [f"📅 *Eventos importantes — {header_date}*", subtitle, ""]
    for e in events:
        flag = FLAGS.get(e["country"], "🏳️")
        stars = "⭐⭐⭐" if e["impact"] == "High" else "⭐⭐"
        lines.append(f"🕐 *{e['time']}* {flag} {e['country']} {stars} — {e['title']}")
        details = []
        if e["forecast"]:
            details.append(f"F: {e['forecast']}")
        if e["previous"]:
            details.append(f"P: {e['previous']}")
        if details:
            lines.append(f"       _{' · '.join(details)}_")
    return "\n".join(lines)


def send_whatsapp(message: str) -> None:
    phone = os.environ["WA_PHONE"]
    apikey = os.environ["WA_APIKEY"]
    params = {"phone": phone, "text": message, "apikey": apikey}
    log(f"Sending WhatsApp to +{phone}")
    r = requests.get(CALLMEBOT_URL, params=params, timeout=30)
    log(f"Response status: {r.status_code}")
    body_preview = r.text[:200].replace("\n", " ")
    log(f"Response body: {body_preview}")
    r.raise_for_status()


def main() -> int:
    try:
        if not should_run_now():
            return 0
        events = fetch_events()
        log(f"Fetched {len(events)} total events")
        today_events = filter_today_events(events)
        log(f"Found {len(today_events)} relevant event(s) today")
        message = format_message(today_events)
        print("--- Message ---")
        print(message)
        print("---------------")
        send_whatsapp(message)
        # Only mark as sent for scheduled runs (manual runs shouldn't block tomorrow)
        if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
            write_marker()
        log("Done.")
        return 0
    except Exception as exc:
        log(f"ERROR: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
