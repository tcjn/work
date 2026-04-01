import os
import time
import random
import logging
import json
from datetime import datetime, timezone

import garth
from garminconnect import Garmin

logging.basicConfig(
    level=logging.INFO,
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


def kudo_activity(client: Garmin, activity_id: int) -> bool:
    try:
        client.garth.connectapi(
            f"/activity-service/activity/{activity_id}/kudos",
            method="PUT",
        )
        logger.info(f"Liked activity {activity_id}")
        return True
    except Exception as e:
        logger.warning(f"Failed to like activity {activity_id}: {e}")
        return False


def comment_activity(client: Garmin, activity_id: int) -> bool:
    comment = random.choice(POLISH_COMMENTS)
    try:
        client.garth.connectapi(
            f"/comment-service/comment/activity/{activity_id}",
            method="POST",
            json={"comment": comment},
        )
        logger.info(f"Commented on activity {activity_id}: {comment}")
        return True
    except Exception as e:
        logger.warning(f"Failed to comment on activity {activity_id}: {e}")
        return False


def get_social_feed(client: Garmin, limit: int = 20) -> list:
    try:
        activities = client.garth.connectapi(
            "/activitylist-service/activities/subscriptions",
            params={"start": 0, "limit": limit},
        )
        if isinstance(activities, list):
            return activities
        return activities.get("activityList", []) if isinstance(activities, dict) else []
    except Exception as e:
        logger.error(f"Failed to fetch social feed: {e}")
        return []


def process_feed(client: Garmin, liked: set) -> set:
    logger.info("Fetching social feed...")
    activities = get_social_feed(client)
    logger.info(f"Found {len(activities)} activities in feed")

    for activity in activities:
        activity_id = activity.get("activityId")
        owner = activity.get("ownerDisplayName", "unknown")

        if not activity_id:
            continue

        if activity_id in liked:
            logger.debug(f"Activity {activity_id} by {owner} already liked, skipping")
            continue

        already_kudoed = activity.get("userKudoed", False)
        if already_kudoed:
            logger.info(f"Activity {activity_id} by {owner} already kudoed on Garmin, skipping")
            liked.add(activity_id)
            continue

        logger.info(f"Processing activity {activity_id} by {owner}...")

        # Small random delay to appear more human-like (3-10 seconds)
        time.sleep(random.uniform(3, 10))

        success = kudo_activity(client, activity_id)
        if success:
            liked.add(activity_id)
            save_liked_activities(liked)

            if ADD_COMMENTS:
                time.sleep(random.uniform(2, 5))
                comment_activity(client, activity_id)

    return liked


def login_with_retry(client: Garmin, email: str) -> None:
    """Login with exponential backoff. Raises on final failure."""
    delays = [30, 60, 120, 300]
    for attempt, delay in enumerate(delays, start=1):
        try:
            client.login()
            logger.info("Successfully logged in to Garmin Connect")
            return
        except Exception as e:
            if "429" in str(e) or "Too Many Requests" in str(e):
                if attempt <= len(delays):
                    logger.warning(f"Rate limited (429). Waiting {delay}s before retry {attempt}/{len(delays)}...")
                    time.sleep(delay)
                else:
                    raise
            else:
                raise
    client.login()  # final attempt


def get_client(email: str, password: str) -> Garmin:
    """Return an authenticated Garmin client, reusing saved tokens when possible."""
    os.makedirs(TOKEN_STORE, exist_ok=True)
    client = Garmin(email, password)

    try:
        client.garth.load(TOKEN_STORE)
        # Quick test to verify the token is still valid
        client.get_full_name()
        logger.info("Reused saved session tokens — no login needed")
        return client
    except Exception:
        logger.info("No valid saved session, logging in...")

    login_with_retry(client, email)
    client.garth.dump(TOKEN_STORE)
    logger.info("Session tokens saved to disk")
    return client


def main():
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")

    if not email or not password:
        logger.error("GARMIN_EMAIL and GARMIN_PASSWORD environment variables are required")
        raise SystemExit(1)

    logger.info(f"Starting Garmin auto-like bot for {email}")
    logger.info(f"Check interval: {CHECK_INTERVAL_SECONDS}s | Comments: {ADD_COMMENTS}")

    try:
        client = get_client(email, password)
    except Exception as e:
        logger.error(f"Login failed: {e}")
        raise SystemExit(1)

    liked = load_liked_activities()
    logger.info(f"Loaded {len(liked)} previously liked activities")

    while True:
        try:
            liked = process_feed(client, liked)
        except Exception as e:
            logger.error(f"Error during feed processing: {e}")
            # Token may have expired — refresh
            try:
                logger.info("Attempting token refresh / re-login...")
                client = get_client(email, password)
            except Exception as login_err:
                logger.error(f"Re-login failed: {login_err}")

        logger.info(f"Sleeping for {CHECK_INTERVAL_SECONDS} seconds until next check...")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
