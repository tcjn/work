import os
import re
import time
import random
import logging
import json
import requests as _requests
from datetime import datetime, timezone

import garth
from garminconnect import Garmin
from garth.exc import GarthHTTPError


class SessionExpiredError(RuntimeError):
    """Raised when Garmin returns an HTML sign-in/SSO page instead of API JSON."""

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

POLISH_COMMENTS = [
    "Brawo! Świetna robota! 💪",
    "Gratulacje! Tak trzymaj! 🏆",
    "Niesamowita aktywność! 🔥",
    "Super wynik! Podziwiam! 👏",
    "Świetny trening! Dobra robota!",
    "Rewelacja! Jesteś w formie! 💯",
    "Kapitalnie! Tak dalej! 🚀",
    "No to sztos! Mega trening!",
    "Znakomicie! Wzorowa forma!",
    "Wspaniale! Niesamowite osiągnięcie!",
    "Dobra robota! Trzeba tak trzymać!",
    "Petarda! Wielkie gratulacje!",
    "Wow, świetny wynik! Brawo!",
    "Nieźle, nieźle! Tak dalej!",
    "Szacun! Świetna robota!",
    "Niesamowite! Jesteś maszyną! 🤖",
    "To jest to! Świetna aktywność!",
    "Robi wrażenie! Gratulacje!",
    "Bomba! Dobra passa!",
    "Ekstra! Tak trzymaj!",
]

LIKED_ACTIVITIES_FILE  = "/data/liked_activities.json"
HISTORY_FILE           = "/data/history.json"
TOKEN_STORE            = "/data/tokens"
TOKEN_STORE_FILE       = "/data/garmin_tokens.json"
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "3600"))
ADD_COMMENTS           = os.getenv("ADD_COMMENTS", "true").lower() == "true"

CONNECT_BASE = "https://connect.garmin.com"
SSO_BASE     = "https://sso.garmin.com/sso"
gc_client: Garmin | None = None


def _url_host(url: str) -> str:
    try:
        return _requests.utils.urlparse(url).netloc.lower()
    except Exception:
        return ""


def _is_connect_modern_url(url: str) -> bool:
    host = _url_host(url)
    return host == "connect.garmin.com"


def _url_host(url: str) -> str:
    try:
        return _requests.utils.urlparse(url).netloc.lower()
    except Exception:
        return ""


def _is_connect_modern_url(url: str) -> bool:
    host = _url_host(url)
    return host == "connect.garmin.com"


def _url_host(url: str) -> str:
    try:
        return _requests.utils.urlparse(url).netloc.lower()
    except Exception:
        return ""


def _is_connect_modern_url(url: str) -> bool:
    host = _url_host(url)
    return host == "connect.garmin.com"


# ---------- persistence ----------

def load_liked_activities() -> set:
    try:
        with open(LIKED_ACTIVITIES_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_liked_activities(liked: set) -> None:
    os.makedirs(os.path.dirname(LIKED_ACTIVITIES_FILE), exist_ok=True)
    with open(LIKED_ACTIVITIES_FILE, "w") as f:
        json.dump(list(liked), f)


def append_history(entry: dict) -> None:
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    try:
        with open(HISTORY_FILE) as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        history = []
    history.append(entry)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


# ---------- auth ----------

def login_with_retry(email: str, password: str) -> None:
    global gc_client
    delays = [30, 60, 120, 300]
    last_exc = None
    for attempt, delay in enumerate(delays, start=1):
        try:
            gc_client = Garmin(email, password)
            gc_client.login(tokenstore=TOKEN_STORE_FILE)
            logger.info("Successfully logged in to Garmin Connect")
            return
        except Exception as e:
            last_exc = e
            if "429" in str(e) or "Too Many Requests" in str(e):
                logger.warning(f"Rate limited. Waiting {delay}s before retry {attempt}/{len(delays)}...")
                time.sleep(delay)
            else:
                raise
    raise last_exc


def _session():
    if gc_client is not None:
        return gc_client.garth.sess
    return garth.client.sess


def _sso_web_cookie_exchange() -> bool:
    """
    Exchange garth's sso.garmin.com session cookies for connect.garmin.com
    web session cookies — no second credential login required.

    After garth.login(), garth.client.sess holds SSO cookies for sso.garmin.com.
    Standard CAS SSO: hitting sso/signin with service=connect.garmin.com/modern
    and valid SSO session cookies returns a redirect straight to connect.garmin.com
    (no credentials needed), which sets the web session cookies in garth.client.sess.
    """
    try:
        sess = _session()
        r = sess.get(
            f"{SSO_BASE}/signin",
            params={"service": f"{CONNECT_BASE}/modern"},
            allow_redirects=True,
            timeout=15,
        )
        logger.info(
            f"SSO web exchange: final_url={r.url} "
            f"status={r.status_code} "
            f"cookies={list(sess.cookies.keys())}"
        )
        # Success: we should have landed on connect.garmin.com host (not just a query param)
        return _is_connect_modern_url(r.url)
    except Exception as e:
        logger.warning(f"SSO web exchange failed: {e}")
        return False


def _sso_web_login_full(email: str, password: str) -> bool:
    """
    Full SSO web login for connect.garmin.com — used as fallback if the
    cookie exchange doesn't work.  Stores cookies in garth.client.sess so
    all subsequent web requests automatically carry them.
    """
    try:
        sess = _session()
        MODERN = f"{CONNECT_BASE}/modern"
        params = {
            "service": MODERN,
            "gauthHost": SSO_BASE,
            "locale": "en_US",
            "id": "gauth-widget",
            "clientId": "GarminConnect",
            "consumeServiceTicket": "false",
            "embedWidget": "false",
            "generateExtraServiceTicket": "true",
        }

        # 1. Get CSRF token
        r = sess.get(f"{SSO_BASE}/signin", params=params, timeout=15)
        csrf_m = re.search(r'name="_csrf"\s+value="(.+?)"', r.text)
        if not csrf_m:
            logger.warning("Full SSO: CSRF token not found in login page")
            return False

        # 2. Submit credentials
        r = sess.post(
            f"{SSO_BASE}/signin",
            params=params,
            data={
                "username": email,
                "password": password,
                "embed": "false",
                "_csrf": csrf_m.group(1),
            },
            allow_redirects=True,
            timeout=15,
        )

        logger.info(
            f"Full SSO web login: final_url={r.url} "
            f"cookies={list(sess.cookies.keys())}"
        )
        # Treat only a real host redirect as success (query-string "service=" is not enough)
        return _is_connect_modern_url(r.url)
    except Exception as e:
        logger.warning(f"Full SSO web login failed: {e}")
        return False


def ensure_authenticated(email: str, password: str) -> None:
    global gc_client
    os.makedirs(TOKEN_STORE, exist_ok=True)

    # Step 1: OAuth via python-garminconnect (mobile SSO flow)
    try:
        gc_client = Garmin(email, password)
        gc_client.login(tokenstore=TOKEN_STORE_FILE)
        gc_client.connectapi("/userprofile-service/userprofile/personal-information")
        logger.info("Reused saved OAuth session tokens")
    except Exception:
        logger.info("No valid saved session, logging in via OAuth...")
        login_with_retry(email, password)
        logger.info(f"OAuth tokens saved to {TOKEN_STORE_FILE}")

    # Step 2: Web session cookies for connect.garmin.com/modern/proxy endpoints
    # Try fast exchange first (reuses existing sso.garmin.com cookies from garth),
    # fall back to full web SSO login if needed.
    logger.info("Establishing connect.garmin.com web session...")
    if not _sso_web_cookie_exchange():
        logger.info("Cookie exchange failed, trying full SSO web login...")
        if not _sso_web_login_full(email, password):
            logger.warning("Could not establish web session — feed may fail")

    # Update session headers for web requests
    _session().headers.update({
        "NK": "NT",
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{CONNECT_BASE}/app/newsfeed",
    })


# ---------- HTTP helpers ----------
# All requests use garth.client.sess which now has both:
#   • SSO cookies  → accepted by connect.garmin.com/modern/proxy/*
#   • OAuth Bearer → added via api=True for connectapi.garmin.com

def _garth_api_get(path: str, **kwargs):
    """GET connectapi.garmin.com with OAuth Bearer (garth managed)."""
    try:
        if gc_client is not None:
            resp = gc_client.connectapi(path, params=kwargs.get("params"))
        else:
            resp = garth.client.connectapi(path, params=kwargs.get("params"))
        return resp
    except GarthHTTPError as e:
        raise


def _garth_api_request(path: str, method: str, **kwargs):
    if gc_client is not None:
        return gc_client.connectapi(path, method=method, **kwargs)
    return garth.client.connectapi(path, method=method, **kwargs)


def _web_get(path: str, **kwargs):
    """GET connect.garmin.com using SSO session cookies."""
    sess = _session()
    r = sess.get(f"{CONNECT_BASE}{path}", **kwargs)
    logger.debug(f"WEB GET {path} → {r.status_code}")
    r.raise_for_status()
    if r.status_code == 204 or not r.content:
        return None
    ct = r.headers.get("content-type", "")
    if "json" not in ct:
        snippet = (r.text or "")[:300]
        logger.warning(
            f"Non-JSON from {path}: status={r.status_code} "
            f"ct={ct} body={snippet}"
        )
        lowered = snippet.lower()
        if "garmin connect | sign in" in lowered or "garmin sso portal" in lowered:
            raise SessionExpiredError("Garmin web session appears expired")
        return None
    return r.json()


def _web_put(path: str, **kwargs) -> bool:
    sess = _session()
    try:
        r = sess.put(f"{CONNECT_BASE}{path}", **kwargs)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.debug(f"WEB PUT {path} failed: {e}")
        return False


def _web_post(path: str, **kwargs) -> bool:
    sess = _session()
    try:
        r = sess.post(f"{CONNECT_BASE}{path}", **kwargs)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.debug(f"WEB POST {path} failed: {e}")
        return False


# ---------- newsfeed ----------

_API_FEED_ENDPOINTS = [
    # Current activities endpoint (used by newer Garmin API wrappers).
    "/activitylist-service/activities/search/activities",
    # Legacy feed endpoints retained as fallbacks.
    "/activitylist-service/activities/subscriptionFeed",
    "/activitylist-service/activities/subscriptions",
]

# Backward-compatible alias for any older references/logging that still use this name.
_FEED_ENDPOINTS = _API_FEED_ENDPOINTS

_WEB_FEED_ENDPOINTS = [
    # Current activities endpoint via web proxy.
    "/modern/proxy/activitylist-service/activities/search/activities",
    # Legacy feed endpoints retained as fallbacks.
    "/modern/proxy/activitylist-service/activities/subscriptionFeed",
    "/modern/proxy/activitylist-service/activities/subscriptions",
]


def _parse_activities(raw) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("activityList", "activities", "feedList", "items", "results"):
            val = raw.get(key)
            if val:
                return val
        logger.debug(f"Feed dict keys: {list(raw.keys())}")
    return []


def get_feed(limit: int = 10) -> list:
    page_size = min(limit, 20)
    auth_failures = 0

    for ep in _API_FEED_ENDPOINTS:
        collected: list = []
        try:
            for start in range(0, limit, page_size):
                raw = _garth_api_get(ep, params={"start": start, "limit": page_size})
                if raw is None:
                    break
                page = _parse_activities(raw)
                logger.debug(f"API {ep} start={start} → {len(page)} items")
                collected.extend(page)
                if len(page) < page_size:
                    break
            if collected:
                logger.info(f"Feed via API {ep}: {len(collected)} activities")
                return collected
            logger.debug(f"Feed API {ep}: 0 activities, trying next")
        except Exception as e:
            logger.warning(f"Feed API {ep} failed: {e}")

    # 2) Fallback to web feed endpoints when API returns nothing/fails
    auth_failures = 0
    for ep in _WEB_FEED_ENDPOINTS:
        collected = []
        try:
            for start in range(0, limit, page_size):
                raw = _web_get(ep, params={"start": start, "limit": page_size})
                if raw is None:
                    break
                page = _parse_activities(raw)
                logger.debug(f"WEB {ep} start={start} → {len(page)} items")
                collected.extend(page)
                if len(page) < page_size:
                    break
            if collected:
                logger.info(f"Feed via web {ep}: {len(collected)} activities")
                return collected
            logger.debug(f"Feed {ep}: 0 activities, trying next")
        except SessionExpiredError:
            auth_failures += 1
            logger.warning(f"Feed {ep} indicates expired web session")
        except Exception as e:
            logger.warning(f"Feed web {ep} failed: {e}")

    if auth_failures == len(_WEB_FEED_ENDPOINTS):
        raise SessionExpiredError("All web feed endpoints redirected to sign-in")

    logger.warning("All feed endpoints returned 0 activities")
    return []


# ---------- kudos / comment ----------

def kudo_activity(activity_id: int) -> bool:
    # Try OAuth (connectapi) first — more reliable than web session for writes
    try:
        _garth_api_request(
            f"/activity-service/activity/{activity_id}/kudos",
            method="PUT",
        )
        return True
    except GarthHTTPError as e:
        logger.debug(f"connectapi kudo failed: {e}")

    # Fall back to web session
    return _web_put(f"/modern/proxy/social-service/kudos/{activity_id}")


def comment_activity(activity_id: int) -> str | None:
    comment = random.choice(POLISH_COMMENTS)
    # Try OAuth first
    try:
        _garth_api_request(
            f"/comment-service/comment/activity/{activity_id}",
            method="POST",
            json={"comment": comment},
        )
        return comment
    except GarthHTTPError as e:
        logger.debug(f"connectapi comment failed: {e}")

    # Fall back to web session
    if _web_post(
        f"/modern/proxy/comment-service/comment/activity/{activity_id}",
        json={"comment": comment},
    ):
        return comment
    return None


# ---------- processing ----------

def like_activity(activity: dict, liked: set) -> set:
    activity_id = activity.get("activityId")
    if not activity_id or activity_id in liked:
        return liked
    if activity.get("userKudoed", False):
        liked.add(activity_id)
        return liked

    owner = activity.get("ownerDisplayName", "unknown")
    name  = activity.get("activityName", "—")
    atype = (activity.get("activityType", {}) or {}).get("typeKey", "unknown")
    date  = activity.get("startTimeLocal") or activity.get("beginTimestamp", "—")

    logger.info(f'Processing: [{atype}] "{name}" by {owner} on {date}')
    time.sleep(random.uniform(3, 10))

    if kudo_activity(activity_id):
        liked.add(activity_id)
        save_liked_activities(liked)
        comment = None
        if ADD_COMMENTS:
            time.sleep(random.uniform(2, 5))
            comment = comment_activity(activity_id)
        append_history({
            "liked_at":      datetime.now(timezone.utc).isoformat(),
            "activity_id":   activity_id,
            "owner":         owner,
            "activity_name": name,
            "activity_type": atype,
            "activity_date": date,
            "comment":       comment,
        })
        logger.info(
            f'✓ Liked [{atype}] "{name}" by {owner}'
            + (f" | comment: {comment}" if comment else "")
        )

    return liked


def process_feed(liked: set) -> set:
    logger.info("Fetching newsfeed (last 10 activities)...")
    activities = get_feed(limit=10)
    logger.info(f"Retrieved {len(activities)} activities from newsfeed")

    new_likes = 0
    for activity in activities:
        before = len(liked)
        liked = like_activity(activity, liked)
        if len(liked) > before:
            new_likes += 1

    if new_likes > 0:
        logger.info(f"Liked {new_likes} new activities this run")
    else:
        logger.info("No new activities to like this run")

    return liked


# ---------- main ----------

def main():
    email    = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")

    if not email or not password:
        logger.error("GARMIN_EMAIL and GARMIN_PASSWORD are required")
        raise SystemExit(1)

    logger.info(f"Starting Garmin auto-like bot for {email}")
    logger.info(f"Interval: {CHECK_INTERVAL_SECONDS}s | Comments: {ADD_COMMENTS}")

    try:
        ensure_authenticated(email, password)
    except Exception as e:
        logger.error(f"Login failed: {e}")
        raise SystemExit(1)

    liked = load_liked_activities()
    logger.info(f"Loaded {len(liked)} previously liked activities")

    while True:
        should_sleep = True
        try:
            liked = process_feed(liked)
        except Exception as e:
            logger.error(f"Feed processing error: {e}")
            try:
                ensure_authenticated(email, password)
                try:
                    # Only skip backoff if we can verify feed access after re-auth.
                    get_feed(limit=1)
                    should_sleep = False
                    logger.info(
                        "Re-authenticated and verified feed access; retrying immediately"
                    )
                except Exception as probe_ex:
                    logger.warning(
                        f"Re-auth succeeded but feed probe failed: {probe_ex}; "
                        "keeping normal backoff"
                    )
            except Exception as ex:
                logger.error(f"Re-auth failed: {ex}")

        if should_sleep:
            logger.info(f"Sleeping {CHECK_INTERVAL_SECONDS}s...")
            time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
