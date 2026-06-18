#!/usr/bin/env python3
"""Update the static SMM2 dashboard from public TGRCode data.

The TGRCode API does not expose a "levels uploaded yesterday" endpoint, so this
script scans Nintendo's internal data_id sequence around the expected day range,
filters courses by the America/Mexico_City upload day, and regenerates the
static dashboard files used by GitHub Pages.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DATA_JS = ROOT / "data.js"
INDEX_HTML = ROOT / "index.html"
THUMBS_DIR = ROOT / "thumbs"
STATE_PATH = ROOT / "automation-state.json"

API_BASE = "https://tgrcode.com/mm2"
CHARSET = "0123456789BCDFGHJKLMNPQRSTVWXY"
TZ_NAME = "America/Mexico_City"
TZ = ZoneInfo(TZ_NAME)
DIFFICULTY_ORDER = ["Easy", "Normal", "Expert", "Super expert"]
MIN_ATTEMPTS_FOR_LEAST_CLEARED = 20
MIN_CLEAR_CHECK_MS_FOR_TOP_LIKED = 180_000
TOP_LONGEST_LIMIT = 20
RECENTLY_PLAYED_LIMIT = 10
SLOW_THUMBNAIL_TIMEOUT = 25.0
SLOW_THUMBNAIL_RETRIES = 4
SLOW_THUMBNAIL_DELAY = 0.8
DEFAULT_STATE = {
    "anchorDate": "2026-06-06",
    "anchorStartId": 59389587,
    "idsPerDay": 12600,
    "lastUpdatedDate": "2026-06-05",
}


@dataclass(frozen=True)
class IdPair:
    data_id: int
    course_id: str


def data_id_to_course_id(value: int) -> str:
    data_id = int(value)
    field_b = (data_id - 31) % 64
    exed = data_id ^ 0b00010110100000001110000001111100
    field_c = exed & 0b00000000000011111111111111111111
    field_f = exed >> 20
    intermediate = (
        (8 << 40)
        + (field_b << 34)
        + (field_c << 14)
        + (0 << 13)
        + (1 << 12)
        + field_f
    )

    out = ""
    while intermediate > 0:
        out += CHARSET[intermediate % 30]
        intermediate //= 30
    return out


def chunked(items: list[IdPair], size: int) -> Iterable[list[IdPair]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def request_json(path: str, timeout: float, retries: int) -> tuple[int, dict | None]:
    url = f"{API_BASE}{path}"
    for attempt in range(retries + 1):
        curl_missing = False
        try:
            result = subprocess.run(
                [
                    "curl",
                    "-sS",
                    "--max-time",
                    str(timeout),
                    "-H",
                    "User-Agent: smm2-dashboard-updater/1.0",
                    "-w",
                    "\n%{http_code}",
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=timeout + 5,
                check=False,
            )
            if result.returncode != 0 or not result.stdout:
                if attempt < retries:
                    time.sleep(min(6.0, 0.6 * (attempt + 1) ** 2))
                    continue
                return 0, None
            body, status_text = result.stdout.rsplit("\n", 1)
            status = int(status_text)
            try:
                parsed = json.loads(body) if body else {}
            except json.JSONDecodeError:
                parsed = {"error": body[:500]}
            if (status == 429 or status >= 500) and attempt < retries:
                time.sleep(min(10.0, 0.9 * (attempt + 1) ** 2))
                continue
            return status, parsed
        except FileNotFoundError:
            curl_missing = True
        except (ValueError, subprocess.TimeoutExpired, json.JSONDecodeError):
            if attempt < retries:
                time.sleep(min(6.0, 0.6 * (attempt + 1) ** 2))
                continue
            return 0, None

        if not curl_missing:
            return 0, None

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "smm2-dashboard-updater/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                return response.status, json.loads(body)
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            parsed = None
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = {"error": body[:500]}
            if (error.code == 429 or error.code >= 500) and attempt < retries:
                time.sleep(min(10.0, 0.9 * (attempt + 1) ** 2))
                continue
            return error.code, parsed
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt < retries:
                time.sleep(min(6.0, 0.6 * (attempt + 1) ** 2))
                continue
            return 0, None
    return 0, None


def fetch_pairs(
    pairs: list[IdPair],
    timeout: float,
    retries: int,
    pause: float,
) -> tuple[list[dict], int, int, int]:
    """Fetch a batch of courses.

    Returns courses, HTTP request count, recursive split count, and missing ID
    count. The recursive split keeps one missing/deleted course from making the
    whole batch unusable.
    """
    if not pairs:
        return [], 0, 0, 0

    if len(pairs) == 1:
        status, payload = request_json(f"/level_info/{pairs[0].course_id}", timeout, retries)
        if pause:
            time.sleep(pause)
        if status == 200 and payload and not payload.get("error"):
            return [payload], 1, 0, 0
        return [], 1, 0, 1

    course_ids = ",".join(pair.course_id for pair in pairs)
    status, payload = request_json(f"/level_info_multiple/{course_ids}", timeout, retries)
    if pause:
        time.sleep(pause)

    if status == 200 and payload and isinstance(payload.get("courses"), list):
        found = {int(course["data_id"]): course for course in payload["courses"]}
        missing = len(pairs) - len(found)
        ordered = [found[pair.data_id] for pair in pairs if pair.data_id in found]
        return ordered, 1, 0, missing

    if payload and payload.get("course_id") and "No course with that ID" in str(payload.get("error", "")):
        bad_course_id = str(payload["course_id"]).upper()
        remaining = [pair for pair in pairs if pair.course_id != bad_course_id]
        courses, requests, splits, missing = fetch_pairs(remaining, timeout, retries, pause)
        return courses, 1 + requests, splits, missing + 1

    if status == 0 or status >= 500:
        raise RuntimeError(f"TGRCode API unavailable while fetching {len(pairs)} levels (HTTP {status})")

    mid = len(pairs) // 2
    left = fetch_pairs(pairs[:mid], timeout, retries, pause)
    right = fetch_pairs(pairs[mid:], timeout, retries, pause)
    return (
        left[0] + right[0],
        1 + left[1] + right[1],
        1 + left[2] + right[2],
        left[3] + right[3],
    )


def scan_range(
    start_id: int,
    end_id: int,
    *,
    batch_size: int,
    workers: int,
    timeout: float,
    retries: int,
    pause: float,
) -> list[dict]:
    if end_id < start_id:
        return []

    pairs = [IdPair(data_id, data_id_to_course_id(data_id)) for data_id in range(start_id, end_id + 1)]
    batches = list(chunked(pairs, batch_size))
    completed = 0
    requests = 0
    splits = 0
    missing = 0
    courses: list[dict] = []

    print(f"Scanning data_id {start_id}..{end_id} ({len(pairs):,} IDs)", flush=True)

    def run_batch(batch: list[IdPair]) -> tuple[list[dict], int, int, int]:
        return fetch_pairs(batch, timeout, retries, pause)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(run_batch, batch): batch for batch in batches}
        for future in concurrent.futures.as_completed(future_map):
            batch = future_map[future]
            batch_courses, batch_requests, batch_splits, batch_missing = future.result()
            courses.extend(batch_courses)
            completed += len(batch)
            requests += batch_requests
            splits += batch_splits
            missing += batch_missing
            if completed % max(batch_size * 20, 1) == 0 or completed == len(pairs):
                print(
                    f"  scanned {completed:,}/{len(pairs):,} IDs "
                    f"(courses {len(courses):,}, missing {missing:,}, requests {requests:,}, splits {splits:,})",
                    flush=True,
                )

    courses.sort(key=lambda course: int(course.get("data_id") or 0))
    return courses


def load_state() -> dict:
    if not STATE_PATH.exists():
        return dict(DEFAULT_STATE)
    try:
        loaded = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(DEFAULT_STATE)
    state = dict(DEFAULT_STATE)
    state.update({key: value for key, value in loaded.items() if value not in (None, "")})
    return state


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def target_date_from_args(date_text: str | None) -> dt.date:
    if date_text:
        return dt.date.fromisoformat(date_text)
    return dt.datetime.now(tz=TZ).date() - dt.timedelta(days=1)


def day_bounds(local_date: dt.date) -> tuple[int, int]:
    start = dt.datetime.combine(local_date, dt.time.min, tzinfo=TZ)
    end = start + dt.timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def estimate_range(local_date: dt.date, state: dict, margin: int) -> tuple[int, int]:
    anchor_date = dt.date.fromisoformat(str(state["anchorDate"]))
    anchor_start = int(state["anchorStartId"])
    ids_per_day = int(state.get("idsPerDay") or DEFAULT_STATE["idsPerDay"])
    days = (local_date - anchor_date).days
    expected_start = anchor_start + days * ids_per_day
    expected_end = anchor_start + (days + 1) * ids_per_day
    return max(1, expected_start - margin), expected_end + margin


def scan_target_day(args: argparse.Namespace, local_date: dt.date, state: dict) -> tuple[list[dict], list[dict], tuple[int, int]]:
    start_ts, end_ts = day_bounds(local_date)
    current_start, current_end = estimate_range(local_date, state, args.scan_margin)
    all_courses: dict[int, dict] = {}
    scanned_start: int | None = None
    scanned_end: int | None = None

    def add_scan(scan_start: int, scan_end: int) -> None:
        nonlocal scanned_start, scanned_end
        if scan_end < scan_start:
            return
        found = scan_range(
            scan_start,
            scan_end,
            batch_size=args.batch_size,
            workers=args.workers,
            timeout=args.timeout,
            retries=args.retries,
            pause=args.pause,
        )
        for course in found:
            data_id = course.get("data_id")
            if data_id is not None:
                all_courses[int(data_id)] = course
        scanned_start = scan_start if scanned_start is None else min(scanned_start, scan_start)
        scanned_end = scan_end if scanned_end is None else max(scanned_end, scan_end)

    def coverage() -> tuple[list[dict], list[dict], bool, bool]:
        courses = sorted(all_courses.values(), key=lambda course: int(course.get("data_id") or 0))
        day_courses = [course for course in courses if start_ts <= int(course.get("uploaded") or 0) < end_ts]
        has_before = any(int(course.get("uploaded") or 0) < start_ts for course in courses)
        has_after = any(int(course.get("uploaded") or 0) >= end_ts for course in courses)
        return day_courses, courses, has_before, has_after

    def checked_result(label: str) -> tuple[list[dict], list[dict], tuple[int, int]] | None:
        day_courses, courses, has_before, has_after = coverage()
        print(
            f"{label}: {len(day_courses):,} levels in {local_date.isoformat()}, "
            f"before={has_before}, after={has_after}",
            flush=True,
        )
        if day_courses and has_before and has_after:
            return day_courses, courses, (scanned_start or current_start, scanned_end or current_end)
        return None

    def scan_windows(scan_start: int, scan_end: int, label: str) -> tuple[list[dict], list[dict], tuple[int, int]] | None:
        if scan_end < scan_start:
            return checked_result(label)
        window = max(1, int(args.scan_window))
        for window_start in range(scan_start, scan_end + 1, window):
            window_end = min(scan_end, window_start + window - 1)
            add_scan(window_start, window_end)
            result = checked_result(label)
            if result:
                return result
        return None

    result = scan_windows(current_start, current_end, "Coverage check")
    if result:
        return result

    for extension in range(args.max_extensions + 1):
        day_courses, courses, has_before, has_after = coverage()

        if extension >= args.max_extensions:
            break

        if not day_courses or not has_before:
            next_start = max(1, current_start - args.scan_margin)
            result = scan_windows(next_start, current_start - 1, f"Coverage extension {extension + 1}")
            if result:
                return result
            current_start = next_start

        if not day_courses or not has_after:
            next_end = current_end + args.scan_margin
            result = scan_windows(current_end + 1, next_end, f"Coverage extension {extension + 1}")
            if result:
                return result
            current_end = next_end

    raise RuntimeError(
        f"Could not confirm full coverage for {local_date.isoformat()} "
        f"after scanning {scanned_start or current_start}..{scanned_end or current_end}"
    )


def compact(course: dict) -> dict:
    uploader = course.get("uploader") or {}
    one = course.get("one_screen_thumbnail") or {}
    entire = course.get("entire_thumbnail") or {}
    return {
        "courseId": course.get("course_id"),
        "dataId": course.get("data_id"),
        "name": course.get("name") or "Untitled",
        "description": course.get("description") or "",
        "uploadedPretty": course.get("uploaded_pretty"),
        "uploaderName": uploader.get("name") or "",
        "uploaderCountry": uploader.get("country") or "",
        "difficulty": course.get("difficulty_name") or "",
        "style": course.get("game_style_name") or "",
        "theme": course.get("theme_name") or "",
        "likes": course.get("likes") or 0,
        "plays": course.get("plays") or 0,
        "clears": course.get("clears") or 0,
        "attempts": course.get("attempts") or 0,
        "clearRate": course.get("clear_rate") or 0,
        "clearRatePretty": course.get("clear_rate_pretty") or "0%",
        "uploadTime": course.get("upload_time") or 0,
        "uploadTimePretty": course.get("upload_time_pretty") or "00:00.000",
        "thumbnail": one.get("url") or entire.get("url") or "",
        "entireThumbnail": entire.get("url") or one.get("url") or "",
    }


def top_liked_courses(courses: list[dict]) -> list[dict]:
    return sorted(
        [c for c in courses if (c.get("upload_time") or 0) > MIN_CLEAR_CHECK_MS_FOR_TOP_LIKED],
        key=lambda c: (
            -(c.get("likes") or 0),
            -(c.get("plays") or 0),
            c.get("uploaded") or 0,
        ),
    )[:10]


def top_longest_courses(courses: list[dict]) -> list[dict]:
    return sorted(
        courses,
        key=lambda c: (
            -(c.get("upload_time") or 0),
            -(c.get("plays") or 0),
            -(c.get("likes") or 0),
        ),
    )[:TOP_LONGEST_LIMIT]


def least_cleared_courses(courses: list[dict]) -> list[dict]:
    return sorted(
        [c for c in courses if (c.get("attempts") or 0) >= MIN_ATTEMPTS_FOR_LEAST_CLEARED],
        key=lambda c: (
            c.get("clear_rate") if c.get("clear_rate") is not None else 999999,
            -(c.get("attempts") or 0),
            -(c.get("plays") or 0),
        ),
    )[:10]


def safe_asset_version(value: object) -> str:
    version = re.sub(r"[^0-9A-Za-z]+", "-", str(value or "")).strip("-")
    return version or "auto"


def payload_asset_version(payload: dict) -> str:
    return safe_asset_version(payload.get("assetVersion") or payload.get("generatedAt") or payload.get("date"))


def normalize_maker_id(value: object) -> str:
    raw = str(value or "").replace("-", "").strip().upper()
    return raw if len(raw) == 9 else ""


def build_payload(local_date: dt.date, courses: list[dict]) -> dict:
    start_ts, end_ts = day_bounds(local_date)
    generated_at = dt.datetime.now(tz=TZ).isoformat(timespec="seconds")
    top_liked = top_liked_courses(courses)
    top_longest = top_longest_courses(courses)
    least_cleared = least_cleared_courses(courses)

    return {
        "generatedAt": generated_at,
        "assetVersion": safe_asset_version(generated_at),
        "date": local_date.isoformat(),
        "timezone": TZ_NAME,
        "difficulties": DIFFICULTY_ORDER,
        "range": {"startTs": start_ts, "endTs": end_ts},
        "stats": {
            "totalLevels": len(courses),
            "topLikedCount": len(top_liked),
            "topLikedMinClearCheckMs": MIN_CLEAR_CHECK_MS_FOR_TOP_LIKED,
            "topLongestCount": len(top_longest),
            "leastClearedCount": len(least_cleared),
            "leastClearedMinAttempts": MIN_ATTEMPTS_FOR_LEAST_CLEARED,
            "totalLikes": sum(c.get("likes") or 0 for c in courses),
            "totalPlays": sum(c.get("plays") or 0 for c in courses),
        },
        "topLiked": [compact(c) for c in top_liked],
        "topLikedByDifficulty": {
            difficulty: [compact(c) for c in top_liked_courses([c for c in courses if (c.get("difficulty_name") or "") == difficulty])]
            for difficulty in DIFFICULTY_ORDER
        },
        "topLongest": [compact(c) for c in top_longest],
        "topLongestByDifficulty": {
            difficulty: [compact(c) for c in top_longest_courses([c for c in courses if (c.get("difficulty_name") or "") == difficulty])]
            for difficulty in DIFFICULTY_ORDER
        },
        "leastCleared": [compact(c) for c in least_cleared],
        "leastClearedByDifficulty": {
            difficulty: [compact(c) for c in least_cleared_courses([c for c in courses if (c.get("difficulty_name") or "") == difficulty])]
            for difficulty in DIFFICULTY_ORDER
        },
        "recentlyPlayed": [],
    }


def fetch_recently_played(args: argparse.Namespace) -> tuple[str, list[dict]]:
    maker_id = normalize_maker_id(args.played_maker_id)
    if not maker_id:
        return "", []

    status, payload = request_json(f"/get_played/{maker_id}", args.timeout, args.retries)
    if status != 200 or not isinstance(payload, dict):
        print(f"Could not load recently played levels for {maker_id}: HTTP {status}", flush=True)
        return maker_id, []

    courses = payload.get("courses")
    if not isinstance(courses, list):
        return maker_id, []

    limit = max(0, int(args.played_limit or RECENTLY_PLAYED_LIMIT))
    return maker_id, [compact(course) for course in courses[:limit]]


def save_payload(payload: dict) -> None:
    DATA_JS.write_text(
        "window.SMM2_DASHBOARD_DATA = "
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + ";\n",
        encoding="utf-8",
    )


def course_groups(payload: dict) -> Iterable[list[dict]]:
    yield payload.get("topLiked", [])
    yield payload.get("topLongest", [])
    yield payload.get("leastCleared", [])
    yield payload.get("recentlyPlayed", [])
    for group in (payload.get("topLikedByDifficulty") or {}).values():
        yield group
    for group in (payload.get("topLongestByDifficulty") or {}).values():
        yield group
    for group in (payload.get("leastClearedByDifficulty") or {}).values():
        yield group


def download_binary(url: str, output: Path, timeout: float, retries: int) -> None:
    temp = output.with_suffix(".tmp")
    last_error: Exception | str | None = None
    for attempt in range(retries + 1):
        curl_missing = False
        try:
            result = subprocess.run(
                [
                    "curl",
                    "-sS",
                    "-L",
                    "--fail",
                    "--max-time",
                    str(timeout),
                    "-H",
                    "User-Agent: smm2-dashboard-updater/1.0",
                    "-o",
                    str(temp),
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=timeout + 5,
                check=False,
            )
            if result.returncode == 0 and temp.exists() and temp.stat().st_size > 0:
                temp.replace(output)
                return
            temp.unlink(missing_ok=True)
            last_error = result.stderr.strip() or "empty thumbnail response"
        except FileNotFoundError:
            curl_missing = True
        except (subprocess.TimeoutExpired, OSError) as error:
            temp.unlink(missing_ok=True)
            last_error = error

        if curl_missing:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "smm2-dashboard-updater/1.0"})
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    data = response.read()
                if data:
                    temp.write_bytes(data)
                    temp.replace(output)
                    return
                last_error = "empty thumbnail response"
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                temp.unlink(missing_ok=True)
                last_error = error

        if attempt < retries:
            time.sleep(min(14.0, 1.5 * (attempt + 1) ** 2))
    raise RuntimeError(str(last_error or "thumbnail download failed"))


def localize_thumbnails(
    payload: dict,
    local_date: dt.date,
    *,
    timeout: float,
    retries: int,
    delay: float,
    workers: int,
) -> None:
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    version = payload_asset_version(payload)
    seen: set[str] = set()
    course_ids: list[str] = []
    local_thumbs: dict[str, str] = {}
    existing = 0
    downloaded = 0
    unavailable = 0

    for courses in course_groups(payload):
        for course in courses:
            course_id = str(course.get("courseId") or "")
            if course_id and course_id not in seen:
                seen.add(course_id)
                course_ids.append(course_id)

    missing: list[str] = []
    for course_id in course_ids:
        output = THUMBS_DIR / f"{course_id}.jpg"
        if output.exists() and output.stat().st_size > 0:
            local_thumbs[course_id] = f"./thumbs/{course_id}.jpg?v={version}"
            existing += 1
        else:
            missing.append(course_id)

    def fetch_thumbnail(course_id: str) -> tuple[str, bool, str]:
        if delay:
            time.sleep(delay)
        output = THUMBS_DIR / f"{course_id}.jpg"
        try:
            download_binary(f"{API_BASE}/level_thumbnail/{course_id}", output, timeout, retries)
        except RuntimeError as error:
            return course_id, False, str(error)
        return course_id, output.exists() and output.stat().st_size > 0, ""

    if missing:
        print(f"Downloading {len(missing):,} missing thumbnails", flush=True)
        errors: list[tuple[str, str]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {executor.submit(fetch_thumbnail, course_id): course_id for course_id in missing}
            for future in concurrent.futures.as_completed(futures):
                course_id, ok, error = future.result()
                if ok:
                    local_thumbs[course_id] = f"./thumbs/{course_id}.jpg?v={version}"
                    downloaded += 1
                else:
                    errors.append((course_id, error))

        if errors:
            print(f"Retrying {len(errors):,} thumbnails slowly", flush=True)
            retry_errors: list[tuple[str, str]] = []
            for course_id, first_error in errors:
                if SLOW_THUMBNAIL_DELAY:
                    time.sleep(SLOW_THUMBNAIL_DELAY)
                output = THUMBS_DIR / f"{course_id}.jpg"
                try:
                    download_binary(
                        f"{API_BASE}/level_thumbnail/{course_id}",
                        output,
                        max(timeout, SLOW_THUMBNAIL_TIMEOUT),
                        max(retries, SLOW_THUMBNAIL_RETRIES),
                    )
                except RuntimeError as error:
                    local_thumbs[course_id] = ""
                    unavailable += 1
                    retry_errors.append((course_id, f"{first_error}; slow retry: {error}"))
                    continue

                if output.exists() and output.stat().st_size > 0:
                    local_thumbs[course_id] = f"./thumbs/{course_id}.jpg?v={version}"
                    downloaded += 1
                else:
                    local_thumbs[course_id] = ""
                    unavailable += 1
                    retry_errors.append((course_id, f"{first_error}; slow retry: empty thumbnail response"))
            errors = retry_errors

        for course_id, error in errors[:8]:
            print(f"  thumbnail fallback {course_id}: {error}", flush=True)
        if len(errors) > 8:
            print(f"  ... {len(errors) - 8:,} more thumbnail fallbacks", flush=True)

    for courses in course_groups(payload):
        for course in courses:
            course_id = str(course.get("courseId") or "")
            if not course_id:
                continue

            local_thumb = local_thumbs.get(course_id, "")
            course["thumbnail"] = local_thumb
            course["entireThumbnail"] = local_thumb

    print(
        f"Prepared {len(seen):,} thumbnails "
        f"({existing:,} existing, {downloaded:,} downloaded, {unavailable:,} unavailable)",
        flush=True,
    )


def esc(value: object) -> str:
    if value is None or value == "":
        return "Sin dato"
    return html.escape(str(value), quote=True)


def metric(value: object) -> str:
    return f"{int(value or 0):,}"


def format_course_id(value: object) -> str:
    raw = str(value or "").replace("-", "").upper()
    if len(raw) != 9:
        return esc(value)
    return html.escape(f"{raw[:3]}-{raw[3:6]}-{raw[6:]}", quote=True)


def score(course: dict, mode: str) -> str:
    if mode == "likes":
        return f"""
          <div class="primary-score">
            <strong>{metric(course.get("likes"))}</strong>
            <span>likes</span>
          </div>
        """
    if mode == "time":
        return f"""
          <div class="primary-score">
            <strong>{esc(course.get("uploadTimePretty"))}</strong>
            <span>clear-check</span>
          </div>
        """
    if mode == "plays":
        return f"""
          <div class="primary-score">
            <strong>{metric(course.get("plays"))}</strong>
            <span>plays</span>
          </div>
        """
    return f"""
      <div class="primary-score">
        <strong>{esc(course.get("clearRatePretty"))}</strong>
        <span>clear rate</span>
      </div>
    """


def card(course: dict, index: int, mode: str) -> str:
    title = esc(course.get("name"))
    raw_thumb = course.get("thumbnail")
    thumb = html.escape(str(raw_thumb), quote=True) if raw_thumb else ""
    course_id = esc(course.get("courseId"))
    display_course_id = format_course_id(course.get("courseId"))
    image = (
        f'<img src="{thumb}" alt="{title}" loading="lazy" referrerpolicy="no-referrer" />'
        if raw_thumb
        else '<div class="thumb-fallback">sin imagen</div>'
    )
    return f"""
            <article class="level-card" data-course-id="{course_id}" tabindex="0" role="button" aria-label="Ver detalles de {title}, ID {display_course_id}">
              <div class="rank">{index + 1}</div>
              <div class="thumb">{image}</div>
              <div class="level-body">
                <div class="level-top">
                  <div class="level-title">
                    <h3>{title}</h3>
                    <span class="course-id">{display_course_id}</span>
                  </div>
                  {score(course, mode)}
                </div>
                <div class="level-meta">
                  <span class="pill difficulty">{esc(course.get("difficulty"))}</span>
                </div>
                <div class="metrics">
                  <span>{metric(course.get("plays"))} plays</span>
                  <span>{metric(course.get("clears"))} clears</span>
                  <span>{metric(course.get("attempts"))} intentos</span>
                  <span>{esc(course.get("uploadTimePretty"))} clear-check</span>
                </div>
              </div>
            </article>
    """.rstrip()


def replace_list(html_text: str, element_id: str, content: str, end_marker: str) -> str:
    start_marker = f'          <div id="{element_id}" class="level-list">'
    start = html_text.index(start_marker)
    end = html_text.index(end_marker, start)
    replacement = f"{start_marker}\n{content}\n          </div>"
    return html_text[:start] + replacement + html_text[end:]


def rendered_list(courses: list[dict], mode: str, empty_text: str) -> str:
    if courses:
        return "\n".join(card(course, i, mode) for i, course in enumerate(courses))
    return f'            <div class="empty-state">{esc(empty_text)}</div>'


def update_index(payload: dict) -> None:
    top = "\n".join(card(course, i, "likes") for i, course in enumerate(payload["topLiked"]))
    least = "\n".join(card(course, i, "clear") for i, course in enumerate(payload["leastCleared"]))
    longest = "\n".join(card(course, i, "time") for i, course in enumerate(payload["topLongest"]))
    played = rendered_list(
        payload.get("recentlyPlayed", []),
        "plays",
        "No hay niveles jugados registrados para esta fecha.",
    )
    text = INDEX_HTML.read_text(encoding="utf-8")
    text = replace_list(
        text,
        "topLiked",
        top,
        '\n        </section>\n\n        <section class="board" aria-labelledby="clearedTitle">',
    )
    text = replace_list(
        text,
        "leastCleared",
        least,
        '\n        </section>\n\n        <section class="board" aria-labelledby="longestTitle">',
    )
    text = replace_list(
        text,
        "topLongest",
        longest,
        "\n        </section>\n      </section>\n\n      <section class=\"played-section board\" aria-labelledby=\"playedTitle\">",
    )
    text = replace_list(
        text,
        "recentlyPlayed",
        played,
        "\n      </section>\n    </main>",
    )
    version = payload_asset_version(payload)
    text = re.sub(r"data\.js\?v=[^\"']+", f"data.js?v={version}", text)
    text = "\n".join(line.rstrip() for line in text.splitlines()) + "\n"
    INDEX_HTML.write_text(text, encoding="utf-8")


def refresh_state(state: dict, local_date: dt.date, day_courses: list[dict], scanned_courses: list[dict], scanned_range: tuple[int, int]) -> dict:
    start_ts, end_ts = day_bounds(local_date)
    next_date = local_date + dt.timedelta(days=1)
    in_day_ids = [int(c["data_id"]) for c in day_courses if c.get("data_id") is not None]
    next_day_ids = [
        int(c["data_id"])
        for c in scanned_courses
        if c.get("data_id") is not None and int(c.get("uploaded") or 0) >= end_ts
    ]

    new_state = dict(state)
    if next_day_ids:
        next_anchor = min(next_day_ids)
        new_state["anchorDate"] = next_date.isoformat()
        new_state["anchorStartId"] = next_anchor
        if in_day_ids:
            observed = next_anchor - min(in_day_ids)
            if 8000 <= observed <= 20000:
                old = int(new_state.get("idsPerDay") or DEFAULT_STATE["idsPerDay"])
                new_state["idsPerDay"] = round(old * 0.7 + observed * 0.3)
    elif in_day_ids:
        new_state["anchorDate"] = local_date.isoformat()
        new_state["anchorStartId"] = min(in_day_ids)

    new_state["lastUpdatedDate"] = local_date.isoformat()
    new_state["lastTotalLevels"] = len(day_courses)
    new_state["lastScanRange"] = {"startId": scanned_range[0], "endId": scanned_range[1]}
    new_state["updatedAt"] = dt.datetime.now(tz=TZ).isoformat(timespec="seconds")
    return new_state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Update the SMM2 static dashboard")
    parser.add_argument("--date", help="Local date to publish, YYYY-MM-DD. Defaults to yesterday in Mexico City.")
    parser.add_argument("--scan-margin", type=int, default=1000, help="Extra IDs to scan before and after the estimate.")
    parser.add_argument("--scan-window", type=int, default=400, help="IDs to scan before checking day coverage.")
    parser.add_argument("--max-extensions", type=int, default=10, help="How many times to extend the scan if coverage is incomplete.")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--pause", type=float, default=0.08, help="Pause after each HTTP request per worker.")
    parser.add_argument("--thumbnail-timeout", type=float, default=10.0)
    parser.add_argument("--thumbnail-retries", type=int, default=1)
    parser.add_argument("--thumbnail-delay", type=float, default=0.05)
    parser.add_argument("--thumbnail-workers", type=int, default=4)
    parser.add_argument(
        "--played-maker-id",
        default=os.environ.get("SMM2_PLAYER_ID", ""),
        help="Maker ID used to load recently played levels from TGRCode.",
    )
    parser.add_argument("--played-limit", type=int, default=RECENTLY_PLAYED_LIMIT)
    parser.add_argument("--skip-thumbnails", action="store_true", help="Only for local debugging.")
    parser.add_argument("--validate", action="store_true", help="Print the estimated range without making network requests.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    local_date = target_date_from_args(args.date)
    state = load_state()
    estimated = estimate_range(local_date, state, args.scan_margin)

    if args.validate:
        print(
            json.dumps(
                {
                    "targetDate": local_date.isoformat(),
                    "timezone": TZ_NAME,
                    "estimatedRange": {"startId": estimated[0], "endId": estimated[1]},
                    "state": state,
                    "knownConversionCheck": {"59378798": data_id_to_course_id(59378798)},
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(f"Updating dashboard for {local_date.isoformat()} ({TZ_NAME})", flush=True)
    day_courses, scanned_courses, scanned_range = scan_target_day(args, local_date, state)
    if not day_courses:
        raise RuntimeError(f"No levels found for {local_date.isoformat()}")

    payload = build_payload(local_date, day_courses)
    played_maker_id, recently_played = fetch_recently_played(args)
    payload["recentlyPlayedMakerId"] = played_maker_id
    payload["recentlyPlayed"] = recently_played
    payload["stats"]["recentlyPlayedCount"] = len(recently_played)
    if not args.skip_thumbnails:
        localize_thumbnails(
            payload,
            local_date,
            timeout=args.thumbnail_timeout,
            retries=args.thumbnail_retries,
            delay=args.thumbnail_delay,
            workers=args.thumbnail_workers,
        )

    save_payload(payload)
    update_index(payload)
    save_state(refresh_state(state, local_date, day_courses, scanned_courses, scanned_range))

    print(
        json.dumps(
            {
                "date": payload["date"],
                "totalLevels": payload["stats"]["totalLevels"],
                "topLiked": [course["courseId"] for course in payload["topLiked"]],
                "leastCleared": [course["courseId"] for course in payload["leastCleared"]],
                "topLongest": [course["courseId"] for course in payload["topLongest"]],
                "recentlyPlayed": [course["courseId"] for course in payload["recentlyPlayed"]],
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130)
