import os
import re
import time
import random
import logging
import json
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
    "Niesamowite tempo! 🔥",
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

LIKED_ACTIVITIES_FILE = "/data/liked_activities.json"
HISTORY_FILE           = "/data/history.json"
TOKEN_STORE            = "/data/tokens"
COOKIES_FILE           = "/data/connect_cookies.json"
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "3600"))
ADD_COMMENTS           = os.getenv("ADD_COMMENTS", "true").lower() == "true"
MANUAL_COLLEAGUES      = [c.strip() for c in os.getenv("GARMIN_COLLEAGUES", "").split(",") if c.strip()]

CONNECT_BASE = "https://connect.garmin.com"


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

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


def save_connect_cookies() -> None:
    os.makedirs(os.path.dirname(COOKIES_FILE), exist_ok=True)
    cookies = {
        c.name: c.value
        for c in garth.client.sess.cookies
        if "garmin.com" in (c.domain or "")
    }
    with open(COOKIES_FILE, "w") as f:
        json.dump(cookies, f)


def load_connect_cookies() -> bool:
    try:
        with open(COOKIES_FILE) as f:
            cookies = json.load(f)
        for name, value in cookies.items():
            garth.client.sess.cookies.set(name, value)
        return bool(cookies)
    except (FileNotFoundError, json.JSONDecodeError):
        return False


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def setup_connect_web_session() -> bool:
    """Exchange the existing SSO session for a connect.garmin.com session cookie."""
    try:
        resp = garth.client.sess.get(
            "https://sso.garmin.com/sso/login",
            params={
                "service": "https://connect.garmin.com/modern",
                "gauthHost": "https://sso.garmin.com/sso",
                "clientId": "GarminConnect",
                "consumeServiceTicket": "false",
            },
            allow_redirects=False,
            timeout=15,
        )
        location = resp.headers.get("Location", "")
        logger.debug(f"SSO ticket response: status={resp.status_code} location={location[:300]}")
        m = re.search(r"[?&]ticket=([^&]+)", location)
        if not m:
            logger.warning(f"No SSO ticket in redirect (status={resp.status_code})")
            return False

        ticket = m.group(1)
        garth.client.sess.get(
            f"{CONNECT_BASE}/modern/",
            params={"ticket": ticket},
            allow_redirects=True,
            timeout=15,
        )
        cookie_names = [c.name for c in garth.client.sess.cookies]
        logger.info(f"Web session cookies: {cookie_names}")
        save_connect_cookies()
        logger.info("Established connect.garmin.com web session")
        return True
    except Exception as e:
        logger.warning(f"Could not set up web session: {e}")
        return False


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
    os.makedirs(TOKEN_STORE, exist_ok=True)

    # Try to reuse saved tokens
    try:
        garth.load(TOKEN_STORE)
        garth.client.connectapi("/userprofile-service/userprofile/personal-information")
        logger.info("Reused saved OAuth tokens")
        # Also restore web session cookies
        if load_connect_cookies():
            logger.info("Reused saved web session cookies")
        else:
            setup_connect_web_session()
        return
    except Exception:
        logger.info("No valid saved session, logging in...")

    login_with_retry(email, password)
    garth.save(TOKEN_STORE)
    logger.info("OAuth tokens saved")
    setup_connect_web_session()


# ---------------------------------------------------------------------------
# Garmin Connect API helpers
# ---------------------------------------------------------------------------

def web_get(path: str, **kwargs) -> dict | list:
    """GET via connect.garmin.com web session (cookie-based)."""
    resp = garth.client.sess.get(f"{CONNECT_BASE}{path}", **kwargs)
    logger.debug(f"GET {path} → {resp.status_code} body={resp.text[:300]}")
    resp.raise_for_status()
    if not resp.content:
        return []
    return resp.json()


def kudo_activity(activity_id: int) -> bool:
    try:
        garth.client.connectapi(f"/activity-service/activity/{activity_id}/kudos", method="PUT")
        return True
    except Exception as e:
        logger.warning(f"Failed to like {activity_id}: {e}")
        return False


def comment_activity(activity_id: int) -> str | None:
    comment = random.choice(POLISH_COMMENTS)
    try:
        garth.client.connectapi(
            f"/comment-service/comment/activity/{activity_id}",
            method="POST",
            json={"comment": comment},
        )
        return comment
    except Exception as e:
        logger.warning(f"Failed to comment on {activity_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# Social feed & connections
# ---------------------------------------------------------------------------

def get_social_feed(max_activities: int = 100) -> list:
    paths = [
        "/proxy/activitylist-service/activities/subscriptions",
        "/proxy/activitylist-service/activities/following",
    ]
    page_size = 20
    for path in paths:
        collected = []
        try:
            for start in range(0, max_activities, page_size):
                raw = web_get(path, params={"start": start, "limit": page_size})
                logger.debug(f"{path} start={start}: {str(raw)[:200]}")

                if isinstance(raw, list):
                    page = raw
                elif isinstance(raw, dict):
                    page = next(
                        (raw[k] for k in ("activityList", "activities", "items") if raw.get(k)),
                        [],
                    )
                else:
                    page = []

                collected.extend(page)
                if len(page) < page_size:
                    break

            if collected:
                logger.info(f"Social feed via {path}: {len(collected)} activities")
                return collected
        except Exception as e:
            logger.debug(f"Feed path {path} failed: {e}")

    return []


def get_connections() -> list:
    if MANUAL_COLLEAGUES:
        logger.info(f"Using manual colleague list: {MANUAL_COLLEAGUES}")
        return [{"displayName": n, "fullName": n} for n in MANUAL_COLLEAGUES]

    endpoints = [
        "/proxy/userprofile-service/socialProfile/connections",
        "/proxy/userprofile-service/socialProfile/followers",
        "/proxy/userprofile-service/socialProfile/following",
        "/proxy/connection-service/connection/connected",
    ]
    for ep in endpoints:
        try:
            data = web_get(ep, params={"start": 0, "limit": 100})
            logger.info(f"Connections [{ep}] raw: {str(data)[:300]}")
            if isinstance(data, list) and data:
                logger.info(f"Found {len(data)} connections via {ep}")
                return data
            if isinstance(data, dict):
                for key in ("connections", "userConnections", "connectionsList", "followers", "following"):
                    if data.get(key):
                        result = data[key]
                        logger.info(f"Found {len(result)} connections via {ep} key={key}")
                        return result
        except Exception as e:
            logger.warning(f"Connections {ep} → {e}")

    logger.warning(
        "Could not auto-discover connections. "
        "Add GARMIN_COLLEAGUES=displayname1,displayname2 in .env to override."
    )
    return []


def get_colleague_activities(display_name: str, limit: int = 10) -> list:
    path = f"/proxy/activitylist-service/activities/{display_name}"
    try:
        data = web_get(path, params={"start": 0, "limit": limit})
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("activityList", data.get("activities", []))
    except Exception as e:
        logger.warning(f"Failed to fetch activities for {display_name}: {e}")
    return []


# ---------------------------------------------------------------------------
# Like logic
# ---------------------------------------------------------------------------

def like_activity(activity: dict, owner: str, liked: set) -> set:
    activity_id = activity.get("activityId")
    if not activity_id or activity_id in liked:
        return liked
    if activity.get("userKudoed", False):
        liked.add(activity_id)
        return liked

    activity_name = activity.get("activityName", "—")
    activity_type = (
        activity.get("activityType", {}).get("typeKey", "unknown")
        if isinstance(activity.get("activityType"), dict)
        else activity.get("activityType", "unknown")
    )
    activity_date = activity.get("startTimeLocal") or activity.get("beginTimestamp", "—")

    logger.info(f'Processing: [{activity_type}] "{activity_name}" by {owner} on {activity_date}')
    time.sleep(random.uniform(3, 10))

    if kudo_activity(activity_id):
        liked.add(activity_id)
        save_liked_activities(liked)

        comment = None
        if ADD_COMMENTS:
            time.sleep(random.uniform(2, 5))
            comment = comment_activity(activity_id)

        append_history({
            "liked_at": datetime.now(timezone.utc).isoformat(),
            "activity_id": activity_id,
            "owner": owner,
            "activity_name": activity_name,
            "activity_type": activity_type,
            "activity_date": activity_date,
            "comment": comment,
        })
        logger.info(
            f'✓ Liked [{activity_type}] "{activity_name}" by {owner}'
            + (f" | comment: {comment}" if comment else "")
        )
    return liked


def process_feed(liked: set) -> set:
    logger.info("Fetching social feed...")
    # Fetch a large window so we always have old activities available as fallback
    activities = get_social_feed(max_activities=200)
    logger.info(f"Found {len(activities)} activities in feed")

    new_likes = 0
    for activity in activities:
        owner = activity.get("ownerDisplayName", "unknown")
        before = len(liked)
        liked = like_activity(activity, owner, liked)
        if len(liked) > before:
            new_likes += 1

    if new_likes == 0 and activities:
        # Feed returned activities but all already liked — nothing new to do
        logger.info("All feed activities already liked")
    elif new_likes == 0:
        # Feed was empty — fall back to per-colleague history
        logger.info("Feed empty — fetching last 10 activities per colleague")
        for conn in get_connections():
            display_name = conn.get("displayName") or conn.get("userProfileId")
            full_name = conn.get("fullName") or display_name
            if not display_name:
                continue
            col_activities = get_colleague_activities(str(display_name), limit=10)
            logger.info(f"  {full_name}: {len(col_activities)} activities fetched")
            for activity in col_activities:
                liked = like_activity(activity, full_name, liked)

    return liked


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    email = os.environ.get("GARMIN_EMAIL")
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
                logger.info("Re-authenticating...")
                ensure_authenticated(email, password)
            except Exception as ex:
                logger.error(f"Re-auth failed: {ex}")

        logger.info(f"Sleeping {CHECK_INTERVAL_SECONDS}s...")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
