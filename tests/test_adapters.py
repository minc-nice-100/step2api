"""Unit tests for the Anthropic and Responses adapters."""

import json
import unittest

from step2api.adapters import (
    AnthropicAdapter, ResponsesAdapter, anthropic_to_internal, has_web_search, is_server_tool,
    responses_to_internal,
)
from step2api.translate import Translator, normalize_messages


EVENTS = [
    {"type": "message_start", "data": {"message_id": "m1"}},
    {"type": "content_block_start", "data": {"block_index": 0, "block_type": "CONTENT_BLOCK_TYPE_TEXT"}},
    {"type": "text_delta", "data": {"block_index": 0, "delta": "Hel"}},
    {"type": "text_delta", "data": {"block_index": 0, "delta": "lo"}},
    {"type": "message_stop", "data": {"status": "MESSAGE_STATUS_SUCCESS"}},
    {"type": "final", "data": {"usage": {"tokens_in": "7", "tokens_out": "3", "cached_tokens": "1"}}},
]


class AnthropicInputTest(unittest.TestCase):
    def test_blocks(self):
        messages, system, tools = anthropic_to_internal({
            "system": [{"type": "text", "text": "sys"}],
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": [
                    {"type": "text", "text": "calling"},
                    {"type": "tool_use", "id": "t1", "name": "f", "input": {"a": 1}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "res"},
                ]},
            ],
            "tools": [{"name": "f", "description": "d", "input_schema": {"type": "object"}}],
        })
        self.assertEqual(system, "sys")
        self.assertEqual(messages[1].tool_calls[0]["arguments"], {"a": 1})
        self.assertEqual(messages[2].role, "tool")
        self.assertEqual(messages[2].tool_call_id, "t1")
        self.assertEqual(tools[0]["function"]["name"], "f")


class AnthropicDocumentTest(unittest.TestCase):
    def test_base64_document(self):
        messages, _system, _tools = anthropic_to_internal({
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "read this"},
                {"type": "document", "title": "memo.pdf",
                 "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0="}},
            ]}],
        })
        self.assertEqual(messages[0].documents[0]["name"], "memo.pdf")
        self.assertEqual(messages[0].documents[0]["mime"], "application/pdf")

    def test_text_document_is_base64_encoded(self):
        messages, _system, _tools = anthropic_to_internal({
            "messages": [{"role": "user", "content": [
                {"type": "document", "title": "note.txt",
                 "source": {"type": "text", "media_type": "text/plain", "data": "hello"}},
            ]}],
        })
        doc = messages[0].documents[0]
        self.assertTrue(doc["data"].startswith("data:text/plain;base64,"))


class AnthropicAdapterTest(unittest.TestCase):
    def test_stream_sequence(self):
        adapter = AnthropicAdapter("step-3.7-flash")
        frames = []
        for ev in EVENTS:
            frames.extend(adapter.feed(ev))
        frames.extend(adapter.finish())
        names = [f["event"] for f in frames]
        self.assertEqual(names[0], "message_start")
        self.assertIn("content_block_start", names)
        self.assertEqual(names[-2:], ["message_delta", "message_stop"])
        text = "".join(f["data"]["delta"]["text"] for f in frames
                       if f["event"] == "content_block_delta")
        self.assertEqual(text, "Hello")
        # Anthropic counts cached prompt tokens separately from input_tokens.
        self.assertEqual(adapter.message()["usage"],
                         {"input_tokens": 6, "output_tokens": 3,
                          "cache_read_input_tokens": 1, "cache_creation_input_tokens": 0})
        self.assertEqual(adapter.message()["stop_reason"], "end_turn")

    def test_tool_use(self):
        adapter = AnthropicAdapter("m")
        for ev in [
            {"type": "message_start", "data": {"message_id": "m1"}},
            {"type": "tool_call_start", "data": {"block_index": 0, "tool_call_id": "c1",
                                                 "tool_name": "f", "arguments_json": "{\"a\":"}},
            {"type": "tool_call_delta", "data": {"block_index": 0, "progress_text": "2}"}},
            {"type": "final", "data": {"usage": {"tokens_in": 1, "tokens_out": 1}}},
        ]:
            adapter.feed(ev)
        adapter.finish()
        message = adapter.message()
        self.assertEqual(message["stop_reason"], "tool_use")
        self.assertEqual(message["content"][0]["type"], "tool_use")
        self.assertEqual(message["content"][0]["input"], {"a": 2})


class TruncationTest(unittest.TestCase):
    EVENTS = [
        {"type": "message_start", "data": {"message_id": "m1"}},
        {"type": "text_delta", "data": {"delta": "cut"}},
        {"type": "final", "data": {"usage": {"tokens_in": "5", "tokens_out": "64"}}},
    ]

    def test_anthropic_reports_max_tokens(self):
        adapter = AnthropicAdapter("m")
        for ev in self.EVENTS:
            adapter.feed(ev)
        adapter.finish(max_tokens=64)
        self.assertEqual(adapter.message(max_tokens=64)["stop_reason"], "max_tokens")
        self.assertEqual(adapter.message(max_tokens=512)["stop_reason"], "end_turn")

    def test_responses_reports_incomplete(self):
        adapter = ResponsesAdapter("m")
        for ev in self.EVENTS:
            adapter.feed(ev)
        frames = adapter.finish(max_tokens=64)
        self.assertEqual(frames[-1]["event"], "response.incomplete")
        self.assertEqual(adapter.response(max_tokens=64)["status"], "incomplete")
        self.assertEqual(adapter.response(max_tokens=512)["status"], "completed")


class ResponsesToolStreamTest(unittest.TestCase):
    def test_function_call_event_sequence(self):
        adapter = ResponsesAdapter("m")
        frames = []
        for ev in [
            {"type": "message_start", "data": {"message_id": "m1"}},
            {"type": "tool_call_start", "data": {"block_index": 0, "tool_call_id": "c1",
                                                 "tool_name": "f", "arguments_json": "{\"a\":"}},
            {"type": "tool_call_delta", "data": {"block_index": 0, "progress_text": "1}"}},
            {"type": "final", "data": {"usage": {"tokens_in": 1, "tokens_out": 1}}},
        ]:
            frames.extend(adapter.feed(ev))
        frames.extend(adapter.finish())

        names = [f["event"] for f in frames]
        self.assertIn("response.function_call_arguments.delta", names)
        self.assertIn("response.function_call_arguments.done", names)
        self.assertEqual(names[-1], "response.completed")

        added = [f["data"]["item"] for f in frames if f["event"] == "response.output_item.added"]
        done = [f["data"]["item"] for f in frames if f["event"] == "response.output_item.done"]
        call_added = next(i for i in added if i["type"] == "function_call")
        call_done = next(i for i in done if i["type"] == "function_call")
        self.assertEqual(call_added["id"], call_done["id"])   # ids must line up
        self.assertEqual(call_done["arguments"], "{\"a\":1}")
        self.assertEqual(call_done["status"], "completed")


class ResponsesOutputStreamIndexTest(unittest.TestCase):
    """The SDK indexes its snapshot by output_index, so a tool-only turn must start at 0."""

    def test_tool_without_text_is_index_zero(self):
        adapter = ResponsesAdapter("m", tools_given=True)
        frames = []
        frames += adapter.feed({"type": "message_start", "data": {"message_id": "x"}})
        frames += adapter.feed({"type": "tool_call_start", "data": {
            "block_index": 0, "tool_call_id": "c1", "tool_name": "f", "arguments_json": "{}"}})
        frames += adapter.finish(max_tokens=64)
        added = [f["data"] for f in frames
                 if f.get("event") == "response.output_item.added"]
        self.assertEqual([a["output_index"] for a in added], [0], frames)
        done = [f["data"] for f in frames if f.get("event") == "response.output_item.done"]
        self.assertEqual([d["output_index"] for d in done], [0])
        response = adapter.response(max_tokens=64)
        self.assertEqual([o["type"] for o in response["output"]], ["function_call"])

    def test_text_then_tool_keeps_order(self):
        adapter = ResponsesAdapter("m", tools_given=True)
        frames = []
        frames += adapter.feed({"type": "message_start", "data": {"message_id": "x"}})
        frames += adapter.feed({"type": "text_delta", "data": {"block_index": 0, "delta": "hi"}})
        frames += adapter.feed({"type": "tool_call_start", "data": {
            "block_index": 1, "tool_call_id": "c1", "tool_name": "f", "arguments_json": "{}"}})
        frames += adapter.finish(max_tokens=64)
        added = [f["data"] for f in frames if f.get("event") == "response.output_item.added"]
        self.assertEqual([a["output_index"] for a in added], [0, 1])
        self.assertEqual([o["type"] for o in adapter.response(max_tokens=64)["output"]],
                         ["message", "function_call"])


class ResponsesInputTest(unittest.TestCase):
    def test_items(self):
        messages, system, tools = responses_to_internal({
            "instructions": "sys",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                {"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{\"a\":1}"},
                {"type": "function_call_output", "call_id": "c1", "output": "done"},
            ],
            "tools": [{"type": "function", "name": "f", "parameters": {"type": "object"}}],
        })
        self.assertEqual(system, "sys")
        self.assertEqual(messages[0].text, "hi")
        self.assertEqual(messages[1].tool_calls[0]["name"], "f")
        self.assertEqual(messages[2].role, "tool")
        self.assertEqual(tools[0]["function"]["name"], "f")


class ResponsesAdapterTest(unittest.TestCase):
    def test_stream_and_response(self):
        adapter = ResponsesAdapter("step-3.7-flash")
        frames = []
        for ev in EVENTS:
            frames.extend(adapter.feed(ev))
        frames.extend(adapter.finish())
        names = [f["event"] for f in frames]
        self.assertIn("response.created", names)
        self.assertIn("response.output_text.delta", names)
        self.assertEqual(names[-1], "response.completed")
        text = "".join(f["data"]["delta"] for f in frames if f["event"] == "response.output_text.delta")
        self.assertEqual(text, "Hello")
        payload = adapter.response()
        self.assertEqual(payload["output_text"], "Hello")
        self.assertEqual(payload["usage"]["total_tokens"], 10)
        json.dumps(payload)  # must stay serialisable


if __name__ == "__main__":
    unittest.main()


class ServerToolTest(unittest.TestCase):
    """Provider built-ins must not be advertised as client functions."""

    def test_web_search_is_not_a_client_function(self):
        _, _, tools = responses_to_internal({
            "model": "m", "input": "hi",
            "tools": [{"type": "web_search_preview"},
                      {"type": "function", "name": "get_weather", "parameters": {"type": "object"}}]})
        self.assertEqual([t["function"]["name"] for t in tools], ["get_weather"])
        self.assertTrue(has_web_search([{"type": "web_search_preview"}]))

    def test_anthropic_server_tools_are_filtered(self):
        _, _, tools = anthropic_to_internal({
            "model": "m", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "web_search_20250305", "name": "web_search"},
                      {"name": "get_weather", "input_schema": {"type": "object"}}]})
        self.assertEqual([t["function"]["name"] for t in tools], ["get_weather"])
        self.assertTrue(has_web_search([{"type": "web_search_20250305", "name": "web_search"}]))

    def test_custom_tools_are_kept(self):
        self.assertFalse(is_server_tool({"name": "f", "input_schema": {}}))
        self.assertFalse(is_server_tool({"type": "function", "name": "f"}))
        self.assertFalse(has_web_search([{"name": "get_weather"}]))
        self.assertFalse(has_web_search([]))


class AssistantFingerprintTest(unittest.TestCase):
    """assistant_key() must match Msg.key() exactly, or session reuse silently dies."""

    @staticmethod
    def echo(role="assistant", text=None, calls=None):
        item = {"role": role, "content": text}
        if calls:
            item["tool_calls"] = calls
        return normalize_messages([item])[0]

    def test_translator_matches_echoed_tool_call(self):
        tr = Translator("m", chunked=False)
        tr.feed({"type": "message_start", "data": {"message_id": "x"}})
        tr.feed({"type": "tool_call_start", "data": {"block_index": 0, "tool_call_id": "c1",
                                                     "tool_name": "f", "arguments_json": '{"a": 1}'}})
        echoed = self.echo(calls=[{"id": "c1", "type": "function",
                                   "function": {"name": "f", "arguments": '{"a": 1}'}}])
        self.assertEqual(tr.assistant_key(), echoed.key())

    def test_anthropic_matches_echoed_tool_call(self):
        adapter = AnthropicAdapter("m")
        adapter.feed({"type": "message_start", "data": {"message_id": "x"}})
        adapter.feed({"type": "tool_call_start", "data": {"block_index": 0, "tool_call_id": "c1",
                                                          "tool_name": "f", "arguments_json": "{}"}})
        echoed = self.echo(calls=[{"id": "c1", "type": "function",
                                   "function": {"name": "f", "arguments": "{}"}}])
        self.assertEqual(adapter.assistant_key(), echoed.key())

    def test_responses_matches_echoed_tool_call(self):
        adapter = ResponsesAdapter("m")
        adapter.feed({"type": "message_start", "data": {"message_id": "x"}})
        adapter.feed({"type": "tool_call_start", "data": {"block_index": 0, "tool_call_id": "c1",
                                                          "tool_name": "f", "arguments_json": "{}"}})
        echoed = self.echo(calls=[{"id": "c1", "type": "function",
                                   "function": {"name": "f", "arguments": "{}"}}])
        self.assertEqual(adapter.assistant_key(), echoed.key())

    def test_plain_text_fingerprint(self):
        tr = Translator("m", chunked=False)
        tr.feed({"type": "message_start", "data": {"message_id": "x"}})
        tr.feed({"type": "text_delta", "data": {"delta": "hello"}})
        self.assertEqual(tr.assistant_key(), self.echo(text="hello").key())


class InterleavedToolTest(unittest.TestCase):
    """Parallel tool calls: deltas must land on the tool identified by block_index."""

    def _feed(self, adapter):
        adapter.feed({"type": "message_start", "data": {"message_id": "x"}})
        adapter.feed({"type": "tool_call_start", "data": {"block_index": 0, "tool_call_id": "c0",
                                                          "tool_name": "f0", "arguments_json": ""}})
        adapter.feed({"type": "tool_call_start", "data": {"block_index": 1, "tool_call_id": "c1",
                                                          "tool_name": "f1", "arguments_json": ""}})
        adapter.feed({"type": "tool_call_delta", "data": {"block_index": 0, "progress_text": '{"x":'}})
        adapter.feed({"type": "tool_call_delta", "data": {"block_index": 1, "progress_text": '{"y":2}'}})
        adapter.feed({"type": "tool_call_delta", "data": {"block_index": 0, "progress_text": "1}"}})
        adapter.finish()

    def test_anthropic(self):
        adapter = AnthropicAdapter("m")
        self._feed(adapter)
        calls = {b["name"]: b["input"] for b in adapter.message()["content"] if b["type"] == "tool_use"}
        self.assertEqual(calls["f0"], {"x": 1})
        self.assertEqual(calls["f1"], {"y": 2})

    def test_responses(self):
        adapter = ResponsesAdapter("m")
        self._feed(adapter)
        calls = {i["name"]: i["arguments"] for i in adapter.response()["output"] if i["type"] == "function_call"}
        self.assertEqual(json.loads(calls["f0"]), {"x": 1})
        self.assertEqual(json.loads(calls["f1"]), {"y": 2})
