"""Unit tests for the pure translation layer."""

import unittest

from step2api.translate import (
    ASR_MODEL_ALIASES, TTS_MODEL_ALIASES, StopFilter, Translator, build_chat_config,
    build_system_prompt, effective_model, effort_for_thinking_budget, map_audio_model,
    map_upstream_error, normalize_messages, parse_text_tool_call, render_delta,
    render_transcript, split_system,
)


class NormalizeTest(unittest.TestCase):
    def test_string_and_multipart(self):
        msgs = normalize_messages([
            {"role": "system", "content": "be nice"},
            {"role": "user", "content": [{"type": "text", "text": "hi"},
                                         {"type": "image_url", "image_url": {"url": "http://x/1.png"}}]},
        ])
        self.assertEqual(msgs[0].role, "system")
        self.assertEqual(msgs[1].text, "hi")
        self.assertEqual(msgs[1].images, ["http://x/1.png"])

    def test_file_parts_become_documents(self):
        msgs = normalize_messages([{
            "role": "user",
            "content": [
                {"type": "text", "text": "summarise this"},
                {"type": "file", "file": {"filename": "memo.txt", "file_data": "data:text/plain;base64,aGVsbG8="}},
            ],
        }])
        self.assertEqual(msgs[0].text, "summarise this")
        self.assertEqual(msgs[0].documents[0]["name"], "memo.txt")
        self.assertIn("base64", msgs[0].documents[0]["data"])

    def test_tool_calls_and_results(self):
        msgs = normalize_messages([
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{\"a\": 1}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ])
        self.assertEqual(msgs[0].tool_calls[0]["arguments"], {"a": 1})
        self.assertEqual(msgs[1].tool_call_id, "c1")

    def test_split_system(self):
        messages = normalize_messages([
            {"role": "system", "content": "s1"},
            {"role": "user", "content": "u"},
            {"role": "developer", "content": "s2"},
        ])
        system, rest = split_system(messages)
        self.assertEqual(system, "s1\n\ns2")
        self.assertEqual(len(rest), 1)
        self.assertEqual(rest[0].text, "u")


class PromptTest(unittest.TestCase):
    def test_tools_appended(self):
        tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}]
        prompt = build_system_prompt("base", tools)
        self.assertIn("base", prompt)
        self.assertIn("get_weather", prompt)

    def test_non_function_tools_ignored(self):
        self.assertEqual(build_system_prompt("base", [{"type": "web_search"}]), "base")

    def test_render_transcript(self):
        msgs = normalize_messages([
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ])
        text = render_transcript(msgs)
        self.assertIn("### User", text)
        self.assertIn("q2", text)
        self.assertTrue(text.endswith("reply to the final user message."))

    def test_render_delta(self):
        msgs = normalize_messages([{"role": "user", "content": "q2"}])
        self.assertEqual(render_delta(msgs), "q2")


class ConfigTest(unittest.TestCase):
    meta = {
        "config": [
            {"key": "max_tokens", "max": 64000, "min": 1},
            {"key": "temperature", "min": 0, "max": 1},
        ],
        "thinking": {"default_enabled": True, "default_effort": 2, "effort_options": [1, 2, 3]},
    }

    def test_effort_levels_are_words(self):
        cfg = build_chat_config({"max_tokens": 100, "reasoning_effort": "high"}, self.meta)
        self.assertEqual(cfg["effort"], "high")
        self.assertTrue(cfg["thinking"])
        self.assertEqual(cfg["max_tokens"], 100)

    def test_effort_clamped_to_options(self):
        meta = dict(self.meta)
        meta["thinking"] = {"default_enabled": True, "default_effort": 1, "effort_options": [1, 3]}
        cfg = build_chat_config({"reasoning_effort": "medium"}, meta)
        self.assertIn(cfg["effort"], ("low", "high"))

    def test_thinking_off(self):
        cfg = build_chat_config({"reasoning_effort": "none"}, self.meta)
        self.assertFalse(cfg["thinking"])
        self.assertNotIn("effort", cfg)

    def test_max_tokens_clamped(self):
        cfg = build_chat_config({"max_tokens": 999999}, self.meta)
        self.assertEqual(cfg["max_tokens"], 64000)

    def test_model_alias(self):
        self.assertEqual(effective_model("stepfun/step-5-preview", ["step-5-preview"]), "step-5-preview")
        self.assertEqual(effective_model("STEP-5-PREVIEW", ["step-5-preview"]), "step-5-preview")

    def test_openai_audio_names_map_to_the_upstream_default(self):
        for name in ("tts-1", "TTS-1", "tts-1-hd", "gpt-4o-mini-tts", "openai/tts-1"):
            self.assertEqual(map_audio_model(name, "stepaudio-2.5-tts", TTS_MODEL_ALIASES),
                             "stepaudio-2.5-tts", name)
        for name in ("whisper-1", "gpt-4o-transcribe", "stepfun/whisper-1"):
            self.assertEqual(map_audio_model(name, "stepaudio-2.5-asr", ASR_MODEL_ALIASES),
                             "stepaudio-2.5-asr", name)

    def test_thinking_budget_bands(self):
        self.assertEqual(effort_for_thinking_budget({"budget_tokens": 1024}), "low")
        self.assertEqual(effort_for_thinking_budget({"budget_tokens": 4095}), "low")
        self.assertEqual(effort_for_thinking_budget({"budget_tokens": 4096}), "medium")
        self.assertEqual(effort_for_thinking_budget({"budget_tokens": 16383}), "medium")
        self.assertEqual(effort_for_thinking_budget({"budget_tokens": 16384}), "high")
        self.assertEqual(effort_for_thinking_budget({"budget_tokens": "8192"}), "medium")

    def test_thinking_budget_unknown_keeps_the_default(self):
        for block in ({}, {"budget_tokens": 0}, {"budget_tokens": "many"}, None, "enabled"):
            self.assertEqual(effort_for_thinking_budget(block), "", block)

    def test_audio_model_passthrough_and_default(self):
        # A real upstream id passes through so typos still fail loudly upstream.
        self.assertEqual(map_audio_model("stepaudio-2.5-tts", "stepaudio-2.5-asr",
                                         TTS_MODEL_ALIASES), "stepaudio-2.5-tts")
        self.assertEqual(map_audio_model("", "stepaudio-2.5-tts", TTS_MODEL_ALIASES),
                         "stepaudio-2.5-tts")
        self.assertEqual(map_audio_model("no-such-model", "stepaudio-2.5-tts", TTS_MODEL_ALIASES),
                         "no-such-model")


class TranslatorTest(unittest.TestCase):
    def _events(self):
        return [
            {"type": "message_start", "data": {"message_id": "m1"}},
            {"type": "content_block_start", "data": {"block_index": 0, "block_type": "CONTENT_BLOCK_TYPE_REASONING"}},
            {"type": "reasoning_delta", "data": {"block_index": 0, "delta": "think"}},
            {"type": "content_block_start", "data": {"block_index": 1, "block_type": "CONTENT_BLOCK_TYPE_TEXT"}},
            {"type": "text_delta", "data": {"block_index": 1, "delta": "hello"}},
            {"type": "text_delta", "data": {"block_index": 1, "delta": " world"}},
            {"type": "message_stop", "data": {"status": "MESSAGE_STATUS_SUCCESS"}},
            {"type": "final", "data": {"usage": {"tokens_in": "10", "tokens_out": "5", "cached_tokens": "2"}}},
        ]

    def test_chunks(self):
        tr = Translator("m", chunked=True)
        frames = []
        for ev in self._events():
            frames.extend(tr.feed(ev))
        frames.extend(tr.finish())
        text = "".join(f["choices"][0]["delta"].get("content") or "" for f in frames)
        reasoning = "".join(f["choices"][0]["delta"].get("reasoning_content") or "" for f in frames)
        self.assertEqual(text, "hello world")
        self.assertEqual(reasoning, "think")
        self.assertEqual(frames[-1]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(tr.usage["tokens_in"], 10)

    def test_empty_answer_is_empty_string(self):
        tr = Translator("m", chunked=False)
        tr.feed({"type": "message_start", "data": {"message_id": "m1"}})
        payload = tr.completion()
        self.assertEqual(payload["choices"][0]["message"]["content"], "")

    def test_completion_shape(self):
        tr = Translator("m", chunked=False)
        for ev in self._events():
            tr.feed(ev)
        payload = tr.completion()
        self.assertEqual(payload["object"], "chat.completion")
        self.assertEqual(payload["choices"][0]["message"]["content"], "hello world")
        self.assertEqual(payload["usage"]["total_tokens"], 15)

    def test_tool_call_stream(self):
        tr = Translator("m", chunked=True)
        frames = []
        for ev in [
            {"type": "message_start", "data": {"message_id": "m1"}},
            {"type": "tool_call_start", "data": {"block_index": 0, "tool_call_id": "c1", "tool_name": "f", "arguments_json": "{\"a\":"}},
            {"type": "tool_call_delta", "data": {"block_index": 0, "progress_text": "1}"}},
            {"type": "final", "data": {"usage": {"tokens_in": 1, "tokens_out": 1}}},
        ]:
            frames.extend(tr.feed(ev))
        frames.extend(tr.finish())
        self.assertEqual(frames[-1]["choices"][0]["finish_reason"], "tool_calls")
        msg = tr.message()
        self.assertEqual(msg["tool_calls"][0]["function"]["arguments"], "{\"a\":1}")

    def test_builtin_tool_calls_are_filtered(self):
        tr = Translator("m", chunked=True, allowed_tools={"client_tool"})
        frames = []
        for ev in [
            {"type": "message_start", "data": {"message_id": "m1"}},
            {"type": "tool_call_start", "data": {"block_index": 0, "tool_call_id": "c1",
                                                 "tool_name": "document_parser", "arguments_json": "{}"}},
            {"type": "tool_call_delta", "data": {"block_index": 0, "progress_text": "{}"}},
            {"type": "text_delta", "data": {"delta": "plain answer"}},
            {"type": "final", "data": {"usage": {"tokens_in": 1, "tokens_out": 1}}},
        ]:
            frames.extend(tr.feed(ev))
        frames.extend(tr.finish())
        self.assertEqual(tr.tool_calls, [])
        self.assertEqual(frames[-1]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(tr.message()["content"], "plain answer")

    def test_declared_tool_call_passes(self):
        tr = Translator("m", chunked=True, allowed_tools={"client_tool"})
        for ev in [
            {"type": "message_start", "data": {"message_id": "m1"}},
            {"type": "tool_call_start", "data": {"block_index": 0, "tool_call_id": "c1",
                                                 "tool_name": "client_tool", "arguments_json": "{}"}},
        ]:
            tr.feed(ev)
        tr.finish()
        self.assertEqual(tr.tool_calls[0]["name"], "client_tool")

    def test_text_tool_call_recovery(self):
        tr = Translator("m", chunked=False)
        tr.feed({"type": "message_start", "data": {"message_id": "m1"}})
        tr.feed({"type": "text_delta", "data": {"delta": '{"name": "get_weather", "arguments": {"city": "X"}}'}})
        msg = tr.completion(tools_given=True)["choices"][0]["message"]
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "get_weather")
        self.assertIsNone(msg["content"])

    def test_error_event(self):
        tr = Translator("m")
        tr.feed({"type": "error", "data": {"code": "AGENT_ERROR", "message": "boom"}})
        self.assertEqual(tr.error["message"], "boom")


class UnparsedArgumentsTest(unittest.TestCase):
    """Truncated tool arguments must fingerprint the same on both sides of the prefix."""

    def test_matches_echoed_tool_call(self):
        from step2api.translate import Msg, normalize_messages

        tr = Translator("m", chunked=False)
        tr.feed({"type": "message_start", "data": {"message_id": "x"}})
        tr.feed({"type": "tool_call_start", "data": {"block_index": 0, "tool_call_id": "c1",
                                                     "tool_name": "f", "arguments_json": '{"a":'}})
        echoed = normalize_messages([{"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "f", "arguments": '{"a":'}}]}])[0]
        self.assertIsInstance(echoed, Msg)
        self.assertEqual(tr.assistant_key(), echoed.key())


class StopFilterTest(unittest.TestCase):
    def test_cuts_across_chunks(self):
        filt = StopFilter(["\n\nUser:"])
        out = filt.feed("answer par")
        out += filt.feed("t one\n\nUs")
        out += filt.feed("er: next")
        out += filt.flush()
        self.assertEqual(out, "answer part one")
        self.assertTrue(filt.hit)

    def test_holds_back_partial_tail(self):
        filt = StopFilter(["<<END>>"])
        # nothing can be released until the tail is provably not a stop prefix
        self.assertEqual(filt.feed("hi <"), "")
        out = filt.feed("<END") + filt.feed(">>") + filt.flush()
        self.assertEqual(out, "hi ")
        self.assertTrue(filt.hit)

    def test_no_stops_is_passthrough(self):
        self.assertEqual(StopFilter(None).feed("hello"), "hello")

    def test_flush_releases_held_tail(self):
        filt = StopFilter(["<<END>>"])
        collected = filt.feed("tail hello") + filt.flush()
        self.assertEqual(collected, "tail hello")
        self.assertFalse(filt.hit)


    def test_text_after_a_hit_is_dropped(self):
        flt = StopFilter(["STOP"])
        self.assertEqual(flt.feed("before STOP after"), "before ")
        self.assertTrue(flt.hit)
        self.assertEqual(flt.feed("more text"), "")
        self.assertEqual(flt.flush(), "")


class TranslatorStopTest(unittest.TestCase):
    def test_stop_sequence_truncates(self):
        tr = Translator("m", chunked=False, stop=["STOP"])
        tr.feed({"type": "message_start", "data": {"message_id": "m1"}})
        tr.feed({"type": "text_delta", "data": {"delta": "keep this "}})
        tr.feed({"type": "text_delta", "data": {"delta": "STO"}})
        tr.feed({"type": "text_delta", "data": {"delta": "P and drop"}})
        payload = tr.completion()
        self.assertEqual(payload["choices"][0]["message"]["content"], "keep this ")
        self.assertEqual(payload["choices"][0]["finish_reason"], "stop")


class ErrorMappingTest(unittest.TestCase):
    def test_censored_is_400_and_not_transient(self):
        err = map_upstream_error({"biz_code": "CENSORED", "message": "blocked"})
        self.assertEqual(err.status, 400)
        self.assertFalse(err.transient)
        self.assertEqual(err.code, "content_policy_blocked")

    def test_invalid_model_is_400(self):
        self.assertEqual(map_upstream_error({"biz_code": "INVALID_MODEL", "message": "x"}).status, 400)

    def test_unknown_is_502(self):
        err = map_upstream_error({"biz_code": "SOMETHING", "message": "boom"})
        self.assertEqual(err.status, 502)


class ParseToolCallTest(unittest.TestCase):
    def test_plain(self):
        parsed = parse_text_tool_call('{"name":"f","arguments":{"a":1}}')
        self.assertEqual(parsed["name"], "f")
        self.assertEqual(parsed["arguments"], {"a": 1})

    def test_fenced(self):
        parsed = parse_text_tool_call('```json\n{"name":"f","arguments":{}}\n```')
        self.assertEqual(parsed["name"], "f")

    def test_multi(self):
        parsed = parse_text_tool_call('{"tool_calls":[{"name":"f","arguments":{"x":2}}]}')
        self.assertEqual(parsed["arguments"], {"x": 2})

    def test_prose_is_rejected(self):
        self.assertIsNone(parse_text_tool_call("I cannot help with that."))


if __name__ == "__main__":
    unittest.main()
