"""Poll VTOP's facility registration endpoint and ntfy on status changes.

Runs on a schedule in GitHub Actions. Never logs in and never solves the
CAPTCHA — it reuses a session cookie captured by a manual login (see
scripts/login_helper.py and the README) and refreshes the CSRF token (which
rotates per request) from an authenticated GET before every state-changing
POST.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://vtopcc.vit.ac.in"
CONTENT_URL = f"{BASE_URL}/vtop/content"
FACILITY_URL = f"{BASE_URL}/vtop/phyedu/facilityAvailable"
STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "last_state.json"
FACILITY_FILTER = "gymnasium"  # substring match, case-insensitive
CSRF_RE = re.compile(r'name="_csrf"[^>]*value="([^"]+)"')

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def notify(topic: str, title: str, message: str) -> None:
    requests.post(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers={"Title": title},
        timeout=15,
    )


def fetch_csrf_token(session: requests.Session) -> str | None:
    resp = session.get(CONTENT_URL, timeout=20)
    match = CSRF_RE.search(resp.text)
    return match.group(1) if match else None


def fetch_facility_rows(session: requests.Session, csrf_token: str, reg_no: str) -> dict:
    resp = session.post(
        FACILITY_URL,
        data={
            "verifyMenu": "true",
            "authorizedID": reg_no,
            "_csrf": csrf_token,
            "nocache": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
        },
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": CONTENT_URL,
        },
        timeout=20,
    )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    facilities = {}
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        name = cells[0].get_text(strip=True)
        if FACILITY_FILTER not in name.lower():
            continue
        facilities[name] = {
            "fee": cells[1].get_text(strip=True),
            "seats_available": cells[2].get_text(strip=True),
            "status": cells[3].get_text(strip=True),
        }
    return facilities


def load_previous_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text()).get("facilities", {})
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(facilities: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(
            {
                "last_checked_utc": datetime.now(timezone.utc).isoformat(),
                "facilities": facilities,
            },
            indent=2,
        )
        + "\n"
    )


def diff_facilities(previous: dict, current: dict) -> list[str]:
    changes = []
    for name, info in current.items():
        prev_info = previous.get(name)
        if prev_info is None:
            continue  # first time we've seen this facility, nothing to compare
        status_changed = prev_info.get("status") != info["status"]
        seats_now_open = (
            prev_info.get("seats_available") in ("0", "")
            and info["seats_available"] not in ("0", "")
        )
        if status_changed or seats_now_open:
            changes.append(
                f"{name}: \"{prev_info.get('status')}\" -> \"{info['status']}\" "
                f"(seats: {info['seats_available']})"
            )
    return changes


def main() -> int:
    cookie = os.environ["VTOP_COOKIE"]
    reg_no = os.environ["VTOP_REG_NO"]
    ntfy_topic = os.environ["NTFY_TOPIC"]

    session = requests.Session()
    session.headers.update({"Cookie": cookie, "User-Agent": USER_AGENT})

    csrf_token = fetch_csrf_token(session)
    if not csrf_token:
        print("Session appears to be dead (no _csrf token found).", file=sys.stderr)
        notify(
            ntfy_topic,
            "VTOP session expired",
            "The stored VTOP_COOKIE is no longer valid. Log in manually and "
            "refresh the GitHub Actions secret to resume monitoring.",
        )
        return 0

    current = fetch_facility_rows(session, csrf_token, reg_no)
    if not current:
        print(
            f"No facilities matched filter '{FACILITY_FILTER}' — page shape may "
            "have changed.",
            file=sys.stderr,
        )
        return 0

    previous = load_previous_state()
    changes = diff_facilities(previous, current)

    if changes:
        notify(
            ntfy_topic,
            "VIT Gym Registration status changed",
            "\n".join(changes),
        )
        print("Change(s) detected and notified:\n" + "\n".join(changes))
    else:
        print("No change since last check.")

    save_state(current)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
