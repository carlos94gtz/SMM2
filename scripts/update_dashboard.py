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
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DATA_JS = ROOT / "data.js"
INDEX_HTML = ROOT / "index.html"
THUMBS_DIR = ROOT / "thumbs"
STATE_PATH = ROOT / "automation-state.json"
SCAN_CACHE_DIR = ROOT / ".scan-cache"

API_BASE = "https://tgrcode.com/mm2"
CHARSET = "0123456789BCDFGHJKLMNPQRSTVWXY"
TZ_NAME = "America/Mexico_City"
TZ = ZoneInfo(TZ_NAME)
PUBLISHED_DIFFICULTY = "Expert"
DIFFICULTY_ORDER = [PUBLISHED_DIFFICULTY]
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


class RequestPacer:
    def __init__(self, interval: float = 5.0):
        self.minimum_interval = interval
        self.interval = interval
        self.successes = 0
        self.next_request = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self.lock:
                delay = self.next_request - time.monotonic()
                if delay <= 0:
                    self.next_request = time.monotonic() + self.interval
                    return
            time.sleep(delay)

    def defer(self, seconds: float) -> None:
        with self.lock:
            self.next_request = max(self.next_request, time.monotonic() + seconds)

    def throttle(self, seconds: float) -> None:
        with self.lock:
            self.interval = min(60.0, self.interval * 2)
            self.successes = 0
            self.next_request = max(self.next_request, time.monotonic() + seconds)

    def succeeded(self) -> None:
        with self.lock:
            self.successes += 1
            if self.successes >= 50:
                self.interval = max(self.minimum_interval, self.interval * 0.9)
                self.successes = 0

    def restore_interval(self, interval: float) -> None:
        with self.lock:
            self.interval = max(self.interval, min(60.0, interval))


API_PACER = RequestPacer()


def rate_limit_delay(retry_after: str | None, attempt: int) -> float:
    delay = min(300.0, 60.0 * 2 ** attempt)
    if retry_after:
        try:
            requested = float(retry_after)
        except ValueError:
            try:
                requested = parsedate_to_datetime(retry_after).timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                requested = 0.0
        if requested > delay:
            delay = requested
    return delay


def retry_http(status: int, retry_after: str | None, attempt: int, retries: int) -> bool:
    if status == 429:
        delay = rate_limit_delay(retry_after, attempt)
        API_PACER.throttle(delay)
        print(f"TGRCode HTTP 429: pausing all API workers for {delay:.0f}s "
              f"(attempt {attempt + 1}/{retries + 1}); "
              f"request interval now {API_PACER.interval:.1f}s", flush=True)
        return attempt < retries
    if status >= 500 and attempt < retries:
        time.sleep(min(10.0, 0.9 * (attempt + 1) ** 2))
        return True
    return False


def request_json(path: str, timeout: float, retries: int) -> tuple[int, dict | None]:
    url = f"{API_BASE}{path}"
    for attempt in range(retries + 1):
        API_PACER.wait()
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
                    "\n%{http_code}\n%header{retry-after}",
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
            body, status_text, retry_after = result.stdout.rsplit("\n", 2)
            status = int(status_text)
            try:
                parsed = json.loads(body) if body else {}
            except json.JSONDecodeError:
                parsed = {"error": body[:500]}
            if retry_http(status, retry_after.strip(), attempt, retries):
                continue
            if status == 200:
                API_PACER.succeeded()
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
                payload = json.loads(body)
                API_PACER.succeeded()
                return response.status, payload
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            parsed = None
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = {"error": body[:500]}
            if retry_http(error.code, error.headers.get("Retry-After"), attempt, retries):
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
    """Fetch by internal ID, skipping deleted courses in the same API response.

    Returns courses, request count, split count, and missing ID count.
    """
    if not pairs:
        return [], 0, 0, 0

    if len(pairs) > 100:
        results = [fetch_pairs(batch, timeout, retries, pause) for batch in chunked(pairs, 100)]
        return (
            [course for result in results for course in result[0]],
            sum(result[1] for result in results),
            0,
            sum(result[3] for result in results),
        )

    data_ids = ",".join(str(pair.data_id) for pair in pairs)
    status, payload = request_json(f"/level_info_multiple_dataid/{data_ids}", timeout, retries)
    if pause:
        time.sleep(pause)

    if status != 200 or not isinstance(payload, dict) or payload.get("error") or not isinstance(payload.get("courses"), list):
        raise RuntimeError(f"TGRCode API unavailable while fetching {len(pairs)} levels (HTTP {status})")

    requested = {pair.data_id for pair in pairs}
    found = {}
    for course in payload["courses"]:
        if not isinstance(course, dict) or type(course.get("data_id")) is not int:
            raise RuntimeError("TGRCode returned a malformed course")
        data_id = course["data_id"]
        if data_id not in requested or data_id in found:
            raise RuntimeError(f"TGRCode returned an unrequested or duplicate course: {data_id}")
        uploaded = course.get("uploaded")
        if type(uploaded) not in (int, float) or not math.isfinite(uploaded) or uploaded <= 0:
            raise RuntimeError(f"TGRCode returned an invalid upload timestamp: {data_id}")
        found[data_id] = course
    ordered = [found[pair.data_id] for pair in pairs if pair.data_id in found]
    return ordered, 1, 0, len(pairs) - len(ordered)


class ScanCache:
    def __init__(self, directory: Path, local_date: dt.date):
        self.date = local_date.isoformat()
        self.path = directory / self.date

    def load(self, name: str) -> dict | None:
        try:
            value = json.loads((self.path / name).read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("version") != 1 or value.get("date") != self.date:
                return None
            interval = value.get("interval", 5.0)
            if isinstance(interval, (int, float)) and 0 < interval <= 60:
                API_PACER.restore_interval(interval)
            return value
        except (OSError, ValueError):
            return None

    def save(self, name: str, value: dict) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        payload = {**value, "version": 1, "date": self.date, "interval": API_PACER.interval}
        output = self.path / name
        temp = output.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp.replace(output)

    def load_range(self) -> tuple[int, int] | None:
        value = self.load("range.json") or {}
        start, end = value.get("start"), value.get("end")
        if type(start) is int and type(end) is int and 0 < start <= end:
            return start, end
        return None

    def load_batch(self, start: int, end: int) -> list[dict] | None:
        value = self.load(f"{start}-{end}.json") or {}
        courses = value.get("courses")
        if value.get("start") != start or value.get("end") != end or not isinstance(courses, list):
            return None
        ids = set()
        for course in courses:
            if not isinstance(course, dict):
                return None
            data_id, uploaded = course.get("data_id"), course.get("uploaded")
            if type(data_id) is not int or not start <= data_id <= end or data_id in ids:
                return None
            if type(uploaded) not in (int, float) or not math.isfinite(uploaded) or uploaded <= 0:
                return None
            ids.add(data_id)
        return courses

    def save_batch(self, start: int, end: int, courses: list[dict]) -> None:
        self.save(f"{start}-{end}.json", {"start": start, "end": end, "courses": courses})


def scan_range(
    start_id: int,
    end_id: int,
    *,
    batch_size: int,
    workers: int,
    timeout: float,
    retries: int,
    pause: float,
    checkpoint: ScanCache | None = None,
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

    pending = []
    for batch in batches:
        saved = checkpoint.load_batch(batch[0].data_id, batch[-1].data_id) if checkpoint else None
        if saved is None:
            pending.append(batch)
        else:
            courses.extend(saved)
            completed += len(batch)
            missing += len(batch) - len(saved)
    if completed:
        print(f"  Resumed {completed:,}/{len(pairs):,} IDs from checkpoint", flush=True)

    def completed_batches():
        if workers == 1:
            for batch in pending:
                yield batch, run_batch(batch)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                future_map = {executor.submit(run_batch, batch): batch for batch in pending}
                try:
                    for future in concurrent.futures.as_completed(future_map):
                        yield future_map[future], future.result()
                finally:
                    for future in future_map:
                        future.cancel()

    for batch, result in completed_batches():
        batch_courses, batch_requests, batch_splits, batch_missing = result
        if checkpoint:
            checkpoint.save_batch(batch[0].data_id, batch[-1].data_id, batch_courses)
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


def locate_day_range(args: argparse.Namespace, local_date: dt.date, state: dict) -> tuple[int, int]:
    start_ts, end_ts = day_bounds(local_date)
    status, payload = request_json("/search_new?count=10", args.timeout, args.retries)
    if status != 200 or not payload or not isinstance(payload.get("courses"), list):
        raise RuntimeError(f"Could not load recent TGRCode courses (HTTP {status})")
    recent = [c for c in payload["courses"] if c.get("data_id") and c.get("uploaded")]
    if not recent:
        raise RuntimeError("TGRCode returned no recent courses; refusing to guess a range")
    latest = max(recent, key=lambda course: int(course["data_id"]))
    if int(latest["uploaded"]) < end_ts:
        raise RuntimeError(f"Recent TGRCode courses do not yet cover the end of {local_date}")
    latest_id = int(latest["data_id"])
    print(f"Recent course: data_id {latest_id}, uploaded {latest['uploaded']}", flush=True)

    def sample(start_id: int, end_id: int) -> list[dict]:
        pairs = [IdPair(value, data_id_to_course_id(value)) for value in range(start_id, end_id + 1)]
        courses, _, _, _ = fetch_pairs(pairs, args.timeout, args.retries, args.pause)
        return sorted(courses, key=lambda course: int(course["data_id"]))

    # Bracket the day using real timestamps, then narrow both boundaries. Deleted
    # IDs may leave a wider bracket, but must never exclude unscanned courses.
    distance = max(1, int(state.get("idsPerDay") or DEFAULT_STATE["idsPerDay"]))
    before = None
    for _ in range(24):
        probe = max(1, latest_id - distance)
        courses = sample(probe, min(latest_id - 1, probe + 19))
        older = [c for c in courses if int(c["uploaded"]) < start_ts]
        if older:
            before = max(older, key=lambda course: int(course["data_id"]))
            break
        if probe == 1:
            break
        distance *= 2
    if before is None:
        raise RuntimeError(f"Could not locate courses before {local_date}; refusing partial coverage")

    def boundary(timestamp: int) -> tuple[dict, dict]:
        low, high = before, latest
        while int(high["data_id"]) - int(low["data_id"]) > 64:
            midpoint = (int(low["data_id"]) + int(high["data_id"])) // 2
            courses = sample(midpoint, min(midpoint + 19, int(high["data_id"]) - 1))
            if not courses:
                break
            for course in courses:
                if int(course["uploaded"]) < timestamp:
                    low = course
                else:
                    high = course
                    break
        return low, high

    before_start, _ = boundary(start_ts)
    _, after_end = boundary(end_ts)
    scan_start, scan_end = int(before_start["data_id"]), int(after_end["data_id"])
    print(f"Calibrated range for {local_date}: {scan_start}..{scan_end}", flush=True)
    return scan_start, scan_end


def scan_target_day(args: argparse.Namespace, local_date: dt.date, state: dict) -> tuple[list[dict], list[dict], tuple[int, int]]:
    start_ts, end_ts = day_bounds(local_date)
    checkpoint = ScanCache(Path(args.checkpoint_dir), local_date) if args.checkpoint_dir else None
    if checkpoint:
        checkpoint.load("pacing.json")
    saved_range = checkpoint.load_range() if checkpoint else None
    current_start, current_end = saved_range or locate_day_range(args, local_date, state)
    if checkpoint and not saved_range:
        checkpoint.save("range.json", {"start": current_start, "end": current_end})
    all_courses: dict[int, dict] = {}
    scanned_start: int | None = None
    scanned_end: int | None = None

    def add_scan(scan_start: int, scan_end: int) -> None:
        nonlocal scanned_start, scanned_end
        if scan_end < scan_start:
            return
        try:
            found = scan_range(
                scan_start,
                scan_end,
                batch_size=args.batch_size,
                workers=args.workers,
                timeout=args.timeout,
                retries=args.retries,
                pause=args.pause,
                checkpoint=checkpoint,
            )
        finally:
            if checkpoint:
                checkpoint.save("pacing.json", {})
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
    courses = [course for course in courses if course.get("difficulty_name") == PUBLISHED_DIFFICULTY]
    start_ts, end_ts = day_bounds(local_date)
    generated_at = dt.datetime.now(tz=TZ).isoformat(timespec="seconds")
    top_liked = top_liked_courses(courses)
    top_longest = top_longest_courses(courses)

    return {
        "generatedAt": generated_at,
        "assetVersion": safe_asset_version(generated_at),
        "date": local_date.isoformat(),
        "timezone": TZ_NAME,
        "difficulties": DIFFICULTY_ORDER,
        "publicationDifficulty": PUBLISHED_DIFFICULTY,
        "range": {"startTs": start_ts, "endTs": end_ts},
        "stats": {
            "totalLevels": len(courses),
            "topLikedCount": len(top_liked),
            "topLikedMinClearCheckMs": MIN_CLEAR_CHECK_MS_FOR_TOP_LIKED,
            "topLongestCount": len(top_longest),
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
    courses = [course for course in courses if course.get("difficulty_name") == PUBLISHED_DIFFICULTY]
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
    for group in (payload.get("topLikedByDifficulty") or {}).values():
        yield group
    for group in (payload.get("topLongestByDifficulty") or {}).values():
        yield group


def download_binary(url: str, output: Path, timeout: float, retries: int) -> None:
    temp = output.with_suffix(".tmp")
    last_error: Exception | str | None = None
    for attempt in range(retries + 1):
        API_PACER.wait()
        curl_missing = False
        try:
            result = subprocess.run(
                [
                    "curl",
                    "-sS",
                    "-L",
                    "--max-time",
                    str(timeout),
                    "-H",
                    "User-Agent: smm2-dashboard-updater/1.0",
                    "-o",
                    str(temp),
                    "-w",
                    "\n%{http_code}\n%header{retry-after}",
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=timeout + 5,
                check=False,
            )
            _, status_text, retry_after = result.stdout.rsplit("\n", 2)
            status = int(status_text)
            if status == 429:
                temp.unlink(missing_ok=True)
                last_error = "TGRCode thumbnail HTTP 429"
                if retry_http(status, retry_after.strip(), attempt, retries):
                    continue
                break
            if result.returncode == 0 and status == 200 and temp.exists() and temp.stat().st_size > 0:
                API_PACER.succeeded()
                temp.replace(output)
                return
            temp.unlink(missing_ok=True)
            last_error = result.stderr.strip() or "empty thumbnail response"
        except FileNotFoundError:
            curl_missing = True
        except (subprocess.TimeoutExpired, OSError, ValueError) as error:
            temp.unlink(missing_ok=True)
            last_error = error

        if curl_missing:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "smm2-dashboard-updater/1.0"})
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    data = response.read()
                if data:
                    temp.write_bytes(data)
                    API_PACER.succeeded()
                    temp.replace(output)
                    return
                last_error = "empty thumbnail response"
            except urllib.error.HTTPError as error:
                temp.unlink(missing_ok=True)
                last_error = error
                if error.code == 429:
                    if retry_http(error.code, error.headers.get("Retry-After"), attempt, retries):
                        continue
                    break
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
    text = INDEX_HTML.read_text(encoding="utf-8")
    text = re.sub(
        r'\s*<section\b[^>]*aria-labelledby="(?:clearedTitle|playedTitle)"[^>]*>.*?</section>',
        "", text, flags=re.DOTALL,
    )
    for board, mode in (("topLiked", "likes"), ("topLongest", "time")):
        content = rendered_list(payload.get(board, []), mode, "No hay niveles Expert para esta fecha.")
        text = replace_list(text, board, content, '\n        </section>')
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
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--checkpoint-dir", type=Path, help="Directory for resumable per-day scan batches.")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--pause", type=float, default=0.08, help="Pause after each HTTP request per worker.")
    parser.add_argument("--thumbnail-timeout", type=float, default=10.0)
    parser.add_argument("--thumbnail-retries", type=int, default=1)
    parser.add_argument("--thumbnail-delay", type=float, default=0.05)
    parser.add_argument("--thumbnail-workers", type=int, default=1)
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
                "topLongest": [course["courseId"] for course in payload["topLongest"]],
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
