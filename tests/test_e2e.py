"""End-to-end tests: a fake Studio upstream, the real gateway, real HTTP/SSE clients.

The fake upstream implements just enough of the recovered protocol (see CLAUDE.md) to
exercise routing, streaming, session reuse and account handling without touching StepFun.
"""

import base64
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

# The fake upstream serves the gateway media over 127.0.0.1; the SSRF guard must not
# fire for that (a real deployment fetches from StepFun's public storage).
os.environ.setdefault("STEP_ALLOW_PRIVATE_FETCH", "1")
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Fake upstream
# ---------------------------------------------------------------------------

STATE = {"contents": [], "conversations": 0, "runs": 0, "refreshes": 0,
         "conv_owner": {}, "accounts_seen": [], "upstream_503": 0, "last_web_search": None,
         "last_config": {}, "last_system": "", "catalog": 0}

# oasis_ids the fake upstream answers 503 for, so failover tests have a broken account.
BAD_ACCOUNTS = set()
# oasis_ids whose refresh token is permanently rejected (401), like a revoked session.
DEAD_ACCOUNTS = set()
# oasis_ids that refresh fine but are guest sessions: the real upstream mints those from
# any forged cookie, and every authenticated call then fails "not a logined oasis account".
GUEST_ACCOUNTS = set()
# oasis_ids whose QueryStudioRateLimit blips with a 5xx (refresh still succeeds).
RATE_LIMIT_503 = set()
# oasis_ids whose RefreshToken answers 429 (rate limited, not a bad credential).
REFRESH_429 = set()
# oasis_id -> remaining RPC calls answered "unauthenticated" (an access token that
# expired early). The next call with a fresh token must succeed.
AUTH_BLIPS = {}
# oasis_id -> remaining chat streams to cut mid-answer. One shot lets a retry succeed
# against the same account, which is what the non-stream replay path needs to be tested.
CUT_ONCE = {}
# Number of upcoming *valid* audio requests to deny (entitlement style). One shot lets a
# second account serve the same request, proving the pool fails over on a 400 denial.
AUDIO_DENY_ONCE = [0]
# Chat contents whose *first* stream is cut. Keyed by content (not account) so two
# concurrent requests can each be cut exactly once before their own replay.
CUT_CONTENT = set()
# Refresh tokens already spent. The real upstream invalidates them on rotation, and a
# gateway that keeps presenting the same one silently loses the session.
CONSUMED_REFRESH = set()
CONSUMED_LOCK = threading.Lock()


def fake_jwt(payload):
    def enc(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return "%s.%s.%s" % (enc({"alg": "HS256", "typ": "JWT"}), enc(payload), "sig")


_TOKEN_SEQ = [0]


def make_token(oasis_id="42", device="dev-1", ttl=3600, nonce=None):
    """A JWT pair. Each call gets a unique nonce, the way every real refresh does (the
    upstream rotates on every call), so two tokens are never byte-identical."""
    if nonce is None:
        _TOKEN_SEQ[0] += 1
        nonce = _TOKEN_SEQ[0]
    access_claims = {"oasis_id": int(oasis_id), "exp": int(time.time()) + ttl,
                     "activated": True, "mode": 2}
    refresh_claims = {"oasis_id": int(oasis_id), "device_id": device,
                      "exp": int(time.time()) + 30 * 86400}
    if nonce is not None:
        access_claims["nonce"] = refresh_claims["nonce"] = nonce
    return "%s...%s" % (fake_jwt(access_claims), fake_jwt(refresh_claims))


def sse(handler, events):
    # Pre-render and advertise a length: closing a socket with unread bytes triggers RST
    # on Windows, which would make these tests flaky for reasons unrelated to the gateway.
    body = "".join("event: message\ndata: %s\n\n" % json.dumps(event) for event in events).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()


def sse_drip(handler, count=200, delay=0.03):
    """Chunked SSE, one event at a time, long enough for a dead client to be noticed."""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()

    def chunk(event):
        data = ("event: message" + chr(10) + "data: " + json.dumps(event)
                + chr(10) + chr(10)).encode()
        handler.wfile.write(b"%X" % len(data) + bytes([13, 10]) + data + bytes([13, 10]))
        handler.wfile.flush()

    try:
        chunk({"type": "message_start", "data": {"message_id": "m1"}})
        chunk({"type": "content_block_start",
               "data": {"block_index": 0, "block_type": "CONTENT_BLOCK_TYPE_TEXT"}})
        for _ in range(count):
            time.sleep(delay)
            chunk({"type": "text_delta", "data": {"block_index": 0, "delta": "x"}})
        chunk({"type": "done", "data": {}})
    except OSError:
        # The gateway hung up because the client left — exactly what the test wants.
        return


def sse_gapped(handler, events, gap=0.6):
    """Chunked SSE with a pause after the first event: a model that goes quiet."""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()

    def chunk(payload):
        data = payload.encode()
        handler.wfile.write(b"%X" % len(data) + bytes([13, 10]) + data + bytes([13, 10]))
        handler.wfile.flush()

    for index, event in enumerate(events):
        chunk("event: message" + chr(10) + "data: %s" % json.dumps(event) + chr(10) + chr(10))
        if index == 1:
            time.sleep(gap)
    chunk("")


def sse_cut(handler, events, keep=2):
    """Stream a few events, then drop the connection without the terminating chunk."""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()
    for event in events[:keep]:
        data = ("event: message" + chr(10) + "data: %s" % json.dumps(event) + chr(10) + chr(10)).encode()
        handler.wfile.write(b"%X" % len(data) + bytes([13, 10]) + data + bytes([13, 10]))
        handler.wfile.flush()
    handler.close_connection = True


class FakeStudio(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _identity(self):
        """oasis_id from the Oasis-Token cookie, like the real server's auth layer."""
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name.lower() != "oasis-token":
                continue
            access = value.split("...")[0]
            chunks = access.split(".")
            if len(chunks) < 2:
                return ""
            pad = "=" * (-len(chunks[1]) % 4)
            try:
                payload = json.loads(base64.urlsafe_b64decode(chunks[1] + pad))
            except (ValueError, UnicodeDecodeError):
                return ""
            return str(payload.get("oasis_id") or "")
        return ""

    def _refresh_half(self):
        """The refresh JWT from the Oasis-Token cookie, verbatim."""
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name.lower() == "oasis-token":
                return value.split("...")[-1]
        return ""

    def _refresh_device(self):
        """device_id from the refresh half of the Oasis-Token cookie, if any."""
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name.lower() != "oasis-token":
                continue
            chunks = value.split("...")[-1].split(".")
            if len(chunks) < 2:
                return ""
            pad = "=" * (-len(chunks[1]) % 4)
            try:
                payload = json.loads(base64.urlsafe_b64decode(chunks[1] + pad))
            except (ValueError, UnicodeDecodeError):
                return ""
            return str(payload.get("device_id") or "")
        return ""

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = self.path
        if self._identity() in BAD_ACCOUNTS:
            self._body()                      # keep the connection framed
            STATE["upstream_503"] += 1
            return self._json({"code": "unavailable", "message": "fake upstream outage"}, status=503)
        if path.endswith("/studio/file"):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            STATE["uploads"] = STATE.get("uploads", 0) + 1
            STATE["last_upload"] = raw[:80]
            return self._json({"code": 0, "biz_code": "OK", "message": "success",
                               "data": {"asset": {"asset_id": "asset-1"}}})
        body = self._body()
        if self._identity() in DEAD_ACCOUNTS:
            return self._json({"code": "unauthenticated", "message": "session revoked"}, status=401)
        if "RefreshToken" in path:
            STATE["refreshes"] += 1
            if not self._identity():
                # Real Studio 401s when the cookie carries no decodable JWT pair
                # (a mistyped paste) or when the session was revoked.
                return self._json({"code": "unauthenticated", "message": "bad cookie"}, status=401)
            # Echo the caller's identity: a refresh must never hand back a session
            # belonging to a different account.
            if self._identity() in REFRESH_429:
                return self._json({"code": "rate_limited", "message": "slow down"}, status=429)
            presented = self._refresh_half()
            with CONSUMED_LOCK:
                spent = presented in CONSUMED_REFRESH
                CONSUMED_REFRESH.add(presented)
            if spent:
                return self._json({"code": "unauthenticated",
                                   "message": "refresh token already used"}, status=401)
            ident = self._identity() or "42"
            device = self._refresh_device() or "dev-1"
            # make_token's own counter guarantees a token nobody has presented before,
            # so the single-use check below can never mistake a fresh token for a spent one.
            fresh = make_token(oasis_id=ident, device=device)
            return self._json({"accessToken": {"raw": fresh.split("...")[0]},
                               "refreshToken": {"raw": fresh.split("...")[1]}})
        blip = AUTH_BLIPS.get(self._identity(), 0)
        if blip > 0:
            AUTH_BLIPS[self._identity()] = blip - 1
            return self._json({"code": "unauthenticated",
                               "message": "access token expired"}, status=401)
        if path.endswith("/CreateConversation"):
            STATE["conversations"] += 1
            conv_id = "conv-%d" % STATE["conversations"]
            STATE["conv_owner"][conv_id] = self._identity()
            return self._json({"conversation": {"conversation_id": conv_id,
                                                "workspace_id": str(body.get("workspaceId") or "0")}})
        if path.endswith("/GetStudioPlaygroundConfig"):
            STATE["catalog"] += 1
            return self._json({"chat": {"default_model": "step-test", "models": [
                {"model": "step-test", "display_name": "Step Test",
                 "config": [{"key": "max_tokens", "max": 4096, "min": 1, "default_value": 1024},
                            {"key": "temperature", "min": 0, "max": 1, "default_value": 1}],
                 "thinking": {"default_enabled": True, "default_effort": 2, "effort_options": [1, 2, 3]},
                 "model_type": 0, "is_multimodal": True}]},
                "tts": {"default_model": "stepaudio-2.5-tts", "formats": ["mp3", "wav", "opus"],
                        "voices": [{"voice": "xiaoyue", "display_name": {"zh_cn": "xiaoyue"}},
                                   {"voice": "step_f06", "display_name": {"zh_cn": "step_f06"}}],
                        "models": [{"model": "stepaudio-2.5-tts"}]},
                "asr": {"default_model": "stepaudio-2.5-asr",
                        "models": [{"model": "stepaudio-2.5-asr"}]}})
        if path.endswith("/QueryStudioRateLimit"):
            if self._identity() in RATE_LIMIT_503:
                return self._json({"code": "unavailable", "message": "rate limit backend down"},
                                  status=503)
            if self._identity() in GUEST_ACCOUNTS:
                return self._json({"code": "unauthenticated",
                                   "message": "auth failed: not a logined oasis account"}, status=401)
            return self._json({"free_rate": 1.0})
        if path.endswith("/CreateStudioWorkspace"):
            return self._json({"workspace": {"workspace_id": "ws-1"}})
        if path.endswith("/StartStudioWorkspaceRun"):
            STATE["runs"] += 1
            STATE["contents"].append(body.get("text", ""))
            return self._json({"workspace_id": "ws-1", "conversation_id": body.get("conversationId"),
                               "user_message_id": "u1", "assistant_message_id": "a1"})
        if path.endswith("/studio/stream"):
            if body.get("kind") in ("audio_asr", "audio_tts"):
                # The real Studio answers an unknown audio model with 400; keeping that
                # strictness here makes the OpenAI-alias mapping load-bearing.
                section = body.get(body["kind"]) or {}
                want = "stepaudio-2.5-asr" if body["kind"] == "audio_asr" else "stepaudio-2.5-tts"
                if str(((section.get("model") or {}).get("model_id")) or "") != want:
                    return self._json({"code": "MODEL_ACCESS_DENIED",
                                       "message": "model access denied"}, status=400)
                if AUDIO_DENY_ONCE[0] > 0:
                    AUDIO_DENY_ONCE[0] -= 1
                    return self._json({"code": "MODEL_ACCESS_DENIED",
                                       "message": "model access denied"}, status=400)
            if body.get("kind") == "audio_asr":
                return sse(self, [
                    {"type": "message_start", "data": {"message_id": "asr-1"}},
                    {"type": "content_block_start", "data": {"block_index": 0,
                                                             "block_type": "CONTENT_BLOCK_TYPE_TEXT"}},
                    {"type": "text_delta", "data": {"block_index": 0, "delta": "hello "}},
                    {"type": "text_delta", "data": {"block_index": 0, "delta": "world"}},
                    {"type": "final", "data": {"usage": {"tokens_in": "0", "tokens_out": "0"}}},
                    {"type": "done", "data": {"final_status": "MESSAGE_STATUS_SUCCESS"}},
                ])
            if body.get("kind") == "audio_tts":
                return sse(self, [
                    {"type": "message_start", "data": {"message_id": "tts-1"}},
                    {"type": "content_block_start", "data": {"block_index": 0,
                                                             "block_type": "CONTENT_BLOCK_TYPE_ASSETS"}},
                    {"type": "media_asset_ready", "data": {"asset_id": "a1",
                                                           "asset_kind": "ASSET_KIND_AUDIO_TTS",
                                                           "url": "%s/asset.mp3" % _fake_url}},
                    {"type": "final", "data": {"usage": {"tokens_in": "0", "tokens_out": "0"}}},
                    {"type": "done", "data": {"final_status": "MESSAGE_STATUS_SUCCESS"}},
                ])
            if "BIG" in str(body["chat"].get("content") or ""):
                # large body so a client that stops reading forces a write error
                events = [{"type": "message_start", "data": {"message_id": "big-1"}},
                          {"type": "content_block_start", "data": {"block_index": 0,
                                                                   "block_type": "CONTENT_BLOCK_TYPE_TEXT"}}]
                events += [{"type": "text_delta", "data": {"block_index": 0, "delta": "x" * 64}}
                           for _ in range(20000)]
                events += [{"type": "message_stop", "data": {"status": "MESSAGE_STATUS_SUCCESS"}},
                           {"type": "final", "data": {"usage": {"tokens_in": "1", "tokens_out": "1"}}},
                           {"type": "done", "data": {"final_status": "MESSAGE_STATUS_SUCCESS"}}]
                return sse(self, events)
            if "MIDERR" in str(body["chat"].get("content") or ""):
                return sse(self, [
                    {"type": "message_start", "data": {"message_id": "mid-1"}},
                    {"type": "content_block_start", "data": {"block_index": 0,
                                                             "block_type": "CONTENT_BLOCK_TYPE_TEXT"}},
                    {"type": "text_delta", "data": {"block_index": 0, "delta": "partial"}},
                    {"type": "error", "data": {"code": "AGENT_ERROR", "biz_code": "BAD_REQUEST",
                                               "message": "model crashed"}},
                    {"type": "done", "data": {"final_status": "MESSAGE_STATUS_FAILED"}},
                ])
            if "CENSOR" in str(body["chat"].get("content") or ""):
                return sse(self, [
                    {"type": "message_start", "data": {"message_id": "blocked"}},
                    {"type": "error", "data": {"code": "AGENT_ERROR", "biz_code": "CENSORED",
                                               "message": "Input text data may contain inappropriate content"}},
                    {"type": "done", "data": {"final_status": "MESSAGE_STATUS_CENSORED"}},
                ])
            conv_id = str(body.get("conversation_id") or "")
            owner = STATE["conv_owner"].get(conv_id)
            if owner is not None and owner != self._identity():
                # mirrors the real server: conversations belong to their creator
                return self._json({"code": 404, "biz_code": "NOT_FOUND",
                                   "message": "conversation not found"})
            if "SLOW" in str(body["chat"].get("content") or ""):
                time.sleep(0.8)          # keep the turn in flight for the overlap test
            STATE["last_web_search"] = bool(body["chat"].get("enable_web_search"))
            STATE["last_config"] = dict((body["chat"].get("model") or {}).get("config") or {})
            STATE["last_system"] = str(body["chat"].get("system_prompt") or "")
            STATE["accounts_seen"].append(self._identity())
            STATE["contents"].append(body["chat"]["content"])
            STATE["last_asset_ids"] = list(body["chat"].get("asset_ids") or [])
            events = self._events(body["chat"]["content"])
            mine = self._identity()
            text = str(body["chat"].get("content") or "")
            for marker in list(CUT_CONTENT):
                if marker in text:          # the gateway sends a rendered transcript
                    CUT_CONTENT.discard(marker)
                    return sse_cut(self, events)
            if CUT_ONCE.get(mine):
                CUT_ONCE[mine] -= 1
                return sse_cut(self, events)
            if "GAP" in str(body["chat"]["content"] or ""):
                return sse_gapped(self, events)
            if "CUT" in str(body["chat"]["content"] or ""):
                return sse_cut(self, events)
            if "EMPTY" in str(body["chat"]["content"] or ""):
                return sse(self, [])          # 200 with zero events
            if "DRIP" in str(body["chat"]["content"] or ""):
                return sse_drip(self)
            if "REJECT" in str(body["chat"]["content"] or ""):
                return sse(self, [
                    {"type": "message_start", "data": {"message_id": "m1"}},
                    {"type": "error", "data": {"code": "INVALID_REQUEST",
                                               "biz_code": "INVALID_REQUEST",
                                               "message": "bad request"}},
                    {"type": "done", "data": {}},
                ])
            return sse(self, events)
        self._json({"code": "not_found", "message": path})

    def do_GET(self):
        if self.path.startswith("/asset.mp3"):
            payload = b"ID3\x03\x00\x00\x00fake-mp3"
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if "/events" in self.path:
            # mirror the last run's prompt so agent-mode tool tests can drive the events
            return sse(self, self._events(STATE["contents"][-1] if STATE["contents"] else "agent"))
        self._json({"code": "not_found", "message": self.path})

    @staticmethod
    def _events(content):
        text = "Hello from fake"
        if "SEQ" in content:
            text = "Hello STOP world"
        if "FENCE" in content:
            text = "```json" + "\n{\"a\": 1}\n```"
        events = [
            # Studio sends these while the model warms up; seq 0 and no content.
            {"type": "heartbeat", "data": {"message_id": "msg-1"}, "seq": 0},
            {"type": "message_start", "data": {"message_id": "msg-1", "model_id": "step-test"}, "seq": 1},
            {"type": "content_block_start", "data": {"message_id": "msg-1", "block_index": 0,
                                                     "block_type": "CONTENT_BLOCK_TYPE_REASONING"}, "seq": 2},
            {"type": "reasoning_delta", "data": {"message_id": "msg-1", "block_index": 0, "delta": "think"}, "seq": 3},
            {"type": "content_block_stop", "data": {"message_id": "msg-1", "block_index": 0}, "seq": 4},
            {"type": "content_block_start", "data": {"message_id": "msg-1", "block_index": 1,
                                                     "block_type": "CONTENT_BLOCK_TYPE_TEXT"}, "seq": 5},
            {"type": "text_delta", "data": {"message_id": "msg-1", "block_index": 1,
                                            "delta": text}, "seq": 6},
            {"type": "heartbeat", "data": {"message_id": "msg-1"}, "seq": 0},
            {"type": "content_block_stop", "data": {"message_id": "msg-1", "block_index": 1}, "seq": 6},
        ]
        if "TOOL" in content:
            events.append({"type": "tool_call_start", "data": {
                "message_id": "msg-1", "block_index": 2, "tool_call_id": "call-1",
                "tool_name": "get_weather", "arguments_json": "{\"city\": \"X\"}"}, "seq": 6})
        if "SEQ2" in content:
            events.append({"type": "text_delta", "data": {
                "message_id": "msg-1", "block_index": 1, "delta": " leaked tail"}, "seq": 6})
        events += [
            {"type": "message_stop", "data": {"message_id": "msg-1", "status": "MESSAGE_STATUS_SUCCESS"}, "seq": 7},
            {"type": "final", "data": {"message_id": "msg-1", "usage": {"tokens_in": "12", "tokens_out": "4",
                                                                         "cached_tokens": "0"}}, "seq": 8},
            {"type": "done", "data": {"message_id": "msg-1", "final_status": "MESSAGE_STATUS_SUCCESS"}, "seq": 9},
        ]
        return events


_fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeStudio)
_fake.daemon_threads = True
_fake_url = "http://127.0.0.1:%d" % _fake.server_address[1]
threading.Thread(target=_fake.serve_forever, daemon=True).start()

os.environ["STEP_BASE_URL"] = _fake_url
os.environ["STEP_SSE_HEARTBEAT"] = "0"

from step2api.config import Config  # noqa: E402
from step2api.server import make_server  # noqa: E402
from step2api.store import Store  # noqa: E402


class UsageRotationTest(unittest.TestCase):
    def test_rotates_when_large(self):
        import os
        import tempfile

        from step2api.store import Store

        tmp = tempfile.mkdtemp(prefix="step2api-usage-")
        store = Store(tmp)
        path = os.path.join(tmp, "usage.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("x" * 100)
        store.log_usage({"a": 1}, path, max_bytes=64)
        self.assertTrue(os.path.exists(path + ".1"))
        self.assertFalse(os.path.exists(path + ".1.1"))
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(len(fh.readlines()), 1)


class DrainTest(unittest.TestCase):
    def test_waits_for_inflight_then_returns(self):
        import threading

        from step2api.cli import drain

        class Gw:
            inflight_now = 1

        gw = Gw()
        threading.Timer(0.3, lambda: setattr(gw, "inflight_now", 0)).start()
        waited = drain(gw, timeout=5.0, poll=0.05)
        self.assertGreater(waited, 0)
        self.assertLess(waited, 5.0)

    def test_gives_up_after_timeout(self):
        from step2api.cli import drain

        class Gw:
            inflight_now = 1

        waited = drain(Gw(), timeout=0.3, poll=0.1)
        self.assertGreaterEqual(waited, 0.3)


class DashboardTest(unittest.TestCase):
    def test_panel_javascript_parses(self):
        """The panel is one big JS string; a stray brace breaks every button silently."""
        import re
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        from step2api.dashboard import DASHBOARD_HTML

        blocks = re.findall(r"<script>(.*?)</script>", DASHBOARD_HTML, re.S)
        self.assertTrue(blocks, "no <script> block in the panel")
        tmp = tempfile.mkdtemp(prefix="step2api-js-")
        path = os.path.join(tmp, "panel.js")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(chr(10).join(blocks))
            proc = subprocess.run([node, "--check", path], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_dashboard_contains_analytics(self):
        from step2api.dashboard import DASHBOARD_HTML

        for needle in ("model-stats", "btn-csv", "免费额度", "btn-refresh", "btn-export",
                       "凭证无效或已停用",   # a parked account must say why on hover
                       "r.finish_reason",    # and the log should show why a turn ended
                       "r.tenant"):          # plus which key label served it
            self.assertIn(needle, DASHBOARD_HTML)


class SlotTest(unittest.TestCase):
    def test_release_is_idempotent(self):
        import threading

        from step2api.server import Slot

        class FakeGateway:
            def __init__(self):
                self.inflight = threading.BoundedSemaphore(1)
                self.inflight_lock = threading.Lock()
                self.inflight_now = 1

        gw = FakeGateway()
        gw.inflight.acquire()
        slot = Slot(gw)
        slot.release()
        slot.release()  # must not inflate the semaphore or raise
        self.assertEqual(gw.inflight_now, 0)
        self.assertTrue(gw.inflight.acquire(blocking=False))
        gw.inflight.release()


class GatewayE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="step2api-test-")
        cls.cfg = Config(data_dir=cls.tmp, port=0, host="127.0.0.1", max_retries=2,
                         max_concurrency=1, sse_heartbeat=0)
        cls.store = Store(cls.tmp)
        cls.account = cls.store.add_account("fake", make_token(), "dev-1")
        cls.httpd = make_server(cls.cfg, cls.store)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def post(self, path, body, stream=False):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        resp = urllib.request.urlopen(req, timeout=30)
        if not stream:
            return json.loads(resp.read())
        raw = resp.read().decode()
        frames = []
        for chunk in raw.split("\n\n"):
            for line in chunk.split("\n"):
                # Anthropic/Responses frames carry `event: ...` before the `data:` line.
                if line.startswith("data: ") and line[6:] != "[DONE]":
                    frames.append(json.loads(line[6:]))
        return frames, raw

    def test_server_header_does_not_leak_the_python_version(self):
        with urllib.request.urlopen(self.base + "/health", timeout=10) as resp:
            server = resp.headers.get("Server") or ""
        self.assertIn("step2api/", server)
        self.assertNotIn("Python", server)

    def test_responses_carry_a_request_id(self):
        """The same id on the wire and in usage.jsonl is what makes a report traceable."""
        with urllib.request.urlopen(self.base + "/health", timeout=10) as resp:
            health_id = resp.headers.get("X-Request-Id") or ""
        self.assertRegex(health_id, r"^req_[0-9a-f]{16}$")

        req = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=json.dumps({"model": "step-test",
                             "messages": [{"role": "user", "content": "request id probe"}]}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            chat_id = resp.headers.get("X-Request-Id") or ""
            resp.read()
        self.assertRegex(chat_id, r"^req_[0-9a-f]{16}$")
        self.assertNotEqual(health_id, chat_id, "ids must be unique per request")
        # The row is written in the handler's `finally`, just after the body goes out.
        deadline = time.time() + 5
        rows = []
        while time.time() < deadline:
            rows = self.store.read_usage(self.cfg.usage_file, 1000)
            if rows and rows[-1].get("request_id") == chat_id:
                break
            time.sleep(0.02)
        self.assertTrue(rows and rows[-1].get("request_id") == chat_id, rows[-1:])

        sreq = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=json.dumps({"model": "step-test", "stream": True,
                             "messages": [{"role": "user", "content": "stream id probe"}]}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(sreq, timeout=30) as resp:
            self.assertRegex(resp.headers.get("X-Request-Id") or "",
                             r"^req_[0-9a-f]{16}$")
            resp.read()

        # Audio logs through `log_media`, not `_record`: it must carry the id too.
        areq = urllib.request.Request(
            self.base + "/v1/audio/speech",
            data=json.dumps({"input": "id probe", "voice": "nova"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(areq, timeout=30) as resp:
            audio_id = resp.headers.get("X-Request-Id") or ""
            resp.read()
        self.assertRegex(audio_id, r"^req_[0-9a-f]{16}$")
        deadline = time.time() + 5
        found = False
        while time.time() < deadline:
            rows = self.store.read_usage(self.cfg.usage_file, 1000)
            if any(r.get("request_id") == audio_id and r.get("mode") == "tts" for r in rows):
                found = True
                break
            time.sleep(0.02)
        self.assertTrue(found, "the TTS usage row lost its request id")

    def test_health(self):
        with urllib.request.urlopen(self.base + "/health", timeout=10) as resp:
            payload = json.loads(resp.read())
        self.assertEqual(payload["status"], "ok")
        self.assertIn("sessions", payload)
        self.assertIn("rss_mb", payload)
        if payload["rss_mb"] is not None:
            self.assertGreater(payload["rss_mb"], 0)

    def test_metrics_are_prometheus_readable_and_keyed(self):
        """`/metrics` must be scrape-ready: bare numbers, declared types, no upstream I/O."""
        with urllib.request.urlopen(self.base + "/metrics", timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            ctype = resp.headers.get("Content-Type") or ""
            self.assertIn("text/plain", ctype)
            self.assertIn("version=0.0.4", ctype)
            body = resp.read().decode("utf-8")

        samples, types = {}, set()
        for line in body.splitlines():
            if not line.strip():
                continue
            if line.startswith("# TYPE "):
                types.add(line.split()[3])
                continue
            if line.startswith("#"):
                continue
            name, _, value = line.partition(" ")
            self.assertNotEqual(value, "", "sample %r has no value" % name)
            samples[name] = float(value)   # every sample must be a bare number

        for required in ("step2api_up", "step2api_uptime_seconds",
                         "step2api_inflight_requests", "step2api_sessions",
                         "step2api_accounts_total", "step2api_accounts_available",
                         "step2api_requests_total", "step2api_tokens_in_total",
                         "step2api_tokens_out_total", "step2api_cached_tokens_total"):
            self.assertIn(required, samples)
        self.assertEqual(samples["step2api_up"], 1)
        self.assertGreaterEqual(samples["step2api_accounts_total"], 1)
        self.assertIn("gauge", types)
        self.assertIn("counter", types)

        # Once any key exists the endpoint is authenticated like the rest of the API.
        entry = self.store.add_api_key("metrics")
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(self.base + "/metrics", timeout=10)
            self.assertEqual(ctx.exception.code, 401)
            ctx.exception.close()
            req = urllib.request.Request(self.base + "/metrics",
                                         headers={"Authorization": "Bearer " + entry["key"]})
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("step2api_up 1", resp.read().decode("utf-8"))
        finally:
            self.store.remove_api_key(entry["key"])

    def test_smoke_script_reports_all_green(self):
        """scripts/smoke.py is the deployment check: keep it working against a real server."""
        import contextlib
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "smoke_script", os.path.join(os.path.dirname(__file__), "..", "scripts", "smoke.py"))
        smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smoke)

        argv = ["smoke.py", "--base", self.base, "--timeout", "30"]
        saved = sys.argv
        buf = io.StringIO()
        try:
            sys.argv = argv
            with contextlib.redirect_stdout(buf):
                code = smoke.main()
        finally:
            sys.argv = saved
        output = buf.getvalue()
        self.assertEqual(code, 0, output)
        self.assertIn("全部通过", output)
        self.assertEqual(output.count("✗"), 0, output)

    def test_unavailable_openai_endpoints_explain_themselves(self):
        for path, needle in (("/v1/embeddings", "no embedding model"),
                             ("/v1/moderations", "no moderation endpoint"),
                             ("/v1/images/generations", "image generation is unavailable")):
            req = urllib.request.Request(self.base + path,
                                         data=json.dumps({"model": "step-test",
                                                          "input": "x"}).encode(),
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            with self.assertRaises(urllib.error.HTTPError, msg=path) as ctx:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(ctx.exception.code, 400, path)
            body = json.loads(ctx.exception.read())
            self.assertIn(needle, body["error"]["message"], path)

    def test_models(self):
        payload = json.loads(urllib.request.urlopen(self.base + "/v1/models", timeout=10).read())
        self.assertEqual(payload["data"][0]["id"], "step-test")

    def test_model_detail(self):
        entry = json.loads(urllib.request.urlopen(self.base + "/v1/models/step-test", timeout=10).read())
        self.assertEqual(entry["id"], "step-test")
        req = urllib.request.Request(self.base + "/v1/models/nope")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 404)

    def test_foreign_model_names_fall_back(self):
        payload = self.post("/v1/chat/completions", {
            "model": "claude-sonnet-4-5-20250929",
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(payload["choices"][0]["message"]["content"], "Hello from fake")
        self.assertEqual(payload["model"], "step-test")
        req = urllib.request.Request(self.base + "/v1/chat/completions",
                                     data=json.dumps({"model": "typo-model",
                                                      "messages": [{"role": "user", "content": "hi"}]}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 400)

    def test_rejects_n_greater_than_one(self):
        req = urllib.request.Request(self.base + "/v1/chat/completions",
                                     data=json.dumps({"model": "step-test", "n": 3,
                                                      "messages": [{"role": "user", "content": "hi"}]}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 400)

    def test_non_stream(self):
        payload = self.post("/v1/chat/completions", {
            "model": "step-test", "messages": [{"role": "user", "content": "hi"}]})
        message = payload["choices"][0]["message"]
        self.assertEqual(message["content"], "Hello from fake")
        self.assertEqual(message["reasoning_content"], "think")
        self.assertEqual(payload["usage"]["prompt_tokens"], 12)

    def test_stream(self):
        frames, raw = self.post("/v1/chat/completions", {
            "model": "step-test", "messages": [{"role": "user", "content": "hi"}],
            "stream": True, "stream_options": {"include_usage": True}}, stream=True)
        self.assertIn("[DONE]", raw)
        text = "".join(f["choices"][0]["delta"].get("content") or "" for f in frames)
        self.assertEqual(text, "Hello from fake")
        self.assertEqual(frames[-1]["usage"]["total_tokens"], 16)

    def test_session_reuse_sends_only_delta(self):
        STATE["contents"].clear()
        first = self.post("/v1/chat/completions", {
            "model": "step-test", "messages": [{"role": "user", "content": "remember 5"}]})
        messages = [{"role": "user", "content": "remember 5"},
                    {"role": "assistant", "content": first["choices"][0]["message"]["content"]},
                    {"role": "user", "content": "what did I say?"}]
        self.post("/v1/chat/completions", {"model": "step-test", "messages": messages})
        self.assertEqual(STATE["conversations"] >= 1, True)
        # the second upstream turn must carry only the new user message
        self.assertEqual(STATE["contents"][-1], "what did I say?")

    def test_tool_call(self):
        payload = self.post("/v1/chat/completions", {
            "model": "step-test", "messages": [{"role": "user", "content": "TOOL please"}],
            "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}]})
        choice = payload["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertEqual(choice["message"]["tool_calls"][0]["function"]["name"], "get_weather")

    def test_legacy_completions(self):
        payload = self.post("/v1/completions", {"model": "step-test", "prompt": "hello"})
        self.assertEqual(payload["object"], "text_completion")
        self.assertEqual(payload["choices"][0]["text"], "Hello from fake")
        self.assertIn("usage", payload)

        prompt = self.post("/v1/completions", {"model": "step-test", "prompt": ["only one"]})
        self.assertEqual(prompt["choices"][0]["text"], "Hello from fake")

        frames, raw = self.post("/v1/completions", {
            "model": "step-test", "prompt": "hello", "stream": True}, stream=True)
        text = "".join(c.get("text") or "" for f in frames for c in f.get("choices") or [])
        self.assertEqual(text, "Hello from fake")
        self.assertTrue(all(f.get("object") == "text_completion" for f in frames))
        self.assertEqual(raw.count("[DONE]"), 1)

    def test_legacy_completions_rejects_unsupported_fields(self):
        for body, needle in (
                ({"model": "step-test", "prompt": "x", "suffix": "y"}, "fill-in-the-middle"),
                ({"model": "step-test", "prompt": "x", "n": 2}, "n > 1"),
                ({"model": "step-test", "prompt": "x", "logprobs": 1}, "logprobs"),
                ({"model": "step-test", "prompt": [1, 2, 3]}, "prompt must be a string"),
                ({"model": "step-test"}, "prompt is required")):
            req = urllib.request.Request(self.base + "/v1/completions",
                                         data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            with self.assertRaises(urllib.error.HTTPError, msg=str(body)) as ctx:
                urllib.request.urlopen(req, timeout=15)
            self.assertEqual(ctx.exception.code, 400, body)
            self.assertIn(needle, ctx.exception.read().decode(), body)

    def test_anthropic_messages(self):
        payload = self.post("/v1/messages", {"model": "step-test", "max_tokens": 64,
                                             "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(payload["type"], "message")
        self.assertEqual(payload["content"][0]["text"], "Hello from fake")
        self.assertEqual(payload["usage"]["input_tokens"], 12)

    def test_undeclared_tool_calls_are_dropped(self):
        """Studio's own tools (web_search, document_parser) are not client functions."""
        payload = self.post("/v1/chat/completions", {
            "model": "step-test",
            "messages": [{"role": "user", "content": "TOOL without declaring any"}]})
        message = payload["choices"][0]["message"]
        self.assertIsNone(message.get("tool_calls"))
        self.assertEqual(message["content"], "Hello from fake")
        self.assertEqual(payload["choices"][0]["finish_reason"], "stop")

        frames, raw = self.post("/v1/chat/completions", {
            "model": "step-test", "stream": True,
            "messages": [{"role": "user", "content": "TOOL without declaring any"}]}, stream=True)
        deltas = [c for f in frames
                  for c in ((f.get("choices") or [{}])[0].get("delta") or {}).get("tool_calls") or []]
        self.assertEqual(deltas, [], raw[:300])
        self.assertIn("Hello from fake", raw)
        finished = [(f.get("choices") or [{}])[0].get("finish_reason") for f in frames]
        self.assertIn("stop", finished)

    def test_reasoning_can_be_hidden(self):
        """STEP_EXPOSE_REASONING=0 keeps the model's thinking out of every protocol."""
        self.cfg.expose_reasoning = False
        try:
            payload = self.post("/v1/chat/completions", {
                "model": "step-test", "messages": [{"role": "user", "content": "hi"}]})
            self.assertNotIn("reasoning_content", payload["choices"][0]["message"])

            frames, _ = self.post("/v1/chat/completions", {
                "model": "step-test", "stream": True,
                "messages": [{"role": "user", "content": "hi"}]}, stream=True)
            for frame in frames:
                choices = frame.get("choices") or [{}]
                delta = choices[0].get("delta") or {}
                self.assertNotIn("reasoning_content", delta)

            body = json.dumps({"model": "step-test", "max_tokens": 64,
                               "messages": [{"role": "user", "content": "hi"}]}).encode()
            req = urllib.request.Request(self.base + "/v1/messages", data=body,
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            payload = json.loads(urllib.request.urlopen(req, timeout=30).read())
            self.assertNotIn("thinking", [b.get("type") for b in payload.get("content") or []])
        finally:
            self.cfg.expose_reasoning = True

    def test_reasoning_effort_reaches_the_upstream(self):
        STATE["last_config"] = {}
        self.post("/v1/chat/completions", {
            "model": "step-test", "reasoning_effort": "high",
            "messages": [{"role": "user", "content": "effort probe"}]})
        self.assertTrue(STATE["last_config"].get("thinking"), STATE["last_config"])
        self.assertEqual(STATE["last_config"].get("effort"), "high")

        STATE["last_config"] = {}
        self.post("/v1/chat/completions", {
            "model": "step-test", "reasoning_effort": "none",
            "messages": [{"role": "user", "content": "effort probe 2"}]})
        self.assertFalse(STATE["last_config"].get("thinking"), STATE["last_config"])

    def test_anthropic_thinking_budget_sets_the_effort(self):
        """`thinking.budget_tokens` must become an effort level, not be silently dropped."""
        for budget, want in ((2048, "low"), (8192, "medium"), (32768, "high")):
            STATE["last_config"] = {}
            self.post("/v1/messages", {
                "model": "step-test", "max_tokens": 4096,
                "thinking": {"type": "enabled", "budget_tokens": budget},
                "messages": [{"role": "user", "content": "budget probe"}]})
            self.assertTrue(STATE["last_config"].get("thinking"), STATE["last_config"])
            self.assertEqual(STATE["last_config"].get("effort"), want,
                             (budget, STATE["last_config"]))

        STATE["last_config"] = {}
        self.post("/v1/messages", {
            "model": "step-test", "max_tokens": 4096,
            "thinking": {"type": "disabled"},
            "messages": [{"role": "user", "content": "no thinking"}]})
        self.assertFalse(STATE["last_config"].get("thinking"), STATE["last_config"])

        # An explicit `reasoning_effort` outranks the token budget.
        STATE["last_config"] = {}
        self.post("/v1/messages", {
            "model": "step-test", "max_tokens": 4096, "reasoning_effort": "high",
            "thinking": {"type": "enabled", "budget_tokens": 1024},
            "messages": [{"role": "user", "content": "both knobs"}]})
        self.assertEqual(STATE["last_config"].get("effort"), "high", STATE["last_config"])

    def test_the_user_field_isolates_sessions(self):
        """`user` partitions conversation affinity: two end users must not share one."""
        body = {"model": "step-test", "messages": [{"role": "user", "content": "same history"}]}
        before = STATE["conversations"]
        alice = self.post("/v1/chat/completions", dict(body, user="alice"))
        self.assertEqual(STATE["conversations"] - before, 1)
        reply = alice["choices"][0]["message"]["content"]
        transcript = [
            {"role": "user", "content": "same history"},
            {"role": "assistant", "content": reply},
            {"role": "user", "content": "next"}]
        # A second user replaying the *same* transcript must not land in alice's session.
        self.post("/v1/chat/completions",
                  {"model": "step-test", "user": "bob", "messages": transcript})
        self.assertEqual(STATE["conversations"] - before, 2, "different users shared a session")
        self.post("/v1/chat/completions",
                  {"model": "step-test", "user": "alice", "messages": transcript})
        self.assertEqual(STATE["conversations"] - before, 2, "alice's session was not reused")

    def test_request_switches_reach_the_upstream(self):
        """Documented request extensions must actually change the upstream call."""
        STATE["last_config"] = {}
        STATE["last_web_search"] = None
        self.post("/v1/chat/completions", {
            "model": "step-test", "step_thinking": False, "web_search": True,
            "messages": [{"role": "user", "content": "switch probe"}]})
        self.assertFalse(STATE["last_config"].get("thinking"), STATE["last_config"])
        self.assertTrue(STATE["last_web_search"], "web_search in the body was ignored")

        STATE["last_config"] = {}
        req = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=json.dumps({"model": "step-test",
                             "messages": [{"role": "user", "content": "header probe"}]}).encode(),
            headers={"Content-Type": "application/json", "X-Step-Thinking": "off"},
            method="POST")
        urllib.request.urlopen(req, timeout=30).read()
        self.assertFalse(STATE["last_config"].get("thinking"),
                         "X-Step-Thinking: off was ignored")

        for path, payload in (
                ("/v1/messages", {"model": "step-test", "max_tokens": 32, "web_search": True,
                                  "messages": [{"role": "user", "content": "news"}]}),
                ("/v1/responses", {"model": "step-test", "input": "news",
                                   "web_search": True})):
            STATE["last_web_search"] = None
            self.post(path, payload)
            self.assertTrue(STATE["last_web_search"], path + " ignored web_search")

    def test_web_search_tool_enables_upstream_search(self):
        STATE["last_web_search"] = None
        payload = self.post("/v1/responses", {
            "model": "step-test", "input": "今天有什么新闻",
            "tools": [{"type": "web_search_preview"}]})
        self.assertEqual(payload["output_text"], "Hello from fake")
        self.assertTrue(STATE["last_web_search"], "built-in web_search was not mapped upstream")

        STATE["last_web_search"] = None
        self.post("/v1/chat/completions", {
            "model": "step-test", "messages": [{"role": "user", "content": "no search"}]})
        self.assertFalse(STATE["last_web_search"], "web search leaked into a plain turn")

    def test_error_bodies_match_the_calling_protocol(self):
        req = urllib.request.Request(self.base + "/v1/messages",
                                     data=json.dumps({"model": "step-test", "max_tokens": 16,
                                                      "messages": []}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=15)
        self.assertEqual(ctx.exception.code, 400)
        payload = json.loads(ctx.exception.read())
        self.assertEqual(payload["type"], "error")
        self.assertEqual(payload["error"]["type"], "invalid_request_error")
        self.assertIn("non-empty", payload["error"]["message"])

        chat = urllib.request.Request(self.base + "/v1/chat/completions",
                                      data=b"{not json",
                                      headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(chat, timeout=15)
        self.assertEqual(ctx.exception.code, 400)
        payload = json.loads(ctx.exception.read())
        self.assertIn("error", payload)
        self.assertEqual(payload["error"]["type"], "invalid_request_error")

    def test_anthropic_stop_sequences_are_honoured(self):
        payload = self.post("/v1/messages", {
            "model": "step-test", "max_tokens": 64,
            "stop_sequences": ["STOP"],
            "messages": [{"role": "user", "content": "SEQ please"}]})
        text = "".join(b.get("text") or "" for b in payload["content"] if b.get("type") == "text")
        self.assertEqual(text, "Hello ")
        self.assertEqual(payload["stop_reason"], "stop_sequence")
        self.assertEqual(payload["stop_sequence"], "STOP")

        frames, raw = self.post("/v1/messages", {
            "model": "step-test", "max_tokens": 64, "stream": True,
            "stop_sequences": ["STOP"],
            "messages": [{"role": "user", "content": "SEQ please"}]}, stream=True)
        deltas = "".join(d.get("delta", {}).get("text", "") for f in frames
                         for d in [f] if f.get("type") == "content_block_delta")
        self.assertEqual(deltas, "Hello ")
        # The sequence may appear in `stop_sequence` metadata, never in emitted text.
        self.assertTrue(all("STOP" not in (d.get("delta") or {}).get("text", "")
                            for d in frames if d.get("type") == "content_block_delta"))
        deltas_meta = [f for f in frames if f.get("type") == "message_delta"]
        self.assertEqual(deltas_meta[-1]["delta"]["stop_reason"], "stop_sequence")
        self.assertEqual(deltas_meta[-1]["delta"]["stop_sequence"], "STOP")

    def test_text_after_the_stop_sequence_is_dropped(self):
        frames, raw = self.post("/v1/messages", {
            "model": "step-test", "max_tokens": 64, "stream": True,
            "stop_sequences": ["STOP"],
            "messages": [{"role": "user", "content": "SEQ2 please"}]}, stream=True)
        deltas = "".join((d.get("delta") or {}).get("text", "") for d in frames
                         if d.get("type") == "content_block_delta")
        self.assertEqual(deltas, "Hello ")
        self.assertNotIn("leaked", raw)

        payload = self.post("/v1/messages", {
            "model": "step-test", "max_tokens": 64, "stop_sequences": ["STOP"],
            "messages": [{"role": "user", "content": "SEQ2 please"}]})
        text = "".join(b.get("text") or "" for b in payload["content"] if b.get("type") == "text")
        self.assertEqual(text, "Hello ")

    def test_responses_fenced_json_is_unwrapped(self):
        payload = self.post("/v1/responses", {
            "model": "step-test", "input": "FENCE please",
            "text": {"format": {"type": "json_schema", "name": "r",
                                "schema": {"type": "object"}}}})
        self.assertEqual(payload["output_text"], '{"a": 1}')

    def test_responses_reasoning_and_structured_output(self):
        STATE["last_config"] = {}
        self.post("/v1/responses", {"model": "step-test", "input": "hi",
                                    "reasoning": {"effort": "high"}})
        self.assertEqual(STATE["last_config"].get("effort"), "high")

        schema = {"type": "object", "properties": {"answer": {"type": "string"}},
                  "required": ["answer"]}
        STATE["last_system"] = ""
        self.post("/v1/responses", {
            "model": "step-test", "input": "hi",
            "text": {"format": {"type": "json_schema", "name": "reply", "schema": schema}}})
        self.assertIn("Follow this JSON Schema", STATE["last_system"])
        self.assertIn('"answer"', STATE["last_system"])

    def test_previous_response_id_is_rejected_with_a_reason(self):
        req = urllib.request.Request(self.base + "/v1/responses",
                                     data=json.dumps({"model": "step-test", "input": "hi",
                                                      "previous_response_id": "resp_123"}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=15)
        self.assertEqual(ctx.exception.code, 400)
        payload = json.loads(ctx.exception.read())
        self.assertIn("previous_response_id", json.dumps(payload, ensure_ascii=False))

    def test_responses(self):
        payload = self.post("/v1/responses", {"model": "step-test", "input": "hi"})
        self.assertEqual(payload["object"], "response")
        self.assertEqual(payload["output_text"], "Hello from fake")

    def test_agent_mode(self):
        STATE["runs"] = 0
        payload = self.post("/v1/chat/completions", {
            "model": "step-test", "messages": [{"role": "user", "content": "agent hi"}],
            "step_mode": "agent"})
        self.assertEqual(payload["choices"][0]["message"]["content"], "Hello from fake")
        self.assertEqual(STATE["runs"], 1)

    def test_agent_mode_streaming_with_tool(self):
        tools = [{"type": "function", "function": {
            "name": "get_weather", "parameters": {"type": "object",
                                                  "properties": {"city": {"type": "string"}}}}}]
        STATE["runs"] = 0
        frames, raw = self.post("/v1/chat/completions", {
            "model": "step-test", "stream": True, "step_mode": "agent",
            "messages": [{"role": "user", "content": "TOOL agent stream"}],
            "tools": tools, "tool_choice": "auto"}, stream=True)
        self.assertEqual(STATE["runs"], 1, "request did not take the agent surface")
        deltas = [c for f in frames
                  for c in ((f.get("choices") or [{}])[0].get("delta") or {}).get("tool_calls") or []]
        self.assertTrue(deltas, raw[:400])
        self.assertEqual(deltas[0].get("function", {}).get("name"), "get_weather")
        finished = [(f.get("choices") or [{}])[0].get("finish_reason") for f in frames]
        self.assertIn("tool_calls", finished)
        self.assertEqual(raw.count("[DONE]"), 1, raw[-200:])

    def test_agent_mode_reuses_conversation_and_sends_only_the_tail(self):
        STATE["conversations"] = 0
        STATE["runs"] = 0
        first = self.post("/v1/chat/completions", {
            "model": "step-test", "step_mode": "agent",
            "messages": [{"role": "user", "content": "agent one"}]})
        self.assertEqual(first["choices"][0]["message"]["content"], "Hello from fake")
        self.assertEqual(STATE["runs"], 1)
        conversations = STATE["conversations"]
        self.assertEqual(conversations, 1)

        second = self.post("/v1/chat/completions", {
            "model": "step-test", "step_mode": "agent",
            "messages": [{"role": "user", "content": "agent one"},
                         {"role": "assistant", "content": "Hello from fake"},
                         {"role": "user", "content": "agent two"}]})
        self.assertEqual(second["choices"][0]["message"]["content"], "Hello from fake")
        self.assertEqual(STATE["conversations"], conversations, "a new conversation was created")
        self.assertEqual(STATE["runs"], 2)
        # only the new turn goes upstream, not the replayed transcript
        self.assertEqual(STATE["contents"][-1], "agent two")

        # History that does not match what we actually sent must NOT be appended to the
        # same conversation: a fresh one is created and the transcript replayed.
        self.post("/v1/chat/completions", {
            "model": "step-test", "step_mode": "agent",
            "messages": [{"role": "user", "content": "agent one"},
                         {"role": "assistant", "content": "a reply we never sent"},
                         {"role": "user", "content": "agent three"}]})
        self.assertEqual(STATE["conversations"], conversations + 1)
        self.assertIn("agent one", STATE["contents"][-1])

    def test_an_upstream_cut_ends_the_client_stream_cleanly(self):
        """A dead upstream socket must become one error frame + [DONE], not a hang."""
        frames, raw = self.post("/v1/chat/completions", {
            "model": "step-test", "stream": True,
            "messages": [{"role": "user", "content": "CUT please"}]}, stream=True)
        self.assertEqual(raw.count("[DONE]"), 1, raw)
        self.assertIn("error", raw)
        texts = "".join(f["choices"][0]["delta"].get("content") or ""
                        for f in frames if f.get("choices"))
        self.assertEqual(texts, "", "text after the cut must not be forwarded")

    def test_an_upstream_cut_ends_the_responses_stream_with_an_error(self):
        frames, raw = self.post("/v1/responses", {
            "model": "step-test", "stream": True, "input": "CUT please"}, stream=True)
        self.assertIn("event: error", raw)
        self.assertNotIn("response.completed", raw, raw)
        self.assertTrue(any(f.get("type") == "error" for f in frames), frames)

    def test_an_upstream_cut_is_a_502_for_legacy_completions(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/v1/completions", {"model": "step-test", "prompt": "CUT please"})
        self.assertEqual(ctx.exception.code, 502)
        frames, raw = self.post("/v1/completions", {
            "model": "step-test", "prompt": "CUT please", "stream": True}, stream=True)
        self.assertIn('"error"', raw)
        self.assertEqual(raw.count("[DONE]"), 1, raw)

    def test_an_empty_upstream_body_is_an_upstream_error_not_an_empty_answer(self):
        """A 200 with no events must not become a successful empty completion."""
        for stream in (False, True):
            req = urllib.request.Request(
                self.base + "/v1/chat/completions",
                data=json.dumps({"model": "step-test", "stream": stream,
                                 "messages": [{"role": "user", "content": "EMPTY please"}]}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=30)
            self.assertEqual(ctx.exception.code, 502, stream)
            body = ctx.exception.read().decode()
            self.assertIn("done", body, body)

    def test_a_cut_non_stream_turn_is_replayed_once(self):
        """A cut after the turn started is retried for non-stream: nothing was sent yet."""
        identity = self.store.accounts[0].oasis_id or "42"
        CUT_ONCE[identity] = 1
        before = STATE["conversations"]
        try:
            payload = self.post("/v1/chat/completions", {
                "model": "step-test", "messages": [{"role": "user", "content": "replay me"}]})
            self.assertTrue(payload["choices"][0]["message"]["content"].strip(), payload)
            self.assertEqual(CUT_ONCE.get(identity, 0), 0, "the cut was never served")
            self.assertEqual(STATE["conversations"] - before, 2,
                             "the replay must open a fresh conversation, not reuse the cut one")
            deadline = time.time() + 5
            row = None
            while time.time() < deadline:
                rows = self.store.read_usage(self.cfg.usage_file, 1000)
                if rows and rows[-1].get("replayed"):
                    row = rows[-1]
                    break
                time.sleep(0.02)
            self.assertIsNotNone(row, "the replay is invisible in the usage log")
        finally:
            CUT_ONCE.pop(identity, None)

    def test_a_cut_non_stream_anthropic_turn_is_replayed_once(self):
        """The replay lives in `_collect_response`, so every non-stream protocol gets it."""
        identity = self.store.accounts[0].oasis_id or "42"
        CUT_ONCE[identity] = 1
        try:
            payload = self.post("/v1/messages", {
                "model": "step-test", "max_tokens": 64,
                "messages": [{"role": "user", "content": "replay me too"}]})
            text = "".join(b.get("text") or "" for b in payload.get("content") or []
                           if b.get("type") == "text")
            self.assertTrue(text.strip(), payload)
            self.assertEqual(CUT_ONCE.get(identity, 0), 0, "the cut was never served")
        finally:
            CUT_ONCE.pop(identity, None)

    def test_a_replayed_turn_keeps_its_json_mode_extraction(self):
        """`json_mode` rides on the run dict; the replay must not lose the caller's keys."""
        identity = self.store.accounts[0].oasis_id or "42"
        CUT_ONCE[identity] = 1
        try:
            payload = self.post("/v1/chat/completions", {
                "model": "step-test", "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": "FENCE please"}]})
            self.assertEqual(payload["choices"][0]["message"]["content"], '{"a": 1}', payload)
        finally:
            CUT_ONCE.pop(identity, None)

    def test_two_concurrent_replays_do_not_leak_permits(self):
        """Each replay keeps its own request's permit; a 1-slot gateway must not deadlock."""
        cfg = Config(data_dir=self.tmp, port=0, host="127.0.0.1", max_retries=2,
                     max_concurrency=1, sse_heartbeat=0)
        httpd = make_server(cfg, Store(self.tmp))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % httpd.server_address[1]
        CUT_CONTENT.update({"replay A", "replay B"})
        results, errors = [], []

        def worker(text):
            req = urllib.request.Request(
                base + "/v1/chat/completions",
                data=json.dumps({"model": "step-test",
                                 "messages": [{"role": "user", "content": text}]}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    payload = json.loads(resp.read())
                results.append(payload["choices"][0]["message"]["content"])
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        try:
            threads = [threading.Thread(target=worker, args=(text,))
                       for text in ("replay A", "replay B")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(90)
            self.assertFalse(errors, errors)
            self.assertEqual(len(results), 2, results)
            with urllib.request.urlopen(base + "/health", timeout=10) as resp:
                health = json.loads(resp.read())
            self.assertEqual(health["inflight"], 0)
            self.assertEqual(CUT_CONTENT, set(), "a cut was never served")
        finally:
            CUT_CONTENT.clear()
            httpd.shutdown()
            httpd.server_close()

    def test_an_upstream_error_event_is_not_replayed(self):
        """Only a *cut* is retried: a deterministic upstream error must not burn a turn."""
        before = STATE["conversations"]
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/v1/chat/completions", {
                "model": "step-test",
                "messages": [{"role": "user", "content": "REJECT please"}]})
        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(STATE["conversations"] - before, 1,
                         "a deterministic upstream error was replayed")

    def test_two_cuts_in_a_row_give_up_with_a_502(self):
        identity = self.store.accounts[0].oasis_id or "42"
        CUT_ONCE[identity] = 2
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.post("/v1/chat/completions", {
                    "model": "step-test", "messages": [{"role": "user", "content": "two cuts"}]})
            self.assertEqual(ctx.exception.code, 502)
            self.assertEqual(CUT_ONCE.get(identity, 0), 0, "only one replay is allowed")
        finally:
            CUT_ONCE.pop(identity, None)

    def test_a_cut_stream_is_not_replayed(self):
        """Streaming has already sent bytes; a replay would duplicate text."""
        identity = self.store.accounts[0].oasis_id or "42"
        CUT_ONCE[identity] = 1
        try:
            frames, raw = self.post("/v1/chat/completions", {
                "model": "step-test", "stream": True,
                "messages": [{"role": "user", "content": "stream cut"}]}, stream=True)
            self.assertIn("error", raw)
            self.assertEqual(CUT_ONCE.get(identity, 0), 0, "the cut was never served")
        finally:
            CUT_ONCE.pop(identity, None)

    def test_an_upstream_cut_ends_the_anthropic_stream_with_an_error_event(self):
        """The Anthropic envelope must get its own error shape, not OpenAI JSON or a stop."""
        frames, raw = self.post("/v1/messages", {
            "model": "claude-sonnet-4-5", "max_tokens": 64, "stream": True,
            "messages": [{"role": "user", "content": "CUT please"}]}, stream=True)
        self.assertIn("event: error", raw)
        self.assertNotIn("message_stop", raw, raw)
        errors = [f for f in frames if f.get("type") == "error"]
        self.assertEqual(len(errors), 1, frames)
        self.assertTrue(errors[0].get("error", {}).get("message"), errors)

    def test_a_departed_non_stream_client_stops_the_upstream(self):
        """A client that walks away mid-answer must not keep the upstream generating."""
        cfg = Config(data_dir=self.tmp, port=0, host="127.0.0.1", max_retries=1,
                     max_concurrency=1, sse_heartbeat=0)
        httpd = make_server(cfg, Store(self.tmp))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
        store = Store(self.tmp)
        before = len(store.read_usage(cfg.usage_file, 100000))
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        body = json.dumps({"model": "step-test",
                           "messages": [{"role": "user", "content": "DRIP please"}]}).encode()
        CRLF = bytes([13, 10])
        head = (b"POST /v1/chat/completions HTTP/1.1" + CRLF +
                b"Host: test" + CRLF +
                b"Content-Type: application/json" + CRLF +
                b"Content-Length: " + str(len(body)).encode() + CRLF + CRLF)
        sock.sendall(head + body)
        sock.shutdown(socket.SHUT_WR)      # gone, but keep the fd for a clean close
        try:
            deadline = time.time() + 10
            row = None
            while time.time() < deadline:
                rows = store.read_usage(cfg.usage_file, 100000)
                if len(rows) > before and rows[-1].get("status") in ("client_closed", "error"):
                    row = rows[-1]
                    break
                time.sleep(0.05)
            self.assertIsNotNone(row, "the departure was never recorded")
            self.assertEqual(row["status"], "client_closed", row)
            self.assertEqual(sum("DRIP" in str(c) for c in STATE["contents"]), 1,
                             "the turn was restarted for a departed client")
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=10) as resp:
                self.assertEqual(json.loads(resp.read())["inflight"], 0)
        finally:
            sock.close()
            httpd.shutdown()
            httpd.server_close()

    def test_heartbeat_while_the_model_is_quiet(self):
        """StepFun can stall mid-stream; the client must see `: ping` comments, not silence."""
        cfg = Config(data_dir=self.tmp, port=0, host="127.0.0.1", max_retries=1,
                     max_concurrency=1, sse_heartbeat=0.1)
        httpd = make_server(cfg, Store(self.tmp))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            base = "http://127.0.0.1:%d" % httpd.server_address[1]
            req = urllib.request.Request(
                base + "/v1/chat/completions",
                data=json.dumps({"model": "step-test", "stream": True,
                                 "messages": [{"role": "user", "content": "GAP please"}]}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", "replace")
            self.assertIn(": ping", raw, raw[:300])
            self.assertIn("[DONE]", raw)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_parallel_load_drains_permits(self):
        """Twelve overlapping turns (stream/non-stream/tool) through a 4-slot gateway."""
        cfg = Config(data_dir=self.tmp, port=0, host="127.0.0.1", max_retries=1,
                     max_concurrency=4, sse_heartbeat=0)
        httpd = make_server(cfg, Store(self.tmp))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % httpd.server_address[1]
        tools = [{"type": "function", "function": {"name": "get_weather",
                                                   "parameters": {"type": "object"}}}]
        errors = []
        barrier = threading.Barrier(12)

        def worker(index):
            body = {"model": "step-test", "stream": index % 2 == 1,
                    "messages": [{"role": "user", "content": "load %d" % index}]}
            if index % 3 == 0:
                body["messages"][0]["content"] = "TOOL load %d" % index
                body["tools"] = tools
            req = urllib.request.Request(base + "/v1/chat/completions",
                                         data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            try:
                barrier.wait()
                with urllib.request.urlopen(req, timeout=60) as resp:
                    resp.read()
                    if resp.status != 200:
                        errors.append("status %s" % resp.status)
            except Exception as exc:        # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        try:
            self.assertEqual(errors, [])
            health = {}
            for _ in range(60):
                with urllib.request.urlopen(base + "/health", timeout=10) as resp:
                    health = json.loads(resp.read())
                if health["inflight"] == 0:
                    break
                time.sleep(0.1)
            self.assertEqual(health["inflight"], 0, "a concurrency permit leaked")
            self.assertLess(health["threads"], 64, health)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_concurrent_identical_turns_do_not_share_a_conversation(self):
        """Two overlapping continuations must not interleave into one upstream chat.

        The class-wide gateway runs with max_concurrency=1 (which makes permit-leak
        tests deterministic), so this needs its own server with real parallelism.
        """
        cfg = Config(data_dir=self.tmp, port=0, host="127.0.0.1", max_retries=1,
                     max_concurrency=4, sse_heartbeat=0)
        httpd = make_server(cfg, Store(self.tmp))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % httpd.server_address[1]

        def post(body):
            req = urllib.request.Request(base + "/v1/chat/completions",
                                         data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())

        try:
            first = post({"model": "step-test",
                          "messages": [{"role": "user", "content": "slow base"}]})
            history = [{"role": "user", "content": "slow base"},
                       {"role": "assistant", "content": first["choices"][0]["message"]["content"]},
                       {"role": "user", "content": "SLOW continue"}]
            conversations = STATE["conversations"]
            barrier = threading.Barrier(2)
            results = []

            def worker():
                barrier.wait()
                try:
                    results.append(post({"model": "step-test", "messages": list(history)}))
                except Exception as exc:            # noqa: BLE001
                    results.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(results), 2)
            self.assertNotIsInstance(results[0], Exception, results[0])
            self.assertNotIsInstance(results[1], Exception, results[1])
            self.assertEqual(STATE["conversations"], conversations + 1,
                             "both overlapping turns were appended to the same conversation")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_user_field_isolates_sessions(self):
        from step2api.server import session_key
        from step2api.translate import normalize_messages

        msgs = normalize_messages([{"role": "user", "content": "same first message"}])
        a = session_key("tenant", "step-test", "sys", msgs, "playground", user="alice")
        b = session_key("tenant", "step-test", "sys", msgs, "playground", user="bob")
        self.assertNotEqual(a, b)

    def test_extract_json_object(self):
        from step2api.server import _extract_json_object

        self.assertEqual(_extract_json_object('{"a":1}'), '{"a":1}')
        self.assertEqual(_extract_json_object("```json\n{\"a\": 1}\n```"), '{"a": 1}')
        self.assertEqual(_extract_json_object('结果如下：{"a": 2} 完毕'), '{"a": 2}')
        self.assertIsNone(_extract_json_object('没有 JSON'))
        self.assertIsNone(_extract_json_object(''))

    def test_tool_choice_none_suppresses_tools(self):
        from step2api.server import _tools_suppressed

        self.assertTrue(_tools_suppressed("none"))
        self.assertTrue(_tools_suppressed({"type": "none"}))
        self.assertFalse(_tools_suppressed("auto"))
        self.assertFalse(_tools_suppressed("required"))

    def test_response_format_injects_json_instruction(self):
        from step2api.server import _apply_response_format

        prompt = _apply_response_format("be terse", {"type": "json_object"})
        self.assertIn("be terse", prompt)
        self.assertIn("JSON", prompt)
        self.assertEqual(_apply_response_format("x", {"type": "text"}), "x")

    def test_multi_account_session_sticks_to_owner(self):
        second = self.store.add_account("fake-2", make_token(oasis_id="77", device="dev-2"), "dev-2")
        try:
            STATE["contents"].clear()
            STATE["accounts_seen"].clear()
            first = self.post("/v1/chat/completions", {
                "model": "step-test", "messages": [{"role": "user", "content": "who am I"}]})
            messages = [{"role": "user", "content": "who am I"},
                        {"role": "assistant", "content": first["choices"][0]["message"]["content"]},
                        {"role": "user", "content": "and now?"}]
            second_reply = self.post("/v1/chat/completions", {
                "model": "step-test", "messages": messages})
            self.assertEqual(second_reply["choices"][0]["message"]["content"], "Hello from fake")
            # both turns must have been served by the same account (the conversation owner)
            self.assertEqual(len(set(STATE["accounts_seen"])), 1)
            self.assertEqual(STATE["contents"][-1], "and now?")
        finally:
            self.store.remove_account(second.id)

    def test_image_inputs_are_uploaded(self):
        STATE["uploads"] = 0
        STATE["last_asset_ids"] = []
        payload = self.post("/v1/chat/completions", {
            "model": "step-test",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "what colour?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
            ]}],
        })
        self.assertEqual(payload["choices"][0]["message"]["content"], "Hello from fake")
        self.assertEqual(STATE["last_asset_ids"], ["asset-1"])
        self.assertGreaterEqual(STATE["uploads"], 1)

    def test_document_inputs_are_uploaded(self):
        STATE["uploads"] = 0
        STATE["last_asset_ids"] = []
        payload = self.post("/v1/chat/completions", {
            "model": "step-test",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "what does the file say?"},
                {"type": "file", "file": {"filename": "memo.txt",
                                          "file_data": "data:text/plain;base64,aGVsbG8="}},
            ]}],
        })
        self.assertEqual(payload["choices"][0]["message"]["content"], "Hello from fake")
        self.assertEqual(STATE["last_asset_ids"], ["asset-1"])
        self.assertGreaterEqual(STATE["uploads"], 1)

    def test_audio_speech(self):
        req = urllib.request.Request(self.base + "/v1/audio/speech",
                                     data=json.dumps({"input": "hello", "voice": "nova",
                                                      "response_format": "mp3"}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        resp = urllib.request.urlopen(req, timeout=30)
        payload = resp.read()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("Content-Type"), "audio/mpeg")
        self.assertTrue(payload.startswith(b"ID3"))
        self.assertEqual(resp.headers.get("X-Step-Format"), "mp3")

    def test_audio_speech_survives_an_auth_blip(self):
        """A stale access token must heal via refresh, not fail the speech request."""
        urllib.request.urlopen(self.base + "/v1/models", timeout=20).read()  # warm catalogue
        identity = self.store.accounts[0].oasis_id or "42"
        AUTH_BLIPS[identity] = 1
        try:
            req = urllib.request.Request(
                self.base + "/v1/audio/speech",
                data=json.dumps({"input": "hello", "voice": "nova"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.assertEqual(resp.status, 200)
                self.assertTrue(resp.read().startswith(b"ID3"))
            self.assertEqual(AUTH_BLIPS.get(identity, 0), 0, "the blip was never served")
        finally:
            AUTH_BLIPS.pop(identity, None)

    def test_audio_speech_accepts_the_openai_model_names(self):
        """`tts-1` / `gpt-4o-mini-tts` must map to the account's TTS model, not 503."""
        for name in ("tts-1", "gpt-4o-mini-tts", "openai/tts-1-hd"):
            req = urllib.request.Request(
                self.base + "/v1/audio/speech",
                data=json.dumps({"model": name, "input": "hello", "voice": "nova"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.assertEqual(resp.status, 200, name)
                self.assertEqual(resp.headers.get("X-Step-Model"), "stepaudio-2.5-tts", name)
                self.assertTrue(resp.read().startswith(b"ID3"), name)

    def test_a_bad_audio_model_is_a_400_and_does_not_cool_the_pool(self):
        """A typo is the request's fault: 400, and every other client keeps working."""
        req = urllib.request.Request(
            self.base + "/v1/audio/speech",
            data=json.dumps({"model": "no-such-model", "input": "hello"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=30)
        self.assertEqual(ctx.exception.code, 400, ctx.exception.read())
        self.assertFalse(self.store.accounts[0].cooling, "the account was cooled for a typo")

        ok = urllib.request.Request(
            self.base + "/v1/audio/speech",
            data=json.dumps({"input": "hello", "voice": "nova"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(ok, timeout=30) as resp:
            self.assertEqual(resp.status, 200)
            self.assertTrue(resp.read().startswith(b"ID3"))

    def test_a_known_audio_model_denied_by_one_account_fails_over(self):
        """Entitlement denials are per account: try the next one before answering 400."""
        for acc in self.store.accounts:
            acc.mark_ok()
        second = self.store.add_account("fake2", make_token(oasis_id="43"), "dev-2")
        AUDIO_DENY_ONCE[0] = 1
        try:
            req = urllib.request.Request(
                self.base + "/v1/audio/speech",
                data=json.dumps({"input": "hello", "voice": "nova"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.assertEqual(resp.status, 200)
                self.assertTrue(resp.read().startswith(b"ID3"))
            self.assertEqual(AUDIO_DENY_ONCE[0], 0, "the denial was never served")
            for acc in self.store.accounts:
                self.assertFalse(acc.cooling, "%s was cooled for an entitlement" % acc.name)
        finally:
            AUDIO_DENY_ONCE[0] = 0
            self.store.remove_account(second.id)

    def test_audio_transcription_accepts_whisper_1(self):
        status, body, _, headers = self._transcribe(model="whisper-1")
        self.assertEqual(status, 200, body)
        self.assertIn("hello world", body)
        self.assertEqual(headers.get("X-Step-Model"), "stepaudio-2.5-asr")

    def test_audio_transcription(self):
        boundary = "----testboundary"
        payload = (b"--" + boundary.encode() +
                   b"\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.mp3\""
                   b"\r\nContent-Type: audio/mpeg\r\n\r\nID3fake\r\n" +
                   b"--" + boundary.encode() + b"--\r\n")
        req = urllib.request.Request(
            self.base + "/v1/audio/transcriptions", data=payload, method="POST",
            headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        response = json.loads(urllib.request.urlopen(req, timeout=30).read())
        self.assertEqual(response["text"], "hello world")
        self.assertGreaterEqual(STATE.get("uploads", 0), 1)

    def test_audio_transcription_survives_an_auth_blip(self):
        urllib.request.urlopen(self.base + "/v1/models", timeout=20).read()  # warm catalogue
        identity = self.store.accounts[0].oasis_id or "42"
        AUTH_BLIPS[identity] = 1
        try:
            status, body, _, _ = self._transcribe()
            self.assertEqual(status, 200, body)
            self.assertIn("hello world", body)
            self.assertEqual(AUTH_BLIPS.get(identity, 0), 0, "the blip was never served")
        finally:
            AUTH_BLIPS.pop(identity, None)

    def _transcribe(self, response_format="", model=""):
        CRLF = "\r\n"
        boundary = "----testboundary"
        parts = ["--" + boundary,
                 'Content-Disposition: form-data; name="file"; filename="a.mp3"',
                 "Content-Type: audio/mpeg", "", "ID3fake"]
        payload = (CRLF.join(parts) + CRLF).encode()
        if response_format:
            payload += ("--" + boundary + CRLF +
                        'Content-Disposition: form-data; name="response_format"' + CRLF +
                        CRLF + response_format + CRLF).encode()
        if model:
            payload += ("--" + boundary + CRLF +
                        'Content-Disposition: form-data; name="model"' + CRLF +
                        CRLF + model + CRLF).encode()
        payload += ("--" + boundary + "--" + CRLF).encode()
        req = urllib.request.Request(
            self.base + "/v1/audio/transcriptions", data=payload, method="POST",
            headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return (resp.status, resp.read().decode(),
                        resp.headers.get("Content-Type") or "", resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode(), "", exc.headers

    def test_audio_translations_is_an_honest_400(self):
        """Studio cannot translate audio; a 404 would look like a typo in the URL."""
        CRLF = bytes([13, 10])
        payload = (b"--x--" + CRLF +
                   b'Content-Disposition: form-data; name="file"; filename="a.mp3"' + CRLF +
                   CRLF + b"ID3" + CRLF + b"--x--" + CRLF)
        req = urllib.request.Request(
            self.base + "/v1/audio/translations", data=payload, method="POST",
            headers={"Content-Type": "multipart/form-data; boundary=x"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=15)
        self.assertEqual(ctx.exception.code, 400)
        body = json.loads(ctx.exception.read())
        self.assertIn("transcriptions", body["error"]["message"])

    def test_transcription_response_formats(self):
        status, body, ctype, headers = self._transcribe("text")
        self.assertEqual(headers.get("X-Step-Model"), "stepaudio-2.5-asr")
        self.assertEqual(status, 200)
        self.assertEqual(body, "hello world")
        self.assertIn("text/plain", ctype)

        status, body, _, headers = self._transcribe("verbose_json")
        self.assertEqual(headers.get("X-Step-Model"), "stepaudio-2.5-asr")
        self.assertEqual(status, 200)
        parsed = json.loads(body)
        self.assertEqual(parsed["text"], "hello world")
        self.assertEqual(parsed["task"], "transcribe")
        self.assertIsNone(parsed["duration"])

        status, body, _, _ = self._transcribe("srt")
        self.assertEqual(status, 400)
        self.assertIn("srt", body)


    def test_audio_voices(self):
        payload = json.loads(urllib.request.urlopen(self.base + "/v1/audio/voices", timeout=10).read())
        self.assertEqual(payload["object"], "list")
        self.assertEqual(payload["data"][0]["id"], "xiaoyue")

    def test_url_shapes_are_normalised(self):
        """Proxies and SDKs add trailing slashes and query strings; both must still route."""
        for path in ("/v1/models", "/v1/models/", "/v1/models?x=1", "/v1/models/?x=1",
                     "/v1/models/step-test/", "/health?probe=1"):
            with urllib.request.urlopen(self.base + path, timeout=10) as resp:
                self.assertEqual(resp.status, 200, path)
                body = resp.read()
                self.assertTrue(body, path)
        req = urllib.request.Request(self.base + "/v1/models",
                                     data=json.dumps({"model": "step-test",
                                                      "messages": [{"role": "user", "content": "hi"}]}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        # POST with a trailing slash + query routes to the same handler
        req.full_url = self.base + "/v1/chat/completions/?x=1"
        with urllib.request.urlopen(req, timeout=30) as resp:
            self.assertEqual(resp.status, 200)
            self.assertTrue(json.loads(resp.read())["choices"])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/V1/models", timeout=10)
        self.assertEqual(ctx.exception.code, 404)

    def test_admin_is_not_cors_exposed(self):
        req = urllib.request.Request(self.base + "/admin/api/state")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertIsNone(ctx.exception.headers.get("Access-Control-Allow-Origin"))
        preflight = urllib.request.urlopen(
            urllib.request.Request(self.base + "/v1/models", method="OPTIONS"), timeout=10)
        self.assertEqual(preflight.status, 204)
        self.assertEqual(preflight.headers.get("Access-Control-Allow-Origin"), "*")

    def test_malformed_field_types_never_500(self):
        """Wrong types in well-known fields must be a 4xx, never a crash."""
        cases = [
            ("/v1/chat/completions", {"model": 123, "messages": "nope"}),
            ("/v1/chat/completions", {"model": "step-test", "messages": [{"role": "user", "content": {"weird": 1}}]}),
            ("/v1/chat/completions", {"model": "step-test", "messages": [{"role": "user", "content": "x"}], "tools": "nope"}),
            ("/v1/chat/completions", {"model": "step-test", "messages": [{"role": "user", "content": "x"}], "tools": [None, 1, {"function": 5}]}),
            ("/v1/chat/completions", {"model": "step-test", "messages": [{"role": "user", "content": "x"}], "max_tokens": "many"}),
            ("/v1/chat/completions", {"model": "step-test", "messages": [{"role": "user", "content": "x"}], "stop": 5}),
            ("/v1/chat/completions", {"model": "step-test", "messages": [{"role": "user", "content": "x"}], "stream": "yes"}),
            ("/v1/chat/completions", {"model": "step-test", "messages": [{"role": "user", "content": "x"}], "response_format": 5}),
            ("/v1/chat/completions", {"model": "step-test", "messages": [{"role": "user", "content": "x"}], "tool_choice": 5}),
            ("/v1/messages", {"model": "step-test", "max_tokens": 8, "messages": [{"role": "user", "content": 5}]}),
            ("/v1/messages", {"model": "step-test", "max_tokens": 8, "system": 5, "messages": [{"role": "user", "content": "x"}]}),
            ("/v1/messages", {"model": "step-test", "max_tokens": 8, "messages": [{"role": "user", "content": "x"}], "stop_sequences": 5}),
            ("/v1/messages", {"model": "step-test", "max_tokens": 8, "messages": [{"role": "user", "content": "x"}], "thinking": "yes"}),
            ("/v1/responses", {"model": "step-test", "input": 5}),
            ("/v1/responses", {"model": "step-test", "input": "hi", "text": 5}),
            ("/v1/responses", {"model": "step-test", "input": "hi", "reasoning": 5}),
            ("/v1/responses", {"model": "step-test", "input": [None, 5, {"type": 7}]}),
            ("/v1/completions", {"model": "step-test", "prompt": {"a": 1}}),
            ("/v1/completions", {"model": "step-test", "prompt": "x", "stop": {"a": 1}}),
            ("/v1/audio/speech", {"input": 5}),
            ("/v1/audio/speech", {"input": "hi", "voice": 5, "response_format": 5}),
        ]
        for path, body in cases:
            req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    self.assertLess(resp.status, 500, "%s %s" % (path, body))
                    resp.read()
            except urllib.error.HTTPError as exc:
                self.assertLess(exc.code, 500, "%s %s -> %s" % (path, body, exc.read()[:200]))
            # the connection must stay usable for the next iteration
            with urllib.request.urlopen(self.base + "/ping", timeout=10) as ping:
                self.assertEqual(ping.read(), b"pong")

    def test_every_api_route_requires_the_key(self):
        """With a key configured, no /v1 route may answer without one."""
        entry = self.store.add_api_key("gate2")
        try:
            routes = [
                ("GET", "/v1/models"),
                ("GET", "/v1/models/step-test"),
                ("GET", "/v1/models/catalogue"),
                ("GET", "/v1/audio/voices"),
                ("POST", "/v1/chat/completions"),
                ("POST", "/v1/completions"),
                ("POST", "/v1/messages"),
                ("POST", "/v1/messages/count_tokens"),
                ("POST", "/v1/responses"),
                ("POST", "/v1/audio/speech"),
                ("POST", "/v1/audio/transcriptions"),
            ]
            for method, path in routes:
                body = b"{}" if method == "POST" else None
                req = urllib.request.Request(self.base + path, data=body, method=method,
                                             headers={"Content-Type": "application/json"})
                with self.assertRaises(urllib.error.HTTPError, msg=path) as ctx:
                    urllib.request.urlopen(req, timeout=10)
                self.assertEqual(ctx.exception.code, 401, "%s %s is open" % (method, path))
        finally:
            self.store.remove_api_key(entry["key"])

    def test_every_admin_route_requires_the_panel_token(self):
        """A new admin route that forgets the check would fail here."""
        routes = [
            ("GET", "/admin/api/state"),
            ("GET", "/admin/api/logs"),
            ("GET", "/admin/api/export"),
            ("GET", "/admin/api/usage.csv"),
            ("POST", "/admin/api/accounts"),
            ("POST", "/admin/api/accounts/test"),
            ("POST", "/admin/api/accounts/refresh"),
            ("POST", "/admin/api/keys"),
            ("POST", "/admin/api/settings"),
            ("DELETE", "/admin/api/accounts/x"),
            ("DELETE", "/admin/api/keys/x"),
            ("PATCH", "/admin/api/accounts/x"),
            ("PATCH", "/admin/api/keys/x"),
        ]
        for method, path in routes:
            body = b"{}" if method in ("POST", "PATCH") else None
            req = urllib.request.Request(self.base + path, data=body, method=method,
                                         headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError, msg="%s %s" % (method, path)) as ctx:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(ctx.exception.code, 401, "%s %s is open" % (method, path))

    def test_disabling_the_last_key_does_not_open_the_gateway(self):
        entry = self.store.add_api_key("gate")
        try:
            self.store.set_api_key(entry["key"], False)
            for headers in ({}, {"Authorization": "Bearer " + entry["key"]},
                            {"Authorization": "Bearer wrong"}):
                req = urllib.request.Request(self.base + "/v1/models", headers=headers)
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req, timeout=10)
                self.assertEqual(ctx.exception.code, 401, headers)
        finally:
            self.store.remove_api_key(entry["key"])

    def test_patch_with_a_broken_body_still_answers(self):
        import http.client

        token = self.post("/admin/login", {"password": "admin"})["token"]
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=10)
        try:
            conn.request("PATCH", "/admin/api/accounts/nope", body=b"{not json",
                         headers={"Content-Type": "application/json", "X-Panel-Token": token})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 400, resp.read())
        finally:
            conn.close()

    def test_head_on_export_has_no_body(self):
        import http.client

        token = self.post("/admin/login", {"password": "admin"})["token"]
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=10)
        try:
            conn.request("HEAD", "/admin/api/export", headers={"X-Panel-Token": token})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"")
            self.assertGreater(int(resp.getheader("Content-Length")), 0)
            self.assertEqual(resp.getheader("Cache-Control"), "no-store")
            conn.request("GET", "/ping")
            self.assertEqual(conn.getresponse().read().decode(), "pong")
        finally:
            conn.close()

    def test_chunked_request_bodies_are_rejected(self):
        import socket

        CRLF = "\r\n"
        sock = socket.create_connection(("127.0.0.1", self.httpd.server_address[1]), timeout=10)
        try:
            sock.sendall(("POST /v1/chat/completions HTTP/1.1" + CRLF +
                          "Host: x" + CRLF +
                          "Transfer-Encoding: chunked" + CRLF +
                          "Content-Type: application/json" + CRLF + CRLF +
                          "2" + CRLF + "{}" + CRLF + "0" + CRLF + CRLF).encode())
            data = b""
            while (CRLF * 2).encode() not in data and len(data) < 65536:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
            head, _, body = data.partition((CRLF * 2).encode())
            while b"}" not in body:            # the JSON error body arrives separately
                chunk = sock.recv(4096)
                if not chunk:
                    break
                body += chunk
            self.assertIn(b"400", head.split(CRLF.encode(), 1)[0], head[:120])
            self.assertIn(b"chunked", body.lower())
        finally:
            sock.close()


    def test_messages_alias_gets_the_anthropic_envelope(self):
        req = urllib.request.Request(self.base + "/messages",
                                     data=json.dumps({"model": "step-test", "max_tokens": 8,
                                                      "messages": []}).encode(),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        payload = json.loads(ctx.exception.read())
        self.assertEqual(payload["type"], "error")

    def test_export_requires_panel_and_returns_store(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/admin/api/export", timeout=10)
        self.assertEqual(ctx.exception.code, 401)
        login = self.post("/admin/login", {"password": "admin"})
        req = urllib.request.Request(self.base + "/admin/api/export",
                                     headers={"X-Panel-Token": login["token"]})
        payload = json.loads(urllib.request.urlopen(req, timeout=10).read())
        self.assertTrue(payload["accounts"])
        self.assertIn("token", payload["accounts"][0])

    def test_usage_logging_can_be_disabled(self):
        before = len(self.store.read_usage(self.cfg.usage_file, 100000))
        self.cfg.usage_log = False
        try:
            self.post("/v1/chat/completions", {
                "model": "step-test", "messages": [{"role": "user", "content": "quiet"}]})
        finally:
            self.cfg.usage_log = True
        after = len(self.store.read_usage(self.cfg.usage_file, 100000))
        self.assertEqual(after, before, "a row was written with STEP_USAGE_LOG=0")

    def test_usage_records_the_client_user_agent(self):
        """Which client actually hit us is the first question when a proxy misroutes."""
        before = len(self.store.read_usage(self.cfg.usage_file, 100000))
        req = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=json.dumps({"model": "step-test",
                             "messages": [{"role": "user", "content": "ua probe"}]}).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": "claude-cli/2.1.310 (external, cli)"},
            method="POST")
        urllib.request.urlopen(req, timeout=30).read()
        deadline = time.time() + 8
        rows = []
        while time.time() < deadline:
            rows = self.store.read_usage(self.cfg.usage_file, 100000)[before:]
            if rows:
                break
            time.sleep(0.1)
        self.assertTrue(rows, "no usage row written")
        self.assertTrue(rows[-1]["client"].startswith("claude-cli/2.1.310"), rows[-1])

    def _usage_for(self, user_agent):
        deadline = time.time() + 8
        while time.time() < deadline:
            rows = [r for r in self.store.read_usage(self.cfg.usage_file, 100000)
                    if r.get("client") == user_agent]
            if rows:
                return rows
            time.sleep(0.1)
        return []

    def test_the_refresh_button_rotates_one_account(self):
        account = self.store.add_account("one", make_token(oasis_id="798", device="dev-798"),
                                         "dev-798", "798")
        stored = account.token
        try:
            login = self.post("/admin/login", {"password": "admin"})
            req = urllib.request.Request(
                self.base + "/admin/api/accounts/" + account.id + "/refresh", data=b"{}",
                method="POST",
                headers={"Content-Type": "application/json", "X-Panel-Token": login["token"]})
            payload = json.loads(urllib.request.urlopen(req, timeout=30).read())
            self.assertTrue(payload["ok"], payload)
            self.assertNotEqual(account.token, stored)
            self.assertGreater(payload["account"]["token_exp"], 0)
        finally:
            self.store.remove_account(account.id)

    def test_panel_state_never_calls_the_upstream(self):
        """A 15 s panel poll must not park a thread on a stalled upstream."""
        token = self.post("/admin/login", {"password": "admin"})["token"]
        # Wait for the startup prewarm to land before clearing, or it repopulates the
        # cache between the clear and the request.
        deadline = time.time() + 10
        while not self.httpd.gateway.cached_models()[0] and time.time() < deadline:
            time.sleep(0.05)
        self.httpd.gateway._models_cache = (0.0, [], {})   # cold cache on purpose
        before = STATE["catalog"]
        req = urllib.request.Request(self.base + "/admin/api/state",
                                     headers={"X-Panel-Token": token})
        payload = json.loads(urllib.request.urlopen(req, timeout=10).read())
        self.assertEqual(payload["models"], [])
        self.assertIn("模型目录", payload["model_error"])
        self.assertEqual(STATE["catalog"], before, "the panel poll hit the upstream")
        self.assertNotIn("panel_password", payload["settings"], payload["settings"])

    def test_logout_revokes_the_panel_token(self):
        token = self.post("/admin/login", {"password": "admin"})["token"]
        req = urllib.request.Request(self.base + "/admin/api/state",
                                     headers={"X-Panel-Token": token})
        self.assertTrue(json.loads(urllib.request.urlopen(req, timeout=10).read())["accounts"])
        req = urllib.request.Request(self.base + "/admin/logout", data=b"", method="POST",
                                     headers={"X-Panel-Token": token})
        self.assertTrue(json.loads(urllib.request.urlopen(req, timeout=10).read())["ok"])
        req = urllib.request.Request(self.base + "/admin/api/state",
                                     headers={"X-Panel-Token": token})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 401)

    def test_unsupported_input_parts_are_rejected_not_dropped(self):
        """An inline audio block or a file_id would otherwise be dropped silently."""
        cases = [
            ([{"type": "text", "text": "transcribe"},
              {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}}],
             "inline audio input is not supported"),
            ([{"type": "file", "file": {"file_id": "file-abc"}}],
             "file_id references are not supported"),
        ]
        for content, expected in cases:
            body = json.dumps({"model": "step-test",
                               "messages": [{"role": "user", "content": content}]}).encode()
            req = urllib.request.Request(self.base + "/v1/chat/completions", data=body,
                                         method="POST",
                                         headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError, msg=expected) as ctx:
                urllib.request.urlopen(req, timeout=30)
            self.assertEqual(ctx.exception.code, 400, expected)
            self.assertIn(expected, json.loads(ctx.exception.read())["error"]["message"])

    def test_agent_mode_rejects_uploads(self):
        """The workspace has no upload slot: answering anyway would ignore the image."""
        png = "data:image/png;base64," + base64.b64encode(
            bytes([137, 80, 78, 71, 13, 10, 26, 10]) + b"y" * 16).decode()
        body = {"model": "step-test", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "what is this"},
            {"type": "image_url", "image_url": {"url": png}}]}]}
        req = urllib.request.Request(
            self.base + "/v1/chat/completions", data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "X-Step-Mode": "agent"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=30)
        self.assertEqual(ctx.exception.code, 400)
        self.assertIn("agent mode accepts no uploaded attachments",
                      json.loads(ctx.exception.read())["error"]["message"])

    def test_a_replayed_history_is_not_charged_for_repeated_attachments(self):
        """On a cache miss the whole transcript is re-sent; nine turns of the same image
        are one upload, not nine attachments."""
        png = "data:image/png;base64," + base64.b64encode(
            bytes([137, 80, 78, 71, 13, 10, 26, 10]) + b"history!" * 3).decode()
        messages = []
        for i in range(9):
            messages.append({"role": "user", "content": [
                {"type": "text", "text": "turn %d" % i},
                {"type": "image_url", "image_url": {"url": png}}]})
            messages.append({"role": "assistant", "content": "ok %d" % i})
        messages.append({"role": "user", "content": "and now?"})
        before = STATE.get("uploads", 0)
        payload = self.post("/v1/chat/completions",
                            {"model": "step-test", "messages": messages})
        self.assertEqual(payload["choices"][0]["message"]["content"], "Hello from fake")
        self.assertEqual(STATE.get("uploads", 0) - before, 1,
                         "the same image was uploaded more than once")
        self.assertEqual(len(STATE["last_asset_ids"]), 1, STATE["last_asset_ids"])

    def test_duplicate_images_in_one_message_upload_once(self):
        png = "data:image/png;base64," + base64.b64encode(
            bytes([137, 80, 78, 71, 13, 10, 26, 10]) + b"dupe!" * 3).decode()
        before = STATE.get("uploads", 0)
        payload = self.post("/v1/chat/completions", {
            "model": "step-test", "messages": [{"role": "user", "content":
                [{"type": "image_url", "image_url": {"url": png}} for _ in range(3)]}]})
        self.assertEqual(payload["choices"][0]["message"]["content"], "Hello from fake")
        self.assertEqual(STATE.get("uploads", 0) - before, 1)
        self.assertEqual(len(STATE["last_asset_ids"]), 1, STATE["last_asset_ids"])

    def test_more_than_eight_attachments_is_a_400(self):
        """Silently dropping the ninth file would let the model answer blind."""
        png = "data:image/png;base64," + base64.b64encode(
            bytes([137, 80, 78, 71, 13, 10, 26, 10]) + b"x" * 16).decode()
        content = [{"type": "text", "text": "what is this"}] + [
            {"type": "image_url", "image_url": {"url": png}} for _ in range(9)]
        req = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=json.dumps({"model": "step-test",
                             "messages": [{"role": "user", "content": content}]}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=30)
        self.assertEqual(ctx.exception.code, 400)
        self.assertIn("too many attachments in one message",
                      json.loads(ctx.exception.read())["error"]["message"])

    def test_usage_tenant_is_the_key_label_not_the_key(self):
        """usage.jsonl and the CSV get shared; a key prefix is still key material."""
        entry = self.store.add_api_key("team-a")
        try:
            req = urllib.request.Request(
                self.base + "/v1/chat/completions",
                data=json.dumps({"model": "step-test",
                                 "messages": [{"role": "user", "content": "tenant probe"}]}).encode(),
                method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + entry["key"],
                         "User-Agent": "tenant-probe/1"})
            urllib.request.urlopen(req, timeout=30).read()
            rows = self._usage_for("tenant-probe/1")
            self.assertTrue(rows, "no usage row")
            self.assertTrue(rows[-1]["tenant"].startswith("team-a-"), rows[-1])
            self.assertNotIn(entry["key"][:16], json.dumps(rows[-1]))
        finally:
            self.store.remove_api_key(entry["key"])

    def test_two_keys_with_the_same_label_stay_isolated(self):
        first = self.store.add_api_key("shared")
        second = self.store.add_api_key("shared")
        try:
            msgs = [{"role": "user", "content": "hi"}]
            for entry in (first, second):
                req = urllib.request.Request(
                    self.base + "/v1/chat/completions",
                    data=json.dumps({"model": "step-test", "messages": msgs}).encode(),
                    method="POST",
                    headers={"Content-Type": "application/json",
                             "Authorization": "Bearer " + entry["key"],
                             "User-Agent": "tenant-iso/1"})
                urllib.request.urlopen(req, timeout=30).read()
            deadline = time.time() + 8
            rows = []
            while time.time() < deadline:
                rows = self._usage_for("tenant-iso/1")
                if len(rows) >= 2:
                    break
                time.sleep(0.1)
            self.assertEqual(len(rows), 2, rows)
            self.assertNotEqual(rows[-1]["tenant"], rows[-2]["tenant"], rows)
            self.assertEqual(len({r["tenant"] for r in rows}), 2)
        finally:
            self.store.remove_api_key(first["key"])
            self.store.remove_api_key(second["key"])

    def test_open_mode_does_not_echo_a_stray_key(self):
        req = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=json.dumps({"model": "step-test",
                             "messages": [{"role": "user", "content": "open probe"}]}).encode(),
            method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer whatever-1234567890",
                     "User-Agent": "open-probe/1"})
        urllib.request.urlopen(req, timeout=30).read()
        rows = self._usage_for("open-probe/1")
        self.assertTrue(rows, "no usage row")
        self.assertEqual(rows[-1]["tenant"], "anon", rows[-1])

    def test_logs_endpoint_returns_the_newest_first(self):
        self.post("/v1/chat/completions", {
            "model": "step-test", "messages": [{"role": "user", "content": "log me"}]})
        login = self.post("/admin/login", {"password": "admin"})
        req = urllib.request.Request(self.base + "/admin/api/logs",
                                     headers={"X-Panel-Token": login["token"]})
        items = json.loads(urllib.request.urlopen(req, timeout=10).read())["items"]
        self.assertTrue(items)
        self.assertLessEqual(len(items), 200)
        self.assertGreaterEqual(items[0]["ts"], items[-1]["ts"])

    def test_count_tokens_is_a_local_estimate(self):
        """Anthropic clients pre-flight with count_tokens; it must not spend quota."""
        before = STATE["conversations"]
        short = self.post("/v1/messages/count_tokens", {
            "model": "step-test", "messages": [{"role": "user", "content": "hi"}]})
        long = self.post("/v1/messages/count_tokens", {
            "model": "step-test",
            "system": "be brief",
            "messages": [{"role": "user", "content": "hi" * 200}]})
        self.assertGreater(short["input_tokens"], 0)
        self.assertGreater(long["input_tokens"], short["input_tokens"] * 10)
        self.assertEqual(STATE["conversations"], before, "count_tokens went upstream")

    def test_a_panel_password_change_overrides_the_flag(self):
        """Once the panel stores a hash, --panel-password must stop being the password."""
        token = self.post("/admin/login", {"password": "admin"})["token"]
        try:
            req = urllib.request.Request(
                self.base + "/admin/api/settings",
                data=json.dumps({"panel_password": "n3w-panel"}).encode(),
                headers={"Content-Type": "application/json", "X-Panel-Token": token},
                method="POST")
            urllib.request.urlopen(req, timeout=10).read()
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.post("/admin/login", {"password": "admin"})
            self.assertIn(ctx.exception.code, (401, 429))
            recovered = self.post("/admin/login", {"password": "n3w-panel"})
            self.assertTrue(recovered["token"])
            self.assertFalse(recovered.get("password_default"))
        finally:
            self.store.set_setting("panel_password", "")

    def test_admin_key_lifecycle_over_http(self):
        """The panel's key buttons address /admin/api/keys/<key>; check that route."""
        import urllib.parse

        login = self.post("/admin/login", {"password": "admin"})
        headers = {"Content-Type": "application/json", "X-Panel-Token": login["token"]}
        req = urllib.request.Request(
            self.base + "/admin/api/keys", data=json.dumps({"name": "route-check"}).encode(),
            headers=headers, method="POST")
        created = json.loads(urllib.request.urlopen(req, timeout=10).read())
        self.assertTrue(created["ok"], created)
        key = created["key"]["key"]
        url = self.base + "/admin/api/keys/" + urllib.parse.quote(key, safe="")
        try:
            patch = urllib.request.Request(
                url, data=json.dumps({"enabled": False}).encode(), headers=headers,
                method="PATCH")
            self.assertTrue(json.loads(urllib.request.urlopen(patch, timeout=10).read())["ok"])
            entry = next(k for k in self.store.api_keys() if k["key"] == key)
            self.assertFalse(entry["enabled"])
        finally:
            delete = urllib.request.Request(url, headers=headers, method="DELETE")
            json.loads(urllib.request.urlopen(delete, timeout=10).read())
        self.assertFalse(any(k["key"] == key for k in self.store.api_keys()))

    def test_refresh_all_rotates_and_persists(self):
        """Rotating without persisting is how a pool bricks itself on the next restart."""
        from step2api.store import Store

        first = self.store.add_account("ra-1", make_token(oasis_id="796", device="dev-796"),
                                       "dev-796", "796")
        second = self.store.add_account("ra-2", make_token(oasis_id="797", device="dev-797"),
                                        "dev-797", "797")
        before = {first.id: first.token, second.id: second.token}
        try:
            login = self.post("/admin/login", {"password": "admin"})
            req = urllib.request.Request(
                self.base + "/admin/api/accounts/refresh", data=b"{}", method="POST",
                headers={"Content-Type": "application/json", "X-Panel-Token": login["token"]})
            payload = json.loads(urllib.request.urlopen(req, timeout=30).read())
            results = {item["id"]: item["ok"] for item in payload["items"]}
            self.assertTrue(results[first.id], results)
            self.assertTrue(results[second.id], results)
            self.assertNotEqual(first.token, before[first.id])
            self.assertNotEqual(second.token, before[second.id])
            on_disk = {a.id: a.token for a in Store(self.tmp).accounts}
            self.assertEqual(on_disk[first.id], first.token)
            self.assertEqual(on_disk[second.id], second.token)
        finally:
            self.store.remove_account(first.id)
            self.store.remove_account(second.id)

    def test_finish_reason_is_recorded_for_every_protocol(self):
        """Each protocol names its terminal reason differently; the usage row singled
        out just the chat vocabulary, so messages/responses/completions rows were blank."""
        cases = [
            ("usage-anthropic/1", "/v1/messages",
             {"model": "step-test", "max_tokens": 64,
              "messages": [{"role": "user", "content": "hi"}]}, "end_turn"),
            ("usage-responses/1", "/v1/responses",
             {"model": "step-test", "input": "hi"}, "completed"),
            ("usage-completions/1", "/v1/completions",
             {"model": "step-test", "prompt": "hi", "max_tokens": 64}, "stop"),
            ("usage-chat/1", "/v1/chat/completions",
             {"model": "step-test", "max_tokens": 64,
              "messages": [{"role": "user", "content": "hi"}]}, "stop"),
        ]
        for user_agent, path, body, expected in cases:
            req = urllib.request.Request(
                self.base + path, data=json.dumps(body).encode(), method="POST",
                headers={"Content-Type": "application/json", "User-Agent": user_agent})
            urllib.request.urlopen(req, timeout=30).read()
            rows = self._usage_for(user_agent)
            self.assertTrue(rows, user_agent)
            self.assertEqual(rows[-1]["finish_reason"], expected, (user_agent, rows[-1]))

    def test_the_panel_cannot_be_framed_or_sniffed(self):
        """The panel holds a live session token; framing it would enable clickjacking."""
        with urllib.request.urlopen(self.base + "/", timeout=10) as resp:
            self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")
            self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
            csp = resp.headers.get("Content-Security-Policy") or ""
            for directive in ("default-src 'self'", "connect-src 'self'",
                              "img-src 'self' data:", "frame-ancestors 'none'",
                              "object-src 'none'", "base-uri 'none'"):
                self.assertIn(directive, csp)
        with urllib.request.urlopen(self.base + "/health", timeout=10) as resp:
            self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")

    def test_usage_csv_neutralises_formulas(self):
        """The User-Agent column is client-controlled; a leading = must not reach Excel."""
        req = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=json.dumps({"model": "step-test",
                             "messages": [{"role": "user", "content": "csv probe"}]}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "=2+2"})
        urllib.request.urlopen(req, timeout=30).read()
        self.assertTrue(self._usage_for("=2+2"), "no usage row")
        login = self.post("/admin/login", {"password": "admin"})
        req = urllib.request.Request(self.base + "/admin/api/usage.csv",
                                     headers={"X-Panel-Token": login["token"]})
        text = urllib.request.urlopen(req, timeout=10).read().decode("utf-8")
        self.assertIn("'=2+2", text)
        self.assertNotIn(",=2+2", text)

    def test_usage_csv_quotes_embedded_carriage_returns(self):
        """A bare CR inside a field would split the row in Excel (CSV injection)."""
        account = self.store.accounts[0]
        original = account.name
        account.name = "a" + chr(13) + "b"
        try:
            self.post("/v1/chat/completions", {
                "model": "step-test", "messages": [{"role": "user", "content": "cr probe"}]})
            login = self.post("/admin/login", {"password": "admin"})
            req = urllib.request.Request(self.base + "/admin/api/usage.csv",
                                         headers={"X-Panel-Token": login["token"]})
            text = urllib.request.urlopen(req, timeout=10).read().decode("utf-8")
            self.assertIn('"a' + chr(13) + 'b"', text)
        finally:
            account.name = original

    def test_usage_csv_export(self):
        login = self.post("/admin/login", {"password": "admin"})
        req = urllib.request.Request(self.base + "/admin/api/usage.csv",
                                     headers={"X-Panel-Token": login["token"]})
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("utf-8")
            self.assertIn("attachment", resp.headers.get("Content-Disposition") or "")
        header = text.split(chr(10))[0]
        self.assertTrue(text.startswith("ts,account,model,mode"))
        self.assertIn("client", header)
        self.assertIn("finish_reason", header)
        self.assertGreaterEqual(len(text.strip().splitlines()), 1)

    def test_an_auth_blip_is_retried_on_the_same_account(self):
        """A 401 from an RPC heals via refresh; the healed account must serve the retry."""
        account = self.store.add_account("blip", make_token(oasis_id="799", device="dev-799"),
                                         "dev-799", "799")
        main = self.store.accounts[0]
        main.disabled = True
        try:
            # Warm the catalogue first: otherwise the 401 is eaten by that fetch (which
            # has its own retry) and the chat path is never exercised.
            urllib.request.urlopen(self.base + "/v1/models", timeout=20).read()
            AUTH_BLIPS["799"] = 1
            payload = self.post("/v1/chat/completions", {
                "model": "step-test", "messages": [{"role": "user", "content": "auth blip"}]})
            self.assertEqual(payload["choices"][0]["message"]["content"], "Hello from fake")
            self.assertEqual(AUTH_BLIPS.get("799", 0), 0, "the blip was never served")
        finally:
            main.disabled = False
            AUTH_BLIPS.pop("799", None)
            self.store.remove_account(account.id)

    def test_transient_account_failure_fails_over(self):
        BAD_ACCOUNTS.add("666")
        bad = self.store.add_account("bad", make_token(oasis_id="666", device="dev-666"), "dev-666")
        try:
            before = STATE["upstream_503"]
            contents = []
            for i in range(4):
                payload = self.post("/v1/chat/completions", {
                    "model": "step-test",
                    "messages": [{"role": "user", "content": "failover %d" % i}]})
                contents.append(payload["choices"][0]["message"]["content"])
            self.assertEqual(contents, ["Hello from fake"] * 4)
            self.assertGreater(STATE["upstream_503"], before,
                               "the broken account was never tried, so failover is untested")
        finally:
            BAD_ACCOUNTS.discard("666")
            self.store.remove_account(bad.id)

    def test_a_client_reset_does_not_escape_the_handler(self):
        """A reset at either stage -- an idle request line or a half-sent body -- must
        not escape: socketserver turns every escaping exception into a printed traceback."""
        import contextlib
        import tempfile

        from step2api.config import Config
        from step2api.server import Gateway, Handler
        from step2api.store import Store

        class _IdleReset:
            """The client vanished while we waited for the request line."""

            def readline(self, *args):
                raise ConnectionResetError(10054, "reset by peer")

            def read(self, *args):
                return b""

            def close(self):
                pass

        class _BodyReset:
            """Headers arrived, then the client vanished mid-body."""

            HEAD = [b"POST /v1/chat/completions HTTP/1.1" + bytes([13, 10]),
                    b"Host: localhost" + bytes([13, 10]),
                    b"Content-Length: 32" + bytes([13, 10]),
                    bytes([13, 10])]

            def __init__(self):
                self.lines = list(self.HEAD)

            def readline(self, *args):
                return self.lines.pop(0) if self.lines else b""

            def read(self, *args):
                raise ConnectionResetError(10054, "reset by peer")

            def close(self):
                pass

        class _Sock:
            def __init__(self, reader_cls):
                self.reader_cls = reader_cls

            def settimeout(self, timeout):
                pass

            def makefile(self, mode, bufsize=-1):
                return self.reader_cls() if "r" in mode else io.BytesIO()

            def sendall(self, data):
                return None

            def send(self, data):
                return len(data)

            def close(self):
                pass

        tmp = tempfile.mkdtemp(prefix="step2api-reset-")
        store = Store(tmp)          # no API keys: the request reaches the body read
        gateway = Gateway(Config(data_dir=tmp, max_retries=1, max_concurrency=1,
                                 sse_heartbeat=0), store)
        handler_cls = type("H", (Handler,), {"gw": gateway})
        for reader_cls in (_IdleReset, _BodyReset):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                handler_cls(_Sock(reader_cls), ("127.0.0.1", 1), object())
            self.assertEqual(stderr.getvalue(), "", "%s printed a traceback"
                             % reader_cls.__name__)

    def test_doctor_notices_a_running_gateway(self):
        """doctor rotates the same refresh token the gateway holds: it must say so."""
        from step2api.config import Config
        from step2api.diag import gateway_is_up

        live = Config(data_dir=self.tmp, port=self.httpd.server_address[1], host="127.0.0.1")
        self.assertTrue(gateway_is_up(live))
        # A gateway bound to the wildcard address is still reachable on loopback.
        self.assertTrue(gateway_is_up(Config(data_dir=self.tmp, host="0.0.0.0",
                                             port=self.httpd.server_address[1])))
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
        probe.close()
        self.assertFalse(gateway_is_up(Config(data_dir=self.tmp, port=free_port, host="127.0.0.1"),
                                       timeout=0.5))

    def test_doctor_refuses_to_refresh_beside_a_live_gateway(self):
        """Running doctor next to a gateway rotates the same token and can brick the pool."""
        import contextlib
        import tempfile

        from step2api import diag
        from step2api.config import Config
        from step2api.store import Store

        tmp = tempfile.mkdtemp(prefix="step2api-doctor-")
        Store(tmp).add_account("doc", make_token(), "dev-1")
        cfg = Config(data_dir=tmp, port=self.httpd.server_address[1], host="127.0.0.1")
        before = STATE["refreshes"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = diag.run(cfg)
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("已有网关在跑", out.getvalue())
        self.assertEqual(STATE["refreshes"], before, "doctor refreshed anyway")

    def test_doctor_ping_url_brackets_ipv6(self):
        from step2api.config import Config
        from step2api.diag import ping_url

        self.assertEqual(ping_url(Config(host="::1", port=8790)), "http://[::1]:8790/ping")
        self.assertEqual(ping_url(Config(host="0.0.0.0", port=8790)), "http://127.0.0.1:8790/ping")
        self.assertEqual(ping_url(Config(host="::", port=8790)), "http://127.0.0.1:8790/ping")
        self.assertEqual(ping_url(Config(host="127.0.0.1", port=8790)),
                         "http://127.0.0.1:8790/ping")

    def test_a_second_instance_cannot_share_the_port(self):
        """On Windows SO_REUSEADDR would let a second gateway hijack the port and race
        the same account's refresh token against the first one."""
        from step2api.config import Config
        from step2api.server import make_server

        cfg = Config(data_dir=self.tmp, port=self.httpd.server_address[1],
                     host="127.0.0.1", max_retries=1, max_concurrency=1, sse_heartbeat=0)
        with self.assertRaises(OSError):
            make_server(cfg, self.store)

    def test_adding_a_rejected_token_parks_the_account(self):
        """A mistyped cookie must not take turns in the pool and fail live requests."""
        login = self.post("/admin/login", {"password": "admin"})
        req = urllib.request.Request(
            self.base + "/admin/api/accounts",
            data=json.dumps({"name": "typo", "token": "not-a-jwt"}).encode(),
            headers={"Content-Type": "application/json", "X-Panel-Token": login["token"]},
            method="POST")
        payload = json.loads(urllib.request.urlopen(req, timeout=10).read())
        added = None
        try:
            self.assertFalse(payload["ok"], payload)
            added = payload["account"]
            self.assertTrue(added["disabled"], added)
            self.assertIn("已停用", payload["detail"])
            reply = self.post("/v1/chat/completions", {
                "model": "step-test", "messages": [{"role": "user", "content": "still alive"}]})
            self.assertEqual(reply["choices"][0]["message"]["content"], "Hello from fake")
        finally:
            if added:
                self.store.remove_account(added["id"])

    def _add_account(self, body):
        login = self.post("/admin/login", {"password": "admin"})
        req = urllib.request.Request(
            self.base + "/admin/api/accounts", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Panel-Token": login["token"]},
            method="POST")
        return json.loads(urllib.request.urlopen(req, timeout=10).read())

    def test_repasting_a_session_updates_its_row(self):
        """Pasting the fixed cookie again must not leave the parked copy behind."""
        first = self._add_account({"name": "merge-me",
                                   "token": make_token(oasis_id="777", device="dev-777")})
        account_id = (first.get("account") or {}).get("id")
        try:
            self.assertTrue(first["ok"], first)
            self.assertFalse(first["updated"])
            self.assertIsNotNone(account_id, first)
            # A second browser session of the same Studio user: same oasis_id, new device.
            pasted = make_token(oasis_id="777", device="dev-777b")
            second = self._add_account({"name": "merge-me", "token": pasted})
            self.assertTrue(second["ok"], second)
            self.assertEqual(second["account"]["id"], account_id)
            self.assertTrue(second["updated"])
            self.assertEqual(second["account"]["webid"], "dev-777b")
            # A working access token is used as-is: rotating would log out the browser
            # session the cookie was copied from.
            merged = next(a for a in self.store.accounts if a.id == account_id)
            self.assertEqual(merged.token, pasted)
            rows = [a for a in self.store.accounts if a.oasis_id == "777"]
            self.assertEqual(len(rows), 1, [a.name for a in self.store.accounts])
        finally:
            self.store.remove_account(account_id)

    def test_adding_an_account_without_a_token_is_a_400(self):
        login = self.post("/admin/login", {"password": "admin"})
        req = urllib.request.Request(
            self.base + "/admin/api/accounts", data=b"{}", method="POST",
            headers={"Content-Type": "application/json", "X-Panel-Token": login["token"]})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 400)
        self.assertIn("Oasis-Token", json.loads(ctx.exception.read())["error"]["message"])

    def test_a_passing_test_puts_a_parked_account_back_in_the_pool(self):
        first = self._add_account({"name": "enable-me",
                                   "token": make_token(oasis_id="782", device="dev-782")})
        account_id = first["account"]["id"]
        login = self.post("/admin/login", {"password": "admin"})
        headers = {"Content-Type": "application/json", "X-Panel-Token": login["token"]}
        try:
            req = urllib.request.Request(self.base + "/admin/api/accounts/" + account_id,
                                         data=json.dumps({"disabled": True}).encode(),
                                         headers=headers, method="PATCH")
            urllib.request.urlopen(req, timeout=10).read()
            req = urllib.request.Request(self.base + "/admin/api/accounts/test",
                                         data=json.dumps({"id": account_id}).encode(),
                                         headers=headers, method="POST")
            payload = json.loads(urllib.request.urlopen(req, timeout=10).read())
            self.assertTrue(payload["ok"], payload)
            self.assertTrue(payload["re_enabled"], payload)
            self.assertFalse(payload["account"]["disabled"], payload)
        finally:
            self.store.remove_account(account_id)

    def test_concurrent_adds_of_one_session_make_one_row(self):
        """Four panels posting the same cookie at once must not leave three parked
        duplicates: the lookup and the insert happen under one lock."""
        login = self.post("/admin/login", {"password": "admin"})
        body = json.dumps({"name": "race", "token": make_token(oasis_id="793", device="dev-793")})
        results = []

        def worker():
            req = urllib.request.Request(
                self.base + "/admin/api/accounts", data=body.encode(), method="POST",
                headers={"Content-Type": "application/json", "X-Panel-Token": login["token"]})
            try:
                results.append(json.loads(urllib.request.urlopen(req, timeout=30).read()))
            except Exception as exc:  # noqa: BLE001 - surface it in the assertion
                results.append({"error": str(exc)})

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        rows = [a for a in self.store.accounts if a.oasis_id == "793"]
        try:
            self.assertEqual(len(rows), 1, [r.name for r in rows])
            self.assertTrue(rows[0].available(), rows[0].last_error)
            # Re-pastes validate without rotating the chain, so merges succeed too.
            self.assertTrue(all(r.get("ok") for r in results), results)
        finally:
            for row in rows:
                self.store.remove_account(row.id)

    def test_an_oversized_paste_is_rejected(self):
        login = self.post("/admin/login", {"password": "admin"})
        req = urllib.request.Request(
            self.base + "/admin/api/accounts",
            data=json.dumps({"token": "x" * 9000}).encode(), method="POST",
            headers={"Content-Type": "application/json", "X-Panel-Token": login["token"]})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 400)
        self.assertIn("过长", json.loads(ctx.exception.read())["error"]["message"])
        self.assertFalse([a for a in self.store.accounts if a.token.startswith("xxx")])

    def test_a_pasted_cookie_header_adds_the_account(self):
        """The panel's primary field is the whole Cookie line copied from DevTools."""
        token = make_token(oasis_id="795", device="dev-795")
        payload = self._add_account({"cookie": "Cookie: Oasis-Token=%s; Oasis-Webid=dev-795"
                                              % token})
        added = payload.get("account") or {}
        try:
            self.assertTrue(payload["ok"], payload)
            self.assertEqual(added["oasis_id"], "795")
            self.assertEqual(added["webid"], "dev-795")
        finally:
            if added.get("id"):
                self.store.remove_account(added["id"])

    def test_a_row_without_oasis_id_is_merged_by_its_token(self):
        """Rows imported before oasis_id was recorded must not turn into duplicates."""
        old = self.store.add_account("legacy", make_token(oasis_id="794", device="dev-794"),
                                     "dev-794")
        try:
            self.assertEqual(old.oasis_id, "")
            payload = self._add_account({"token": make_token(oasis_id="794", device="dev-794b")})
            self.assertTrue(payload["ok"], payload)
            self.assertTrue(payload["updated"], payload)
            self.assertEqual(payload["account"]["id"], old.id)
            self.assertEqual(old.oasis_id, "794")
            self.assertEqual([a for a in self.store.accounts if a.oasis_id == "794"], [old])
        finally:
            self.store.remove_account(old.id)

    def test_a_validation_blip_keeps_the_rotated_token(self):
        """probe.refresh() already killed the pasted refresh token: dropping the rotated
        one would leave the account to die at the next real refresh."""
        first = self._add_account({"name": "rotate-me",
                                   "token": make_token(oasis_id="786", device="dev-786")})
        account_id = first["account"]["id"]
        account = next(a for a in self.store.accounts if a.id == account_id)
        stored = account.token
        RATE_LIMIT_503.add("786")
        AUTH_BLIPS["786"] = 1      # the pasted access token is stale: a refresh is required
        try:
            second = self._add_account({"name": "rotate-me", "token": stored})
        finally:
            RATE_LIMIT_503.discard("786")
            AUTH_BLIPS.pop("786", None)
        try:
            self.assertFalse(second["ok"], second)
            self.assertTrue(second["updated"], second)
            self.assertNotEqual(account.token, stored, "the rotated token was thrown away")
            self.assertFalse(account.disabled, "a 5xx on validation must not park the account")
        finally:
            self.store.remove_account(account_id)

    def test_a_new_account_keeps_the_rotated_token_when_validation_blips(self):
        pasted = make_token(oasis_id="787", device="dev-787")
        RATE_LIMIT_503.add("787")
        try:
            payload = self._add_account({"name": "blip", "token": pasted})
        finally:
            RATE_LIMIT_503.discard("787")
        added = payload.get("account") or {}
        try:
            self.assertFalse(payload["ok"], payload)
            self.assertFalse(added.get("disabled"), added)
            account = next(a for a in self.store.accounts if a.id == added["id"])
            self.assertNotEqual(account.token, pasted, "the killed token reached the store")
        finally:
            if added.get("id"):
                self.store.remove_account(added["id"])

    def test_a_revoked_session_is_adopted_and_parked(self):
        account = self.store.add_account(
            "revoked", make_token(oasis_id="788", device="dev-788"), "dev-788", "788")
        stored = account.token
        GUEST_ACCOUNTS.add("788")
        try:
            payload = self._add_account({"token": stored})
        finally:
            GUEST_ACCOUNTS.discard("788")
        try:
            self.assertFalse(payload["ok"], payload)
            self.assertTrue(payload["updated"], payload)
            self.assertNotEqual(account.token, stored)
            self.assertTrue(account.disabled, "a guest session must not stay in the pool")
        finally:
            self.store.remove_account(account.id)

    def test_a_blip_on_a_new_session_leaves_the_working_row_alone(self):
        """Refreshing a *different* session of the same user must not touch the row's
        own credential when validation then blips."""
        first = self._add_account({"name": "two-sessions",
                                   "token": make_token(oasis_id="791", device="dev-791")})
        account_id = first["account"]["id"]
        account = next(a for a in self.store.accounts if a.id == account_id)
        stored = account.token
        RATE_LIMIT_503.add("791")
        try:
            second = self._add_account(
                {"token": make_token(oasis_id="791", device="dev-791b")})
        finally:
            RATE_LIMIT_503.discard("791")
        try:
            self.assertFalse(second["ok"], second)
            self.assertEqual(account.token, stored)
            self.assertFalse(account.disabled)
        finally:
            self.store.remove_account(account_id)

    def test_refresh_rate_limit_is_transient(self):
        """429 means "come back later", not "this credential is broken"."""
        from step2api.client import StudioClient, StudioError

        REFRESH_429.add("792")
        try:
            with self.assertRaises(StudioError) as ctx:
                StudioClient(make_token(oasis_id="792", device="dev-792")).refresh()
        finally:
            REFRESH_429.discard("792")
        self.assertTrue(ctx.exception.transient, ctx.exception)

    def test_a_parked_account_is_revived_by_a_good_repaste(self):
        """The parked row remembers its oasis_id, so the fixed cookie updates it."""
        DEAD_ACCOUNTS.add("781")
        try:
            first = self._add_account({"name": "revive-me",
                                       "token": make_token(oasis_id="781", device="dev-781")})
        finally:
            DEAD_ACCOUNTS.discard("781")
        added = first["account"]
        try:
            self.assertFalse(first["ok"], first)
            self.assertTrue(added["disabled"], added)
            self.assertEqual(added["oasis_id"], "781")
            second = self._add_account({"name": "revive-me",
                                        "token": make_token(oasis_id="781", device="dev-781")})
            self.assertTrue(second["ok"], second)
            self.assertTrue(second["updated"])
            self.assertEqual(second["account"]["id"], added["id"])
            self.assertFalse(second["account"]["disabled"])
            self.assertEqual([a.oasis_id for a in self.store.accounts].count("781"), 1)
        finally:
            self.store.remove_account(added["id"])

    def test_a_rejected_repaste_leaves_the_live_account_alone(self):
        """A bad paste for a session we already hold must not poison its credential."""
        first = self._add_account({"name": "merge-me",
                                   "token": make_token(oasis_id="778", device="dev-778")})
        account_id = (first.get("account") or {}).get("id")
        try:
            self.assertTrue(first["ok"], first)
            self.assertFalse(first["updated"])
            self.assertIsNotNone(account_id, first)
            account = next(a for a in self.store.accounts if a.id == account_id)
            good_token = account.token
            DEAD_ACCOUNTS.add("778")
            try:
                # Same Studio user, different (revoked) session: the token must differ
                # from the good one, otherwise this test proves nothing.
                second = self._add_account(
                    {"name": "merge-me",
                     "token": make_token(oasis_id="778", device="dev-revoked")})
            finally:
                DEAD_ACCOUNTS.discard("778")
            self.assertFalse(second["ok"], second)
            self.assertTrue(second["updated"])
            self.assertEqual(second["account"]["id"], account_id)
            self.assertIn("未被修改", second["detail"])
            self.assertEqual(account.token, good_token)
            # The cached client is what serves requests: it must still hold the good token.
            self.assertEqual(self.httpd.gateway.client_for(account).token, good_token)
            self.assertFalse(account.disabled)
            reply = self.post("/v1/chat/completions", {
                "model": "step-test", "messages": [{"role": "user", "content": "after bad paste"}]})
            self.assertEqual(reply["choices"][0]["message"]["content"], "Hello from fake")
        finally:
            if account_id:
                self.store.remove_account(account_id)

    def test_a_guest_session_is_not_accepted_as_an_account(self):
        """The real upstream refreshes forged cookies into guest sessions: refresh alone lies."""
        GUEST_ACCOUNTS.add("556")
        try:
            payload = self._add_account({"name": "guest",
                                         "token": make_token(oasis_id="556", device="dev-556")})
        finally:
            GUEST_ACCOUNTS.discard("556")
        added = payload.get("account") or {}
        try:
            self.assertFalse(payload["ok"], payload)
            self.assertIn("not a logined", payload["detail"])
            self.assertTrue(added.get("disabled"), added)
        finally:
            if added.get("id"):
                self.store.remove_account(added["id"])

    def test_transient_refresh_failure_keeps_the_account_enabled(self):
        """An upstream 5xx says nothing about the credential: do not park a good account."""
        BAD_ACCOUNTS.add("665")
        login = self.post("/admin/login", {"password": "admin"})
        req = urllib.request.Request(
            self.base + "/admin/api/accounts",
            data=json.dumps({"name": "flaky",
                             "token": make_token(oasis_id="665", device="dev-665")}).encode(),
            headers={"Content-Type": "application/json", "X-Panel-Token": login["token"]},
            method="POST")
        try:
            payload = json.loads(urllib.request.urlopen(req, timeout=10).read())
        finally:
            BAD_ACCOUNTS.discard("665")
        added = payload.get("account") or {}
        try:
            self.assertFalse(payload["ok"], payload)
            self.assertFalse(added.get("disabled"), added)
            self.assertNotIn("已停用", payload["detail"])
        finally:
            if added.get("id"):
                self.store.remove_account(added["id"])

    def test_no_available_account_returns_503(self):
        account = self.store.accounts[0]
        account.disabled = True
        try:
            req = urllib.request.Request(self.base + "/v1/chat/completions",
                                         data=json.dumps({"model": "step-test",
                                                          "messages": [{"role": "user", "content": "hi"}]}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=15)
            self.assertEqual(ctx.exception.code, 503)
            payload = json.loads(ctx.exception.read())
            detail = json.dumps(payload, ensure_ascii=False)
            self.assertIn("account", detail)
            # The account exists but is parked: say so instead of "add one".
            self.assertIn("disabled", detail)
            # Clients must be told how long to wait instead of hammering.
            self.assertTrue(ctx.exception.headers.get("Retry-After"), ctx.exception.headers)
        finally:
            account.disabled = False

    def test_no_account_errors_keep_their_protocol_shape(self):
        """A 503 must still look like an Anthropic error to an Anthropic client."""
        account = self.store.accounts[0]
        account.disabled = True
        try:
            req = urllib.request.Request(
                self.base + "/v1/messages",
                data=json.dumps({"model": "step-test", "max_tokens": 8,
                                 "messages": [{"role": "user", "content": "hi"}]}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=15)
            self.assertEqual(ctx.exception.code, 503)
            self.assertEqual(ctx.exception.headers.get("Retry-After"), "10")
            payload = json.loads(ctx.exception.read())
            self.assertEqual(payload.get("type"), "error", payload)
            self.assertIn("message", payload.get("error") or {})
        finally:
            account.disabled = False

    def test_keyless_browser_origins_are_limited_to_loopback(self):
        """A page the user visits must not be able to spend a keyless gateway's quota.

        Withholding ACAO only hides the reply; the simple POST would still run, so the
        request itself is refused (403) before any upstream work.
        """
        def probe(origin, extra=None, method="GET"):
            req = urllib.request.Request(self.base + "/v1/models", method=method,
                                         headers={"Origin": origin, **(extra or {})})
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status, resp.headers.get("Access-Control-Allow-Origin")
            except urllib.error.HTTPError as exc:
                return exc.code, exc.headers.get("Access-Control-Allow-Origin")

        self.assertEqual(probe("http://evil.example"), (403, None))
        self.assertEqual(probe("http://evil.example", method="OPTIONS")[1], None)
        self.assertEqual(probe("http://127.0.0.1:3000"), (200, "http://127.0.0.1:3000"))
        self.assertEqual(probe("http://localhost:3000", method="OPTIONS"),
                         (204, "http://localhost:3000"))

        # The refusal happens before the upstream: no billed generation.
        before = len(STATE["contents"])
        req = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=json.dumps({"model": "step-test",
                             "messages": [{"role": "user", "content": "origin probe"}]}).encode(),
            headers={"Content-Type": "application/json",
                     "Origin": "https://evil.example"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=15)
        self.assertEqual(ctx.exception.code, 403)
        self.assertEqual(len(STATE["contents"]), before, "the refused turn reached upstream")

        entry = self.store.add_api_key("cors")
        try:
            self.assertEqual(probe("http://evil.example",
                                   {"Authorization": "Bearer " + entry["key"]}),
                             (200, "http://evil.example"))
        finally:
            self.store.remove_api_key(entry["key"])

    def test_env_key_counts_as_configured_for_cors(self):
        """`--api-key` is an API key: a cross-origin caller holding it must not be 403'd.

        `_cors_headers` already treated `cfg.api_key` as configured (it echoed the origin),
        while `_reject_foreign_origin` only looked at the *store*. The browser was told
        "go ahead" by the preflight and then refused by the request itself.
        """
        tmp = tempfile.mkdtemp(prefix="step2api-envkey-")
        store = Store(tmp)
        store.add_account("fake", make_token(), "dev-1")
        cfg = Config(data_dir=tmp, port=0, host="127.0.0.1", api_key="sf-env-key",
                     max_retries=1, max_concurrency=2, sse_heartbeat=0)
        httpd = make_server(cfg, store)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % httpd.server_address[1]

        def call(path, headers, method="GET", body=None):
            req = urllib.request.Request(base + path, method=method,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    return resp.status, resp.headers.get("Access-Control-Allow-Origin"), resp.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.headers.get("Access-Control-Allow-Origin"), exc.read()

        origin = "https://app.example"
        try:
            # The key is the authorisation, whichever tier it came from.
            status, acao, _ = call("/v1/models", {"Origin": origin,
                                                 "Authorization": "Bearer sf-env-key"})
            self.assertEqual((status, acao), (200, origin))

            # ... and it is still required.
            status, _, _ = call("/v1/models", {"Origin": origin})
            self.assertEqual(status, 401)

            preflight = urllib.request.Request(
                base + "/v1/chat/completions", method="OPTIONS",
                headers={"Origin": origin, "Access-Control-Request-Method": "POST"})
            with urllib.request.urlopen(preflight, timeout=10) as resp:
                self.assertEqual(resp.status, 204)

            status, _, raw = call(
                "/v1/chat/completions",
                {"Origin": origin, "Authorization": "Bearer sf-env-key",
                 "Content-Type": "application/json"},
                method="POST",
                body={"model": "step-test", "messages": [{"role": "user", "content": "env key"}]})
            self.assertEqual(status, 200, raw[:300])
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_a_models_503_carries_retry_after(self):
        """GET paths must answer like POST ones when no account can serve."""
        account = self.store.accounts[0]
        account.disabled = True
        self.httpd.gateway._models_cache = (0.0, [], {})   # force a fresh catalogue fetch
        try:
            req = urllib.request.Request(self.base + "/v1/models")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=15)
            self.assertEqual(ctx.exception.code, 503)
            self.assertEqual(ctx.exception.headers.get("Retry-After"), "10")
            self.assertIn("error", json.loads(ctx.exception.read()))
        finally:
            account.disabled = False

    def test_short_cooldown_is_waited_out(self):
        account = self.store.accounts[0]
        account.cooldown_until = time.time() + 0.4
        started = time.time()
        try:
            payload = self.post("/v1/chat/completions", {
                "model": "step-test", "messages": [{"role": "user", "content": "cooldown"}]})
        finally:
            account.cooldown_until = 0.0
        self.assertEqual(payload["choices"][0]["message"]["content"], "Hello from fake")
        self.assertGreaterEqual(time.time() - started, 0.35)

    def test_keepalive_loop_survives_a_failing_round(self):
        """A background thread that dies on one bad round stops renewing every account."""
        gw = self.httpd.gateway
        calls = []

        def boom():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("upstream hiccup")
            return 0

        saved_interval = self.cfg.keepalive_interval
        self.cfg.keepalive_interval = 0.05
        try:
            with mock.patch.object(gw, "keepalive_once", side_effect=boom):
                gw.start_keepalive()
                deadline = time.time() + 5
                while time.time() < deadline and len(calls) < 3:
                    time.sleep(0.05)
            self.assertGreaterEqual(len(calls), 3, "the keepalive thread stopped after an error")
        finally:
            gw._keepalive_stop.set()
            self.cfg.keepalive_interval = saved_interval

    def test_keepalive_refreshes_near_expiry(self):
        gw = self.httpd.gateway
        short = self.store.add_account("short", make_token(ttl=-10, oasis_id="99"), "dev-99")
        original_token = short.token
        try:
            self.assertTrue(gw.should_keepalive(short))
            before = STATE["refreshes"]
            self.assertGreaterEqual(gw.keepalive_once(), 1)
            self.assertGreater(STATE["refreshes"], before)
            self.assertFalse(gw.should_keepalive(short))  # freshly renewed
            # The rotated refresh token must reach disk: a restart otherwise kills the
            # account, because Studio invalidated the old one the moment it rotated.
            rotated = gw.client_for(short).token
            self.assertNotEqual(rotated, original_token, "refresh did not rotate the token")
            self.assertEqual(short.token, rotated, "the account kept the stale token")
            with open(self.cfg.accounts_file, encoding="utf-8") as fh:
                on_disk = {a["name"]: a for a in json.load(fh)["accounts"]}
            self.assertEqual(on_disk["short"]["token"], rotated, "the rotated token was not saved")
        finally:
            self.store.remove_account(short.id)

    def test_tolerates_bad_optional_fields(self):
        payload = self.post("/v1/chat/completions", {
            "model": "step-test",
            "messages": [{"role": "user", "content": "hi"}],
            "stream_options": "not-a-dict",
            "metadata": "also-not-a-dict",
            "response_format": "nope",
        })
        self.assertEqual(payload["choices"][0]["message"]["content"], "Hello from fake")

    def test_midstream_error_terminates_once(self):
        frames, raw = self.post("/v1/chat/completions", {
            "model": "step-test", "stream": True,
            "messages": [{"role": "user", "content": "MIDERR please"}]}, stream=True)
        self.assertEqual(raw.count("[DONE]"), 1, raw)
        self.assertEqual(raw.count('"error"'), 1, raw)
        tail = raw.split('"error"')[-1]
        self.assertEqual(tail.count("data: "), 1, tail)   # only the [DONE] frame

    def test_midstream_error_anthropic_single_terminal(self):
        body = json.dumps({"model": "step-test", "max_tokens": 50, "stream": True,
                           "messages": [{"role": "user", "content": "MIDERR"}]}).encode()
        req = urllib.request.Request(self.base + "/v1/messages", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        raw = urllib.request.urlopen(req, timeout=30).read().decode()
        self.assertEqual(raw.count("event: error"), 1, raw)
        self.assertNotIn("message_stop", raw.split("event: error")[-1])

    def test_a_chunked_body_cannot_smuggle_a_second_request(self):
        """CL/TE desync: bytes after a chunked body must never be read as a request."""
        for method, path in (("POST", "/admin/logout"), ("POST", "/v1/embeddings"),
                             ("GET", "/health"), ("DELETE", "/admin/api/keys/x")):
            sock = socket.create_connection(("127.0.0.1", self.httpd.server_address[1]),
                                            timeout=5)
            try:
                sock.sendall(("%s %s HTTP/1.1" % (method, path)).encode() + bytes([13, 10]) +
                             b"Host: localhost" + bytes([13, 10]) +
                             b"Transfer-Encoding: chunked" + bytes([13, 10, 13, 10]) +
                             b"0" + bytes([13, 10, 13, 10]) +
                             b"GET /health HTTP/1.1" + bytes([13, 10]) + bytes([13, 10]))
                sock.settimeout(3)
                data = b""
                while True:
                    try:
                        chunk = sock.recv(4096)
                    except (socket.timeout, ConnectionResetError):
                        break
                    if not chunk:
                        break
                    data += chunk
            finally:
                sock.close()
            self.assertTrue(data.startswith(b"HTTP/1.1 400"), (method, path, data[:120]))
            self.assertEqual(data.count(b"HTTP/1.1 "), 1,
                             "%s %s served the smuggled request: %r" % (method, path, data))

    def test_unread_body_keeps_connection_in_sync(self):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=10)
        try:
            conn.request("POST", "/v1/completions", body='{"x": 1}',
                         headers={"Content-Type": "application/json"})
            first = conn.getresponse()
            first.read()
            self.assertEqual(first.status, 400)
            conn.request("GET", "/ping")
            second = conn.getresponse()
            self.assertEqual(second.status, 200)
            self.assertEqual(second.read().decode(), "pong")
        finally:
            conn.close()

    def test_unreadable_attachment_is_rejected_not_ignored(self):
        body = {"model": "step-test", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "what is this"},
            {"type": "image_url", "image_url": {"url": "data:image/png,not-base64"}}]}]}
        req = urllib.request.Request(self.base + "/v1/chat/completions",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=30)
        self.assertEqual(ctx.exception.code, 400)
        payload = json.loads(ctx.exception.read())
        self.assertIn("attachment could not be read", json.dumps(payload, ensure_ascii=False))
        # A bad request is the client's fault: it must not park the account for 30s.
        account = self.store.accounts[0]
        self.assertTrue(account.available(), "a client-side 400 cooled the account down")

    def _panel_status(self, token):
        req = urllib.request.Request(self.base + "/admin/api/state",
                                     headers={"X-Panel-Token": token})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def _login_status(self, password):
        req = urllib.request.Request(self.base + "/admin/login",
                                     data=json.dumps({"password": password}).encode(),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_password_change_revokes_other_panel_sessions(self):
        first = self.post("/admin/login", {"password": "admin"})["token"]
        second = self.post("/admin/login", {"password": "admin"})["token"]
        try:
            req = urllib.request.Request(
                self.base + "/admin/api/settings",
                data=json.dumps({"panel_password": "s3cret-panel"}).encode(),
                headers={"Content-Type": "application/json", "X-Panel-Token": first},
                method="POST")
            urllib.request.urlopen(req, timeout=10).read()
            self.assertEqual(self._panel_status(first), 200, "the caller lost its own session")
            self.assertEqual(self._panel_status(second), 401, "an old session survived")
        finally:
            self.store.set_setting("panel_password", "")

    def test_lockout_blocks_even_a_correct_password(self):
        import step2api.server as server_module

        gateway = self.httpd.gateway
        gateway.panel_failures.clear()
        try:
            with mock.patch.object(server_module, "PANEL_LOCK_STEPS", (5.0,)):
                statuses = [self._login_status("wrong") for _ in range(3)]
                self.assertIn(429, statuses, statuses)
                # The lock is consulted before PBKDF2: a correct guess does not get in.
                self.assertEqual(self._login_status("admin"), 429)
        finally:
            gateway.panel_failures.clear()

    def test_panel_login_backs_off_after_repeated_failures(self):
        import step2api.server as server_module

        def attempt(password):
            req = urllib.request.Request(self.base + "/admin/login",
                                         data=json.dumps({"password": password}).encode(),
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=10)
            return ctx.exception

        gateway = self.httpd.gateway
        gateway.panel_failures.clear()
        try:
            with mock.patch.object(server_module, "PANEL_LOCK_STEPS", (0.2,)):
                for _ in range(3):
                    self.assertEqual(attempt("wrong").code, 401)
                throttled = attempt("wrong")
                self.assertEqual(throttled.code, 429)
                self.assertTrue(throttled.headers.get("Retry-After"))
                time.sleep(0.3)
                self.assertEqual(attempt("wrong").code, 401)   # lock expired
        finally:
            gateway.panel_failures.clear()

    def test_idle_connection_is_closed(self):
        """A connection that sends nothing must not park a thread forever."""
        import socket

        from step2api.server import Handler

        self.assertIsInstance(Handler.timeout, (int, float),
                              "the handler must set a default socket timeout")
        self.assertGreater(Handler.timeout, 5)
        saved = Handler.timeout
        Handler.timeout = 0.3
        sock = socket.create_connection(("127.0.0.1", self.httpd.server_address[1]), timeout=5)
        try:
            started = time.time()
            sock.settimeout(5)
            self.assertEqual(sock.recv(1), b"")
            self.assertLess(time.time() - started, 4)
        finally:
            Handler.timeout = saved
            sock.close()

    def test_stalled_client_is_logged_as_closed_not_error(self):
        """A socket write timeout means the client stopped reading, not that upstream failed."""
        from step2api.server import Handler

        before = len(self.store.read_usage(self.cfg.usage_file, 100000))
        # socket.timeout (not the builtin) is what a stalled socket actually raises,
        # and on Python 3.9 the two are different classes.
        with mock.patch.object(Handler, "_write_frame",
                               side_effect=socket.timeout("write timed out")):
            try:
                self.post("/v1/chat/completions", {
                    "model": "step-test", "stream": True,
                    "messages": [{"role": "user", "content": "hi there"}]}, stream=True)
            except Exception:      # noqa: BLE001 - the client sees a truncated stream
                pass               # what matters is how the server recorded it
        deadline = time.time() + 8
        rows = []
        while time.time() < deadline:
            rows = self.store.read_usage(self.cfg.usage_file, 100000)[before:]
            if any(r["status"] == "client_closed" for r in rows):
                break
            time.sleep(0.1)
        statuses = [r["status"] for r in rows]
        self.assertIn("client_closed", statuses, rows)
        self.assertNotIn("error", statuses, rows)

    def test_client_hangup_is_detected_between_frames(self):
        """A closed peer must end the turn even when writes would still succeed."""
        import step2api.server as server_module

        before = len(self.store.read_usage(self.cfg.usage_file, 100000))
        with mock.patch.object(server_module, "PEER_CHECK_INTERVAL", 0),                 mock.patch.object(server_module, "peer_closed", lambda sock: True):
            try:
                self.post("/v1/chat/completions", {
                    "model": "step-test", "stream": True,
                    "messages": [{"role": "user", "content": "hangup probe"}]}, stream=True)
            except Exception:     # noqa: BLE001 - the stream is cut short on purpose
                pass
        deadline = time.time() + 8
        rows = []
        while time.time() < deadline:
            rows = self.store.read_usage(self.cfg.usage_file, 100000)[before:]
            if rows:
                break
            time.sleep(0.1)
        self.assertTrue(rows, "no usage row was written")
        self.assertEqual(rows[-1]["status"], "client_closed", rows[-1])

    def test_head_answers_without_a_body(self):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=10)
        try:
            conn.request("HEAD", "/health")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"")
            self.assertGreater(int(resp.getheader("Content-Length")), 0)
            self.assertEqual(resp.getheader("Cache-Control"), "no-store")

            conn.request("HEAD", "/")
            page = conn.getresponse()
            self.assertEqual(page.status, 200)
            self.assertEqual(page.read(), b"")
            self.assertEqual(page.getheader("Cache-Control"), "no-store")

            conn.request("HEAD", "/v1/chat/completions")
            other = conn.getresponse()
            other.read()
            self.assertNotEqual(other.status, 501)

            # HEAD must not desync the keep-alive connection
            conn.request("GET", "/ping")
            ping = conn.getresponse()
            self.assertEqual(ping.read().decode(), "pong")
        finally:
            conn.close()

    def test_error_rows_keep_the_reason(self):
        before = len(self.store.read_usage(self.cfg.usage_file, 100000))
        frames, raw = self.post("/v1/chat/completions", {
            "model": "step-test", "stream": True,
            "messages": [{"role": "user", "content": "MIDERR please"}]}, stream=True)
        self.assertIn("error", raw)
        deadline = time.time() + 8
        hits = []
        while time.time() < deadline:
            # Other tests share this usage file and may flush a row late, so match on
            # the reason instead of assuming exactly one new row.
            rows = self.store.read_usage(self.cfg.usage_file, 100000)[before:]
            hits = [r for r in rows
                    if r["status"] == "error" and "model crashed" in (r.get("error") or "")]
            if hits:
                break
            time.sleep(0.1)
        self.assertEqual(len(hits), 1, rows)
        from step2api.server import Handler
        self.assertIn("error", Handler.USAGE_COLUMNS)

    def test_body_flag_resets_between_keepalive_requests(self):
        """A drained body on request N must not make request N+1 skip its drain."""
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=10)
        try:
            body = json.dumps({"model": "step-test", "stream": False,
                               "messages": [{"role": "user", "content": "hello"}]})
            conn.request("POST", "/v1/chat/completions", body=body,
                         headers={"Content-Type": "application/json"})
            first = conn.getresponse()
            first.read()
            self.assertEqual(first.status, 200)

            # Same connection, now an early 400 that leaves a body on the socket.
            conn.request("POST", "/v1/completions", body='{"x": 1}',
                         headers={"Content-Type": "application/json"})
            second = conn.getresponse()
            second.read()
            self.assertEqual(second.status, 400)

            conn.request("GET", "/ping")
            third = conn.getresponse()
            self.assertEqual(third.status, 200)
            self.assertEqual(third.read().decode(), "pong")
        finally:
            conn.close()

    def test_bogus_charset_does_not_500(self):
        boundary = "----b"
        payload = (b"--" + boundary.encode() +
                   b'\r\nContent-Disposition: form-data; name="model"'
                   b"\r\nContent-Type: text/plain; charset=no-such-charset\r\n\r\nstepaudio-2.5-asr\r\n" +
                   b"--" + boundary.encode() +
                   b'\r\nContent-Disposition: form-data; name="file"; filename="a.mp3"'
                   b"\r\nContent-Type: audio/mpeg\r\n\r\nID3fake\r\n" +
                   b"--" + boundary.encode() + b"--\r\n")
        req = urllib.request.Request(self.base + "/v1/audio/transcriptions", data=payload, method="POST",
                                     headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        with urllib.request.urlopen(req, timeout=30) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.loads(resp.read())["text"], "hello world")

    def test_audio_takes_permit_and_logs_usage(self):
        gw = self.httpd.gateway
        body = json.dumps({"input": "hi", "voice": "nova"}).encode()
        req = urllib.request.Request(self.base + "/v1/audio/speech", data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "my-tts-client/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        self.assertEqual(gw.inflight_now, 0)
        rows = self.store.read_usage(self.cfg.usage_file, 200)
        self.assertTrue(any(r.get("mode") == "tts" for r in rows), rows[-3:])
        # media rows carry the caller too, not just chat turns
        self.assertTrue(any(r.get("mode") == "tts" and r.get("client") == "my-tts-client/1.0"
                            for r in rows), rows[-3:])

    def test_client_abort_is_not_logged_as_error(self):
        import http.client
        import time as _time

        before = len(self.store.read_usage(self.cfg.usage_file, 100000))
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=10)
        body = json.dumps({"model": "step-test", "stream": True,
                           "messages": [{"role": "user", "content": "BIG please"}]})
        conn.request("POST", "/v1/chat/completions", body=body,
                     headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
        resp = conn.getresponse()
        resp.read1(128)
        conn.close()                       # client walks away mid-stream

        deadline = _time.time() + 8
        new_rows = []
        while _time.time() < deadline:
            new_rows = self.store.read_usage(self.cfg.usage_file, 100000)[before:]
            if new_rows:
                break
            _time.sleep(0.2)
        self.assertEqual(len(new_rows), 1, new_rows)
        self.assertEqual(new_rows[0]["status"], "client_closed")

    def test_large_stream_is_framed_and_complete(self):
        """Read a ~1.2 MB stream end to end: framing, backpressure and assembly."""
        frames, raw = self.post("/v1/chat/completions", {
            "model": "step-test", "stream": True,
            "messages": [{"role": "user", "content": "BIG please"}]}, stream=True)
        text = "".join(((f.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
                       for f in frames)
        self.assertEqual(len(text), 20000 * 64)
        self.assertEqual(raw.count("[DONE]"), 1)
        finished = [(f.get("choices") or [{}])[0].get("finish_reason") for f in frames]
        self.assertIn("stop", finished)

    def test_connection_reuse_across_streams(self):
        # pooling clients (openai-python, httpx) reuse one connection for many SSE turns
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=30)
        ids = []
        try:
            for _ in range(3):
                body = json.dumps({"model": "step-test", "stream": True,
                                   "messages": [{"role": "user", "content": "hi"}]})
                conn.request("POST", "/v1/chat/completions", body=body,
                             headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
                resp = conn.getresponse()
                raw = resp.read().decode()
                self.assertEqual(resp.status, 200)
                self.assertIn("[DONE]", raw)
                self.assertFalse(resp.will_close, "server dropped the keep-alive connection")
                ids.append(resp.headers.get("X-Request-Id") or "")
        finally:
            conn.close()
        self.assertTrue(all(rid.startswith("req_") for rid in ids), ids)
        self.assertEqual(len(set(ids)), 3, "the request id leaked across keep-alive turns")

    def test_permit_released_after_policy_error(self):
        req = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=json.dumps({"model": "step-test",
                             "messages": [{"role": "user", "content": "CENSOR this"}]}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=30)
        self.assertEqual(ctx.exception.code, 400)
        # with max_concurrency=1 a leaked permit would block this for 60s and then 429
        payload = self.post("/v1/chat/completions",
                            {"model": "step-test", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(payload["choices"][0]["message"]["content"], "Hello from fake")

    def test_unauthorized_when_key_configured(self):
        self.store.add_api_key("t")
        try:
            req = urllib.request.Request(self.base + "/v1/models")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(ctx.exception.code, 401)
            entry = self.store.api_keys()[0]
            req = urllib.request.Request(self.base + "/v1/models",
                                         headers={"Authorization": "Bearer " + entry["key"]})
            self.assertEqual(urllib.request.urlopen(req, timeout=10).status, 200)

            # the catalogue endpoint carries per-model config bounds: same key required
            cat = urllib.request.Request(self.base + "/v1/models/catalogue")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(cat, timeout=10)
            self.assertEqual(ctx.exception.code, 401)
        finally:
            self.store.remove_api_key(self.store.api_keys()[0]["key"])


if __name__ == "__main__":
    unittest.main()
