import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import garth
from garth.exc import GarthHTTPError

CONNECT_API_FEED = "/activitylist-service/activities/search/activities"
CONNECT_API_KUDOS = "/activity-service/activity/{activity_id}/kudos"


@dataclass(frozen=True)
class Config:
    email: str
    password: str
    token_store: Path
    data_dir: Path
    check_interval_seconds: int
    feed_limit: int
    mode: str  # "new" or "last10"
    run_once: bool
    log_level: str


def build_config() -> Config:
    email = os.getenv("GARMIN_EMAIL", "").strip()
    password = os.getenv("GARMIN_PASSWORD", "").strip()
    if not email or not password:
        raise ValueError("GARMIN_EMAIL and GARMIN_PASSWORD must be provided")

    mode = os.getenv("LIKE_MODE", "new").strip().lower()
    if mode not in {"new", "last10"}:
        raise ValueError("LIKE_MODE must be 'new' or 'last10'")

    feed_limit = int(os.getenv("FEED_LIMIT", "10"))
    if feed_limit < 1:
        raise ValueError("FEED_LIMIT must be >= 1")

    return Config(
        email=email,
        password=password,
        token_store=Path(os.getenv("TOKEN_STORE", "/data/tokens")),
        data_dir=Path(os.getenv("DATA_DIR", "/data")),
        check_interval_seconds=max(30, int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))),
        feed_limit=feed_limit,
        mode=mode,
        run_once=os.getenv("RUN_ONCE", "false").lower() == "true",
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_json_set(path: Path) -> set[int]:
    try:
        return {int(item) for item in json.loads(path.read_text())}
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
        return set()


def save_json_set(path: Path, values: set[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(values)))


def append_history(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        history = json.loads(path.read_text())
        if not isinstance(history, list):
            history = []
    except (FileNotFoundError, json.JSONDecodeError):
        history = []
    history.append(entry)
    path.write_text(json.dumps(history, indent=2))


def ensure_login(config: Config) -> None:
    config.token_store.mkdir(parents=True, exist_ok=True)
    try:
        garth.load(str(config.token_store))
        garth.client.connectapi("/userprofile-service/userprofile/personal-information")
        logging.info("Reused saved Garmin tokens")
    except Exception:
        logging.info("Token load failed; logging in with credentials")
        garth.login(config.email, config.password)
        garth.save(str(config.token_store))
        logging.info("Login successful and tokens saved")


def fetch_feed(limit: int) -> list[dict]:
    payload = garth.client.connectapi(CONNECT_API_FEED, params={"start": 0, "limit": limit})

    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("activityList", "activities", "results", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def iter_targets(activities: Iterable[dict], mode: str, already_liked: set[int]) -> list[dict]:
    if mode == "last10":
        return list(activities)

    targets: list[dict] = []
    for activity in activities:
        activity_id = activity.get("activityId")
        if not isinstance(activity_id, int):
            continue
        if activity_id in already_liked:
            continue
        if activity.get("userKudoed", False):
            continue
        targets.append(activity)
    return targets


def like_activity(activity: dict) -> bool:
    activity_id = activity.get("activityId")
    if not isinstance(activity_id, int):
        return False

    try:
        garth.client.connectapi(
            CONNECT_API_KUDOS.format(activity_id=activity_id),
            method="PUT",
        )
        return True
    except GarthHTTPError as exc:
        logging.warning("Failed to like activity %s: %s", activity_id, exc)
        return False


def run_cycle(config: Config, liked_path: Path, history_path: Path, seen_path: Path) -> tuple[int, int]:
    feed = fetch_feed(config.feed_limit)
    logging.info("Fetched %s activities from feed", len(feed))

    liked_ids = load_json_set(liked_path)
    seen_ids = load_json_set(seen_path)

    candidates = [a for a in iter_targets(feed, config.mode, liked_ids) if isinstance(a.get("activityId"), int)]

    # In "new" mode, only process activities not seen in previous cycle.
    if config.mode == "new":
        candidates = [a for a in candidates if a["activityId"] not in seen_ids]

    successes = 0
    for activity in candidates:
        activity_id = activity["activityId"]
        owner = activity.get("ownerDisplayName", "unknown")
        name = activity.get("activityName", "unnamed")

        if like_activity(activity):
            liked_ids.add(activity_id)
            successes += 1
            append_history(
                history_path,
                {
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "activity_id": activity_id,
                    "owner": owner,
                    "activity_name": name,
                },
            )
            logging.info("Liked activity %s | %s | %s", activity_id, owner, name)

    current_feed_ids = {a.get("activityId") for a in feed if isinstance(a.get("activityId"), int)}
    seen_ids.update(current_feed_ids)

    save_json_set(liked_path, liked_ids)
    save_json_set(seen_path, seen_ids)

    return len(candidates), successes


def main() -> None:
    config = build_config()
    setup_logging(config.log_level)

    liked_path = config.data_dir / "liked_activities.json"
    history_path = config.data_dir / "history.json"
    seen_path = config.data_dir / "seen_activities.json"

    logging.info("Starting Garmin auto-like bot | mode=%s | feed_limit=%s", config.mode, config.feed_limit)
    ensure_login(config)

    while True:
        try:
            attempted, liked = run_cycle(config, liked_path, history_path, seen_path)
            logging.info("Cycle complete: attempted=%s liked=%s", attempted, liked)
        except Exception as exc:
            logging.exception("Cycle failed: %s", exc)
            ensure_login(config)

        if config.run_once:
            break
        time.sleep(config.check_interval_seconds)


if __name__ == "__main__":
    main()
