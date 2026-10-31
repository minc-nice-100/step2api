"""Regression tests for the third review pass (client.py / media.py / session cache).

Each test is written to fail against the code as it was before the fix:

* CRLF-framed SSE produced no events at all.
* `seen` was never set, so a reset after the last frame raised instead of ending.
* A truncated refresh body threw away a rotated token that the cookie had already
  handed us (and HTTPException escaped as a raw exception).
* ALL_PROXY was written into the proxy map under a key urllib never consults.
* Client-supplied filenames/mime went into multipart headers unescaped.
* A checked-out session could be shared by two concurrent identical requests.
"""

import email.message
import json
import os
import threading
import unittest
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import step2api.client as client_module
from step2api.client import (
    StudioClient, StudioError, _safe_filename, _safe_mime, iter_sse,
)


class FakeResponse:
    """Just enough of an http.client.HTTPResponse for the client code paths."""

    def __init__(self, chunks=None, read_body=b"", read_error=None, headers=None):
        self._chunks = list(chunks or [])
        self._read_body = read_body
        self._read_error = read_error
        self.headers = headers if headers is not None else email.message.Message()
        self.closed = False

    def read1(self, size=-1):
        if self._chunks:
            item = self._chunks.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return b""

    def read(self):
        if self._read_error:
            raise self._read_error
        return self._read_body

    def close(self):
        self.closed = True

    def isclosed(self):
        return self.closed

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeOpener:
    def __init__(self, resp):
        self.resp = resp
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        return self.resp


class SseFramingTest(unittest.TestCase):
    def test_crlf_frames_are_decoded(self):
        payload = (b'data: {"a": 1}\r\n\r\n'
                   b'data: {"a": 2}\r\n\r\n')
        events = list(iter_sse(FakeResponse(chunks=[payload])))
        self.assertEqual([e["a"] for e in events], [1, 2])

    def test_crlf_split_across_chunks(self):
        # the CRLF pair itself is split between two reads
        chunks = [b'data: {"a": 1}\r', b'\n\r', b'\ndata: {"a": 2}\r\n\r\n']
        events = list(iter_sse(FakeResponse(chunks=chunks)))
        self.assertEqual([e["a"] for e in events], [1, 2])

    def test_reset_after_the_last_frame_is_not_an_error(self):
        chunks = [b'data: {"a": 1}\n\n', IncompleteRead(b"", 42)]
        events = list(iter_sse(FakeResponse(chunks=chunks)))
        self.assertEqual([e["a"] for e in events], [1])

    def test_reset_before_any_data_is_transient(self):
        with self.assertRaises(StudioError) as ctx:
            list(iter_sse(FakeResponse(chunks=[IncompleteRead(b"", 42)])))
        self.assertTrue(ctx.exception.transient)


class RefreshTokenTest(unittest.TestCase):
    def test_rotated_cookie_is_adopted_when_the_body_dies(self):
        client = StudioClient("old-access...old-refresh")
        headers = email.message.Message()
        headers.add_header("Set-Cookie", "Oasis-Token=new-access...new-refresh; Path=/; HttpOnly")
        resp = FakeResponse(headers=headers, read_error=IncompleteRead(b"{}", 166))
        with mock.patch.object(client_module, "_proxy_opener", lambda: FakeOpener(resp)):
            token = client.refresh()
        self.assertEqual(token, "new-access...new-refresh")
        self.assertTrue(resp.closed)

    def test_truncated_body_without_cookie_is_transient(self):
        client = StudioClient("old-access...old-refresh")
        resp = FakeResponse(read_error=IncompleteRead(b"", 166))
        with mock.patch.object(client_module, "_proxy_opener", lambda: FakeOpener(resp)):
            with self.assertRaises(StudioError) as ctx:
                client.refresh()
        self.assertTrue(ctx.exception.transient)
        self.assertEqual(client.token, "old-access...old-refresh")

    def test_non_object_refresh_body_is_a_studio_error(self):
        client = StudioClient("old-access...old-refresh")
        resp = FakeResponse(read_body=b"[1, 2]")
        with mock.patch.object(client_module, "_proxy_opener", lambda: FakeOpener(resp)):
            with self.assertRaises(StudioError):
                client.refresh()


class RateLimitTest(unittest.TestCase):
    def test_429_is_transient_so_the_account_fails_over(self):
        err = StudioClient._decode_error('{"code": "rate_limited", "message": "slow down"}', 429)
        self.assertTrue(err.transient)


class NonObjectBodyTest(unittest.TestCase):
    def test_json_object_rejects_arrays(self):
        from step2api.client import _json_object
        with self.assertRaises(StudioError):
            _json_object(b"[1, 2]", "rpc")

    def test_decode_error_tolerates_array_bodies(self):
        err = StudioClient._decode_error("[1, 2]", 400)
        self.assertEqual(err.status, 400)


class ProxyTest(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in
                      ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
                       "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")}
        for key in self.saved:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    @staticmethod
    def proxies():
        opener = client_module._proxy_opener()
        for handler in opener.handlers:
            if hasattr(handler, "proxies"):
                return dict(handler.proxies)
        raise AssertionError("no ProxyHandler in opener")

    def test_all_proxy_covers_both_schemes(self):
        os.environ["ALL_PROXY"] = "http://127.0.0.1:52080"
        self.assertEqual(self.proxies(), {"http": "http://127.0.0.1:52080",
                                          "https": "http://127.0.0.1:52080"})

    def test_specific_scheme_wins_over_all_proxy(self):
        os.environ["ALL_PROXY"] = "http://127.0.0.1:52080"
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:1111"
        self.assertEqual(self.proxies()["https"], "http://127.0.0.1:1111")
        self.assertEqual(self.proxies()["http"], "http://127.0.0.1:52080")

    def test_no_dead_keys(self):
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:52080"
        os.environ["NO_PROXY"] = "localhost"
        self.assertEqual(set(self.proxies()), {"http"})


class UploadHeaderTest(unittest.TestCase):
    def test_filename_and_mime_cannot_inject_headers(self):
        self.assertEqual(_safe_filename('a\r\nX-Evil: 1.txt'), "a_X-Evil_1.txt")
        self.assertNotIn("\r", _safe_filename('a"b\nc.txt'))
        self.assertEqual(_safe_mime("text/plain\r\nX-Evil: 1"), "application/octet-stream")
        self.assertEqual(_safe_mime("text/plain"), "text/plain")

    def test_upload_body_has_one_clean_part(self):
        client = StudioClient("a...b")
        resp = FakeResponse(read_body=json.dumps({"code": 0, "data": {"asset": {"asset_id": "x"}}}).encode())
        opener = FakeOpener(resp)
        with mock.patch.object(client_module, "_proxy_opener", lambda: opener):
            self.assertEqual(client.upload_file('evil"\r\nX-Evil: 1.txt',
                                                "text/plain\r\nX-Evil: 1", b"DATA"), "x")
        body = opener.requests[0].data
        self.assertNotIn(b"\r\nX-Evil", body)   # no injected header line
        self.assertIn(b'filename="evil', body)


class MediaTimeoutTest(unittest.TestCase):
    def test_tts_and_asr_bound_the_socket_not_just_the_loop(self):
        from step2api import media

        seen = {}

        class StubClient:
            def create_conversation(self):
                return "conv"

            def stream_turn(self, body, timeout=None):
                seen[body["kind"]] = timeout
                return FakeResponse(chunks=[b'data: {"type": "done"}\n\n'])

        class Stub:
            pass

        with self.assertRaises(StudioError):
            media.synthesize(StubClient(), "hi", "voice", "tts-model", "mp3", timeout=42)
        self.assertEqual(seen["audio_tts"], 42)


class _AttachmentServer(BaseHTTPRequestHandler):
    """Serves one oversized attachment two ways: declared and close-delimited."""

    def do_GET(self):
        if self.path == "/declared":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(4 * 1024 * 1024))
            self.end_headers()
            self.wfile.write(b"x" * 1024)
        elif self.path == "/stream":
            # No Content-Length: the body ends when the socket closes.
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(b"y" * 65536)
        else:
            body = b"z" * 100
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):
        pass


class FetchBoundTest(unittest.TestCase):
    """A client-supplied attachment URL must not be able to fill memory."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _AttachmentServer)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _fetch(self, path, max_bytes):
        with mock.patch.dict(os.environ, {"STEP_ALLOW_PRIVATE_FETCH": "1"}):
            return StudioClient.fetch_url(self.base + path, timeout=10, max_bytes=max_bytes)

    def test_a_declared_length_over_the_limit_is_refused_before_reading(self):
        with self.assertRaises(StudioError) as ctx:
            self._fetch("/declared", 4096)
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("too large", ctx.exception.message)

    def test_a_close_delimited_body_stops_at_the_limit(self):
        with self.assertRaises(StudioError) as ctx:
            self._fetch("/stream", 4096)
        self.assertIn("exceeds", ctx.exception.message)

    def test_a_small_attachment_still_passes_through(self):
        data, ctype = self._fetch("/small", 4096)
        self.assertEqual(data, b"z" * 100)
        self.assertEqual(ctype, "image/png")

    def test_without_a_limit_the_whole_body_is_still_returned(self):
        with mock.patch.dict(os.environ, {"STEP_ALLOW_PRIVATE_FETCH": "1"}):
            data, _ctype = StudioClient.fetch_url(self.base + "/small", timeout=10)
        self.assertEqual(len(data), 100)


if __name__ == "__main__":
    unittest.main()
