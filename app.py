import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import garth
import requests
from garth.exc import GarthHTTPError

CONNECT_API_FEEDS = (
    "/activitylist-service/activities/subscribed",
    "/activitylist-service/activities/search/activities",
)
CONNECT_API_KUDOS = "/activity-service/activity/{activity_id}/kudos"
LOGIN_BACKOFF_SECONDS = (15, 30, 60, 120, 240)
STARTUP_AUTH_RETRY_SECONDS = 30


@dataclass(frozen=True)
class Config:
    email: str
    password: str
    token_store: Path
    data_dir: Path
    check_interval_seconds: int
    feed_limit: int
    feed_endpoints: tuple[str, ...]
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

    raw_feed_endpoints = os.getenv("FEED_ENDPOINTS", "").strip()
    if raw_feed_endpoints:
        feed_endpoints = tuple(ep.strip() for ep in raw_feed_endpoints.split(",") if ep.strip())
    else:
        feed_endpoints = CONNECT_API_FEEDS

    return Config(
        email=email,
        password=password,
        token_store=Path(os.getenv("TOKEN_STORE", "/data/tokens")),
        data_dir=Path(os.getenv("DATA_DIR", "/data")),
        check_interval_seconds=max(30, int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))),
        feed_limit=feed_limit,
        feed_endpoints=feed_endpoints,
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


def _extract_status_code(exc: Exception) -> int | None:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code

    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return status_code

    return None


def _is_retriable_login_error(exc: Exception) -> bool:
    if _extract_status_code(exc) == 429:
        return True
    return isinstance(exc, (GarthHTTPError, requests.RequestException))


def login_with_retry(config: Config) -> None:
    for attempt, delay in enumerate(LOGIN_BACKOFF_SECONDS, start=1):
        try:
            # garth.login performs cookie exchange, sign-in page retrieval, and form post.
            # Keep all of that behind controlled retries so temporary SSO failures do not
            # crash-loop the process.
            garth.login(config.email, config.password)
            garth.save(str(config.token_store))
            logging.info("Login successful and tokens saved")
            return
        except Exception as exc:
            if not _is_retriable_login_error(exc):
                raise

            status_code = _extract_status_code(exc)
            if status_code == 429:
                logging.warning(
                    "Garmin login rate-limited (429) on attempt %s/%s; retrying in %ss",
                    attempt,
                    len(LOGIN_BACKOFF_SECONDS),
                    delay,
                )
            else:
                logging.warning(
                    "Garmin login/SSO transient error on attempt %s/%s; retrying in %ss: %s",
                    attempt,
                    len(LOGIN_BACKOFF_SECONDS),
                    delay,
                    exc,
                )
            time.sleep(delay)

    # Final attempt after backoff schedule is exhausted.
    garth.login(config.email, config.password)
    garth.save(str(config.token_store))
    logging.info("Login successful and tokens saved")


def ensure_login(config: Config) -> None:
    config.token_store.mkdir(parents=True, exist_ok=True)
    try:
        garth.resume(str(config.token_store))
        garth.client.connectapi("/userprofile-service/userprofile/personal-information")
        logging.info("Reused saved Garmin tokens")
    except Exception:
        logging.info("Token resume failed; logging in with credentials")
        login_with_retry(config)


def _looks_like_activity(item: object) -> bool:
    if not isinstance(item, dict):
        return False

    if _extract_activity_id_from_dict(item) is not None:
        return True

    activity_markers = {
        "activityName",
        "activityType",
        "eventType",
        "ownerDisplayName",
        "startTimeGMT",
        "startTimeLocal",
        "distance",
        "duration",
    }
    marker_hits = sum(1 for key in activity_markers if key in item)
    return marker_hits >= 3



def _extract_activity_id_from_dict(item: dict) -> int | None:
    for key in ("activityId", "id", "itemId"):
        value = item.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)

    entity_id = item.get("entityId")
    if isinstance(entity_id, str) and entity_id.startswith("activity:"):
        possible = entity_id.split(":", 1)[1]
        if possible.isdigit():
            return int(possible)

    return None



def _normalize_activity_shape(item: dict) -> dict:
    activity_id = _extract_activity_id_from_dict(item)
    if activity_id is None:
        return item

    normalized = dict(item)
    normalized["activityId"] = activity_id
    return normalized



def _extract_activities(payload: object) -> list[dict]:
    found: list[dict] = []
    seen_ids: set[int] = set()

    def visit(node: object) -> None:
        if isinstance(node, dict):
            if _looks_like_activity(node):
                normalized = _normalize_activity_shape(node)
                activity_id = _extract_activity_id_from_dict(normalized)
                if activity_id is not None and activity_id not in seen_ids:
                    seen_ids.add(activity_id)
                    found.append(normalized)

            for value in node.values():
                visit(value)
            return

        if isinstance(node, list):
            for value in node:
                visit(value)

    visit(payload)
    return found



def _describe_payload(payload: object) -> str:
    if isinstance(payload, list):
        if not payload:
            return "list(len=0)"
        first = payload[0]
        if isinstance(first, dict):
            first_keys = sorted(first.keys())[:15]
            nested_keys = sorted(
                {
                    nested_key
                    for value in first.values()
                    if isinstance(value, dict)
                    for nested_key in value.keys()
                }
            )[:15]
            return f"list(len={len(payload)} first_keys={first_keys} first_nested_keys={nested_keys})"
        return f"list(len={len(payload)} first_type={type(first).__name__})"

    if isinstance(payload, dict):
        return f"dict(keys={sorted(payload.keys())[:20]})"

    return f"{type(payload).__name__}"


def fetch_feed(endpoints: tuple[str, ...], limit: int) -> list[dict]:
    for endpoint in endpoints:
        payload = garth.client.connectapi(endpoint, params={"start": 0, "limit": limit})
        activities = _extract_activities(payload)
        if activities:
            logging.info("Using feed endpoint '%s' (activities=%s)", endpoint, len(activities))
            return activities

        logging.warning(
            "Feed endpoint '%s' returned 0 activities; payload=%s",
            endpoint,
            _describe_payload(payload),
        )

    return []




def _activity_id(activity: dict) -> int | None:
    return _extract_activity_id_from_dict(activity)

def iter_targets(activities: Iterable[dict], mode: str, already_liked: set[int]) -> list[dict]:
    if mode == "last10":
        return list(activities)

    targets: list[dict] = []
    for activity in activities:
        activity_id = _activity_id(activity)
        if activity_id is None:
            continue
        activity["activityId"] = activity_id
        if activity_id in already_liked:
            continue
        if activity.get("userKudoed", False):
            continue
        targets.append(activity)
    return targets


def like_activity(activity: dict) -> bool:
    activity_id = _activity_id(activity)
    if activity_id is None:
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
    feed = fetch_feed(config.feed_endpoints, config.feed_limit)
    logging.info("Fetched %s activities from feed", len(feed))

    liked_ids = load_json_set(liked_path)
    seen_ids = load_json_set(seen_path)

    candidates = [a for a in iter_targets(feed, config.mode, liked_ids) if _activity_id(a) is not None]

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

    current_feed_ids = {activity_id for a in feed if (activity_id := _activity_id(a)) is not None}
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

    logging.info("Starting Garmin auto-like bot | mode=%s | feed_limit=%s | feed_endpoints=%s", config.mode, config.feed_limit, config.feed_endpoints)

    while True:
        try:
            ensure_login(config)
            break
        except Exception as exc:
            logging.exception(
                "Initial authentication failed; retrying in %ss: %s",
                STARTUP_AUTH_RETRY_SECONDS,
                exc,
            )
            time.sleep(STARTUP_AUTH_RETRY_SECONDS)

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
