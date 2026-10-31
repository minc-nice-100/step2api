"""Text-to-speech over the Studio `audio_tts` turn.

Studio renders speech into a signed object-storage URL and reports it as a
`media_asset_ready` event, so synthesis is: create a conversation, ask for audio,
wait for the URL, fetch the bytes.
"""

from __future__ import annotations

import base64
import os
import time
import urllib.parse
from typing import Any, Dict, Iterable, Optional, Tuple

from .client import StudioClient, StudioError, iter_sse_closing

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024

_DOC_EXT_BY_MIME = {"application/pdf": "pdf", "text/plain": "txt", "text/markdown": "md",
                    "application/json": "json", "text/csv": "csv", "text/html": "html"}

_EXT_BY_MIME = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
                "image/webp": "webp", "image/gif": "gif"}

MIME_BY_FORMAT = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "opus": "audio/ogg",
    "ogg": "audio/ogg",
    "pcm": "audio/pcm",
}

# OpenAI's voice names have no StepFun equivalent; map them onto the closest
# published voice so SDK examples work unchanged.
OPENAI_VOICE_MAP = {
    "alloy": "step_f15",
    "echo": "M02",
    "fable": "闫雨婷",
    "onyx": "钟离",
    "nova": "step_f06",
    "shimmer": "F21",
    "ash": "bingshu-1",
    "sage": "nuannuan",
    "coral": "huanhuan",
}


def resolve_voice(requested: str, catalogue: Iterable[Dict[str, Any]]) -> str:
    voices = {str(v.get("voice") or ""): v for v in catalogue or [] if isinstance(v, dict)}
    name = (requested or "").strip()
    if name in voices:
        return name
    lowered = {key.lower(): key for key in voices}
    if name.lower() in lowered:
        return lowered[name.lower()]
    mapped = OPENAI_VOICE_MAP.get(name.lower())
    if mapped and (not voices or mapped in voices):
        return mapped
    return "xiaoyue" if (not voices or "xiaoyue" in voices) else (next(iter(voices), "xiaoyue"))


def normalize_format(requested: str, supported: Iterable[str]) -> str:
    fmt = (requested or "mp3").strip().lower()
    allowed = [str(f).lower() for f in supported or ["mp3", "wav", "opus"]]
    if fmt in allowed:
        return fmt
    # OpenAI accepts aac/flac/pcm; Studio does not, so fall back to the closest container.
    return "wav" if fmt == "pcm" and "wav" in allowed else ("mp3" if "mp3" in allowed else allowed[0])


def synthesize(client: StudioClient, text: str, voice: str, model: str, fmt: str,
               timeout: float = 300.0) -> Tuple[bytes, str]:
    conversation_id = client.create_conversation()
    body = {
        "conversation_id": conversation_id,
        "kind": "audio_tts",
        "audio_tts": {
            "model": {"model_id": model, "config": {"voice": voice, "format": fmt}},
            "content": text,
        },
    }
    # Bounding the socket (not just the loop) matters: a silent stream would
    # otherwise hold the handler -- and a concurrency permit -- for upstream_timeout.
    resp = client.stream_turn(body, timeout=timeout)
    url = ""
    deadline = time.time() + timeout
    # `iter_sse_closing` closes the socket for us and raises when the stream dies before
    # `done`; without that a truncated TTS turn would look like "no audio" (a permanent
    # failure) instead of a transient upstream cut.
    try:
        for event in iter_sse_closing(resp):
            etype = event.get("type")
            data = event.get("data") or {}
            if etype == "media_asset_ready":
                url = str(data.get("url") or "")
            elif etype == "error":
                raise StudioError(str(data.get("message") or "tts failed"), status=502,
                                  biz_code=str(data.get("biz_code") or ""))
            elif etype == "done":
                break
            if time.time() > deadline:
                raise StudioError("tts timed out", status=504, transient=True)
    finally:
        # The wrapper closes on exhaustion/abandon, but an explicit close keeps the
        # socket bounded even if a caller ever holds the iterator past a `break`.
        resp.close()
    if not url:
        raise StudioError("tts produced no audio", status=502)
    audio = client.fetch_bytes(url)
    return audio, MIME_BY_FORMAT.get(fmt, "application/octet-stream")


def transcribe(client: StudioClient, asset_id: str, model: str, timeout: float = 300.0) -> str:
    """Run the `audio_asr` turn for an uploaded asset and collect the transcript."""
    conversation_id = client.create_conversation()
    body = {
        "conversation_id": conversation_id,
        "kind": "audio_asr",
        "audio_asr": {"model": {"model_id": model}, "asset_ids": [str(asset_id)]},
    }
    resp = client.stream_turn(body, timeout=timeout)
    parts = []
    deadline = time.time() + timeout
    # Same contract as TTS: a cut before `done` is a transient failure, not a short
    # transcript (returning a truncated transcript as success hides the data loss).
    try:
        for event in iter_sse_closing(resp):
            etype = event.get("type")
            data = event.get("data") or {}
            if etype == "text_delta":
                parts.append(str(data.get("delta") or ""))
            elif etype == "error":
                raise StudioError(str(data.get("message") or "asr failed"), status=502,
                                  biz_code=str(data.get("biz_code") or ""))
            elif etype == "done":
                break
            if time.time() > deadline:
                raise StudioError("asr timed out", status=504, transient=True)
    finally:
        resp.close()
    return "".join(parts).strip()


def load_image(src: str):
    """Turn an OpenAI/Anthropic image reference into (bytes, mime, filename).

    Supports `data:` URIs and public http(s) URLs; anything else is skipped.
    """
    src = (src or "").strip()
    if not src:
        return None
    if src.startswith("data:"):
        header, _, payload = src.partition(",")
        mime = (header[5:].split(";")[0] or "image/png").lower()
        if ";base64" not in header:
            return None
        try:
            data = base64.b64decode(payload, validate=False)
        except (ValueError, TypeError):
            return None
        if not data or len(data) > MAX_IMAGE_BYTES:
            return None
        return data, mime, "image." + _EXT_BY_MIME.get(mime, "png")
    if src.startswith("http://") or src.startswith("https://"):
        try:
            data, ctype = StudioClient.fetch_url(src, timeout=60.0,
                                                 max_bytes=MAX_IMAGE_BYTES)
        except StudioError:
            return None
        mime = (ctype or "image/png").split(";")[0].strip().lower()
        if not mime.startswith("image/") or not data or len(data) > MAX_IMAGE_BYTES:
            return None
        name = os.path.basename(src.split("?")[0]) or "image"
        if "." not in name:
            name += "." + _EXT_BY_MIME.get(mime, "png")
        return data, mime, name
    return None


def load_document(doc) -> Optional[Tuple[bytes, str, str]]:
    """Normalise an Anthropic document / OpenAI file part into (bytes, mime, filename)."""
    if not isinstance(doc, dict):
        return None
    data = str(doc.get("data") or "").strip()
    name = str(doc.get("name") or "document").strip() or "document"
    mime = str(doc.get("mime") or "").strip().lower()
    if not data:
        return None
    if data.startswith("data:"):
        header, _, payload = data.partition(",")
        mime = mime or (header[5:].split(";")[0].strip().lower() or "text/plain")
        try:
            raw = (base64.b64decode(payload, validate=False) if ";base64" in header
                   else urllib.parse.unquote_to_bytes(payload))
        except (ValueError, TypeError):
            return None
    elif data.startswith("http://") or data.startswith("https://"):
        try:
            raw, ctype = StudioClient.fetch_url(data, timeout=60.0,
                                                max_bytes=MAX_DOCUMENT_BYTES)
        except StudioError:
            return None
        mime = mime or (ctype or "").split(";")[0].strip().lower()
        if not name or name == "document":
            name = os.path.basename(data.split("?")[0]) or "document"
    else:
        try:
            raw = base64.b64decode(data, validate=False)
        except (ValueError, TypeError):
            return None
    if not raw or len(raw) > MAX_DOCUMENT_BYTES:
        return None
    mime = mime or "application/octet-stream"
    if "." not in name:
        name += "." + _DOC_EXT_BY_MIME.get(mime, "txt")
    return raw, mime, name
