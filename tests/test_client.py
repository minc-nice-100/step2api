"""Tests for the upstream SSE reader."""

import unittest

from step2api.client import StudioError, iter_sse, iter_sse_closing


class FakeResponse:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False

    def read1(self, _size):
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        self.closed = True


def frame(payload: str) -> bytes:
    return ("event: message\ndata: %s\n\n" % payload).encode()


class SseTest(unittest.TestCase):
    def test_parses_frames(self):
        resp = FakeResponse([frame('{"type":"a"}') + frame('{"type":"b"}')])
        events = list(iter_sse(resp))
        self.assertEqual([e["type"] for e in events], ["a", "b"])
        self.assertFalse(resp.closed)

    def test_handles_split_frames(self):
        blob = frame('{"type":"a","data":{"x":"你好"}}')
        resp = FakeResponse([blob[:5], blob[5:17], blob[17:]])
        events = list(iter_sse(resp))
        self.assertEqual(events[0]["data"]["x"], "你好")

    def test_ignores_heartbeats_and_comments(self):
        resp = FakeResponse([b": ping\n\n" + frame('{"type":"done"}')])
        events = list(iter_sse(resp))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "done")

    def test_closing_wrapper_closes_on_exhaust(self):
        resp = FakeResponse([frame('{"type":"a"}') + frame('{"type":"done"}')])
        self.assertEqual(len(list(iter_sse_closing(resp))), 2)
        self.assertTrue(resp.closed)

    def test_closing_wrapper_flags_a_missing_done(self):
        """A stream that dies before `done` must not look like a finished answer."""
        resp = FakeResponse([frame('{"type":"a"}')])
        with self.assertRaises(StudioError):
            list(iter_sse_closing(resp))
        self.assertTrue(resp.closed)

    def test_closing_wrapper_accepts_error_as_a_terminal_frame(self):
        """`error` ends a turn too; it must not be followed by a bogus StudioError."""
        resp = FakeResponse([frame('{"type":"error","data":{"message":"boom"}}')])
        events = list(iter_sse_closing(resp))
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertTrue(resp.closed)

    def test_closing_wrapper_flags_a_heartbeat_only_stream(self):
        """A stream that only ever sent heartbeats was cut, not completed."""
        resp = FakeResponse([frame('{"type":"heartbeat","seq":0,"data":{}}')])
        with self.assertRaises(StudioError):
            list(iter_sse_closing(resp))
        self.assertTrue(resp.closed)

    def test_closing_wrapper_accepts_an_unterminated_last_frame(self):
        """A socket that dies right after `done` (no trailing blank line) still completed."""
        resp = FakeResponse([frame('{"type":"a"}') + b'data: {"type":"done"}'])
        self.assertEqual([e["type"] for e in iter_sse_closing(resp)], ["a", "done"])

    def test_closing_wrapper_flags_an_empty_stream(self):
        """A 200 with no events is a cut, not an empty but finished answer."""
        resp = FakeResponse([])
        with self.assertRaises(StudioError):
            list(iter_sse_closing(resp))
        self.assertTrue(resp.closed)

    def test_closing_wrapper_closes_on_abandon(self):
        resp = FakeResponse([frame('{"type":"a"}'), frame('{"type":"b"}')])
        gen = iter_sse_closing(resp)
        next(gen)
        gen.close()
        self.assertTrue(resp.closed)


if __name__ == "__main__":
    unittest.main()
