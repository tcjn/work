import os
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
HISTORY_FILE = "/data/history.json"
TOKEN_STORE = "/data/tokens"
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "3600"))
ADD_COMMENTS = os.getenv("ADD_COMMENTS", "true").lower() == "true"


def load_liked_activities() -> set:
    try:
        with open(LIKED_ACTIVITIES_FILE, "r") as f:
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
        with open(HISTORY_FILE, "r") as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        history = []
    history.append(entry)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


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
                logger.warning(f"Rate limited (429). Waiting {delay}s before retry {attempt}/{len(delays)}...")
                time.sleep(delay)
            else:
                raise
    raise last_exc


def ensure_authenticated(email: str, password: str) -> None:
    """Load saved tokens or perform a fresh login."""
    os.makedirs(TOKEN_STORE, exist_ok=True)
    try:
        garth.load(TOKEN_STORE)
        # Verify tokens are still valid
        garth.client.get("connect", "/proxy/userprofile-service/userprofile/personal-information")
        logger.info("Reused saved session tokens — no login needed")
        return
    except Exception:
        logger.info("No valid saved session, logging in...")

    login_with_retry(email, password)
    garth.save(TOKEN_STORE)
    logger.info("Session tokens saved to disk")


def kudo_activity(activity_id: int) -> bool:
    try:
        garth.client.put("connect", f"/proxy/activity-service/activity/{activity_id}/kudos")
        return True
    except Exception as e:
        logger.warning(f"Failed to like activity {activity_id}: {e}")
        return False


def comment_activity(activity_id: int) -> str | None:
    comment = random.choice(POLISH_COMMENTS)
    try:
        garth.client.post(
            "connect",
            f"/proxy/comment-service/comment/activity/{activity_id}",
            json={"comment": comment},
        )
        return comment
    except Exception as e:
        logger.warning(f"Failed to comment on activity {activity_id}: {e}")
        return None


def garmin_get(path: str, **kwargs) -> dict | list:
    """Authenticated GET to connect.garmin.com/proxy/{path}."""
    resp = garth.client.get("connect", f"/proxy{path}", **kwargs)
    return resp.json()


def get_social_feed(max_activities: int = 100) -> list:
    paths = [
        "/activitylist-service/activities/subscriptions",
        "/activitylist-service/activities/following",
    ]
    page_size = 20
    for path in paths:
        collected = []
        try:
            for start in range(0, max_activities, page_size):
                raw = garmin_get(path, params={"start": start, "limit": page_size})
                logger.debug(f"Raw response from {path} start={start}: {str(raw)[:500]}")

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
                logger.info(f"Feed path: {path}, total activities: {len(collected)}")
                return collected
        except Exception as e:
            logger.warning(f"Path {path} failed: {e}")

    logger.warning("All feed endpoints returned 0 activities. Check that you follow people on Garmin Connect.")
    return []


def get_connections() -> list:
    """Return list of connections (people you follow)."""
    try:
        data = garmin_get("/userprofile-service/socialProfile/connections", params={"start": 0, "limit": 100})
        logger.debug(f"Connections raw: {str(data)[:500]}")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("connections", data.get("userConnections", []))
    except Exception as e:
        logger.warning(f"Failed to fetch connections: {e}")
    return []


def get_colleague_activities(display_name: str, limit: int = 10) -> list:
    try:
        data = garmin_get(f"/activitylist-service/activities/{display_name}", params={"start": 0, "limit": limit})
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("activityList", data.get("activities", []))
    except Exception as e:
        logger.warning(f"Failed to fetch activities for {display_name}: {e}")
    return []


def like_activity(activity: dict, owner: str, liked: set) -> set:
    activity_id = activity.get("activityId")
    if not activity_id or activity_id in liked:
        return liked
    if activity.get("userKudoed", False):
        liked.add(activity_id)
        return liked

    activity_name = activity.get("activityName", "—")
    activity_type = activity.get("activityType", {}).get("typeKey", "unknown") if isinstance(activity.get("activityType"), dict) else activity.get("activityType", "unknown")
    activity_date = activity.get("startTimeLocal") or activity.get("beginTimestamp", "—")

    logger.info(f"Processing: [{activity_type}] \"{activity_name}\" by {owner} on {activity_date}")
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
            f"✓ Liked [{activity_type}] \"{activity_name}\" by {owner}"
            + (f" | comment: {comment}" if comment else "")
        )
    return liked


def process_feed(liked: set) -> set:
    logger.info("Fetching social feed...")
    activities = get_social_feed()
    logger.info(f"Found {len(activities)} activities in feed")

    new_likes = 0
    for activity in activities:
        owner = activity.get("ownerDisplayName", "unknown")
        before = len(liked)
        liked = like_activity(activity, owner, liked)
        if len(liked) > before:
            new_likes += 1

    if new_likes == 0:
        logger.info("No new activities to like in feed — falling back to last 10 activities per colleague")
        connections = get_connections()
        logger.info(f"Found {len(connections)} connections")
        for conn in connections:
            display_name = conn.get("displayName") or conn.get("userProfileId")
            full_name = conn.get("fullName") or display_name
            if not display_name:
                continue
            colleague_activities = get_colleague_activities(str(display_name))
            logger.info(f"  {full_name}: {len(colleague_activities)} activities fetched")
            for activity in colleague_activities:
                liked = like_activity(activity, full_name, liked)

    return liked


def main():
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")

    if not email or not password:
        logger.error("GARMIN_EMAIL and GARMIN_PASSWORD environment variables are required")
        raise SystemExit(1)

    logger.info(f"Starting Garmin auto-like bot for {email}")
    logger.info(f"Check interval: {CHECK_INTERVAL_SECONDS}s | Comments: {ADD_COMMENTS}")

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
            logger.error(f"Error during feed processing: {e}")
            try:
                logger.info("Attempting token refresh / re-login...")
                ensure_authenticated(email, password)
            except Exception as login_err:
                logger.error(f"Re-login failed: {login_err}")

        logger.info(f"Sleeping for {CHECK_INTERVAL_SECONDS} seconds until next check...")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
