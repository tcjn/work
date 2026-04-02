import os
import time
import random
import logging
import json
import requests
from datetime import datetime, timezone

import garth
from garth.exc import GarthHTTPError

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
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "3600"))
ADD_COMMENTS           = os.getenv("ADD_COMMENTS", "true").lower() == "true"


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
    delays = [30, 60, 120, 300]
    last_exc = None
    for attempt, delay in enumerate(delays, start=1):
        try:
            garth.login(email, password)
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


def _exchange_for_web_session() -> None:
    """Exchange OAuth2 token for connect.garmin.com web session cookies.

    Garth's SSO flow never visits connect.garmin.com, so modern/proxy endpoints
    have no session cookies.  The di-oauth/exchange endpoint converts a Bearer
    token into those cookies so subsequent requests to /modern/proxy/ work.
    """
    try:
        resp = garth.client.request(
            "GET", "connect", "/modern/di-oauth/exchange",
            api=True,
            headers={"NK": "NT"},
        )
        logger.debug(
            f"di-oauth exchange: status={resp.status_code} "
            f"cookies={list(garth.client.sess.cookies.keys())}"
        )
    except Exception as e:
        logger.debug(f"di-oauth exchange skipped: {e}")


def ensure_authenticated(email: str, password: str) -> None:
    os.makedirs(TOKEN_STORE, exist_ok=True)
    try:
        garth.load(TOKEN_STORE)
        garth.client.connectapi("/userprofile-service/userprofile/personal-information")
        logger.info("Reused saved session tokens")
        _exchange_for_web_session()
        return
    except Exception:
        logger.info("No valid saved session, logging in...")
    login_with_retry(email, password)
    garth.save(TOKEN_STORE)
    logger.info("Tokens saved to disk")
    _exchange_for_web_session()


# ---------- HTTP helpers (garth.client.request → correct subdomain + Bearer) ----------
# garth.client.request(method, subdomain, path, api=True) builds:
#   https://{subdomain}.garmin.com{path}  + Authorization: Bearer <oauth2>

_WEB_HEADERS = {
    "NK": "NT",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://connect.garmin.com/app/newsfeed",
    "X-app-ver": "4.70.1.0",
    "di-backend": "connectapi.garmin.com",
}


def _garth_get(subdomain: str, path: str, **kwargs):
    resp = garth.client.request(
        "GET", subdomain, path,
        api=True,
        headers=_WEB_HEADERS,
        **kwargs,
    )
    if resp.status_code == 204:
        return None
    ct = resp.headers.get("content-type", "")
    if "json" not in ct:
        logger.warning(
            f"Non-JSON from {subdomain} {path}: "
            f"status={resp.status_code} ct={ct} body={resp.text[:300]}"
        )
        return None
    return resp.json()


def _garth_put(subdomain: str, path: str, **kwargs) -> bool:
    try:
        garth.client.request(
            "PUT", subdomain, path,
            api=True,
            headers=_WEB_HEADERS,
            **kwargs,
        )
        return True
    except GarthHTTPError as e:
        logger.debug(f"PUT {subdomain}/{path} → {e}")
        return False


def _garth_post(subdomain: str, path: str, **kwargs) -> bool:
    try:
        garth.client.request(
            "POST", subdomain, path,
            api=True,
            headers=_WEB_HEADERS,
            **kwargs,
        )
        return True
    except GarthHTTPError as e:
        logger.debug(f"POST {subdomain}/{path} → {e}")
        return False


# ---------- newsfeed ----------

# Each entry: (subdomain, path_template)
# connect.garmin.com/modern/proxy/* routes to internal services via di-backend header
# connectapi.garmin.com/* uses OAuth Bearer directly
_FEED_CANDIDATES = [
    # modern proxy — relies on di-oauth exchange cookies + Bearer
    ("connect", "/modern/proxy/activitylist-service/activities/subscriptionFeed"),
    ("connect", "/modern/proxy/activitylist-service/activities/subscriptions"),
    # direct on connect (no proxy prefix)
    ("connect", "/activitylist-service/activities/subscriptionFeed"),
    # connectapi variants — some may still be alive
    ("connectapi", "/activitylist-service/activities/subscriptions"),
    ("connectapi", "/activitylist-service/activities/subscriptionFeed"),
    # social-service paths seen in reverse-engineering
    ("connectapi", "/social-service/social/connections/activities"),
    ("connectapi", "/community-user-api/community/activities/following"),
]


def _parse_activities(raw) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("activityList", "activities", "feedList", "items"):
            val = raw.get(key)
            if val:
                return val
        logger.debug(f"Feed dict keys: {list(raw.keys())}")
    return []


def get_feed(limit: int = 10) -> list:
    """Fetch up to `limit` recent activities from the social newsfeed."""
    page_size = min(limit, 20)

    for subdomain, ep in _FEED_CANDIDATES:
        collected: list = []
        try:
            for start in range(0, limit, page_size):
                raw = _garth_get(subdomain, ep, params={"start": start, "limit": page_size})
                if raw is None:
                    break
                page = _parse_activities(raw)
                logger.debug(f"{subdomain}{ep} start={start} → {len(page)} items")
                collected.extend(page)
                if len(page) < page_size:
                    break
            if collected:
                logger.info(f"Feed via {subdomain}{ep}: {len(collected)} activities")
                return collected
            else:
                logger.debug(f"Feed {subdomain}{ep}: 0 activities, trying next")
        except GarthHTTPError as e:
            logger.warning(f"Feed {subdomain}{ep} failed: {e}")
        except Exception as e:
            logger.warning(f"Feed {subdomain}{ep} error: {e}")

    logger.warning("All feed endpoints returned 0 activities")
    return []


# ---------- kudos / comment ----------

_KUDO_CANDIDATES = [
    ("connectapi", "/activity-service/activity/{id}/kudos"),
    ("connect",    "/modern/proxy/social-service/kudos/{id}"),
    ("connect",    "/modern/proxy/activity-service/activity/{id}/kudos"),
]

_COMMENT_CANDIDATES = [
    ("connectapi", "/comment-service/comment/activity/{id}"),
    ("connect",    "/modern/proxy/comment-service/comment/activity/{id}"),
]


def kudo_activity(activity_id: int) -> bool:
    for subdomain, tpl in _KUDO_CANDIDATES:
        path = tpl.format(id=activity_id)
        if _garth_put(subdomain, path):
            logger.debug(f"Kudos sent via {subdomain}{path}")
            return True
    logger.warning(f"All kudo endpoints failed for {activity_id}")
    return False


def comment_activity(activity_id: int) -> str | None:
    comment = random.choice(POLISH_COMMENTS)
    for subdomain, tpl in _COMMENT_CANDIDATES:
        path = tpl.format(id=activity_id)
        if _garth_post(subdomain, path, json={"comment": comment}):
            return comment
    logger.warning(f"All comment endpoints failed for {activity_id}")
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
        try:
            liked = process_feed(liked)
        except Exception as e:
            logger.error(f"Feed processing error: {e}")
            try:
                ensure_authenticated(email, password)
            except Exception as ex:
                logger.error(f"Re-auth failed: {ex}")

        logger.info(f"Sleeping {CHECK_INTERVAL_SECONDS}s...")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
