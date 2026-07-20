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
import ssl
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import certifi
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://vtopcc.vit.ac.in"
CONTENT_URL = f"{BASE_URL}/vtop/content"
FACILITY_URL = f"{BASE_URL}/vtop/phyedu/facilityAvailable"
STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "last_state.json"
FACILITY_FILTER = "gymnasium"  # substring match, case-insensitive
CSRF_RE = re.compile(r'name="_csrf"[^>]*value="([^"]+)"')
HEARTBEAT_INTERVAL = timedelta(minutes=15)
SESSION_EXPIRED_MESSAGE = (
    "The stored VTOP_COOKIE is no longer valid. Log in manually and "
    "refresh the GitHub Actions secret to resume monitoring."
)

# vtopcc.vit.ac.in serves an incomplete TLS chain (leaf cert only, no
# intermediate). Browsers paper over this with cached intermediates;
# `requests`/OpenSSL don't, and fail strict verification. The intermediate's
# own well-known distribution URL (from the leaf cert's Authority
# Information Access extension) is plain HTTP, so fetching it doesn't hit
# the same problem.
MISSING_INTERMEDIATE_URL = "http://crt.sectigo.com/SectigoRSADomainValidationSecureServerCA.crt"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def build_ca_bundle() -> str:
    """Return a CA bundle path = certifi's default + the missing intermediate.

    Falls back to certifi's default bundle (i.e. verification will behave as
    before, possibly failing) if the intermediate can't be fetched for any
    reason — this never silently disables verification.
    """
    default_bundle = certifi.where()
    try:
        der_bytes = urllib.request.urlopen(MISSING_INTERMEDIATE_URL, timeout=15).read()
        pem = ssl.DER_cert_to_PEM_cert(der_bytes)
    except Exception as exc:  # noqa: BLE001 - best-effort, fall back below
        print(f"Could not fetch missing intermediate cert: {exc}", file=sys.stderr)
        return default_bundle

    combined_path = Path(tempfile.gettempdir()) / "vtop_ca_bundle.pem"
    combined_path.write_text(Path(default_bundle).read_text() + "\n" + pem)
    return str(combined_path)


def notify(topic: str, title: str, message: str) -> None:
    requests.post(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers={"Title": title},
        timeout=15,
    )


class SessionExpired(Exception):
    """The stored VTOP_COOKIE no longer maps to a live, logged-in session."""


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
    if resp.status_code == 404:
        # VTOP returns a plain 404 (not 401/403) from this endpoint once the
        # session is dead — confirmed both for a fully logged-out request and
        # for a stale-but-plausible-looking cookie. Spring hands out a CSRF
        # token to anonymous sessions too, so `_csrf` being present on
        # /vtop/content is *not* sufficient proof of being logged in — this
        # 404 on the actual data endpoint is the reliable signal.
        raise SessionExpired
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
        return json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(facilities: dict, last_heartbeat_utc: str | None) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(
            {
                "last_checked_utc": datetime.now(timezone.utc).isoformat(),
                "last_heartbeat_utc": last_heartbeat_utc,
                "facilities": facilities,
            },
            indent=2,
        )
        + "\n"
    )


def heartbeat_due(last_heartbeat_utc: str | None) -> bool:
    if not last_heartbeat_utc:
        return True
    try:
        last = datetime.fromisoformat(last_heartbeat_utc)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - last >= HEARTBEAT_INTERVAL


def summarize_status(facilities: dict) -> str:
    statuses = {info["status"] for info in facilities.values()}
    if len(statuses) == 1:
        return f"All {len(facilities)} tracked gym slots: {statuses.pop()}"
    lines = [f"{name}: {info['status']}" for name, info in facilities.items()]
    return "\n".join(lines)


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
    session.verify = build_ca_bundle()

    csrf_token = fetch_csrf_token(session)
    if not csrf_token:
        # Extremely unlikely in practice (Spring hands out a CSRF token even
        # to anonymous sessions), but cheap to keep as a first-line check.
        print("Session appears to be dead (no _csrf token found).", file=sys.stderr)
        notify(ntfy_topic, "VTOP session expired", SESSION_EXPIRED_MESSAGE)
        return 0

    try:
        current = fetch_facility_rows(session, csrf_token, reg_no)
    except SessionExpired:
        print("Session appears to be dead (facilityAvailable returned 404).", file=sys.stderr)
        notify(ntfy_topic, "VTOP session expired", SESSION_EXPIRED_MESSAGE)
        return 0

    if not current:
        print(
            f"No facilities matched filter '{FACILITY_FILTER}' — page shape may "
            "have changed.",
            file=sys.stderr,
        )
        return 0

    previous_state = load_previous_state()
    changes = diff_facilities(previous_state.get("facilities", {}), current)
    last_heartbeat_utc = previous_state.get("last_heartbeat_utc")

    if changes:
        notify(
            ntfy_topic,
            "VIT Gym Registration status changed",
            "\n".join(changes),
        )
        print("Change(s) detected and notified:\n" + "\n".join(changes))
        # A change notification counts as "you've been informed" — no need
        # for a heartbeat right after it.
        last_heartbeat_utc = datetime.now(timezone.utc).isoformat()
    elif heartbeat_due(last_heartbeat_utc):
        last_heartbeat_utc = datetime.now(timezone.utc).isoformat()
        notify(
            ntfy_topic,
            "VIT Gym check-in",
            f"Checked at {last_heartbeat_utc} — still closed.\n"
            + summarize_status(current),
        )
        print("No change, but heartbeat was due — sent.")
    else:
        print("No change since last check; heartbeat not due yet.")

    save_state(current, last_heartbeat_utc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
