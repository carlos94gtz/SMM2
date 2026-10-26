import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import update_dashboard as dashboard


class RangeCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.date = dt.date(2026, 10, 18)
        self.start, self.end = dashboard.day_bounds(self.date)
        self.args = dashboard.build_parser().parse_args([])
        self.args.pause = 0
        self.state = {"anchorDate": "2026-10-12", "anchorStartId": 60759044, "idsPerDay": 12235}

    def course(self, value):
        return {"data_id": value, "uploaded": self.start + (value - 60810000) * 10}

    def calibrate(self, deleted=None):
        latest = self.course(60825000)

        def fetch(pairs, *args):
            courses = [self.course(pair.data_id) for pair in pairs if not deleted or not deleted(pair.data_id)]
            return courses, 1, 0, len(pairs) - len(courses)

        with patch.object(dashboard, "request_json", return_value=(200, {"courses": [latest]})), patch.object(
            dashboard, "fetch_pairs", side_effect=fetch
        ):
            return dashboard.locate_day_range(self.args, self.date, self.state)

    def test_stale_estimate_ahead_of_latest_is_recalibrated(self):
        self.assertGreater(dashboard.estimate_range(self.date, self.state, 1000)[0], 60825000)
        first, last = self.calibrate()
        self.assertLess(self.course(first)["uploaded"], self.start)
        self.assertGreaterEqual(self.course(last)["uploaded"], self.end)
        self.assertLess(60810000 - first, 100)
        self.assertLess(last - 60818640, 100)

    def test_deleted_probe_keeps_a_complete_bracket(self):
        first, last = self.calibrate(lambda value: 60812000 <= value <= 60820000)
        self.assertLess(self.course(first)["uploaded"], self.start)
        self.assertGreaterEqual(self.course(last)["uploaded"], self.end)

    def test_recent_data_must_cover_end_of_day(self):
        with patch.object(dashboard, "request_json", return_value=(200, {"courses": [self.course(60815000)]})):
            with self.assertRaisesRegex(RuntimeError, "do not yet cover"):
                dashboard.locate_day_range(self.args, self.date, self.state)

    def test_recent_api_failure_does_not_fall_back_to_stale_estimate(self):
        with patch.object(dashboard, "request_json", return_value=(502, {"error": "Bad gateway"})):
            with self.assertRaisesRegex(RuntimeError, "recent TGRCode"):
                dashboard.locate_day_range(self.args, self.date, self.state)

    def test_scan_only_returns_day_with_both_boundaries_verified(self):
        first, last = 60809999, 60818640

        def scan(start, end, **kwargs):
            return [self.course(value) for value in range(start, end + 1)]

        with patch.object(dashboard, "locate_day_range", return_value=(first, last)), patch.object(
            dashboard, "scan_range", side_effect=scan
        ):
            courses, _, scanned_range = dashboard.scan_target_day(self.args, self.date, self.state)
        self.assertEqual(len(courses), 8640)
        self.assertEqual(scanned_range, (first, last))
        self.assertTrue(all(self.start <= course["uploaded"] < self.end for course in courses))


class LookupFailureTests(unittest.TestCase):
    def setUp(self):
        self.pair = dashboard.IdPair(60815000, "Y03HBGPWG")

    def test_connection_server_rate_limit_and_malformed_failures_are_not_missing(self):
        for response in [(0, None), (502, {"error": "Bad gateway"}), (429, {"error": "Rate limit"}),
                         (200, {}), (404, {"error": "No course with that ID"}),
                         (200, {"error": "Server failure", "courses": []})]:
            with self.subTest(response=response), patch.object(dashboard, "request_json", return_value=response):
                with self.assertRaises(RuntimeError):
                    dashboard.fetch_pairs([self.pair], 20, 4, 0)

    def test_successful_empty_data_id_response_counts_missing_course(self):
        with patch.object(dashboard, "request_json", return_value=(200, {"courses": []})):
            self.assertEqual(dashboard.fetch_pairs([self.pair], 20, 4, 0), ([], 1, 0, 1))

    def test_unrequested_course_is_rejected(self):
        response = (200, {"courses": [{"data_id": 123, "uploaded": 1792453200}]})
        with patch.object(dashboard, "request_json", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "unrequested"):
                dashboard.fetch_pairs([self.pair, self.pair], 20, 4, 0)

    def test_deleted_courses_do_not_retry_valid_ids_in_the_batch(self):
        pairs = [dashboard.IdPair(value, dashboard.data_id_to_course_id(value)) for value in range(1, 21)]
        courses = [{"data_id": value, "uploaded": 1792453200.0} for value in (1, 3, 5, 7, 9, 11, 13)]
        with patch.object(dashboard, "request_json", return_value=(200, {"courses": list(reversed(courses))})) as request:
            result = dashboard.fetch_pairs(pairs, 20, 4, 0)
        self.assertEqual(result, (courses, 1, 0, 13))
        request.assert_called_once_with("/level_info_multiple_dataid/" + ",".join(map(str, range(1, 21))), 20, 4)

    def test_malformed_duplicate_and_invalid_timestamp_courses_are_not_silently_missing(self):
        course = {"data_id": self.pair.data_id, "uploaded": 1792453200}
        for courses in ([None], [{}], [course, course], [{**course, "uploaded": "invalid"}],
                        [{**course, "uploaded": float("nan")}], [{**course, "uploaded": True}]):
            with self.subTest(courses=courses), patch.object(dashboard, "request_json", return_value=(200, {"courses": courses})):
                with self.assertRaises(RuntimeError):
                    dashboard.fetch_pairs([self.pair], 20, 4, 0)

    def test_requests_never_exceed_the_data_id_endpoint_limit(self):
        pairs = [dashboard.IdPair(value, dashboard.data_id_to_course_id(value)) for value in range(1, 206)]
        with patch.object(dashboard, "request_json", return_value=(200, {"courses": []})) as request:
            self.assertEqual(dashboard.fetch_pairs(pairs, 20, 4, 0), ([], 3, 0, 205))
        self.assertEqual([len(call.args[0].rsplit("/", 1)[1].split(",")) for call in request.call_args_list], [100, 100, 5])

    def test_400_id_scan_with_deleted_levels_queries_each_id_only_once(self):
        queried = []

        def respond(path, *args):
            ids = list(map(int, path.rsplit("/", 1)[1].split(",")))
            queried.extend(ids)
            return 200, {"courses": [{"data_id": value, "uploaded": 1792453200} for value in ids if value % 5]}

        with patch.object(dashboard, "request_json", side_effect=respond) as request:
            courses = dashboard.scan_range(1, 400, batch_size=20, workers=1, timeout=20, retries=4, pause=0)
        self.assertEqual(len(courses), 320)
        self.assertEqual(queried, list(range(1, 401)))
        self.assertEqual(request.call_count, 20)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds


class RateLimitTests(unittest.TestCase):
    def test_retry_after_seconds_and_http_dates(self):
        self.assertEqual(dashboard.rate_limit_delay("120", 0), 120)
        self.assertEqual(dashboard.rate_limit_delay("invalid", 0), 60)
        self.assertEqual(dashboard.rate_limit_delay(None, 2), 240)
        with patch.object(dashboard.time, "time", return_value=1792497600):
            self.assertEqual(dashboard.rate_limit_delay("Tue, 20 Oct 2026 12:05:00 GMT", 0), 300)

    def test_pacer_spaces_requests_and_shares_longest_cooldown(self):
        clock = FakeClock()
        pacer = dashboard.RequestPacer(interval=2)
        with patch.object(dashboard.time, "monotonic", side_effect=lambda: clock.now), patch.object(
            dashboard.time, "sleep", side_effect=clock.sleep
        ):
            pacer.wait()
            pacer.wait()
            self.assertEqual(clock.now, 2)
            pacer.defer(120)
            pacer.defer(60)
            pacer.wait()
            self.assertEqual(clock.now, 122)
            pacer.wait()
            self.assertEqual(clock.now, 124)

    def test_curl_429_retries_after_shared_pause(self):
        clock = FakeClock()
        responses = iter([
            '{"error":"Too many requests"}\n429\n120',
            '{"courses":[]}\n200\n',
            '{"courses":[]}\n200\n',
        ])
        starts = []

        def curl(*args, **kwargs):
            starts.append(clock.now)
            return SimpleNamespace(returncode=0, stdout=next(responses))

        with patch.object(dashboard, "API_PACER", dashboard.RequestPacer()), patch.object(
            dashboard.time, "monotonic", side_effect=lambda: clock.now
        ), patch.object(dashboard.time, "sleep", side_effect=clock.sleep), patch.object(
            dashboard.subprocess, "run", side_effect=curl
        ):
            self.assertEqual(dashboard.request_json("/search_new?count=10", 20, 1), (200, {"courses": []}))
            dashboard.request_json("/search_new?count=10", 20, 1)
        self.assertEqual(starts, [0, 120, 130])

    def test_urllib_429_uses_same_cooldown(self):
        clock = FakeClock()
        error = dashboard.urllib.error.HTTPError(
            "https://tgrcode.com/mm2/search_new?count=10", 429, "Rate limit",
            {"Retry-After": "120"}, io.BytesIO(b'{"error":"Too many requests"}')
        )
        response = MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = b'{"courses":[]}'
        with patch.object(dashboard, "API_PACER", dashboard.RequestPacer()), patch.object(
            dashboard.time, "monotonic", side_effect=lambda: clock.now
        ), patch.object(dashboard.time, "sleep", side_effect=clock.sleep), patch.object(
            dashboard.subprocess, "run", side_effect=FileNotFoundError
        ), patch.object(dashboard.urllib.request, "urlopen", side_effect=[error, response]):
            self.assertEqual(dashboard.request_json("/search_new?count=10", 20, 1), (200, {"courses": []}))
        self.assertEqual(clock.now, 120)

    def test_persistent_rate_limit_still_fails_without_losing_data_silently(self):
        response = SimpleNamespace(returncode=0, stdout='{"error":"Rate limit"}\n429\n')
        with patch.object(dashboard, "API_PACER", dashboard.RequestPacer()), patch.object(dashboard.subprocess, "run", return_value=response):
            self.assertEqual(dashboard.request_json("/level_info/TEST", 20, 0)[0], 429)

    def test_rate_limit_slows_future_requests_and_recovers_only_gradually(self):
        pacer = dashboard.RequestPacer()
        with patch.object(dashboard, "API_PACER", pacer):
            dashboard.retry_http(429, "180", 0, 4)
        self.assertEqual(pacer.interval, 10)
        for _ in range(49):
            pacer.succeeded()
        self.assertEqual(pacer.interval, 10)
        pacer.succeeded()
        self.assertEqual(pacer.interval, 9)
        for _ in range(1000):
            pacer.succeeded()
        self.assertEqual(pacer.interval, 5)

    def test_adaptive_interval_is_capped_and_rate_limit_resets_success_streak(self):
        pacer = dashboard.RequestPacer()
        for _ in range(49):
            pacer.succeeded()
        for _ in range(10):
            pacer.throttle(60)
        self.assertEqual(pacer.interval, 60)
        pacer.succeeded()
        self.assertEqual(pacer.interval, 60)

    def test_thumbnails_use_shared_pacer_and_do_not_save_error_bodies(self):
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "level.jpg"
            starts = []

            def curl(*args, **kwargs):
                starts.append(clock.now)
                output.with_suffix(".tmp").write_bytes(b"error" if len(starts) == 1 else b"image")
                return SimpleNamespace(returncode=0, stdout="\n429\n120" if len(starts) == 1 else "\n200\n")

            with patch.object(dashboard, "API_PACER", dashboard.RequestPacer()), patch.object(
                dashboard.time, "monotonic", side_effect=lambda: clock.now
            ), patch.object(dashboard.time, "sleep", side_effect=clock.sleep), patch.object(
                dashboard.subprocess, "run", side_effect=curl
            ):
                dashboard.download_binary("https://tgrcode.com/mm2/level_thumbnail/TEST", output, 20, 1)
            self.assertEqual(starts, [0, 120])
            self.assertEqual(output.read_bytes(), b"image")


class ScanCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.date = dt.date(2026, 10, 24)
        self.start, self.end = dashboard.day_bounds(self.date)
        self.cache = dashboard.ScanCache(Path(self.temp.name), self.date)
        self.pacer_patch = patch.object(dashboard, "API_PACER", dashboard.RequestPacer())
        self.pacer_patch.start()
        self.addCleanup(self.pacer_patch.stop)

    def course(self, value):
        timestamp = self.start - 1 if value == 1 else self.end if value == 6 else self.start + value
        return {"data_id": value, "uploaded": timestamp}

    def scan(self):
        return dashboard.scan_range(1, 6, batch_size=2, workers=1, timeout=20, retries=0, pause=0,
                                    checkpoint=self.cache)

    def test_failure_keeps_completed_batches_and_resume_fetches_only_pending(self):
        def fail(pairs, *args):
            if pairs[0].data_id == 3:
                raise RuntimeError("HTTP 429")
            return [self.course(pair.data_id) for pair in pairs], 1, 0, 0

        with patch.object(dashboard, "fetch_pairs", side_effect=fail) as fetch:
            with self.assertRaisesRegex(RuntimeError, "429"):
                self.scan()
            self.assertEqual(fetch.call_count, 2)
        self.assertEqual(len(self.cache.load_batch(1, 2)), 2)
        self.assertIsNone(self.cache.load_batch(3, 4))
        self.assertIsNone(self.cache.load_batch(5, 6))

        def succeed(pairs, *args):
            return [self.course(pair.data_id) for pair in pairs], 1, 0, 0

        with patch.object(dashboard, "fetch_pairs", side_effect=succeed) as fetch:
            courses = self.scan()
        self.assertEqual([course["data_id"] for course in courses], list(range(1, 7)))
        self.assertEqual([call.args[0][0].data_id for call in fetch.call_args_list], [3, 5])

    def test_complete_checkpoint_reuses_range_but_still_checks_day_coverage(self):
        self.cache.save("range.json", {"start": 1, "end": 6})
        for start in (1, 3, 5):
            self.cache.save_batch(start, start + 1, [self.course(start), self.course(start + 1)])
        args = dashboard.build_parser().parse_args(["--checkpoint-dir", self.temp.name, "--batch-size", "2"])
        with patch.object(dashboard, "locate_day_range") as locate, patch.object(dashboard, "fetch_pairs") as fetch:
            courses, _, scanned_range = dashboard.scan_target_day(args, self.date, {})
        locate.assert_not_called()
        fetch.assert_not_called()
        self.assertEqual(len(courses), 4)
        self.assertEqual(scanned_range, (1, 6))

    def test_empty_batch_is_a_completed_batch_and_other_dates_are_not_reused(self):
        self.cache.save_batch(1, 2, [])
        self.assertEqual(self.cache.load_batch(1, 2), [])
        other = dashboard.ScanCache(Path(self.temp.name), self.date + dt.timedelta(days=1))
        self.assertIsNone(other.load_batch(1, 2))

    def test_float_upload_timestamps_from_data_id_endpoint_are_resumable(self):
        course = {**self.course(2), "uploaded": float(self.start + 2)}
        self.cache.save_batch(1, 2, [course])
        self.assertEqual(self.cache.load_batch(1, 2), [course])

    def test_corrupt_wrong_date_and_out_of_range_checkpoints_are_rejected(self):
        self.cache.path.mkdir(parents=True)
        path = self.cache.path / "1-2.json"
        for payload in ("{", json.dumps({"version": 1, "date": "2026-10-23"}),
                        json.dumps({"version": 1, "date": self.date.isoformat(), "start": 1, "end": 2,
                                    "courses": [self.course(3)]})):
            path.write_text(payload, encoding="utf-8")
            self.assertIsNone(self.cache.load_batch(1, 2))

    def test_saved_slow_rate_is_restored(self):
        dashboard.API_PACER.throttle(60)
        self.cache.save("pacing.json", {})
        with patch.object(dashboard, "API_PACER", dashboard.RequestPacer()):
            self.cache.load("pacing.json")
            self.assertEqual(dashboard.API_PACER.interval, 10)

    def test_defaults_use_one_worker_for_levels_and_thumbnails(self):
        args = dashboard.build_parser().parse_args([])
        self.assertEqual(args.workers, 1)
        self.assertEqual(args.thumbnail_workers, 1)


if __name__ == "__main__":
    unittest.main()
