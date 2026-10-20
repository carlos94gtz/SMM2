import datetime as dt
import io
import unittest
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
        for response in [(0, None), (502, {"error": "Bad gateway"}), (429, {"error": "Rate limit"}), (200, {})]:
            with self.subTest(response=response), patch.object(dashboard, "request_json", return_value=response):
                with self.assertRaises(RuntimeError):
                    dashboard.fetch_pairs([self.pair], 20, 4, 0)

    def test_explicit_missing_course_is_counted(self):
        with patch.object(dashboard, "request_json", return_value=(404, {"error": "No course with that ID"})):
            self.assertEqual(dashboard.fetch_pairs([self.pair], 20, 4, 0), ([], 1, 0, 1))

    def test_unrequested_missing_course_cannot_recurse_forever(self):
        response = (400, {"error": "No course with that ID", "course_id": "OTHERID00"})
        with patch.object(dashboard, "request_json", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "unrequested course"):
                dashboard.fetch_pairs([self.pair, self.pair], 20, 4, 0)


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
        self.assertEqual(starts, [0, 120, 122])

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
        with patch.object(dashboard, "API_PACER"), patch.object(dashboard.subprocess, "run", return_value=response):
            self.assertEqual(dashboard.request_json("/level_info/TEST", 20, 0)[0], 429)


if __name__ == "__main__":
    unittest.main()
