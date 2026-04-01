import os
import time
import random
import logging
import json

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
        garth.connectapi("/userprofile-service/userprofile/personal-information")
        logger.info("Reused saved session tokens — no login needed")
        return
    except Exception:
        logger.info("No valid saved session, logging in...")

    login_with_retry(email, password)
    garth.save(TOKEN_STORE)
    logger.info("Session tokens saved to disk")


def kudo_activity(activity_id: int) -> bool:
    try:
        garth.connectapi(
            f"/activity-service/activity/{activity_id}/kudos",
            method="PUT",
        )
        logger.info(f"Liked activity {activity_id}")
        return True
    except Exception as e:
        logger.warning(f"Failed to like activity {activity_id}: {e}")
        return False


def comment_activity(activity_id: int) -> bool:
    comment = random.choice(POLISH_COMMENTS)
    try:
        garth.connectapi(
            f"/comment-service/comment/activity/{activity_id}",
            method="POST",
            json={"comment": comment},
        )
        logger.info(f"Commented on activity {activity_id}: {comment}")
        return True
    except Exception as e:
        logger.warning(f"Failed to comment on activity {activity_id}: {e}")
        return False


def get_social_feed(max_activities: int = 100) -> list:
    endpoints = [
        "/activitylist-service/activities/subscriptions",
        "/activitylist-service/activities/following",
    ]
    page_size = 20
    for endpoint in endpoints:
        collected = []
        try:
            for start in range(0, max_activities, page_size):
                raw = garth.connectapi(endpoint, params={"start": start, "limit": page_size})
                logger.debug(f"Raw response from {endpoint} start={start}: {str(raw)[:500]}")

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
                    break  # no more pages

            if collected:
                logger.info(f"Feed endpoint: {endpoint}, total activities: {len(collected)}")
                return collected
        except Exception as e:
            logger.warning(f"Endpoint {endpoint} failed: {e}")

    logger.warning("All feed endpoints returned 0 activities. Check that you follow people on Garmin Connect.")
    return []


def process_feed(liked: set) -> set:
    logger.info("Fetching social feed...")
    activities = get_social_feed()
    logger.info(f"Found {len(activities)} activities in feed")

    for activity in activities:
        activity_id = activity.get("activityId")
        owner = activity.get("ownerDisplayName", "unknown")

        if not activity_id:
            continue

        if activity_id in liked:
            logger.debug(f"Activity {activity_id} by {owner} already liked, skipping")
            continue

        if activity.get("userKudoed", False):
            logger.info(f"Activity {activity_id} by {owner} already kudoed, skipping")
            liked.add(activity_id)
            continue

        logger.info(f"Processing activity {activity_id} by {owner}...")
        time.sleep(random.uniform(3, 10))

        if kudo_activity(activity_id):
            liked.add(activity_id)
            save_liked_activities(liked)

            if ADD_COMMENTS:
                time.sleep(random.uniform(2, 5))
                comment_activity(activity_id)

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
