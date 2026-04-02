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

LIKED_ACTIVITIES_FILE  = "/data/liked_activities.json"
HISTORY_FILE           = "/data/history.json"
TOKEN_STORE            = "/data/tokens"
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "3600"))
ADD_COMMENTS           = os.getenv("ADD_COMMENTS", "true").lower() == "true"
FEED_LOOKBACK          = int(os.getenv("FEED_LOOKBACK", "200"))   # how many feed items to check


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
    try:
        garth.load(TOKEN_STORE)
        garth.client.connectapi("/userprofile-service/userprofile/personal-information")
        logger.info("Reused saved session tokens")
        return
    except Exception:
        logger.info("No valid saved session, logging in...")
    login_with_retry(email, password)
    garth.save(TOKEN_STORE)
    logger.info("Tokens saved to disk")


def api_get(path: str, **kwargs):
    return garth.client.connectapi(path, **kwargs)


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


def get_feed(max_activities: int = FEED_LOOKBACK) -> list:
    """Fetch social feed activities via OAuth (connectapi.garmin.com)."""
    endpoints = [
        "/activitylist-service/activities/subscriptionFeed",
        "/activitylist-service/activities/subscriptions",
    ]
    page_size = 20
    for ep in endpoints:
        collected = []
        try:
            for start in range(0, max_activities, page_size):
                raw = api_get(ep, params={"start": start, "limit": page_size})
                logger.debug(f"{ep} start={start}: type={type(raw).__name__} preview={str(raw)[:150]}")

                if isinstance(raw, list):
                    page = raw
                elif isinstance(raw, dict):
                    page = next(
                        (raw[k] for k in ("activityList", "activities", "items") if raw.get(k)),
                        [],
                    )
                    if not page:
                        logger.info(f"Feed {ep} returned dict with keys: {list(raw.keys())}")
                else:
                    page = []

                collected.extend(page)
                if len(page) < page_size:
                    break  # no more pages

            if collected:
                logger.info(f"Feed via {ep}: {len(collected)} activities found")
                return collected

        except Exception as e:
            logger.warning(f"Feed endpoint {ep} failed: {e}")

    logger.warning("Feed returned 0 activities — no colleagues found or no one has posted recently")
    return []


def like_activity(activity: dict, liked: set) -> set:
    activity_id = activity.get("activityId")
    if not activity_id or activity_id in liked:
        return liked
    if activity.get("userKudoed", False):
        liked.add(activity_id)
        return liked

    owner        = activity.get("ownerDisplayName", "unknown")
    name         = activity.get("activityName", "—")
    atype        = (activity.get("activityType", {}) or {}).get("typeKey", "unknown")
    date         = activity.get("startTimeLocal") or activity.get("beginTimestamp", "—")

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
        logger.info(f'✓ Liked [{atype}] "{name}" by {owner}' + (f" | comment: {comment}" if comment else ""))

    return liked


def get_last_activities(limit: int = 10) -> list:
    """Fallback: fetch the last `limit` activities from the subscription feed."""
    endpoints = [
        "/activitylist-service/activities/subscriptionFeed",
        "/activitylist-service/activities/subscriptions",
    ]
    for ep in endpoints:
        try:
            raw = api_get(ep, params={"start": 0, "limit": limit})
            if isinstance(raw, list):
                activities = raw[:limit]
            elif isinstance(raw, dict):
                activities = next(
                    (raw[k] for k in ("activityList", "activities", "items") if raw.get(k)),
                    [],
                )[:limit]
            else:
                activities = []
            if activities:
                logger.info(f"Fallback via {ep}: fetched last {len(activities)} activities")
                return activities
        except Exception as e:
            logger.warning(f"Fallback endpoint {ep} failed: {e}")
    return []


def process_feed(liked: set) -> set:
    logger.info("Fetching social feed...")
    activities = get_feed()
    logger.info(f"Feed contains {len(activities)} activities total")

    new_likes = 0
    for activity in activities:
        before = len(liked)
        liked = like_activity(activity, liked)
        if len(liked) > before:
            new_likes += 1

    if new_likes == 0:
        logger.info("No new activities in feed — falling back to last 10 activities")
        fallback = get_last_activities(10)
        for activity in fallback:
            before = len(liked)
            liked = like_activity(activity, liked)
            if len(liked) > before:
                new_likes += 1

    if new_likes > 0:
        logger.info(f"Liked {new_likes} new activities this run")
    else:
        logger.info("No new activities to like this run")

    return liked


def main():
    email    = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")

    if not email or not password:
        logger.error("GARMIN_EMAIL and GARMIN_PASSWORD are required")
        raise SystemExit(1)

    logger.info(f"Starting Garmin auto-like bot for {email}")
    logger.info(f"Interval: {CHECK_INTERVAL_SECONDS}s | Comments: {ADD_COMMENTS} | Lookback: {FEED_LOOKBACK} activities")

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
