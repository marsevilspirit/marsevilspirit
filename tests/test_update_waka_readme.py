import contextlib
import copy
import importlib.util
from http.client import BadStatusLine, IncompleteRead
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/scripts/update_waka_readme.py"
TOKEN = "waka_tok_simulated_test_only"
START = "<!--START_SECTION:waka-->"
END = "<!--END_SECTION:waka-->"
OLD_README = f"Profile 狐狸\\1\n\n{START}\n\n```txt\nold stats\n```\n\n{END}\n\nOther content\n"
PAYLOAD = json.loads((ROOT / "tests/fixtures/waka_stats.json").read_text())


class Response(io.BytesIO):
    def __init__(self, payload, status=200):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        super().__init__(body)
        self.status = status


class FakeOpener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def http_error(code):
    return HTTPError("https://api.wakatime.com/", code, TOKEN, {}, io.BytesIO(TOKEN.encode()))


class UpdateWakaReadmeTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "OAuth updater has not been implemented")
        spec = importlib.util.spec_from_file_location("waka", SCRIPT)
        self.waka = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.waka)

    def run_main(self, responses, token=TOKEN, readme=OLD_README):
        opener = FakeOpener(responses)
        log = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "README.md"
            path.write_bytes(readme.encode())
            previous_directory = Path.cwd()
            try:
                os.chdir(directory)
                environment = {} if token is None else {"WAKATIME_ACCESS_TOKEN": token}
                with patch.dict(os.environ, environment, clear=True), \
                     patch.object(self.waka, "build_opener", return_value=opener), \
                     patch.object(self.waka, "sleep") as sleep, \
                     contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                    result = self.waka.main()
                contents = path.read_bytes().decode()
            finally:
                os.chdir(previous_directory)
        self.assertNotIn(TOKEN, log.getvalue())
        return result, contents, log.getvalue(), opener, sleep.call_count

    def test_missing_token_fails_before_request_and_keeps_readme(self):
        result, contents, log, opener, _ = self.run_main([], token=None)
        self.assertEqual(result, 1)
        self.assertEqual(contents, OLD_README)
        self.assertIn("WAKATIME_ACCESS_TOKEN", log)
        self.assertEqual(opener.requests, [])

    def test_bearer_header_and_original_all_time_endpoint(self):
        result, contents, _, opener, _ = self.run_main([Response(PAYLOAD)])
        self.assertEqual(result, 0)
        self.assertNotEqual(contents, OLD_README)
        request, timeout = opener.requests[0]
        self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertEqual(request.full_url, "https://api.wakatime.com/api/v1/users/current/stats/all_time")
        self.assertNotIn(TOKEN, request.full_url)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(timeout, 30)

    def test_invalid_token_is_not_sent_or_logged(self):
        for token in ["   ", TOKEN + "\nheader: injected", TOKEN + "\x01", TOKEN + "私"]:
            with self.subTest(token=repr(token)):
                result, contents, _, opener, _ = self.run_main([], token=token)
                self.assertEqual(result, 1)
                self.assertEqual(contents, OLD_README)
                self.assertEqual(opener.requests, [])

    def test_expired_token_fails_without_api_key_fallback(self):
        result, contents, log, opener, sleeps = self.run_main([http_error(401)])
        self.assertEqual(result, 1)
        self.assertEqual(contents, OLD_README)
        self.assertIn("expired or revoked", log)
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(sleeps, 0)

    def test_forbidden_response_explains_required_scope(self):
        result, contents, log, _, _ = self.run_main([http_error(403)])
        self.assertEqual(result, 1)
        self.assertEqual(contents, OLD_README)
        self.assertIn("read_stats.languages", log)

    def test_redirects_cannot_forward_credentials(self):
        handler = self.waka.NoRedirects()
        self.assertIsNone(handler.redirect_request(None, None, 302, TOKEN, {}, "https://example.com"))
        result, contents, _, opener, _ = self.run_main([http_error(302)])
        self.assertEqual(result, 1)
        self.assertEqual(contents, OLD_README)
        self.assertEqual(len(opener.requests), 1)

    def test_transient_http_errors_retry_and_recover(self):
        for code in [429, 500, 502, 503, 504]:
            with self.subTest(code=code):
                result, contents, _, opener, sleeps = self.run_main([http_error(code), Response(PAYLOAD)])
                self.assertEqual(result, 0)
                self.assertNotEqual(contents, OLD_README)
                self.assertEqual(len(opener.requests), 2)
                self.assertEqual(sleeps, 1)

    def test_transient_http_errors_stop_after_three_attempts(self):
        for code in [429, 503]:
            with self.subTest(code=code):
                result, contents, _, opener, sleeps = self.run_main([http_error(code) for _ in range(3)])
                self.assertEqual(result, 1)
                self.assertEqual(contents, OLD_README)
                self.assertEqual(len(opener.requests), 3)
                self.assertEqual(sleeps, 2)

    def test_pending_or_stale_stats_retry_and_recover(self):
        stale = copy.deepcopy(PAYLOAD)
        stale["data"]["is_up_to_date"] = False
        for response in [Response(b"pending", 202), Response(stale)]:
            with self.subTest(status=response.status):
                result, contents, _, opener, sleeps = self.run_main([response, Response(PAYLOAD)])
                self.assertEqual(result, 0)
                self.assertNotEqual(contents, OLD_README)
                self.assertEqual(len(opener.requests), 2)
                self.assertEqual(sleeps, 1)

    def test_pending_or_stale_stats_never_overwrite_readme(self):
        stale = copy.deepcopy(PAYLOAD)
        stale["data"]["is_up_to_date"] = False
        for payload, status in [(stale, 200), (b"pending", 202)]:
            with self.subTest(status=status):
                result, contents, _, opener, _ = self.run_main([Response(payload, status) for _ in range(3)])
                self.assertEqual(result, 1)
                self.assertEqual(contents, OLD_README)
                self.assertEqual(len(opener.requests), 3)

    def test_network_errors_retry_without_logging_exception_details(self):
        for error in [URLError(TOKEN), TimeoutError(TOKEN), BadStatusLine(TOKEN), IncompleteRead(TOKEN.encode())]:
            with self.subTest(error=type(error).__name__):
                result, _, _, opener, _ = self.run_main([error, Response(PAYLOAD)])
                self.assertEqual(result, 0)
                self.assertEqual(len(opener.requests), 2)
        result, contents, _, _, _ = self.run_main([URLError(TOKEN) for _ in range(3)])
        self.assertEqual(result, 1)
        self.assertEqual(contents, OLD_README)

    def test_bad_json_and_api_error_payloads_are_not_logged(self):
        for payload in [TOKEN.encode(), {"error": TOKEN}, {"errors": {"token": TOKEN}},
                        {}, [], {"data": None}, {"data": []}, {"data": {}}]:
            with self.subTest(payload=repr(payload)):
                result, contents, _, _, _ = self.run_main([Response(payload)])
                self.assertEqual(result, 1)
                self.assertEqual(contents, OLD_README)

    def test_missing_display_fields_do_not_silently_change_format(self):
        for field in ["start", "end", "human_readable_total_including_other_language", "languages"]:
            with self.subTest(field=field):
                payload = copy.deepcopy(PAYLOAD)
                del payload["data"][field]
                result, contents, _, _, _ = self.run_main([Response(payload)])
                self.assertEqual(result, 1)
                self.assertEqual(contents, OLD_README)

    def test_credential_echo_in_valid_response_is_never_written(self):
        payload = copy.deepcopy(PAYLOAD)
        payload["data"]["human_readable_total_including_other_language"] = TOKEN
        result, contents, _, _, _ = self.run_main([Response(payload)])
        self.assertEqual(result, 1)
        self.assertEqual(contents, OLD_README)
        self.assertNotIn(TOKEN, contents)

    def test_malformed_display_fields_cannot_damage_readme(self):
        mutations = [("start", TOKEN), ("end", None), ("languages", "invalid"),
                     ("human_readable_total_including_other_language", "```\n" + TOKEN)]
        for percent in [-1, 101, float("nan"), float("inf"), None, 10**400, True]:
            mutations.append(("languages", [{"name": "Python", "text": "1 hr", "percent": percent}]))
        for field, value in [("name", "Python\n```"), ("text", "1 hr\n" + TOKEN)]:
            language = {"name": "Python", "text": "1 hr", "percent": 100}
            language[field] = value
            mutations.append(("languages", [language]))
        mutations.append(("languages", [{"name": "Python", "text": "1 hr"}]))
        for field, value in mutations:
            with self.subTest(field=field, value=repr(value)):
                payload = copy.deepcopy(PAYLOAD)
                payload["data"][field] = value
                result, contents, _, _, _ = self.run_main([Response(payload)])
                self.assertEqual(result, 1)
                self.assertEqual(contents, OLD_README)

    def test_exact_format_matches_pinned_upstream_golden(self):
        expected = (ROOT / "tests/fixtures/waka_section.txt").read_text()
        result, contents, _, _, _ = self.run_main([Response(PAYLOAD)])
        self.assertEqual(result, 0)
        self.assertEqual(contents, OLD_README.split(START)[0] + expected + OLD_README.split(END)[1])
        self.assertIn("Total Time: 100 hrs", contents)
        section = contents.split(START)[1].split(END)[0]
        self.assertNotIn("\nOther ", section)
        self.assertNotIn("SurpriseLongLanguage", contents)
        self.assertIn("50.00 %", contents)

    def test_existing_language_count_boundary_is_preserved(self):
        payload = copy.deepcopy(PAYLOAD)
        other = payload["data"]["languages"].pop(1)
        payload["data"]["languages"].insert(9, other)
        result, contents, _, _, _ = self.run_main([Response(payload)])
        self.assertEqual(result, 0)
        self.assertIn("SurpriseLongLanguage", contents)

    def test_empty_languages_match_existing_no_activity_format(self):
        payload = copy.deepcopy(PAYLOAD)
        payload["data"]["languages"] = []
        result, contents, _, _, _ = self.run_main([Response(payload)])
        self.assertEqual(result, 0)
        self.assertIn("Total Time: 100 hrs\n\nNo activity tracked\n```", contents)

    def test_only_other_matches_existing_total_without_language_rows(self):
        payload = copy.deepcopy(PAYLOAD)
        payload["data"]["languages"] = [payload["data"]["languages"][1]]
        result, contents, _, _, _ = self.run_main([Response(payload)])
        self.assertEqual(result, 0)
        self.assertIn("Total Time: 100 hrs\n```", contents)
        self.assertNotIn("No activity tracked", contents)

    def test_write_error_does_not_log_exception_details(self):
        with patch.object(self.waka.Path, "replace", side_effect=OSError(TOKEN)):
            result, contents, log, _, _ = self.run_main([Response(PAYLOAD)])
        self.assertEqual(result, 1)
        self.assertEqual(contents, OLD_README)
        self.assertIn("Unable to read or write README safely", log)

    def test_marker_errors_keep_original_readme(self):
        for readme in ["No markers", END + START, START + START + END, START + END + END]:
            with self.subTest(readme=readme):
                result, contents, _, _, _ = self.run_main([Response(PAYLOAD)], readme=readme)
                self.assertEqual(result, 1)
                self.assertEqual(contents, readme)

    def test_crlf_and_unicode_outside_section_are_preserved(self):
        readme = OLD_README.replace("\n", "\r\n")
        result, contents, _, _, _ = self.run_main([Response(PAYLOAD)], readme=readme)
        self.assertEqual(result, 0)
        self.assertEqual(contents.split(START)[0], readme.split(START)[0])
        self.assertEqual(contents.split(END)[1], readme.split(END)[1])

    def test_repeated_runs_do_not_rewrite_identical_readme(self):
        _, updated, _, _, _ = self.run_main([Response(PAYLOAD)])
        with patch.object(self.waka.Path, "replace") as replace:
            result, contents, log, _, _ = self.run_main([Response(PAYLOAD)], readme=updated)
        self.assertEqual(result, 0)
        self.assertEqual(contents, updated)
        self.assertIn("unchanged", log)
        replace.assert_not_called()

    def test_atomic_write_failure_keeps_original_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "README.md"
            path.write_bytes(OLD_README.encode())
            with patch.object(self.waka.Path, "replace", side_effect=OSError(TOKEN)):
                with self.assertRaises(OSError):
                    self.waka.update_readme(path, PAYLOAD["data"])
            self.assertEqual(path.read_bytes(), OLD_README.encode())
            self.assertEqual(list(Path(directory).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
