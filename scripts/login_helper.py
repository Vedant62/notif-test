"""LOCAL-ONLY helper: capture a fresh VTOP session cookie after you log in.

Run this on your own machine, never in CI. It opens a real, visible browser
to the VTOP login page and waits for *you* to log in and solve the CAPTCHA —
it never sees your password and never touches the CAPTCHA. Once you reach
the dashboard, it reads the cookie jar (including the HttpOnly session
cookie, which browser JS can't see but Playwright's context API can) and
prints a ready-to-paste Cookie header string.

Usage:
    pip install playwright
    playwright install chromium
    python scripts/login_helper.py                # print + save locally
    python scripts/login_helper.py --push          # also `gh secret set` it
    python scripts/login_helper.py --repo owner/name --push
"""

import argparse
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = "https://vtopcc.vit.ac.in"
LOGIN_WAIT_TIMEOUT_MS = 5 * 60 * 1000  # 5 minutes to log in by hand
LOCAL_COOKIE_FILE = Path(__file__).resolve().parent.parent / ".vtop_cookie"


def capture_cookie_string() -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(BASE_URL)

        print(
            "A browser window has opened. Log in manually (registration "
            "number, password, CAPTCHA) and navigate to the dashboard.\n"
            "Waiting up to 5 minutes for you to finish...",
            file=sys.stderr,
        )
        page.wait_for_url(f"{BASE_URL}/vtop/content", timeout=LOGIN_WAIT_TIMEOUT_MS)

        cookies = context.cookies(BASE_URL)
        cookie_string = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

        browser.close()
        return cookie_string


def push_to_github_secret(cookie_string: str, repo: str | None) -> None:
    # Fed via stdin rather than --body/argv so the cookie never shows up in
    # shell history or `ps` output.
    cmd = ["gh", "secret", "set", "VTOP_COOKIE"]
    if repo:
        cmd += ["--repo", repo]
    print(f"Running: {' '.join(cmd)} (reading cookie from stdin)...", file=sys.stderr)
    subprocess.run(cmd, input=cookie_string, text=True, check=True)
    print("GitHub secret VTOP_COOKIE updated.", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--push",
        action="store_true",
        help="Also push the captured cookie to the GitHub Actions secret via `gh secret set`.",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="owner/name to pass to `gh secret set --repo` (defaults to the current repo).",
    )
    args = parser.parse_args()

    cookie_string = capture_cookie_string()
    if not cookie_string:
        print("Could not capture any cookies — login may not have completed.", file=sys.stderr)
        return 1

    LOCAL_COOKIE_FILE.write_text(cookie_string + "\n")
    print(f"Cookie string written to {LOCAL_COOKIE_FILE} (git-ignored).")
    print("\nVTOP_COOKIE value:\n")
    print(cookie_string)

    if args.push:
        push_to_github_secret(cookie_string, args.repo)
    else:
        print(
            "\nNot pushed to GitHub (pass --push to do that automatically), "
            "paste it into the VTOP_COOKIE secret yourself:\n"
            "  gh secret set VTOP_COOKIE < .vtop_cookie"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
