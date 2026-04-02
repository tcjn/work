import os
import time
import random
import logging
import json
import requests
from datetime import datetime, timezone

import garth

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

CONNECT_BASE = "https://connect.garmin.com"

# Web session used for all connect.garmin.com requests
_web_session: requests.Session | None = None


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


def ensure_authenticated(email: str, password: str) -> None:
    global _web_session
    os.makedirs(TOKEN_STORE, exist_ok=True)
    try:
        garth.load(TOKEN_STORE)
        garth.client.connectapi("/userprofile-service/userprofile/personal-information")
        logger.info("Reused saved session tokens")
    except Exception:
        logger.info("No valid saved session, logging in...")
        login_with_retry(email, password)
        garth.save(TOKEN_STORE)
        logger.info("Tokens saved to disk")

    _web_session = _build_web_session()


def _build_web_session() -> requests.Session:
    """Build a requests.Session authenticated for connect.garmin.com."""
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "NK": "NT",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": CONNECT_BASE,
        "Referer": f"{CONNECT_BASE}/app/newsfeed",
    })

    # Prefer garth's own underlying session (carries SSO cookies)
    garth_inner = None
    for attr in ("sess", "session", "_session", "_sess"):
        candidate = getattr(garth.client, attr, None)
        if candidate is None:
            continue
        if hasattr(candidate, "cookies") and hasattr(candidate, "get"):
            garth_inner = candidate
            break
        # garth wraps another session one level deeper
        for inner_attr in ("sess", "session", "_session", "_sess"):
            inner = getattr(candidate, inner_attr, None)
            if inner is not None and hasattr(inner, "cookies"):
                garth_inner = inner
                break
        if garth_inner:
            break

    if garth_inner is not None:
        sess.cookies.update(garth_inner.cookies)
        logger.debug("Copied garth SSO cookies to web session")
    else:
        logger.debug("Could not access garth inner session; using OAuth Bearer only")

    # Always attach OAuth Bearer token as a fallback / primary auth
    try:
        token = garth.client.oauth2_token
        if token and token.access_token:
            sess.headers["Authorization"] = f"Bearer {token.access_token}"
            logger.debug("Attached OAuth Bearer token to web session")
    except Exception as e:
        logger.warning(f"Could not attach OAuth token: {e}")

    return sess


def _web_session_get() -> requests.Session:
    if _web_session is None:
        raise RuntimeError("Web session not initialised — call ensure_authenticated() first")
    return _web_session


# ---------- web API helpers ----------

def web_get(path: str, **kwargs) -> list | dict:
    sess = _web_session_get()
    r = sess.get(f"{CONNECT_BASE}{path}", **kwargs)
    logger.debug(f"GET {path} → {r.status_code}")
    r.raise_for_status()
    return r.json()


def web_put(path: str, **kwargs) -> requests.Response:
    sess = _web_session_get()
    r = sess.put(f"{CONNECT_BASE}{path}", **kwargs)
    logger.debug(f"PUT {path} → {r.status_code}")
    r.raise_for_status()
    return r


def web_post(path: str, **kwargs) -> requests.Response:
    sess = _web_session_get()
    r = sess.post(f"{CONNECT_BASE}{path}", **kwargs)
    logger.debug(f"POST {path} → {r.status_code}")
    r.raise_for_status()
    return r


# ---------- newsfeed ----------

_FEED_ENDPOINTS = [
    "/activityfeed/feed/activities",
    "/modern/proxy/activitylist-service/activities/subscriptionFeed",
    "/modern/proxy/activitylist-service/activities/subscriptions",
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
    """Fetch up to `limit` recent activities from the newsfeed."""
    page_size = min(limit, 20)
    for ep in _FEED_ENDPOINTS:
        collected: list = []
        try:
            for start in range(0, limit, page_size):
                raw = web_get(ep, params={"start": start, "limit": page_size})
                page = _parse_activities(raw)
                logger.debug(f"{ep} start={start} → {len(page)} items")
                collected.extend(page)
                if len(page) < page_size:
                    break
            if collected:
                logger.info(f"Feed via {ep}: {len(collected)} activities")
                return collected
        except Exception as e:
            logger.warning(f"Feed endpoint {ep} failed: {e}")

    logger.warning("All feed endpoints failed — no activities retrieved")
    return []


# ---------- like / comment ----------

_KUDO_ENDPOINTS = [
    "/modern/proxy/social-service/kudos/{id}",
    "/social/kudos/{id}",
    "/modern/proxy/activity-service/activity/{id}/kudos",
]

_COMMENT_ENDPOINTS = [
    "/modern/proxy/comment-service/comment/activity/{id}",
    "/modern/proxy/activity-service/activity/{id}/comments",
]


def kudo_activity(activity_id: int) -> bool:
    for tpl in _KUDO_ENDPOINTS:
        path = tpl.format(id=activity_id)
        try:
            web_put(path)
            return True
        except Exception as e:
            logger.debug(f"Kudo via {path} failed: {e}")
    logger.warning(f"All kudo endpoints failed for activity {activity_id}")
    return False


def comment_activity(activity_id: int) -> str | None:
    comment = random.choice(POLISH_COMMENTS)
    for tpl in _COMMENT_ENDPOINTS:
        path = tpl.format(id=activity_id)
        try:
            web_post(path, json={"comment": comment})
            return comment
        except Exception as e:
            logger.debug(f"Comment via {path} failed: {e}")
    logger.warning(f"All comment endpoints failed for activity {activity_id}")
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
