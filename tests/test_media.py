"""Unit tests for the speech helpers."""

import json
import unittest

import base64

from step2api.client import StudioError
from step2api.media import (load_document, load_image, normalize_format, resolve_voice,
                            synthesize, transcribe)

VOICES = [{"voice": "xiaoyue"}, {"voice": "step_f06"}, {"voice": "M02"}, {"voice": "F21"}]


class VoiceTest(unittest.TestCase):
    def test_known_voice_passthrough(self):
        self.assertEqual(resolve_voice("step_f06", VOICES), "step_f06")

    def test_case_insensitive(self):
        self.assertEqual(resolve_voice("M02", VOICES), "M02")
        self.assertEqual(resolve_voice("m02", VOICES), "M02")

    def test_openai_alias(self):
        self.assertEqual(resolve_voice("nova", VOICES), "step_f06")
        self.assertEqual(resolve_voice("shimmer", VOICES), "F21")

    def test_unknown_falls_back(self):
        self.assertEqual(resolve_voice("does-not-exist", VOICES), "xiaoyue")
        self.assertEqual(resolve_voice("", VOICES), "xiaoyue")

    def test_empty_catalogue_still_maps_openai_names(self):
        # with no catalogue to validate against, the alias mapping is the best guess
        self.assertEqual(resolve_voice("nova", []), "step_f06")
        self.assertEqual(resolve_voice("unknown", []), "xiaoyue")


class FormatTest(unittest.TestCase):
    def test_supported(self):
        self.assertEqual(normalize_format("wav", ["mp3", "wav"]), "wav")
        self.assertEqual(normalize_format("OPUS", ["mp3", "opus"]), "opus")

    def test_unsupported_maps_to_mp3(self):
        self.assertEqual(normalize_format("aac", ["mp3", "wav"]), "mp3")
        self.assertEqual(normalize_format("flac", ["mp3"]), "mp3")

    def test_pcm_prefers_wav(self):
        self.assertEqual(normalize_format("pcm", ["mp3", "wav"]), "wav")

    def test_default(self):
        self.assertEqual(normalize_format("", ["mp3", "wav"]), "mp3")


class LoadImageTest(unittest.TestCase):
    def test_data_uri(self):
        blob = b"\x89PNG\r\n\x1a\n" + b"0" * 16
        uri = "data:image/png;base64," + base64.b64encode(blob).decode()
        loaded = load_image(uri)
        self.assertEqual(loaded[0], blob)
        self.assertEqual(loaded[1], "image/png")
        self.assertTrue(loaded[2].endswith(".png"))

    def test_jpeg_extension(self):
        uri = "data:image/jpeg;base64," + base64.b64encode(b"x" * 8).decode()
        self.assertTrue(load_image(uri)[2].endswith(".jpg"))

    def test_rejects_garbage(self):
        self.assertIsNone(load_image(""))
        self.assertIsNone(load_image("ftp://example.com/a.png"))
        self.assertIsNone(load_image("data:image/png;base64,"))
        self.assertIsNone(load_image("data:text/plain,hello"))


class LoadDocumentTest(unittest.TestCase):
    def test_data_uri(self):
        uri = "data:text/plain;base64," + base64.b64encode("暗号 PINEAPPLE".encode()).decode()
        raw, mime, name = load_document({"data": uri, "name": "memo"})
        self.assertEqual(raw.decode("utf-8"), "暗号 PINEAPPLE")
        self.assertEqual(mime, "text/plain")
        self.assertTrue(name.endswith(".txt"))

    def test_bare_base64(self):
        raw, mime, name = load_document({"data": base64.b64encode(b"%PDF-1.4").decode(),
                                         "name": "a.pdf", "mime": "application/pdf"})
        self.assertEqual(raw, b"%PDF-1.4")
        self.assertEqual(name, "a.pdf")

    def test_rejects_empty_and_garbage(self):
        self.assertIsNone(load_document({}))
        self.assertIsNone(load_document({"data": ""}))
        self.assertIsNone(load_document({"data": "data:text/plain;base64,"}))



class _SseResp:
    """The part of an upstream response `iter_sse` touches."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def read1(self, _size=-1):
        return self._chunks.pop(0) if self._chunks else b""

    def close(self):
        self.closed = True

    def isclosed(self):
        return self.closed


def _sse(event, **data):
    body = dict(data, type=event)
    return ("data: " + json.dumps(body) + chr(10) + chr(10)).encode()


class _MediaClient:
    def __init__(self, chunks):
        self.chunks = chunks
        self.resp = None

    def create_conversation(self):
        return "conv"

    def stream_turn(self, _body, timeout=None):
        self.resp = _SseResp(self.chunks)
        return self.resp

    def fetch_bytes(self, url):
        return b"AUDIO"


class MediaTerminatorTest(unittest.TestCase):
    """A cut before `done` must not be reported as a finished TTS/ASR turn."""

    def test_tts_happy_path_returns_audio(self):
        client = _MediaClient([
            _sse("media_asset_start"),
            _sse("media_asset_ready", data={"url": "https://cdn.test/a.mp3"}),
            _sse("final"),
            _sse("done"),
        ])
        audio, mime = synthesize(client, "hi", "voice", "tts-model", "mp3")
        self.assertEqual(audio, b"AUDIO")
        self.assertEqual(mime, "audio/mpeg")
        self.assertTrue(client.resp.closed)

    def test_tts_cut_before_done_is_transient(self):
        client = _MediaClient([
            _sse("media_asset_ready", data={"url": "https://cdn.test/a.mp3"}),
        ])
        with self.assertRaises(StudioError) as ctx:
            synthesize(client, "hi", "voice", "tts-model", "mp3")
        self.assertTrue(ctx.exception.transient)
        self.assertTrue(client.resp.closed)

    def test_asr_cut_before_done_does_not_return_a_partial_transcript(self):
        client = _MediaClient([_sse("text_delta", data={"delta": "第一句"})])
        with self.assertRaises(StudioError) as ctx:
            transcribe(client, "asset", "asr-model")
        self.assertTrue(ctx.exception.transient)
        self.assertTrue(client.resp.closed)

    def test_asr_complete_stream_returns_the_transcript(self):
        client = _MediaClient([_sse("text_delta", data={"delta": "第一句"}),
                               _sse("text_delta", data={"delta": "，第二句"}),
                               _sse("done")])
        self.assertEqual(transcribe(client, "asset", "asr-model"), "第一句，第二句")


if __name__ == "__main__":
    unittest.main()
