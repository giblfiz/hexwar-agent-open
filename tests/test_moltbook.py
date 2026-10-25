import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
import urllib.parse

from moltbook import MoltbookClient, MoltbookError, NoRedirect, ORIGIN, venue_record


POST = "test-post"
SECRET = "moltbook_secret-never-print"


class Response(io.BytesIO):
    def __init__(self, data, url, status=200, headers=None):
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        super().__init__(raw)
        self.status, self.headers, self.url = status, headers or {}, url

    def geturl(self):
        return self.url


class FakeOpener:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def open(self, request, timeout):
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(request)
        return Response(item, request.full_url)


def comment(cid, second, **extra):
    return dict(id=cid, post_id=POST, author_id="agent-1", content='{"signed":"data"}',
                created_at=f"2026-10-25T12:00:{second:02d}.000Z", updated_at=None,
                **extra)


def page(comments, more=False, cursor=None):
    value = {"success": True, "post_id": POST, "comments": comments, "has_more": more}
    if cursor is not None:
        value["next_cursor"] = cursor
    return value


class MoltbookTests(unittest.TestCase):
    def test_pagination_oldest_retains_pending_and_raw_replies(self):
        first = comment("a", 1, verification_status="pending", replies=[{"content": "untrusted prose"}])
        opener = FakeOpener(page([first], True, "cursor / two"), page([comment("b", 2)]))
        client = MoltbookClient(SECRET, opener=opener)
        records = client.fetch_comments(POST)
        self.assertEqual([r["id"] for r in records], ["a", "b"])
        self.assertEqual(records[0]["verification_status"], "pending")
        self.assertEqual(venue_record(records[0])["raw"], first)
        for request in opener.requests:
            self.assertNotIn("Authorization", request.headers)
            self.assertIn("sort=old", request.full_url)
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(opener.requests[1].full_url).query)["cursor"], ["cursor / two"])

    def test_incomplete_or_duplicate_pagination_never_silently_succeeds(self):
        scenarios = [
            [page([comment("a", 1)], True)],
            [page([comment("a", 1)], True, "x"), page([comment("b", 2)], True, "x")],
            [page([comment("a", 1)], True, "x"), page([comment("a", 1)])],
            [{"comments": [], "post_id": POST}],
        ]
        for responses in scenarios:
            with self.subTest(responses=responses), self.assertRaises(MoltbookError):
                MoltbookClient(opener=FakeOpener(*responses)).fetch_comments(POST)
        with self.assertRaises(MoltbookError):
            MoltbookClient(opener=FakeOpener(page([comment("a", 1)], True, "x"))).fetch_comments(POST, max_pages=1)
        with self.assertRaises(MoltbookError):
            MoltbookClient(opener=FakeOpener(page([comment("a", 1), comment("b", 2)]))).fetch_comments(POST, max_comments=1)

    def test_failed_second_page_is_error_not_partial_result(self):
        error = urllib.error.URLError("network died")
        client = MoltbookClient(opener=FakeOpener(page([comment("a", 1)], True, "x"), error))
        with self.assertRaises(MoltbookError):
            client.fetch_comments(POST)

    def test_paths_and_identifiers_cannot_send_key_to_another_host(self):
        opener = FakeOpener()
        client = MoltbookClient(SECRET, opener=opener)
        for path in ["https://evil.invalid/api/v1/x", "//evil.invalid/api/v1/x", "/elsewhere", "/api/v1/../x", "/api/v1/a#b", "/api/v1/a\\b"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                client.request_json("GET", path, authenticated=True)
        for cid in ["../x", "id?query=1", "a/b", "x\nHeader: evil"]:
            with self.assertRaises(ValueError):
                client.create_comment(cid, "hello")
        self.assertFalse(opener.requests)

    def test_redirect_is_refused_before_follow_and_final_url_checked(self):
        from urllib.request import Request
        with self.assertRaises(urllib.error.HTTPError):
            NoRedirect().redirect_request(Request(ORIGIN + "/api/v1/x"), io.BytesIO(b""),
                                          302, "Found", {}, "https://evil.invalid")
        opener = FakeOpener(lambda req: Response({"success": True}, "https://evil.invalid"))
        with self.assertRaises(MoltbookError):
            MoltbookClient(SECRET, opener=opener).account_status()

    def test_http_cooldown_is_structured_redacted_and_not_retried(self):
        body = json.dumps({"message": "bad " + SECRET, "retry_after_seconds": 20,
                           "daily_remaining": 0}).encode()
        error = urllib.error.HTTPError(ORIGIN + "/api/v1/x", 429, "too many", {"Retry-After": "20"}, io.BytesIO(body))
        opener = FakeOpener(error)
        client = MoltbookClient(SECRET, opener=opener)
        with self.assertRaises(MoltbookError) as caught:
            client.create_comment(POST, "test")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(caught.exception.details["retry_after_seconds"], 20)
        self.assertNotIn(SECRET, str(caught.exception) + repr(caught.exception.details))
        self.assertEqual(len(opener.requests), 1)

    def test_success_false_network_and_nonjson_errors_do_not_leak_key(self):
        variants = [
            {"success": False, "error": SECRET},
            urllib.error.URLError(SECRET),
            b"not JSON " + SECRET.encode(),
        ]
        for value in variants:
            with self.subTest(value=type(value)), self.assertRaises(MoltbookError) as caught:
                MoltbookClient(SECRET, opener=FakeOpener(value)).account_status()
            self.assertNotIn(SECRET, str(caught.exception) + repr(caught.exception.details))

    def test_bounded_response_and_duplicate_json(self):
        for raw in [b"x" * 65, b'{"success":true,"success":false}', b'{"x":NaN}']:
            with self.assertRaises(MoltbookError):
                MoltbookClient(opener=FakeOpener(raw), max_response_bytes=64).request_json("GET", "/api/v1/x")

    def test_write_and_challenge_helpers_return_structured_data(self):
        challenge = {"success": True, "verification_required": True,
                     "post": {"verification": {"challenge_text": "2 plus 2", "verification_code": "code"}}}
        opener = FakeOpener(challenge, {"success": True}, {"status": "claimed"})
        client = MoltbookClient(SECRET, opener=opener)
        self.assertEqual(client.create_post("hexwar-agent-open", "Test", "payload"), challenge)
        self.assertTrue(client.verify_challenge("code", "4.00")["success"])
        self.assertEqual(client.account_status()["status"], "claimed")
        self.assertTrue(all(r.full_url.startswith(ORIGIN + "/api/v1/") for r in opener.requests))
        self.assertTrue(all(r.headers["Authorization"] == "Bearer " + SECRET for r in opener.requests))
        with self.assertRaises(MoltbookError):
            MoltbookClient(opener=FakeOpener()).create_comment(POST, "test")

    def test_post_and_comment_schema_fail_closed(self):
        bad = comment("a", 1)
        bad["created_at"] = "2026-10-25T12:00:00"
        for record in [bad, comment("a", 1, parent_id="parent"), comment("a", 1, depth=1)]:
            with self.assertRaises(MoltbookError):
                MoltbookClient(opener=FakeOpener(page([record]))).fetch_comments(POST)
        with self.assertRaises(MoltbookError):
            MoltbookClient(opener=FakeOpener({"post": {"id": "wrong"}})).fetch_post(POST)

    def test_credentials_outside_repo_and_no_invalid_secret_in_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            path.write_text(json.dumps({"api_key": SECRET}))
            client = MoltbookClient.from_credentials(path, opener=FakeOpener({"status": "claimed"}))
            self.assertEqual(client.account_status()["status"], "claimed")
            path.write_text(json.dumps({"api_key": SECRET + "\n"}))
            with self.assertRaises(MoltbookError) as caught:
                MoltbookClient.from_credentials(path)
            self.assertNotIn(SECRET, str(caught.exception))
        with self.assertRaises(ValueError):
            MoltbookClient.from_credentials(Path(__file__).resolve().parent.parent / "secret.json")

    def test_official_registration_credentials_nested_under_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registration.json"
            path.write_text(json.dumps({"success": True, "agent": {"api_key": SECRET,
                                                                     "name": "test-agent"}}))
            opener = FakeOpener({"status": "claimed"})
            client = MoltbookClient.from_credentials(path, opener=opener)
            self.assertEqual(client.account_status()["status"], "claimed")
            self.assertEqual(opener.requests[0].headers["Authorization"], "Bearer " + SECRET)
            path.write_text(json.dumps({"api_key": "different", "agent": {"api_key": SECRET}}))
            with self.assertRaises(MoltbookError) as caught:
                MoltbookClient.from_credentials(path)
            self.assertNotIn(SECRET, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
