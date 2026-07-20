# gym-notif

Watches VIT's VTOP portal for gym slot registration opening, and pushes a
notification to your phone via [ntfy](https://ntfy.sh) when it does.

## How it works

VTOP (`https://vtopcc.vit.ac.in`) is a login-walled, server-rendered portal.
Facility registration status lives behind:

- `POST /vtop/phyedu/facilityAvailable` — returns an HTML fragment (not JSON)
  with one row per facility, including fee, seats available, and a status
  cell ("Registration Closed" / open).

Login is CAPTCHA-gated, so this project **never automates login**. Instead:

1. You log in once, manually, in a real browser.
2. A session cookie from that login is stored as a GitHub Actions secret.
3. A workflow polls the facility endpoint every 5 minutes using that cookie
   (refreshing VTOP's short-lived CSRF token from an authenticated page on
   every run — the CSRF token rotates per-request, the cookie doesn't).
4. Polling every 5 minutes is *more frequent* than VTOP's idle session
   timeout (~15-30 min), so the polling itself should keep the session
   alive indefinitely. If the session ever does die anyway (Actions outage,
   VTOP maintenance, etc.), the workflow sends you **one** ntfy notification
   telling you to log in again and refresh the secret — it won't fail
   silently or spam you.
5. Only gym facilities are tracked (rows whose name contains "Gymnasium").
   Change `FACILITY_FILTER` in `scripts/check.py` to watch something else.

## One-time setup

### 1. Repo secrets

In your GitHub repo → Settings → Secrets and variables → Actions, add:

| Secret         | Value                                                              |
|----------------|---------------------------------------------------------------------|
| `VTOP_COOKIE`  | Your VTOP session cookie string (see below)                        |
| `VTOP_REG_NO`  | Your registration number, e.g. `23BCE5094`                         |
| `NTFY_TOPIC`   | A private, hard-to-guess topic name, e.g. `vedant-vtop-gym-x7f2q9`  |

**ntfy has no auth** — anyone who knows the topic name can read (or post to)
it. Pick a long random-ish topic name, don't share it.

### 2. Getting `VTOP_COOKIE`

**Option A — DevTools (no extra install):**
1. Log into VTOP normally in your browser.
2. Open DevTools → Application (Chrome) / Storage (Firefox) → Cookies →
   `https://vtopcc.vit.ac.in`.
3. Copy every `name=value` pair shown for that domain, joined with `; `,
   e.g. `SERVERID=abc; JSESSIONID=xyz`.
4. Paste that whole string as the `VTOP_COOKIE` secret.

**Option B — helper script (does step 2-3 for you):**
```bash
pip install playwright
playwright install chromium
python scripts/login_helper.py            # prints the cookie string, saves to .vtop_cookie
python scripts/login_helper.py --push     # also runs `gh secret set VTOP_COOKIE` for you
```
This opens a real browser window — you log in and solve the CAPTCHA
yourself, same as always. The script never sees your password.

### 3. Subscribe to notifications

Install the [ntfy app](https://ntfy.sh/#install) (iOS/Android) or open
`https://ntfy.sh/<your-topic>` in a browser, and subscribe to your topic.

### 4. Push this repo and enable Actions

Push to a GitHub repo (private recommended, since it's tied to your student
account setup) and make sure Actions is enabled. Trigger one manual run to
confirm everything's wired up:

```bash
gh workflow run check.yml
gh run watch
```

## When the session dies

You'll get an ntfy notification saying so. Just re-run
`scripts/login_helper.py` (or repeat the DevTools copy) and update the
`VTOP_COOKIE` secret. Everything else keeps working as-is.

## Reconfiguring

- **Check frequency:** edit the `cron` line in
  `.github/workflows/check.yml`. GitHub's minimum is every 5 minutes; keep
  it comfortably under VTOP's idle timeout.
- **Which facility to watch:** edit `FACILITY_FILTER` in
  `scripts/check.py` (currently `"gymnasium"`, case-insensitive substring
  match against the facility name column).
- **ntfy topic:** update the `NTFY_TOPIC` secret and re-subscribe in the app.
- **A different page entirely:** the interesting bits are
  `fetch_csrf_token()` (grabs a fresh CSRF token from any authenticated
  page) and `fetch_facility_rows()` (the actual endpoint + parsing) in
  `scripts/check.py` — swap the URL and the BeautifulSoup selectors.

## Known risk

This polls an authenticated endpoint on VIT's student portal on a schedule.
No `robots.txt` or visible Terms-of-Service page was found during setup, so
there's no explicit crawl-rate guidance either way — but many college
portals have blanket acceptable-use clauses against automated access even
when authenticated. This is intended for personal, low-frequency, read-only
use (checking a status field, never submitting the registration form). If
VIT ever asks you to stop, stop.
