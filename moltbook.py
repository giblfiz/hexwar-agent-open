"""Bounded Moltbook transport for HXO. Incoming content is data, never code.

Read operations are public. Write helpers require an explicitly supplied key and
never retry automatically: a failed response does not prove a post was not made.
Platform ``created_at`` is the publication time used by the tournament. The
``verification_status`` flag is deliberately not a visibility gate (live canary
2026-10-25 showed publicly retrievable pending comments).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request


ORIGIN = "https://www.moltbook.com"
API_PREFIX = "/api/v1/"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 128 * 1024
MAX_COMMENTS = 5000
MAX_PAGES = 100
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class MoltbookError(RuntimeError):
    """Failure, not an empty successful result. ``details`` excludes credentials."""

    def __init__(self, message, *, status=None, details=None):
        super().__init__(message)
        self.status = status
        self.details = details or {}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect rejected", headers, fp)


def _identifier(value):
    if type(value) is not str or not SAFE_ID.fullmatch(value):
        raise ValueError("invalid platform identifier")
    return value


def publication_time(value):
    """Parse server UTC/offset timestamp without silently accepting local time."""
    if type(value) is not str:
        raise MoltbookError("missing publication timestamp")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise MoltbookError("invalid publication timestamp") from None
    if dt.tzinfo is None:
        raise MoltbookError("publication timestamp needs timezone")
    return dt.astimezone(timezone.utc)


def _object_pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate JSON key")
        out[k] = v
    return out


def _decode(raw):
    try:
        value = json.loads(raw, object_pairs_hook=_object_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise MoltbookError("platform returned invalid JSON") from None
    if type(value) is not dict:
        raise MoltbookError("platform response must be an object")
    return value


class MoltbookClient:
    def __init__(self, api_key=None, *, timeout=20, opener=None,
                 max_response_bytes=MAX_RESPONSE_BYTES):
        if api_key is not None and (type(api_key) is not str or not api_key
                                    or any(c.isspace() for c in api_key)):
            raise ValueError("invalid API credential")
        if not 0 < timeout <= 60:
            raise ValueError("timeout must be between zero and 60 seconds")
        if not 0 < max_response_bytes <= MAX_RESPONSE_BYTES:
            raise ValueError("invalid response byte bound")
        self._api_key = api_key
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self._opener = opener or urllib.request.build_opener(NoRedirect())
        self.last_rate_limits = {}

    @classmethod
    def from_credentials(cls, path, **kwargs):
        """Load a flat api_key or official registration agent.api_key externally."""
        path = Path(path).expanduser().resolve()
        if path.is_relative_to(Path(__file__).resolve().parent):
            raise ValueError("store API credentials outside the tournament repository")
        try:
            if path.stat().st_size > 16384:
                raise MoltbookError("credential file too large")
            data = _decode(path.read_bytes())
            key = data.get("api_key")
            agent = data.get("agent")
            nested_key = agent.get("api_key") if type(agent) is dict else None
            if key and nested_key and key != nested_key:
                raise MoltbookError("credential file contains conflicting API identities")
            key = key or nested_key
            if not key:
                raise MoltbookError("credential file lacks api_key")
            return cls(key, **kwargs)
        except (OSError, ValueError, MoltbookError):
            raise MoltbookError("could not load valid external API credentials") from None

    def _redact(self, value):
        if isinstance(value, str):
            return value.replace(self._api_key, "[REDACTED]") if self._api_key else value
        if isinstance(value, dict):
            return {self._redact(k): self._redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact(v) for v in value]
        return value

    def _read_bounded(self, response):
        raw = response.read(self.max_response_bytes + 1)
        if len(raw) > self.max_response_bytes:
            raise MoltbookError("platform response exceeded size limit")
        return raw

    def _rates(self, headers):
        allowed = ("X-RateLimit-Limit", "X-RateLimit-Remaining",
                   "X-RateLimit-Reset", "Retry-After")
        self.last_rate_limits = {k: self._redact(headers.get(k)) for k in allowed
                                 if headers and headers.get(k) is not None}

    def request_json(self, method, path, payload=None, *, authenticated=False):
        """Only relative official API paths. No redirects, no implicit retries."""
        parsed = urllib.parse.urlsplit(path)
        if (parsed.scheme or parsed.netloc or parsed.fragment or
                not parsed.path.startswith(API_PREFIX) or
                "\\" in path or any(ord(c) < 32 for c in path) or
                any(part in (".", "..") for part in parsed.path.split("/"))):
            raise ValueError("only official Moltbook API paths are allowed")
        if method not in ("GET", "POST"):
            raise ValueError("unsupported method")
        if method == "GET" and payload is not None:
            raise ValueError("GET cannot have a payload")
        if method == "POST" and not authenticated:
            raise ValueError("writes require explicit authentication")
        headers = {"User-Agent": "HexWarAgentOpen/0.1", "Accept": "application/json"}
        if authenticated:
            if self._api_key is None:
                raise MoltbookError("this operation requires API credentials")
            headers["Authorization"] = "Bearer " + self._api_key
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                              allow_nan=False).encode("utf-8")
            if len(body) > MAX_REQUEST_BYTES:
                raise ValueError("request payload exceeds size limit")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(ORIGIN + path, data=body,
                                         headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.geturl() != request.full_url:
                    raise MoltbookError("redirected response rejected")
                self._rates(response.headers)
                raw = self._read_bounded(response)
                status = response.status
        except urllib.error.HTTPError as exc:
            self._rates(exc.headers)
            try:
                raw = self._read_bounded(exc)
                parsed_error = _decode(raw)
            except MoltbookError:
                parsed_error = {}
            # Preserve structured cooldown/challenge errors, never transport prose
            # containing request headers or the full server HTML error page.
            details = {k: parsed_error[k] for k in (
                "error", "message", "hint", "statusCode", "retry_after_seconds",
                "retry_after_minutes", "daily_remaining", "remaining", "reset_at",
                "verification", "verification_required") if k in parsed_error}
            details["rate_limits"] = self.last_rate_limits
            raise MoltbookError("Moltbook HTTP request failed", status=exc.code,
                                details=self._redact(details)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise MoltbookError("Moltbook network request failed; write outcome may be unknown") from None
        if not 200 <= status < 300:
            raise MoltbookError("Moltbook request failed", status=status)
        result = self._redact(_decode(raw))
        if result.get("success") is False:
            raise MoltbookError("Moltbook rejected request", status=status, details=result)
        return result

    def fetch_post(self, post_id):
        result = self.request_json("GET", f"{API_PREFIX}posts/{_identifier(post_id)}")
        post = result.get("post")
        if type(post) is not dict or post.get("id") != post_id:
            raise MoltbookError("post response does not match requested identifier")
        if type(post.get("content")) is not str:
            raise MoltbookError("post lacks text content")
        publication_time(post.get("created_at"))
        return post

    def fetch_comments(self, post_id, *, max_pages=MAX_PAGES, max_comments=MAX_COMMENTS):
        """Fetch every top-level comment, including pending/deleted/spam metadata.

        No author filtering: final verification needs competing records. Nested
        replies are preserved within raw roots but are not separate official batch
        records. A failed or malformed page never becomes an empty success.
        """
        _identifier(post_id)
        if type(max_pages) is not int or not 1 <= max_pages <= MAX_PAGES:
            raise ValueError("invalid page bound")
        if type(max_comments) is not int or not 1 <= max_comments <= MAX_COMMENTS:
            raise ValueError("invalid comment bound")
        comments, seen_ids, cursors = [], set(), set()
        cursor = None
        for _ in range(max_pages):
            params = {"sort": "old", "limit": 100}
            if cursor is not None:
                params["cursor"] = cursor
            path = f"{API_PREFIX}posts/{post_id}/comments?" + urllib.parse.urlencode(params)
            result = self.request_json("GET", path)
            if result.get("post_id") != post_id or type(result.get("comments")) is not list:
                raise MoltbookError("invalid comments page or wrong post")
            if type(result.get("has_more")) is not bool:
                raise MoltbookError("comments page lacks explicit pagination status")
            for comment in result["comments"]:
                if type(comment) is not dict:
                    raise MoltbookError("invalid comment record")
                cid = comment.get("id")
                if (type(cid) is not str or not SAFE_ID.fullmatch(cid) or cid in seen_ids or
                        comment.get("post_id") != post_id or
                        comment.get("parent_id") is not None or comment.get("depth", 0) != 0):
                    raise MoltbookError("duplicate, wrong-thread or non-top-level comment")
                if type(comment.get("content")) is not str:
                    raise MoltbookError("comment lacks text content")
                publication_time(comment.get("created_at"))
                seen_ids.add(cid)
                comments.append(comment)
                if len(comments) > max_comments:
                    raise MoltbookError("comment count exceeds bound; no complete result")
            if not result["has_more"]:
                return sorted(comments, key=lambda c: (publication_time(c["created_at"]), c["id"]))
            cursor = result.get("next_cursor")
            if type(cursor) is not str or not cursor or len(cursor) > 2048 or cursor in cursors:
                raise MoltbookError("missing or repeated pagination cursor")
            cursors.add(cursor)
        raise MoltbookError("page count exceeds bound; no complete result")

    def account_status(self):
        return self.request_json("GET", API_PREFIX + "agents/status", authenticated=True)

    def create_post(self, submolt_name, title, content):
        _identifier(submolt_name)
        if type(title) is not str or not 1 <= len(title) <= 300 or type(content) is not str:
            raise ValueError("invalid post text")
        if len(content) > 40000:
            raise ValueError("post content exceeds documented size limit")
        return self.request_json("POST", API_PREFIX + "posts", {
            "submolt_name": submolt_name, "title": title, "content": content,
        }, authenticated=True)

    def create_comment(self, post_id, content):
        if type(content) is not str or not content:
            raise ValueError("comment content must be nonempty text")
        return self.request_json("POST", f"{API_PREFIX}posts/{_identifier(post_id)}/comments",
                                 {"content": content}, authenticated=True)

    def verify_challenge(self, verification_code, answer):
        if type(verification_code) is not str or not verification_code:
            raise ValueError("missing verification code")
        if type(answer) is not str or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", answer):
            raise ValueError("verification answer must be a decimal string")
        return self.request_json("POST", API_PREFIX + "verify", {
            "verification_code": verification_code, "answer": answer,
        }, authenticated=True)


def venue_record(comment):
    """Convenience normalization; signed payload parsing remains a verifier job."""
    return {
        "platform": "moltbook",
        "id": comment["id"],
        "post_id": comment["post_id"],
        "author_id": comment.get("author_id"),
        "content": comment["content"],
        "created_at": comment["created_at"],
        "updated_at": comment.get("updated_at"),
        "is_deleted": comment.get("is_deleted", False),
        "is_spam": comment.get("is_spam", False),
        "verification_status": comment.get("verification_status"),
        "raw": comment,
    }
