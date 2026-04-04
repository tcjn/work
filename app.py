import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from json import JSONDecodeError
from pathlib import Path
from typing import Callable, Iterable

import requests
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

CONNECT_API_FEEDS = (
    "/activitylist-service/activities/search/activities",
    "/activitylist-service/activities/subscribed",
)
CONNECT_API_KUDOS_ENDPOINTS = (
    "/activity-service/activity/{activity_id}/kudos",
    "/kudos-service/kudos/activity/{activity_id}",
)
LOGIN_BACKOFF_SECONDS = (15, 30, 60, 120, 240)
STARTUP_AUTH_RETRY_SECONDS = 30
api: Garmin | None = None


class AuthExpiredError(RuntimeError):
    """Raised when Garmin auth/session is no longer valid for feed requests."""


@dataclass(frozen=True)
class Config:
    email: str
    password: str
    token_store: Path
    data_dir: Path
    check_interval_seconds: int
    feed_limit: int
    feed_endpoints: tuple[str, ...]
    run_once: bool
    log_level: str
    like_on_startup: bool
    liked_cache_size: int


@dataclass
class State:
    last_feed_head_id: int | None
    liked_ids: list[int]


def build_config() -> Config:
    email = os.getenv("GARMIN_EMAIL", "").strip()
    password = os.getenv("GARMIN_PASSWORD", "").strip()
    if not email or not password:
        raise ValueError("GARMIN_EMAIL and GARMIN_PASSWORD must be provided")

    feed_limit = int(os.getenv("FEED_LIMIT", "25"))
    if feed_limit < 1:
        raise ValueError("FEED_LIMIT must be >= 1")

    raw_feed_endpoints = os.getenv("FEED_ENDPOINTS", "").strip()
    if raw_feed_endpoints:
        feed_endpoints = tuple(ep.strip() for ep in raw_feed_endpoints.split(",") if ep.strip())
    else:
        feed_endpoints = CONNECT_API_FEEDS

    liked_cache_size = int(os.getenv("LIKED_CACHE_SIZE", "5000"))
    if liked_cache_size < 100:
        raise ValueError("LIKED_CACHE_SIZE must be >= 100")

    return Config(
        email=email,
        password=password,
        token_store=Path(os.getenv("TOKEN_STORE", "/data/tokens")),
        data_dir=Path(os.getenv("DATA_DIR", "/data")),
        check_interval_seconds=max(20, int(os.getenv("CHECK_INTERVAL_SECONDS", "120"))),
        feed_limit=feed_limit,
        feed_endpoints=feed_endpoints,
        run_once=os.getenv("RUN_ONCE", "false").lower() == "true",
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        like_on_startup=os.getenv("LIKE_ON_STARTUP", "false").lower() == "true",
        liked_cache_size=liked_cache_size,
    )


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _state_path(data_dir: Path) -> Path:
    return data_dir / "state.json"


def load_state(path: Path) -> State:
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return State(last_feed_head_id=None, liked_ids=[])

    liked = raw.get("liked_ids", []) if isinstance(raw, dict) else []
    head = raw.get("last_feed_head_id") if isinstance(raw, dict) else None

    liked_ids: list[int] = []
    for item in liked:
        if isinstance(item, int):
            liked_ids.append(item)
        elif isinstance(item, str) and item.isdigit():
            liked_ids.append(int(item))

    if isinstance(head, str) and head.isdigit():
        head = int(head)
    if not isinstance(head, int):
        head = None

    return State(last_feed_head_id=head, liked_ids=liked_ids)


def save_state(path: Path, state: State, liked_cache_size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    # Keep only the latest N liked IDs to bound file growth.
    deduped: list[int] = []
    seen: set[int] = set()
    for activity_id in reversed(state.liked_ids):
        if activity_id in seen:
            continue
        seen.add(activity_id)
        deduped.append(activity_id)
        if len(deduped) >= liked_cache_size:
            break
    deduped.reverse()
    state.liked_ids = deduped

    path.write_text(json.dumps(asdict(state), indent=2))


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

    match = re.search(r"\b([45]\d{2})\b", str(exc))
    if match:
        return int(match.group(1))

    return None


def _is_retriable_login_error(exc: Exception) -> bool:
    if _extract_status_code(exc) == 429:
        return True
    return isinstance(
        exc,
        (
            GarminConnectAuthenticationError,
            GarminConnectConnectionError,
            GarminConnectTooManyRequestsError,
            requests.RequestException,
        ),
    )


def login_with_retry(config: Config) -> None:
    global api
    for attempt, delay in enumerate(LOGIN_BACKOFF_SECONDS, start=1):
        try:
            api = Garmin(email=config.email, password=config.password)
            api.login(str(config.token_store))
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

    api = Garmin(email=config.email, password=config.password)
    api.login(str(config.token_store))
    logging.info("Login successful and tokens saved")


def ensure_login(config: Config) -> None:
    global api
    config.token_store.mkdir(parents=True, exist_ok=True)
    try:
        api = Garmin()
        api.login(str(config.token_store))
        api.connectapi("/userprofile-service/userprofile/personal-information")
        logging.info("Reused saved Garmin tokens")
    except Exception:
        logging.info("Token resume failed; logging in with credentials")
        login_with_retry(config)


def _looks_like_activity(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    if _activity_id(item) is not None:
        return True
    nested = item.get("activity")
    return isinstance(nested, dict) and _activity_id(nested) is not None


def _activity_id(activity: dict) -> int | None:
    for key in ("activityId", "id", "activity_id"):
        value = activity.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return False


def _already_liked_by_me(activity: dict) -> bool:
    for key in ("userKudoed", "viewerHasLiked", "hasLiked", "likedByUser"):
        if key in activity and _as_bool(activity.get(key)):
            return True
    return False


def _extract_activities(payload: object) -> list[dict]:
    def _extract_from_feed_entry(entry: dict) -> list[dict]:
        nested: list[dict] = []
        for nested_key in ("activity", "activityDTO", "activitySummary", "entity", "latestActivity"):
            nested_item = entry.get(nested_key)
            if isinstance(nested_item, dict) and _looks_like_activity(nested_item):
                nested.append(nested_item)

        for nested_list_key in ("activities", "activityList", "items", "results"):
            nested_list = entry.get(nested_list_key)
            if isinstance(nested_list, list):
                nested.extend(_extract_activities(nested_list))
        return nested

    if isinstance(payload, list):
        direct = [item for item in payload if isinstance(item, dict) and _looks_like_activity(item)]
        if direct:
            return direct

        nested: list[dict] = []
        for item in payload:
            if isinstance(item, dict):
                nested.extend(_extract_from_feed_entry(item))
        if nested:
            return nested

    if isinstance(payload, dict):
        for key in ("activityList", "activities", "items", "results", "feedItems"):
            value = payload.get(key)
            if isinstance(value, list):
                direct = [item for item in value if isinstance(item, dict) and _looks_like_activity(item)]
                if direct:
                    return direct
                nested: list[dict] = []
                for entry in value:
                    if isinstance(entry, dict):
                        nested.extend(_extract_from_feed_entry(entry))
                if nested:
                    return nested

        recursive: list[dict] = []
        for value in payload.values():
            recursive.extend(_extract_activities(value))
            if recursive:
                return recursive

    return []


def fetch_feed(endpoints: tuple[str, ...], limit: int) -> list[dict]:
    if api is None:
        raise RuntimeError("Garmin API client is not initialized")

    auth_failures = 0
    attempts = 0

    def _is_auth_failure(exc: Exception) -> bool:
        text = str(exc).lower()
        if any(marker in text for marker in ("sign in", "sso/logout", "forbidden", "unauthorized")):
            return True
        status = _extract_status_code(exc)
        return status in (401, 403)

    def _get_connectapi(endpoint: str) -> object:
        return api.connectapi(endpoint, params={"start": 0, "limit": limit})

    def _get_modern_proxy(endpoint: str) -> object:
        try:
            return api.connectwebproxy(
                f"/modern/proxy{endpoint}",
                params={"start": 0, "limit": limit},
            )
        except JSONDecodeError as exc:
            raise AuthExpiredError("modern/proxy returned non-JSON") from exc

    strategies: list[tuple[str, Callable[[str], object]]] = [("connectapi", _get_connectapi)]
    if hasattr(api, "connectwebproxy"):
        strategies.append(("modern/proxy", _get_modern_proxy))

    for endpoint in endpoints:
        for strategy_name, strategy in strategies:
            attempts += 1
            try:
                payload = strategy(endpoint)
            except Exception as exc:
                if _is_auth_failure(exc):
                    auth_failures += 1
                logging.warning("Feed endpoint '%s' via %s failed: %s", endpoint, strategy_name, exc)
                continue

            activities = _extract_activities(payload)
            if activities:
                logging.info("Using feed endpoint '%s' via %s (activities=%s)", endpoint, strategy_name, len(activities))
                return activities

    if attempts > 0 and auth_failures == attempts:
        raise AuthExpiredError("All feed endpoints failed due to authentication/session errors")

    return []


def _filter_likeable(activities: Iterable[dict], liked_ids: set[int]) -> list[dict]:
    targets: list[dict] = []
    for activity in activities:
        activity_id = _activity_id(activity)
        if activity_id is None:
            continue
        activity["activityId"] = activity_id
        if activity_id in liked_ids:
            continue
        if _already_liked_by_me(activity):
            continue
        targets.append(activity)
    return targets


def _new_items_since_marker(activities: list[dict], marker_id: int | None) -> list[dict]:
    if not activities:
        return []
    if marker_id is None:
        return []

    for idx, activity in enumerate(activities):
        if _activity_id(activity) == marker_id:
            return activities[:idx]

    # Marker missing means the feed moved beyond current FEED_LIMIT.
    # Returning all keeps the bot from missing new activities.
    return activities


def like_activity(activity: dict) -> bool:
    if api is None:
        raise RuntimeError("Garmin API client is not initialized")

    activity_id = _activity_id(activity)
    if activity_id is None:
        return False

    for endpoint in CONNECT_API_KUDOS_ENDPOINTS:
        path = endpoint.format(activity_id=activity_id)
        try:
            api.connectapi(path, method="PUT")
            return True
        except Exception as exc:
            status = _extract_status_code(exc)
            logging.warning("Failed to like activity %s via %s: %s", activity_id, path, exc)
            if status in (401, 403):
                raise AuthExpiredError(f"Kudos request unauthorized for {activity_id}") from exc
            if status == 404:
                continue
            return False

    return False


def run_cycle(config: Config, state_path: Path, history_path: Path) -> tuple[int, int]:
    state = load_state(state_path)
    feed = fetch_feed(config.feed_endpoints, config.feed_limit)
    logging.info("Fetched %s activities from feed", len(feed))

    feed = [item for item in feed if _activity_id(item) is not None]
    if not feed:
        return 0, 0

    old_marker = state.last_feed_head_id
    new_marker = _activity_id(feed[0])

    if old_marker is None and not config.like_on_startup:
        state.last_feed_head_id = new_marker
        save_state(state_path, state, config.liked_cache_size)
        logging.info("Initialized feed marker to %s (LIKE_ON_STARTUP=false, no likes sent)", new_marker)
        return 0, 0

    if old_marker is None and config.like_on_startup:
        new_feed_items = feed
    elif config.like_on_startup and not state.liked_ids:
        # Recovery path: if marker exists but no likes were ever persisted, process visible feed once.
        new_feed_items = feed
    else:
        new_feed_items = _new_items_since_marker(feed, old_marker)

    liked_set = set(state.liked_ids)
    targets = _filter_likeable(new_feed_items, liked_set)
    if new_feed_items and not targets:
        sample_keys = sorted(str(k) for k in new_feed_items[0].keys())
        logging.info(
            "No likeable activities after filtering (feed_items=%s, cached_liked_ids=%s, sample_keys=%s)",
            len(new_feed_items),
            len(liked_set),
            sample_keys,
        )

    successes = 0
    for activity in reversed(targets):
        activity_id = activity["activityId"]
        owner = activity.get("ownerDisplayName", "unknown")
        name = activity.get("activityName", "unnamed")

        if like_activity(activity):
            state.liked_ids.append(activity_id)
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

    state.last_feed_head_id = new_marker
    save_state(state_path, state, config.liked_cache_size)
    return len(targets), successes


def main() -> None:
    config = build_config()
    setup_logging(config.log_level)

    state_path = _state_path(config.data_dir)
    history_path = config.data_dir / "history.json"

    logging.info(
        "Starting Garmin auto-like bot | feed_limit=%s | interval=%ss | like_on_startup=%s",
        config.feed_limit,
        config.check_interval_seconds,
        config.like_on_startup,
    )

    while True:
        try:
            ensure_login(config)
            break
        except Exception as exc:
            logging.exception("Initial authentication failed; retrying in %ss: %s", STARTUP_AUTH_RETRY_SECONDS, exc)
            time.sleep(STARTUP_AUTH_RETRY_SECONDS)

    while True:
        try:
            attempted, liked = run_cycle(config, state_path, history_path)
            logging.info("Cycle complete: attempted=%s liked=%s", attempted, liked)
        except Exception as exc:
            logging.exception("Cycle failed: %s", exc)
            ensure_login(config)

        if config.run_once:
            break
        time.sleep(config.check_interval_seconds)


if __name__ == "__main__":
    main()
