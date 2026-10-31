"""Protocol adapters: Anthropic Messages and OpenAI Responses on top of Studio events.

Both adapters consume the same upstream event stream as `translate.Translator` but emit
their own wire format. Kept separate so each protocol stays readable.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from typing import Any, Dict, List, Optional

from .translate import (Msg, StopFilter, Translator, _content_and_images, _content_to_text,
                        canonical_tool_calls_json)


def _new_message_id() -> str:
    return "msg_" + uuid.uuid4().hex[:24]


# ---------------------------------------------------------------------------
# Anthropic -> internal
# ---------------------------------------------------------------------------

def is_server_tool(tool: Dict[str, Any]) -> bool:
    """True for provider built-ins (web search, computer use, ...), not client functions.

    Custom tools carry no `type` (Anthropic) or `type: "function"` (OpenAI). Anything
    else runs on the provider side and must never enter the prompted tool catalogue:
    the model would emit a call the client cannot answer.
    """
    ttype = str(tool.get("type") or "").strip().lower()
    return bool(ttype) and ttype not in ("function", "custom")


def has_web_search(raw_tools) -> bool:
    """Whether the client asked for built-in web search."""
    for tool in raw_tools or []:
        if not isinstance(tool, dict):
            continue
        ttype = str(tool.get("type") or "").strip().lower()
        if ttype.startswith("web_search"):
            return True
        if not ttype and str(tool.get("name") or "").strip().lower() == "web_search":
            return True
    return False


def anthropic_to_internal(body: Dict[str, Any]) -> tuple[List[Msg], str, List[Dict[str, Any]]]:
    """Return (messages, system_text, openai_style_tools)."""
    system_bits: List[str] = []
    system = body.get("system")
    if isinstance(system, str):
        system_bits.append(system)
    elif isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                system_bits.append(str(block["text"]))
            elif isinstance(block, str):
                system_bits.append(block)

    messages: List[Msg] = []
    for item in body.get("messages") or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user")
        content = item.get("content")
        if isinstance(content, str):
            messages.append(Msg(role=role, text=content))
            continue
        if not isinstance(content, list):
            continue
        text_parts: List[str] = []
        images: List[str] = []
        documents: List[Dict[str, str]] = []
        tool_calls: List[Dict[str, Any]] = []
        tool_results: List[Msg] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                text_parts.append(str(block["text"]))
            elif btype == "thinking" and block.get("thinking"):
                continue  # never replayed upstream
            elif btype == "tool_use":
                tool_calls.append({
                    "id": str(block.get("id") or ""),
                    "name": str(block.get("name") or ""),
                    "arguments": block.get("input") if isinstance(block.get("input"), dict) else {},
                })
            elif btype == "tool_result":
                tool_results.append(Msg(role="tool",
                                        text=_content_to_text(block.get("content")),
                                        tool_call_id=str(block.get("tool_use_id") or "")))
            elif btype == "image":
                source = block.get("source") or {}
                if source.get("type") == "base64" and source.get("data"):
                    images.append("data:%s;base64,%s" % (
                        source.get("media_type") or "image/png", source["data"]))
                elif source.get("type") == "url" and source.get("url"):
                    images.append(str(source["url"]))
            elif btype == "document":
                source = block.get("source") or {}
                title = block.get("title") or "document"
                if source.get("type") == "base64" and source.get("data"):
                    documents.append({"data": source["data"], "name": title,
                                      "mime": source.get("media_type") or "application/pdf"})
                elif source.get("type") == "text" and source.get("data"):
                    encoded = base64.b64encode(str(source["data"]).encode("utf-8")).decode("ascii")
                    documents.append({"data": "data:%s;base64,%s"
                                      % (source.get("media_type") or "text/plain", encoded),
                                      "name": title, "mime": source.get("media_type") or "text/plain"})
                elif source.get("type") == "url" and source.get("url"):
                    documents.append({"data": str(source["url"]), "name": title, "mime": ""})
        if tool_results:
            messages.extend(tool_results)
        if text_parts or tool_calls or images or documents:
            messages.append(Msg(role=role, text="\n".join(text_parts),
                                tool_calls=tool_calls, images=images, documents=documents))

    tools: List[Dict[str, Any]] = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict) or is_server_tool(tool):
            continue
        name = tool.get("name")
        if not name:
            continue
        tools.append({"type": "function", "function": {
            "name": str(name),
            "description": tool.get("description") or "",
            "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
        }})
    return messages, "\n\n".join(system_bits), tools


class AnthropicAdapter:
    """Studio events -> Anthropic Messages SSE events / response object."""

    def __init__(self, model: str, expose_thinking: bool = False,
                 allowed_tools=None, stop_sequences=None) -> None:
        self.model = model
        self.allowed_tools = allowed_tools
        # Studio has no stop parameter either: cut the text client-side, same as OpenAI.
        self.stop_filter = StopFilter(stop_sequences)
        self._ignored_blocks = set()
        self.expose_thinking = expose_thinking
        self.id = _new_message_id()
        self.created = int(time.time())
        self._started = False
        self._block_index = -1
        self._open_block: Optional[str] = None  # "text" | "thinking" | "tool_use"
        self._blocks: List[Dict[str, Any]] = []
        self._text = ""
        self._thinking = ""
        self._tools: List[Dict[str, Any]] = []
        self._tool_by_block: Dict[int, Dict[str, int]] = {}
        self.usage = {"tokens_in": 0, "tokens_out": 0, "cached_tokens": 0}
        self.message_id = ""
        self.stop_reason = "end_turn"
        self.status = ""
        self.error: Optional[Dict[str, Any]] = None

    # -- event helpers ---------------------------------------------------
    def _start(self) -> List[Dict[str, Any]]:
        if self._started:
            return []
        self._started = True
        return [{
            "event": "message_start",
            "data": {"type": "message_start", "message": {
                "id": self.id, "type": "message", "role": "assistant", "model": self.model,
                "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }},
        }]

    def _close_block(self) -> List[Dict[str, Any]]:
        if self._open_block is None:
            return []
        index = self._block_index
        self._open_block = None
        return [{"event": "content_block_stop", "data": {"type": "content_block_stop", "index": index}}]

    def _open(self, kind: str, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        out = self._close_block()
        self._block_index += 1
        self._open_block = kind
        self._blocks.append(payload)
        out.append({"event": "content_block_start", "data": {
            "type": "content_block_start", "index": self._block_index, "content_block": payload}})
        return out

    # -- intake ----------------------------------------------------------
    def feed(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        etype = str(event.get("type") or "")
        data = event.get("data") or {}
        out: List[Dict[str, Any]] = []

        if etype == "message_start":
            self.message_id = str(data.get("message_id") or "")
            return self._start()

        if etype == "content_block_start":
            block_type = data.get("block_type")
            if block_type == "CONTENT_BLOCK_TYPE_REASONING":
                if self.expose_thinking:
                    out += self._open("thinking", {"type": "thinking", "thinking": ""})
            elif block_type == "CONTENT_BLOCK_TYPE_TEXT":
                out += self._open("text", {"type": "text", "text": ""})
            return out

        if etype == "reasoning_delta":
            if not self.expose_thinking:
                return out
            delta = str(data.get("delta") or "")
            if not delta:
                return out
            if self._open_block != "thinking":
                out += self._open("thinking", {"type": "thinking", "thinking": ""})
            self._thinking += delta
            out.append({"event": "content_block_delta", "data": {
                "type": "content_block_delta", "index": self._block_index,
                "delta": {"type": "thinking_delta", "thinking": delta}}})
            return out

        if etype == "text_delta":
            out += self._start()
            delta = self.stop_filter.feed(str(data.get("delta") or ""))
            if not delta:
                return out
            if self._open_block != "text":
                out += self._open("text", {"type": "text", "text": ""})
            self._text += delta
            out.append({"event": "content_block_delta", "data": {
                "type": "content_block_delta", "index": self._block_index,
                "delta": {"type": "text_delta", "text": delta}}})
            return out

        if etype == "tool_call_start":
            name = str(data.get("tool_name") or "")
            if self.allowed_tools is not None and name not in self.allowed_tools:
                self._ignored_blocks.add(int(data.get("block_index", 0)))
                return out
            out += self._start()
            call = {"id": str(data.get("tool_call_id") or "toolu_" + uuid.uuid4().hex[:16]),
                    "name": str(data.get("tool_name") or ""), "arguments": str(data.get("arguments_json") or "")}
            out += self._open("tool_use", {"type": "tool_use", "id": call["id"],
                                           "name": call["name"], "input": {}})
            self._tool_by_block[int(data.get("block_index", 0))] = {
                "slot": len(self._tools), "index": self._block_index}
            self._tools.append(call)
            if call["arguments"]:
                out.append({"event": "content_block_delta", "data": {
                    "type": "content_block_delta", "index": self._block_index,
                    "delta": {"type": "input_json_delta", "partial_json": call["arguments"]}}})
            return out

        if etype == "tool_call_delta":
            block = int(data.get("block_index", 0))
            if block in self._ignored_blocks:
                return out
            target = self._tool_by_block.get(block)
            fragment = str(data.get("progress_text") or data.get("delta") or "")
            if not target or not fragment:
                return out
            self._tools[target["slot"]]["arguments"] += fragment
            out.append({"event": "content_block_delta", "data": {
                "type": "content_block_delta", "index": target["index"],
                "delta": {"type": "input_json_delta", "partial_json": fragment}}})
            return out

        if etype == "tool_call_end":
            return out

        if etype == "message_stop":
            self.status = str(data.get("status") or "")
            return out

        if etype == "final":
            raw = data.get("usage") or {}
            self.usage = {"tokens_in": _int(raw.get("tokens_in")),
                          "tokens_out": _int(raw.get("tokens_out")),
                          "cached_tokens": _int(raw.get("cached_tokens"))}
            return out

        if etype == "error":
            self.error = {"type": "error", "error": {
                "type": "api_error", "message": str(data.get("message") or "upstream error")}}
            return out
        return out

    # -- finish ----------------------------------------------------------
    @property
    def finish_reason(self) -> str:
        """Usage-log view: the protocol's own stop reason (end_turn/tool_use/...)."""
        return self.stop_reason

    def assistant_key(self):
        calls = canonical_tool_calls_json(self._tools) if self._tools else ""
        return ("assistant", self._text, calls, "", "")

    def _wire_usage(self) -> Dict[str, int]:
        # Anthropic reports cached prompt tokens separately, and `input_tokens` must NOT
        # include them (Studio's tokens_in does). Clients such as Claude Code display
        # cache reads, and double counting would inflate their cost view.
        cached = int(self.usage.get("cached_tokens", 0) or 0)
        total_in = int(self.usage.get("tokens_in", 0) or 0)
        prompt = max(0, total_in - cached) if cached else total_in
        return {"input_tokens": prompt,
                "output_tokens": int(self.usage.get("tokens_out", 0) or 0),
                "cache_read_input_tokens": cached,
                "cache_creation_input_tokens": 0}

    def _resolve_stop_reason(self, max_tokens: Optional[int]) -> str:
        if self._tools:
            return "tool_use"
        if isinstance(max_tokens, int) and max_tokens > 0 and self.usage.get("tokens_out", 0) >= max_tokens:
            # Anthropic clients rely on this to know the answer was cut off.
            return "max_tokens"
        return "end_turn"

    def finish(self, max_tokens: Optional[int] = None, tools_given: bool = False) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        tail = self.stop_filter.flush()
        if tail:
            if self._open_block != "text":
                out += self._start()
                out += self._open("text", {"type": "text", "text": ""})
            self._text += tail
            out.append({"event": "content_block_delta", "data": {
                "type": "content_block_delta", "index": self._block_index,
                "delta": {"type": "text_delta", "text": tail}}})
        out += self._close_block()
        self.stop_reason = ("stop_sequence" if self.stop_filter.hit
                            else self._resolve_stop_reason(max_tokens))
        out.append({"event": "message_delta", "data": {
            "type": "message_delta",
            "delta": {"stop_reason": self.stop_reason,
                      "stop_sequence": self.stop_filter.matched},
            "usage": self._wire_usage()}})
        out.append({"event": "message_stop", "data": {"type": "message_stop"}})
        return out

    def message(self, max_tokens: Optional[int] = None) -> Dict[str, Any]:
        content: List[Dict[str, Any]] = []
        if self._thinking and self.expose_thinking:
            content.append({"type": "thinking", "thinking": self._thinking, "signature": ""})
        if self._text:
            content.append({"type": "text", "text": self._text})
        for call in self._tools:
            try:
                parsed = json.loads(call["arguments"]) if call["arguments"].strip() else {}
            except ValueError:
                parsed = {}
            content.append({"type": "tool_use", "id": call["id"], "name": call["name"],
                            "input": parsed if isinstance(parsed, dict) else {}})
        if not content:
            content.append({"type": "text", "text": ""})
        return {
            "id": self.id, "type": "message", "role": "assistant", "model": self.model,
            "content": content,
            "stop_sequence": self.stop_filter.matched,
            "stop_reason": ("stop_sequence" if self.stop_filter.hit
                            else self._resolve_stop_reason(max_tokens)),
            "usage": self._wire_usage(),
        }


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# OpenAI Responses API
# ---------------------------------------------------------------------------

def responses_to_internal(body: Dict[str, Any]) -> tuple[List[Msg], str, List[Dict[str, Any]]]:
    system = body.get("instructions") or ""
    messages: List[Msg] = []
    raw_input = body.get("input")
    if isinstance(raw_input, str):
        messages.append(Msg(role="user", text=raw_input))
    elif isinstance(raw_input, list):
        for item in raw_input:
            if isinstance(item, str):
                messages.append(Msg(role="user", text=item))
                continue
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "function_call_output":
                messages.append(Msg(role="tool", text=_content_to_text(item.get("output")),
                                    tool_call_id=str(item.get("call_id") or "")))
                continue
            if itype == "function_call":
                messages.append(Msg(role="assistant", tool_calls=[{
                    "id": str(item.get("call_id") or item.get("id") or ""),
                    "name": str(item.get("name") or ""),
                    "arguments": _json_or_empty(item.get("arguments")),
                }]))
                continue
            role = str(item.get("role") or "user")
            text, images, documents = _content_and_images(item.get("content"))
            messages.append(Msg(role=role, text=text, images=images, documents=documents))
    tools: List[Dict[str, Any]] = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict) or is_server_tool(tool):
            continue
        name = tool.get("name")
        if not name:
            continue
        tools.append({"type": "function", "function": {
            "name": str(name), "description": tool.get("description") or "",
            "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
        }})
    return messages, str(system), tools


def _json_or_empty(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


class CompletionsAdapter:
    """Legacy `/v1/completions` view over the chat Translator."""

    def __init__(self, model: str, chunked: bool = True, stop=None, allowed_tools=None) -> None:
        self.model = model
        self.inner = Translator(model, chunked=chunked, include_reasoning=False,
                                include_usage=False, allowed_tools=allowed_tools, stop=stop)

    @property
    def error(self):
        return self.inner.error

    @property
    def usage(self):
        return self.inner.usage

    @property
    def message_id(self):
        return self.inner.message_id

    @property
    def finish_reason(self) -> str:
        return self.inner.finish_reason or ""

    def assistant_key(self):
        return self.inner.assistant_key()

    @staticmethod
    def _frame(source: Dict[str, Any], text: str, finish_reason):
        return {"id": source.get("id"), "object": "text_completion",
                "created": source.get("created"), "model": source.get("model"),
                "choices": [{"text": text, "index": 0, "logprobs": None,
                             "finish_reason": finish_reason}]}

    def feed(self, event: Dict[str, Any]):
        out = []
        for frame in self.inner.feed(event):
            choices = frame.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            text = str(delta.get("content") or "")
            finish = choices[0].get("finish_reason")
            if text or finish:
                out.append(self._frame(frame, text, finish))
        return out

    def finish(self, max_tokens=None, tools_given: bool = False):
        out = []
        for frame in self.inner.finish(max_tokens=max_tokens, tools_given=tools_given):
            choices = frame.get("choices") or []
            if choices:
                out.append(self._frame(frame, "", choices[0].get("finish_reason")))
        return out

    def completion(self, max_tokens=None, tools_given: bool = False):
        payload = self.inner.completion(max_tokens=max_tokens, tools_given=tools_given)
        choice = payload["choices"][0]
        return {"id": payload["id"], "object": "text_completion",
                "created": payload["created"], "model": payload["model"],
                "choices": [{"text": (choice.get("message") or {}).get("content") or "",
                             "index": 0, "logprobs": None,
                             "finish_reason": choice["finish_reason"]}],
                "usage": payload["usage"]}


class ResponsesAdapter:
    """Studio events -> Responses API stream events / response object."""

    def __init__(self, model: str, tools_given: bool = False, allowed_tools=None) -> None:
        self.model = model
        self.tools_given = tools_given
        self.allowed_tools = allowed_tools
        self._ignored_blocks = set()
        self._tool_by_block: Dict[int, Dict[str, int]] = {}
        self.id = "resp_" + uuid.uuid4().hex[:24]
        self.created_at = int(time.time())
        self._text: List[str] = []
        self._reasoning: List[str] = []
        self._tools: List[Dict[str, Any]] = []
        self._started = False
        self._text_item_open = False
        self._seq = 0
        # Items are numbered in the order they start: a tool call can come before any
        # text, and the official SDK indexes its snapshot by these numbers.
        self._next_output_index = 0
        self._text_index = 0
        self.usage = {"tokens_in": 0, "tokens_out": 0, "cached_tokens": 0}
        self.message_id = ""
        self.status = ""
        self.finish_state = ""      # "completed" / "incomplete", like the wire status
        self.error: Optional[Dict[str, Any]] = None

    def _evt(self, name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._seq += 1
        return {"event": name, "data": dict({"type": name, "sequence_number": self._seq}, **payload)}

    def _base(self) -> Dict[str, Any]:
        # The official SDK accumulates stream events onto the `response` object carried
        # by response.created/in_progress; it does `snapshot.output.append(...)`, so an
        # absent `output` is an AttributeError on the client side.
        return {"id": self.id, "object": "response", "created_at": self.created_at,
                "model": self.model, "status": "in_progress", "output": [],
                "error": None, "incomplete_details": None, "usage": None}

    def _open_text(self) -> List[Dict[str, Any]]:
        if self._text_item_open:
            return []
        self._text_item_open = True
        self._text_index = self._next_output_index
        self._next_output_index += 1
        return [
            self._evt("response.output_item.added", {"output_index": self._text_index, "item": {
                "id": "msg_" + self.id[5:], "type": "message", "role": "assistant",
                "status": "in_progress", "content": []}}),
            self._evt("response.content_part.added", {"item_id": "msg_" + self.id[5:],
                                                      "output_index": self._text_index,
                                                      "content_index": 0,
                                                      "part": {"type": "output_text", "text": "", "annotations": []}}),
        ]

    def feed(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        etype = str(event.get("type") or "")
        data = event.get("data") or {}
        out: List[Dict[str, Any]] = []
        if etype == "message_start":
            self.message_id = str(data.get("message_id") or "")
            if not self._started:
                self._started = True
                out.append(self._evt("response.created", {"response": self._base()}))
                out.append(self._evt("response.in_progress", {"response": self._base()}))
            return out
        if etype == "reasoning_delta":
            self._reasoning.append(str(data.get("delta") or ""))
            return out
        if etype == "text_delta":
            out += self._open_text()
            delta = str(data.get("delta") or "")
            if delta:
                self._text.append(delta)
                out.append(self._evt("response.output_text.delta", {
                    "item_id": "msg_" + self.id[5:], "output_index": self._text_index,
                    "content_index": 0, "delta": delta}))
            return out
        if etype == "tool_call_start":
            if self.allowed_tools is not None and str(data.get("tool_name") or "") not in self.allowed_tools:
                self._ignored_blocks.add(int(data.get("block_index", 0)))
                return out
            call = {"id": str(data.get("tool_call_id") or ""), "name": str(data.get("tool_name") or ""),
                    "arguments": str(data.get("arguments_json") or ""),
                    "item_id": "fc_" + uuid.uuid4().hex[:16]}
            index = self._next_output_index
            self._next_output_index += 1
            call["output_index"] = index
            self._tool_by_block[int(data.get("block_index", 0))] = {
                "slot": len(self._tools), "output_index": index}
            self._tools.append(call)
            out.append(self._evt("response.output_item.added", {
                "output_index": index, "item": {
                    "id": call["item_id"], "type": "function_call",
                    "call_id": call["id"], "name": call["name"], "arguments": "", "status": "in_progress"}}))
            return out
        if etype == "tool_call_delta":
            block = int(data.get("block_index", 0))
            if block in self._ignored_blocks:
                return out
            target = self._tool_by_block.get(block)
            fragment = str(data.get("progress_text") or data.get("delta") or "")
            if target and fragment:
                call = self._tools[target["slot"]]
                call["arguments"] += fragment
                out.append(self._evt("response.function_call_arguments.delta", {
                    "item_id": call["item_id"], "output_index": target["output_index"],
                    "delta": fragment}))
            return out
        if etype == "final":
            raw = data.get("usage") or {}
            self.usage = {"tokens_in": _int(raw.get("tokens_in")),
                          "tokens_out": _int(raw.get("tokens_out")),
                          "cached_tokens": _int(raw.get("cached_tokens"))}
            return out
        if etype == "message_stop":
            self.status = str(data.get("status") or "")
            return out
        if etype == "error":
            self.error = {"type": "error", "message": str(data.get("message") or "upstream error")}
            return out
        return out

    def _output(self) -> List[Dict[str, Any]]:
        text = "".join(self._text)
        items: List[tuple] = []
        if self._text_item_open or (text or not self._tools):
            items.append((self._text_index, {
                "id": "msg_" + self.id[5:], "type": "message", "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}]}))
        for call in self._tools:
            items.append((call.get("output_index", 0), {
                "id": call["item_id"], "type": "function_call", "call_id": call["id"],
                "name": call["name"], "arguments": call["arguments"], "status": "completed"}))
        items.sort(key=lambda pair: pair[0])
        return [item for _, item in items]

    @property
    def finish_reason(self) -> str:
        """Usage-log view: the Responses wire status, not the upstream enum."""
        return self.finish_state or "completed"

    def assistant_key(self):
        calls = canonical_tool_calls_json(self._tools) if self._tools else ""
        return ("assistant", "".join(self._text), calls, "", "")

    def _wire_usage(self) -> Dict[str, int]:
        return {"input_tokens": self.usage.get("tokens_in", 0),
                "output_tokens": self.usage.get("tokens_out", 0),
                "total_tokens": self.usage.get("tokens_in", 0) + self.usage.get("tokens_out", 0)}

    def _status(self, max_tokens: Optional[int]):
        if isinstance(max_tokens, int) and max_tokens > 0 and self.usage.get("tokens_out", 0) >= max_tokens:
            return "incomplete", {"reason": "max_output_tokens"}
        return "completed", None

    def finish(self, max_tokens: Optional[int] = None, tools_given: bool = False) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for call in self._tools:
            index = call.get("output_index", 0)
            out.append(self._evt("response.function_call_arguments.done", {
                "item_id": call["item_id"], "output_index": index, "arguments": call["arguments"]}))
            out.append(self._evt("response.output_item.done", {
                "output_index": index, "item": {
                    "id": call["item_id"], "type": "function_call", "call_id": call["id"],
                    "name": call["name"], "arguments": call["arguments"], "status": "completed"}}))
        if self._text_item_open:
            text = "".join(self._text)
            out.append(self._evt("response.output_text.done", {
                "item_id": "msg_" + self.id[5:], "output_index": self._text_index,
                "content_index": 0, "text": text}))
            out.append(self._evt("response.content_part.done", {
                "item_id": "msg_" + self.id[5:], "output_index": self._text_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": text, "annotations": []}}))
            out.append(self._evt("response.output_item.done", {
                "output_index": self._text_index,
                "item": {"id": "msg_" + self.id[5:], "type": "message",
                                            "role": "assistant", "status": "completed",
                                            "content": [{"type": "output_text", "text": text, "annotations": []}]}}))
        state, incomplete = self._status(max_tokens)
        self.finish_state = state
        response = self._base()
        response.update({"status": state, "output": self._output(),
                         "output_text": "".join(self._text),
                         "usage": self._wire_usage()})
        if incomplete:
            response["incomplete_details"] = incomplete
            out.append(self._evt("response.incomplete", {"response": response}))
        else:
            out.append(self._evt("response.completed", {"response": response}))
        return out

    def response(self, max_tokens: Optional[int] = None) -> Dict[str, Any]:
        state, incomplete = self._status(max_tokens)
        self.finish_state = state
        payload = self._base()
        payload.update({"status": state, "output": self._output(),
                        "output_text": "".join(self._text),
                        "usage": self._wire_usage()})
        if incomplete:
            payload["incomplete_details"] = incomplete
        return payload
