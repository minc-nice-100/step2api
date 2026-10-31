"""Pure translation between the OpenAI wire format and Studio's event vocabulary.

No I/O here on purpose — everything in this module is unit-testable without a network.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

EFFORT_MAP = {
    "none": "low", "minimal": "low", "low": "low",
    "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh",
}

TOOL_PROTOCOL = """\
You can call tools. A tool call MUST be emitted as a single JSON object and nothing else,
exactly in this shape:

{"name": "<tool_name>", "arguments": {<arguments>}}

Rules:
- The ONLY tools that exist are the ones listed below. Never call anything else
  (no document_parser, web_search, file readers, code runners or other built-ins),
  even if another system message mentions them.
- Emit at most one tool call per turn.
- Never wrap the JSON in markdown fences and never add prose around it.
- When tool results are supplied, use them to answer the user normally.
- If no tool from the list is needed, answer the user directly as plain text.

Available tools (JSON):
{tools}
"""


# ---------------------------------------------------------------------------
# OpenAI request model
# ---------------------------------------------------------------------------

@dataclass
class Msg:
    role: str
    text: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    tool_call_id: str = ""
    images: List[str] = field(default_factory=list)
    documents: List[Dict[str, str]] = field(default_factory=list)

    def key(self) -> Tuple[str, str, str, str, str]:
        calls = json.dumps(self.tool_calls, ensure_ascii=False, sort_keys=True) if self.tool_calls else ""
        attachments = list(self.images) + [
            json.dumps(doc, ensure_ascii=False, sort_keys=True) for doc in self.documents]
        digest = ""
        if attachments:
            import hashlib

            digest = hashlib.sha256("\x00".join(attachments).encode("utf-8")).hexdigest()[:16]
        return (self.role, self.text, calls, self.tool_call_id, digest)


def _content_to_text(content: Any) -> str:
    text, _images, _docs = _content_and_images(content)
    return text


def _content_and_images(content: Any):
    """Split an OpenAI content value into (text, image refs, document refs)."""
    if content is None:
        return ("", [], [])
    if isinstance(content, str):
        return (content, [], [])
    if isinstance(content, list):
        parts: List[str] = []
        images: List[str] = []
        documents: List[Dict[str, str]] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
                continue
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype in ("text", "input_text") and part.get("text"):
                parts.append(str(part["text"]))
            elif ptype in ("image_url", "input_image"):
                url = part.get("image_url")
                url = url.get("url") if isinstance(url, dict) else url
                if url:
                    images.append(str(url))
            elif ptype in ("file", "input_file"):
                spec = part.get("file") if isinstance(part.get("file"), dict) else part
                data = spec.get("file_data") or spec.get("data") or spec.get("url") or ""
                name = spec.get("filename") or spec.get("name") or "file"
                mime = spec.get("mime_type") or spec.get("media_type") or ""
                if data:
                    documents.append({"data": str(data), "name": str(name), "mime": str(mime)})
                elif spec.get("file_id"):
                    # There is no /v1/files store to resolve the id against; dropping the
                    # part would let the model answer about a file it never received.
                    raise ValueError(
                        "file_id references are not supported (there is no /v1/files store): "
                        "inline the file as base64 file_data or pass a URL")
            elif ptype in ("audio", "input_audio", "audio_url"):
                # "[audio]" used to be substituted here, which reads as a real attachment.
                raise ValueError(
                    "inline audio input is not supported: transcribe it first via "
                    "/v1/audio/transcriptions and send the text")
            elif part.get("text"):
                parts.append(str(part["text"]))
        text = "\n".join(p for p in parts if p)
        return (text, images, documents)
    return (str(content), [], [])


def normalize_messages(raw: Any) -> List[Msg]:
    """OpenAI `messages` -> Msg, tolerating parts, tool calls and tool results."""
    out: List[Msg] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user")
        text, images, documents = _content_and_images(item.get("content"))
        calls: List[Dict[str, Any]] = []
        for call in item.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    pass
            calls.append({
                "id": str(call.get("id") or ""),
                "name": str(fn.get("name") or call.get("name") or ""),
                "arguments": args if isinstance(args, dict) else {},
            })
        if role == "tool" and item.get("tool_call_id"):
            out.append(Msg(role="tool", text=text, tool_call_id=str(item.get("tool_call_id"))))
            continue
        if role == "assistant" and calls:
            out.append(Msg(role="assistant", text=text, tool_calls=calls))
            continue
        out.append(Msg(role=role, text=text, images=images, documents=documents))
    return out


def split_system(messages: List[Msg]) -> Tuple[str, List[Msg]]:
    """Collect system/developer messages; everything else stays in order."""
    system_bits: List[str] = []
    rest: List[Msg] = []
    for msg in messages:
        if msg.role in ("system", "developer"):
            if msg.text:
                system_bits.append(msg.text)
        else:
            rest.append(msg)
    return "\n\n".join(system_bits), rest


def build_system_prompt(system_text: str, tools: Any) -> str:
    """Append the prompted-tool-calling protocol when the client declared tools."""
    tool_defs = _tool_defs(tools)
    if not tool_defs:
        return system_text
    protocol = TOOL_PROTOCOL.replace("{tools}", json.dumps(tool_defs, ensure_ascii=False, indent=1))
    return (system_text + "\n\n" + protocol).strip() if system_text else protocol


def _tool_defs(tools: Any) -> List[Dict[str, Any]]:
    defs: List[Dict[str, Any]] = []
    if not isinstance(tools, list):
        return defs
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") not in (None, "function"):
            # non-function tools (web_search, file_search…) are not supported upstream
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = fn.get("name")
        if not name:
            continue
        defs.append({
            "name": name,
            "description": fn.get("description") or "",
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return defs


# ---------------------------------------------------------------------------
# Rendering history for a fresh upstream conversation
# ---------------------------------------------------------------------------

def render_transcript(messages: List[Msg]) -> str:
    """Flatten a full history into one user turn for a brand-new conversation.

    Used only when the client's history does not match what we already sent upstream
    (edited branch, sliding window, first request after a restart).
    """
    blocks: List[str] = []
    for msg in messages:
        if msg.role == "user":
            extra = len(msg.images) + len(msg.documents)
            suffix = "\n[%d attachment(s) supplied as assets]" % extra if extra else ""
            blocks.append("### User\n" + (msg.text or "") + suffix)
        elif msg.role == "assistant":
            if msg.tool_calls:
                calls = "; ".join(
                    "%s(%s)" % (c.get("name"), json.dumps(c.get("arguments") or {}, ensure_ascii=False))
                    for c in msg.tool_calls
                )
                blocks.append("### Assistant (tool calls)\n" + calls + ("\n" + msg.text if msg.text else ""))
            else:
                blocks.append("### Assistant\n" + msg.text)
        elif msg.role == "tool":
            blocks.append("### Tool result (%s)\n%s" % (msg.tool_call_id or "?", msg.text))
        else:
            blocks.append("### %s\n%s" % (msg.role, msg.text))
    blocks.append("### Instruction\nContinue the conversation above; reply to the final user message.")
    return "\n\n".join(blocks)


def render_delta(messages: List[Msg]) -> str:
    """The tail of a matching history — what we hand upstream as the next chat turn."""
    parts: List[str] = []
    for msg in messages:
        if msg.role == "user":
            extra = len(msg.images) + len(msg.documents)
            parts.append(msg.text or ("[%d attachment(s) supplied as assets]" % extra if extra else ""))
        elif msg.role == "tool":
            parts.append("[tool result id=%s]\n%s" % (msg.tool_call_id or "?", msg.text))
        elif msg.role == "assistant":
            if msg.tool_calls:
                calls = "; ".join(
                    "%s(%s)" % (c.get("name"), json.dumps(c.get("arguments") or {}, ensure_ascii=False))
                    for c in msg.tool_calls
                )
                parts.append("[assistant tool calls] " + calls)
            if msg.text:
                parts.append(msg.text)
        else:
            parts.append(msg.text)
    return "\n\n".join(p for p in parts if p).strip()


# ---------------------------------------------------------------------------
# Sampling config
# ---------------------------------------------------------------------------

def build_chat_config(body: Dict[str, Any], model_meta: Optional[Dict[str, Any]] = None,
                      thinking: Optional[bool] = None, effort: Optional[str] = None) -> Dict[str, Any]:
    meta = model_meta or {}
    cfg: Dict[str, Any] = {}

    max_tokens = (body.get("max_completion_tokens") or body.get("max_tokens")
                  or body.get("max_output_tokens"))
    limits = {c.get("key"): c for c in meta.get("config") or [] if isinstance(c, dict)}
    max_limit = int((limits.get("max_tokens") or {}).get("max") or 64000)
    if isinstance(max_tokens, (int, float)) and max_tokens > 0:
        cfg["max_tokens"] = max(1, min(int(max_tokens), max_limit))

    for key in ("temperature", "top_p", "frequency_penalty"):
        value = body.get(key)
        bound = limits.get(key) or {}
        if isinstance(value, (int, float)):
            lo = bound.get("min")
            hi = bound.get("max")
            if isinstance(lo, (int, float)):
                value = max(value, lo)
            if isinstance(hi, (int, float)):
                value = min(value, hi)
            cfg[key] = float(value)

    thinking_meta = meta.get("thinking") or {}
    if thinking is None:
        reasoning_effort = body.get("reasoning_effort")
        if reasoning_effort is None:
            thinking = bool(thinking_meta.get("default_enabled", True))
        else:
            thinking = str(reasoning_effort).lower() not in ("none", "off", "disabled")
            if effort is None:
                effort = EFFORT_MAP.get(str(reasoning_effort).lower())
    cfg["thinking"] = bool(thinking)
    if cfg["thinking"]:
        allowed = [_effort_name(o) for o in (thinking_meta.get("effort_options") or [])]
        allowed = [name for name in allowed if name]
        chosen = _effort_name(effort) or _effort_name(thinking_meta.get("default_effort")) or "medium"
        if allowed and chosen not in allowed:
            # clamp to the nearest advertised level (1=low, 2=medium, 3=high)
            rank = _EFFORT_RANK.get(chosen, 2)
            chosen = min(allowed, key=lambda name: abs(_EFFORT_RANK.get(name, 2) - rank))
        cfg["effort"] = chosen
    return cfg


_EFFORT_RANK = {"low": 1, "medium": 2, "high": 3, "xhigh": 4}


def _effort_name(value: Any) -> Optional[str]:
    """Studio advertises effort levels as 1/2/3 in the catalogue and as words on the wire."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("1", "low", "minimal", "fast"):
        return "low"
    if text in ("2", "medium", "mid", "default"):
        return "medium"
    if text in ("3", "high", "deep"):
        return "high"
    if text in ("4", "xhigh", "max"):
        return "xhigh"
    return EFFORT_MAP.get(text)


def effective_model(model: str, available: Iterable[str]) -> str:
    """Accept `stepfun/step-5-preview`, `step-5-preview` and friends."""
    name = (model or "").strip()
    for prefix in ("stepfun/", "step/", "studio/"):
        if name.lower().startswith(prefix):
            name = name[len(prefix):]
    known = list(available)
    if name in known or not known:
        return name
    lowered = {k.lower(): k for k in known}
    if name.lower() in lowered:
        return lowered[name.lower()]
    return name


# OpenAI SDKs ship fixed audio model names; Studio has its own. Forwarding them verbatim
# gets `MODEL_ACCESS_DENIED`, so the well-known aliases resolve to the upstream default.
TTS_MODEL_ALIASES = frozenset({"tts-1", "tts-1-hd", "gpt-4o-mini-tts", "gpt-4o-tts"})
ASR_MODEL_ALIASES = frozenset({"whisper-1", "gpt-4o-transcribe", "gpt-4o-mini-transcribe"})


def effort_for_thinking_budget(block: Any) -> str:
    """Anthropic spells reasoning depth as `thinking.budget_tokens`; Studio wants a level.

    The bands are a judgement call, not a protocol fact — the point is that a client asking
    for 32k of thinking must not silently get the default effort. An unknown or missing
    budget returns "" so the caller keeps the model's default.
    """
    if not isinstance(block, dict):
        return ""
    try:
        budget = int(block.get("budget_tokens") or 0)
    except (TypeError, ValueError):
        return ""
    if budget <= 0:
        return ""
    if budget < 4096:
        return "low"
    if budget < 16384:
        return "medium"
    return "high"


def map_audio_model(model: str, default: str, aliases: Iterable[str]) -> str:
    """Resolve an audio model name: OpenAI aliases give way to the configured default.

    An empty name also means "whatever the account is configured for". Any other value
    passes through (after provider-prefix stripping) so a typo still surfaces upstream.
    """
    name = (model or "").strip()
    for prefix in ("openai/", "stepfun/", "step/", "studio/"):
        if name.lower().startswith(prefix):
            name = name[len(prefix):]
    if not name:
        return default
    return default if name.lower() in {a.lower() for a in aliases} else name


# ---------------------------------------------------------------------------
# Upstream events -> OpenAI chunks
# ---------------------------------------------------------------------------

def _new_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex[:24]


def canonical_tool_calls_json(calls) -> str:
    """Serialise tool calls exactly the way ``Msg.key()`` does.

    Upstream tool-call arguments arrive as JSON *strings* while client-resent
    ``tool_calls`` are normalised to dicts; both must fingerprint identically or the
    conversation-affinity prefix check silently breaks after every tool call.
    """
    out = []
    for call in calls:
        arguments = call.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except ValueError:
                # normalize_messages() maps the same broken string to {}; both sides
                # of the prefix comparison must fingerprint identically.
                arguments = {}
        out.append({"id": str(call.get("id") or ""),
                    "name": str(call.get("name") or ""),
                    "arguments": arguments if isinstance(arguments, dict) else {}})
    return json.dumps(out, ensure_ascii=False, sort_keys=True)


class StopFilter:
    """Hold back the tail of the stream so a stop sequence can never leak.

    Studio has no `stop` parameter, so the gateway cuts the text itself. Up to
    ``max(len(stop)) - 1`` characters are buffered between chunks because they might
    turn into the start of a stop sequence.
    """

    def __init__(self, stops: Optional[List[str]]) -> None:
        self.stops = [s for s in (stops or []) if s]
        self.keep = max((len(s) - 1 for s in self.stops), default=0)
        self.buf = ""
        self.hit = False
        self.matched: Optional[str] = None

    def feed(self, text: str) -> str:
        # After a hit, nothing else may reach the client: the model keeps generating
        # past the sequence, and those later deltas used to leak through verbatim.
        if self.hit or not text:
            return ""
        if not self.stops:
            return text
        self.buf += text
        hits = [i for i in (self.buf.find(stop) for stop in self.stops) if i >= 0]
        if hits:
            cut = min(hits)
            out = self.buf[:cut]
            self.matched = next(stop for stop in self.stops if self.buf.startswith(stop, cut))
            self.buf = ""
            self.hit = True
            return out
        if self.keep and len(self.buf) > self.keep:
            cut = len(self.buf) - self.keep
            out = self.buf[:cut]
            self.buf = self.buf[cut:]
            return out
        return ""

    def flush(self) -> str:
        out = "" if self.hit else self.buf
        self.buf = ""
        return out


class Translator:
    """Consumes Studio SSE events and produces OpenAI chat.completion(.chunk) objects."""

    def __init__(self, model: str, chunked: bool = True, include_reasoning: bool = True,
                 include_usage: bool = False, allowed_tools: Optional[set] = None,
                 stop: Optional[List[str]] = None) -> None:
        self.model = model
        self.chunked = chunked
        self.include_reasoning = include_reasoning
        self.include_usage = include_usage
        self.id = _new_id()
        self.created = int(time.time())
        self.usage: Dict[str, int] = {}
        self.finish_reason: Optional[str] = None
        self.text_parts: List[str] = []
        self.reasoning_parts: List[str] = []
        self.tool_calls: List[Dict[str, Any]] = []
        self._tool_idx: Dict[int, int] = {}
        self._ignored_blocks: set = set()
        self.allowed_tools = allowed_tools
        self.stop_filter = StopFilter(stop)
        self._sent_role = False
        self.message_id = ""
        self.error: Optional[Dict[str, Any]] = None
        self.status = ""

    # -- helpers ---------------------------------------------------------
    def _chunk(self, delta: Dict[str, Any], finish_reason: Optional[str] = None,
               usage: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        if usage is not None:
            payload["usage"] = usage
        return payload

    def _role_chunk(self) -> Optional[Dict[str, Any]]:
        if self._sent_role or not self.chunked:
            return None
        self._sent_role = True
        return self._chunk({"role": "assistant", "content": ""})

    def _usage_payload(self) -> Dict[str, Any]:
        prompt = self.usage.get("tokens_in", 0)
        completion = self.usage.get("tokens_out", 0)
        cached = self.usage.get("cached_tokens", 0)
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "prompt_tokens_details": {"cached_tokens": cached},
        }

    # -- event intake ----------------------------------------------------
    def feed(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Return zero or more OpenAI chunks for one upstream event."""
        etype = str(event.get("type") or "")
        data = event.get("data") or {}
        out: List[Dict[str, Any]] = []

        if etype == "message_start":
            self.message_id = str(data.get("message_id") or "")
            role = self._role_chunk()
            if role:
                out.append(role)
            return out

        if etype == "content_block_start":
            role = self._role_chunk()
            if role:
                out.append(role)
            if data.get("block_type") == "CONTENT_BLOCK_TYPE_REASONING" and self.include_reasoning:
                out.append(self._chunk({"reasoning_content": ""}))
            return out

        if etype == "reasoning_delta":
            delta = str(data.get("delta") or "")
            if delta:
                self.reasoning_parts.append(delta)
                if self.include_reasoning:
                    out.append(self._chunk({"reasoning_content": delta}))
            return out

        if etype == "text_delta":
            delta = str(data.get("delta") or "")
            if not delta or self.stop_filter.hit:
                return out
            delta = self.stop_filter.feed(delta)
            if not delta:
                return out
            self.text_parts.append(delta)
            out.append(self._chunk({"content": delta}))
            return out

        if etype == "tool_call_start":
            name = str(data.get("tool_name") or "")
            if self.allowed_tools is not None and name not in self.allowed_tools:
                # The model reached for one of Studio's built-in tools; the client never
                # declared it, so the call cannot be executed on this side. Drop it.
                self._ignored_blocks.add(int(data.get("block_index", 0)))
                return out
            role = self._role_chunk()
            if role:
                out.append(role)
            index = len(self.tool_calls)
            self._tool_idx[int(data.get("block_index", index))] = index
            call = {
                "id": str(data.get("tool_call_id") or "call_" + uuid.uuid4().hex[:12]),
                "name": str(data.get("tool_name") or ""),
                "arguments": str(data.get("arguments_json") or ""),
            }
            self.tool_calls.append(call)
            out.append(self._chunk({"tool_calls": [{
                "index": index, "id": call["id"], "type": "function",
                "function": {"name": call["name"], "arguments": call["arguments"]},
            }]}))
            return out

        if etype == "tool_call_delta":
            if int(data.get("block_index", 0)) in self._ignored_blocks:
                return out
            index = self._tool_idx.get(int(data.get("block_index", 0)))
            if index is None:
                return out
            fragment = str(data.get("progress_text") or data.get("delta") or "")
            if not fragment:
                return out
            self.tool_calls[index]["arguments"] += fragment
            out.append(self._chunk({"tool_calls": [{
                "index": index, "function": {"arguments": fragment},
            }]}))
            return out

        if etype == "tool_call_end":
            return out

        if etype == "message_stop":
            self.status = str(data.get("status") or "")
            return out

        if etype == "final":
            raw = data.get("usage") or {}
            self.usage = {
                "tokens_in": _as_int(raw.get("tokens_in")),
                "tokens_out": _as_int(raw.get("tokens_out")),
                "cached_tokens": _as_int(raw.get("cached_tokens")),
            }
            return out

        if etype == "error":
            self.error = {
                "code": str(data.get("code") or ""),
                "biz_code": str(data.get("biz_code") or ""),
                "message": str(data.get("message") or "upstream error"),
            }
            return out

        # heartbeat / done / unknown events carry nothing for the client
        return out

    # -- finish ----------------------------------------------------------
    def _resolve_finish_reason(self, max_tokens: Optional[int] = None) -> str:
        if self.tool_calls:
            return "tool_calls"
        if self.status and self.status not in ("MESSAGE_STATUS_SUCCESS", "MESSAGE_STATUS_PARTIAL"):
            return "stop"
        if max_tokens and self.usage.get("tokens_out", 0) >= max_tokens:
            return "length"
        return "stop"

    def finish(self, max_tokens: Optional[int] = None,
               tools_given: bool = False) -> List[Dict[str, Any]]:
        """Emit the terminal chunk(s). Converts legacy JSON-in-text tool calls if needed."""
        out: List[Dict[str, Any]] = []
        self.finish_reason = self._resolve_finish_reason(max_tokens)
        if tools_given and not self.tool_calls:
            parsed = parse_text_tool_call("".join(self.text_parts))
            if parsed:
                call = {"id": parsed["id"], "name": parsed["name"], "arguments": json.dumps(parsed["arguments"], ensure_ascii=False)}
                self.tool_calls.append(call)
                self.finish_reason = "tool_calls"
                out.append(self._chunk({"tool_calls": [{
                    "index": 0, "id": call["id"], "type": "function",
                    "function": {"name": call["name"], "arguments": call["arguments"]},
                }]}))
                self.text_parts = []
        tail = self.stop_filter.flush()
        if tail:
            self.text_parts.append(tail)
            if self.chunked:
                out.append(self._chunk({"content": tail}))
        if self.chunked:
            out.append(self._chunk({}, self.finish_reason,
                                   self._usage_payload() if self.include_usage else None))
        return out

    # -- aggregation -----------------------------------------------------
    def message(self) -> Dict[str, Any]:
        text = "".join(self.text_parts)
        # OpenAI clients expect a string even for an empty answer; null is reserved for
        # pure tool-call turns.
        msg: Dict[str, Any] = {"role": "assistant", "content": text}
        if self.reasoning_parts and self.include_reasoning:
            msg["reasoning_content"] = "".join(self.reasoning_parts)
        if self.tool_calls:
            msg["tool_calls"] = [{
                "id": call["id"],
                "type": "function",
                "function": {"name": call["name"], "arguments": call["arguments"]},
            } for call in self.tool_calls]
            if not text:
                msg["content"] = None
        return msg

    def assistant_key(self) -> Tuple[str, str, str, str, str]:
        """Fingerprint of the reply, used to detect append-only client histories.

        Must match ``Msg.key()`` (5 elements, arguments normalised to dicts) — a shape
        mismatch silently disables session reuse for that protocol.
        """
        calls = canonical_tool_calls_json(self.tool_calls) if self.tool_calls else ""
        return ("assistant", "".join(self.text_parts), calls, "", "")

    def completion(self, max_tokens: Optional[int] = None, tools_given: bool = False) -> Dict[str, Any]:
        self.finish(max_tokens=max_tokens, tools_given=tools_given)  # resolves finish_reason
        return {
            "id": self.id,
            "object": "chat.completion",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "message": self.message(), "finish_reason": self.finish_reason}],
            "usage": self._usage_payload(),
        }


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_text_tool_call(text: str) -> Optional[Dict[str, Any]]:
    """Last-resort recovery when the model printed a tool call as prose/markdown."""
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate).strip()
    match = _JSON_OBJECT_RE.search(candidate)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    if "tool_calls" in payload and isinstance(payload["tool_calls"], list) and payload["tool_calls"]:
        first = payload["tool_calls"][0]
        name = first.get("name") or (first.get("function") or {}).get("name")
        args = first.get("arguments") or (first.get("function") or {}).get("arguments") or {}
        if not name:
            return None
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        return {"id": str(first.get("id") or "call_" + uuid.uuid4().hex[:12]),
                "name": str(name), "arguments": args if isinstance(args, dict) else {}}
    name = payload.get("name") or (payload.get("function") or {}).get("name")
    args = payload.get("arguments") or payload.get("parameters") or (payload.get("function") or {}).get("arguments")
    if not name:
        return None
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    return {"id": "call_" + uuid.uuid4().hex[:12], "name": str(name),
            "arguments": args if isinstance(args, dict) else {}}


def map_upstream_error(data: Dict[str, Any]):
    """Turn a Studio `error` event into a StudioError with the right status class.

    Content-policy refusals are deterministic: retrying or switching accounts never helps,
    so they surface as 400 with a hint instead of a 502.
    """
    from .client import StudioError

    message = str(data.get("message") or "upstream error")
    biz = str(data.get("biz_code") or "")
    code = str(data.get("code") or "")
    lowered = message.lower()
    if (biz in ("CENSORED", "CONTENT_POLICY") or "datainspectionfailed" in lowered
            or "inappropriate content" in lowered or "safety" in biz.lower()):
        return StudioError("upstream content policy rejection: %s (retrying will not help)"
                           % message, status=400, code="content_policy_blocked", biz_code=biz)
    if biz in ("INVALID_MODEL", "MODEL_ACCESS_DENIED", "INVALID_REQUEST", "BAD_REQUEST"):
        return StudioError(message, status=400, code=code or biz, biz_code=biz)
    return StudioError(message, status=502, code=code, biz_code=biz)


def error_payload(exc: Exception, model: str = "") -> Dict[str, Any]:
    """OpenAI-shaped error body."""
    status = getattr(exc, "status", None) or 500
    code = getattr(exc, "code", "") or ""
    biz = getattr(exc, "biz_code", "") or ""
    message = getattr(exc, "message", None) or str(exc)
    kind = "invalid_request_error" if 400 <= int(status) < 500 else "upstream_error"
    payload = {
        "error": {
            "message": message,
            "type": kind,
            "param": None,
            "code": biz or code or None,
        }
    }
    if model:
        payload["model"] = model
    return payload
