"""
Mercor -> Telegram job alert bot.

Checks Mercor's public job list, finds listings it hasn't seen before,
filters them using config.yaml, and sends each match to Telegram.

Usage:
    python main.py                  # normal run (what GitHub Actions does)
    python main.py --dry-run        # print what would be sent; send/save nothing
    python main.py --test-telegram  # send one test message to check your setup

Needs two environment variables (GitHub secrets):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

# Public JSON endpoint the explore page itself uses (no login needed).
API_URL = "https://aws.api.mercor.com/work/listings-explore-page"
# Backup: the same data is embedded in the explore page's HTML.
EXPLORE_URL = "https://work.mercor.com/explore"
JOB_URL = "https://work.mercor.com/jobs/{id}"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.yaml")
SEEN_FILE = os.path.join(HERE, "seen.json")        # {listing_id: "YYYY-MM-DD" last seen}
STATE_FILE = os.path.join(HERE, "state.json")      # failure counter etc.
LAST_RUN_FILE = os.path.join(HERE, "last_run.txt")  # keep-alive timestamp

PRUNE_AFTER_DAYS = 60     # forget IDs not seen for this long
REFRESH_SEEN_DAYS = 7     # only rewrite a "last seen" date once a week (fewer commits)
KEEPALIVE_DAYS = 25       # touch last_run.txt at least this often
SUMMARY_THRESHOLD = 10    # more new matches than this -> one summary message
FAILURE_ALERT_AFTER = 3   # warn after this many failed runs in a row
MAX_COUNTRIES_SHOWN = 12  # keep messages short when a job lists 40 countries

GLOBAL_WORDS = re.compile(r"\b(worldwide|anywhere|global)\b", re.IGNORECASE)


# --------------------------------------------------------------------------
# Small file helpers
# --------------------------------------------------------------------------

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
        f.write("\n")


def today():
    return datetime.now(timezone.utc).date()


def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    # Fill in defaults so a half-edited config still works.
    return {
        "include_keywords": [str(k).lower() for k in (cfg.get("include_keywords") or [])],
        "exclude_keywords": [str(k).lower() for k in (cfg.get("exclude_keywords") or [])],
        "min_hourly_rate": float(cfg.get("min_hourly_rate") or 0),
        "eligible_countries": [str(c).upper() for c in (cfg.get("eligible_countries") or [])],
        "notify_if_eligibility_unknown": bool(cfg.get("notify_if_eligibility_unknown", True)),
    }


# --------------------------------------------------------------------------
# Fetching listings
# --------------------------------------------------------------------------

def make_session():
    """HTTP session with a browser-like User-Agent and polite retries."""
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json, text/html"})
    retry = Retry(
        total=3,
        backoff_factor=5,  # waits ~5s, 10s, 20s between attempts
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def fetch_from_api(session):
    r = session.get(API_URL, timeout=(10, 60))
    r.raise_for_status()
    return r.json()["listings"]


def fetch_from_html(session):
    """Backup: read the listings embedded in the explore page (__NEXT_DATA__)."""
    r = session.get(EXPLORE_URL, timeout=(10, 60))
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise ValueError("__NEXT_DATA__ not found on explore page")
    data = json.loads(m.group(1))
    queries = data["props"]["pageProps"]["dehydratedState"]["queries"]
    for q in queries:
        listings = (q.get("state", {}).get("data") or {}).get("listings")
        if listings is not None:
            return listings
    raise ValueError("no listings found in __NEXT_DATA__")


def fetch_listings():
    """Return a list of active, public listings. Raises if both sources fail."""
    session = make_session()
    try:
        raw = fetch_from_api(session)
    except Exception as e:
        print(f"API fetch failed ({e}); trying explore page HTML...")
        raw = fetch_from_html(session)

    listings = [
        x for x in raw
        if x.get("listingId") and x.get("status", "active") == "active" and not x.get("isPrivate")
    ]
    if not listings:
        # An empty result almost certainly means the format changed.
        raise ValueError("got 0 listings - site format may have changed")
    return listings


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------

def country_list(listing):
    """
    Which countries can apply?
      returns list  -> restricted to these ISO-3 codes (e.g. ["CAN"])
      returns []    -> open to everyone (Mercor sends an empty list)
      returns None  -> unknown (Mercor sends no country data at all)
    """
    elig = listing.get("eligibleLocation")
    if elig:  # non-empty list
        return elig
    # Some listings only fill in the residence field.
    residence = listing.get("eligibleResidenceLocation")
    if residence:
        return residence
    if elig is None:
        return None
    return []


def country_ok(listing, cfg):
    wanted = cfg["eligible_countries"]
    if not wanted:
        return True  # no country filter set

    # Explicitly excluded countries always win.
    blocked = set(listing.get("ineligibleLocation") or []) | set(
        listing.get("ineligibleResidenceLocation") or []
    )
    if all(c in blocked for c in wanted):
        return False

    if GLOBAL_WORDS.search(listing.get("location") or ""):
        return True

    countries = country_list(listing)
    if countries is None:
        return cfg["notify_if_eligibility_unknown"]
    if countries == []:
        return True  # global
    return any(c in countries for c in wanted)


def listing_text(listing):
    parts = [listing.get("title"), listing.get("description"), listing.get("listingDomain")]
    return " ".join(p for p in parts if p).lower()


def passes_filters(listing, cfg):
    """Return (True, "") if we should notify, else (False, reason)."""
    text = listing_text(listing)

    if cfg["include_keywords"] and not any(k in text for k in cfg["include_keywords"]):
        return False, "no include keyword"
    for k in cfg["exclude_keywords"]:
        if k in text:
            return False, f"exclude keyword '{k}'"

    # Only compare hourly pay; per-task / one-time pay can't be compared to an hourly figure.
    if cfg["min_hourly_rate"] > 0 and listing.get("payRateFrequency") == "hourly":
        top_rate = listing.get("rateMax") or listing.get("rateMin")
        if top_rate is not None and top_rate < cfg["min_hourly_rate"]:
            return False, f"rate {top_rate} below minimum"

    if not country_ok(listing, cfg):
        return False, "country not eligible"
    return True, ""


# --------------------------------------------------------------------------
# Message formatting
# --------------------------------------------------------------------------

def fmt_pay(listing):
    lo, hi = listing.get("rateMin"), listing.get("rateMax")
    if lo is None and hi is None:
        return "Not listed"
    lo = lo if lo is not None else hi
    hi = hi if hi is not None else lo
    amount = f"${lo:,.0f}" if lo == hi else f"${lo:,.0f}–${hi:,.0f}"
    unit = {"hourly": "/hr", "per-task": " per task", "one-time": " one-time"}.get(
        listing.get("payRateFrequency"), ""
    )
    return amount + unit


def fmt_countries(listing):
    if GLOBAL_WORDS.search(listing.get("location") or ""):
        return "Worldwide"
    countries = country_list(listing)
    if countries is None:
        return "Not specified"
    if countries == []:
        return "Worldwide"
    shown = ", ".join(countries[:MAX_COUNTRIES_SHOWN])
    extra = len(countries) - MAX_COUNTRIES_SHOWN
    return shown + (f" +{extra} more" if extra > 0 else "")


def fmt_commitment(listing):
    c = (listing.get("commitment") or "").replace("-", " ").capitalize() or "Not listed"
    hours = listing.get("hoursPerWeek")
    return f"{c} · {hours} hrs/week" if hours else c


def fmt_spots(listing):
    """'3 of 10 left', plus 'closing soon' once 90% are filled (same rule as
    Mercor's own badge). Returns None when Mercor gives no slot data."""
    total = listing.get("maxFutureHcg")
    filled = listing.get("suppliedSlots") or 0
    left = listing.get("remainingSlots")
    if left is None and total:
        left = max(total - filled, 0)
    if left is None:
        return None
    text = f"{left} of {total} left" if total else f"{left} left"
    if total and filled >= 0.9 * total:
        text += " · 🔥 closing soon"
    return text


def job_url(listing):
    return JOB_URL.format(id=listing["listingId"])


def format_listing(listing):
    e = html.escape
    location = listing.get("location") or "Not listed"
    arrangement = listing.get("workArrangement")
    if arrangement and arrangement.lower() not in location.lower():
        location += f" ({arrangement})"
    lines = [
        f"🆕 <b>{e(listing.get('title') or 'Untitled')}</b>",
        f"💰 {e(fmt_pay(listing))}",
        f"📍 {e(location)}",
        f"🌍 Eligible: {e(fmt_countries(listing))}",
        f"⏱ {e(fmt_commitment(listing))}",
    ]
    spots = fmt_spots(listing)
    if spots:
        lines.append(f"👥 Spots: {e(spots)}")
    if listing.get("listingDomain"):
        lines.append(f"🏷 {e(listing['listingDomain'])}")
    lines.append(f'🔗 <a href="{e(job_url(listing))}">View &amp; apply</a>')
    return "\n".join(lines)


def format_summary(listings):
    lines = [f"🆕 <b>{len(listings)} new Mercor listings</b>", ""]
    for x in listings:
        line = (
            f'• <a href="{html.escape(job_url(x))}">{html.escape(x.get("title") or "Untitled")}</a>'
            f" — {html.escape(fmt_pay(x))}"
        )
        spots = fmt_spots(x)
        if spots:
            line += f" · {html.escape(spots)}"
        # Telegram messages max out at 4096 characters.
        if len("\n".join(lines + [line])) > 3900:
            lines.append("…and more on https://work.mercor.com/explore")
            break
        lines.append(line)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def send_telegram(text, dry_run=False):
    """Send to every chat in TELEGRAM_CHAT_ID (comma-separated for several people).
    Succeeds if at least one chat got it, so one person blocking the bot
    doesn't stop everyone else's alerts."""
    if dry_run:
        print("----- would send -----")
        print(text)
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_ids = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    if not token or not chat_ids:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")

    errors = []
    for n, chat_id in enumerate(chat_ids, start=1):
        try:
            send_to_chat(token, chat_id, text)
        except Exception as e:
            # Print the chat's position, not its ID, to keep IDs out of the public log.
            print(f"Could not send to chat #{n}: {e}")
            errors.append(e)
    if len(errors) == len(chat_ids):
        raise RuntimeError(f"Telegram send failed for all chats: {errors[0]}")


def send_to_chat(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    for attempt in range(4):
        try:
            r = requests.post(url, json=payload, timeout=(10, 30))
        except requests.RequestException as e:
            # Don't print e directly: the URL inside it contains the token.
            print(f"Telegram network error ({type(e).__name__}), retrying...")
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 429:  # rate-limited: Telegram says how long to wait
            wait = r.json().get("parameters", {}).get("retry_after", 5)
            time.sleep(wait + 1)
            continue
        if r.ok:
            time.sleep(1)  # stay well under Telegram's ~1 msg/sec per chat limit
            return
        raise RuntimeError(f"Telegram error {r.status_code}: {r.text[:300]}")
    raise RuntimeError("Telegram send failed after retries")


# --------------------------------------------------------------------------
# Main logic
# --------------------------------------------------------------------------

def touch_keepalive(dry_run):
    """Rewrite last_run.txt every ~25 days so the repo always has recent activity
    (GitHub pauses scheduled workflows after 60 days without any)."""
    try:
        with open(LAST_RUN_FILE, encoding="utf-8") as f:
            last = datetime.fromisoformat(f.read().strip()).date()
    except (FileNotFoundError, ValueError):
        last = None
    if last is None or (today() - last).days >= KEEPALIVE_DAYS:
        if not dry_run:
            with open(LAST_RUN_FILE, "w", encoding="utf-8") as f:
                f.write(datetime.now(timezone.utc).replace(microsecond=0).isoformat() + "\n")
        print("Keep-alive timestamp updated.")


def handle_failure(error, state, dry_run):
    print(f"ERROR: could not fetch listings: {error}")
    if dry_run:
        return
    state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
    print(f"Failures in a row: {state['consecutive_failures']}")
    if state["consecutive_failures"] >= FAILURE_ALERT_AFTER and not state.get("failure_alert_sent"):
        try:
            send_telegram(
                "⚠️ <b>Mercor bot problem</b>\n"
                f"The last {state['consecutive_failures']} checks failed to read Mercor's job list.\n"
                f"Latest error: <code>{html.escape(str(error)[:300])}</code>\n"
                "Check the Actions tab on GitHub. I'll message you again when it recovers."
            )
            state["failure_alert_sent"] = True
        except Exception as e:
            print(f"Could not send failure alert: {e}")
    save_json(STATE_FILE, state)


def run(dry_run=False):
    cfg = load_config()
    state = load_json(STATE_FILE, {"consecutive_failures": 0, "failure_alert_sent": False})
    first_run = not os.path.exists(SEEN_FILE)
    seen = load_json(SEEN_FILE, {})

    touch_keepalive(dry_run)

    try:
        listings = fetch_listings()
    except Exception as e:
        handle_failure(e, state, dry_run)
        return  # exit 0: the Telegram alert is the warning, not 96 GitHub emails a day
    print(f"Fetched {len(listings)} listings.")

    # Fetch worked: reset the failure counter (and say so if we had warned).
    if state.get("failure_alert_sent"):
        send_telegram("✅ Mercor bot recovered — checks are working again.", dry_run)
    state = {"consecutive_failures": 0, "failure_alert_sent": False}

    new = [x for x in listings if x["listingId"] not in seen]

    if first_run:
        print(f"First run: marking all {len(listings)} listings as seen without notifying.")
        send_telegram(f"✅ Mercor bot is live — tracking {len(listings)} listings.", dry_run)
    else:
        matches = []
        for x in new:
            ok, reason = passes_filters(x, cfg)
            print(f"  NEW {'MATCH' if ok else 'skip '} {x['listingId']} {x.get('title')!r}"
                  + ("" if ok else f" ({reason})"))
            if ok:
                matches.append(x)
        print(f"{len(new)} new listings, {len(matches)} match your filters.")

        if len(matches) > SUMMARY_THRESHOLD:
            send_telegram(format_summary(matches), dry_run)
        else:
            for x in matches:
                send_telegram(format_listing(x), dry_run)
        # If sending raised an error above, we never reach the save below,
        # so these listings will be retried on the next run.

    # Update "last seen" dates and prune old IDs.
    t = today()
    for x in listings:
        last = seen.get(x["listingId"])
        if last is None or (t - datetime.fromisoformat(last).date()).days >= REFRESH_SEEN_DAYS:
            seen[x["listingId"]] = t.isoformat()
    cutoff = t - timedelta(days=PRUNE_AFTER_DAYS)
    seen = {k: v for k, v in seen.items() if datetime.fromisoformat(v).date() >= cutoff}

    if dry_run:
        print(f"[dry run] would save {len(seen)} seen IDs; nothing was sent or saved.")
        return
    save_json(SEEN_FILE, seen)
    save_json(STATE_FILE, state)


def main():
    p = argparse.ArgumentParser(description="Mercor -> Telegram job alerts")
    p.add_argument("--dry-run", action="store_true", help="print messages; send and save nothing")
    p.add_argument("--test-telegram", action="store_true", help="send a test message and exit")
    args = p.parse_args()

    if args.test_telegram:
        send_telegram("👋 Test from your Mercor bot — Telegram setup works!")
        print("Test message sent.")
        return
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {e}")
        sys.exit(1)
