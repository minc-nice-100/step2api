#!/usr/bin/env python3
"""Wire-compatibility check using the official SDKs.

    pip install openai anthropic
    python scripts/sdk_check.py [--base http://127.0.0.1:8790] [--model step-5-preview]

Exercises chat / streaming / tools / models / speech / transcription through the real
client libraries rather than hand-rolled HTTP, so schema drift shows up immediately.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import tempfile
import traceback


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8790")
    parser.add_argument("--model", default="step-5-preview")
    parser.add_argument("--fast-model", default="step-3.7-flash")
    parser.add_argument("--key", default="sk-step2api")
    parser.add_argument("--traceback", action="store_true", help="print stacks for failures")
    args = parser.parse_args()

    try:
        import anthropic
        import openai
    except ImportError:  # pragma: no cover - environment dependent
        print("需要先安装：pip install openai anthropic", file=sys.stderr)
        return 2

    oa = openai.OpenAI(base_url=args.base + "/v1", api_key=args.key, timeout=180)
    an = anthropic.Anthropic(base_url=args.base, api_key=args.key, timeout=180)
    failures = 0

    def check(label, fn, attempts=2):
        """Run one check, retrying once: model behaviour and transport both wobble."""
        nonlocal failures
        error = None
        tb = ""
        detail = None
        for attempt in range(attempts):
            try:
                detail = fn()
                break
            except AssertionError as exc:        # expectation missed: often model behaviour
                error, tb = exc, traceback.format_exc()
            except Exception as exc:             # noqa: BLE001 - upstream hiccup
                error, tb = exc, traceback.format_exc()
                if attempt + 1 < attempts:
                    time.sleep(1.5)
        if detail is None:
            failures += 1
            print("  ✗ %-24s %s" % (label, error))
            if "--traceback" in sys.argv:
                print(tb)
            return
        text = "  ✓ %-24s %s" % (
            label, detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False))
        print(text[:200])

    def openai_chat():
        r = oa.chat.completions.create(model=args.model, max_tokens=200,
                                       messages=[{"role": "user", "content": "用一句话介绍你自己"}])
        return "%s | finish=%s" % (r.choices[0].message.content[:40], r.choices[0].finish_reason)

    def openai_stream():
        chunks, text, usage = 0, "", None
        for ch in oa.chat.completions.create(
                model=args.fast_model, max_tokens=200, stream=True,
                stream_options={"include_usage": True},
                messages=[{"role": "user", "content": "从1数到5"}]):
            chunks += 1
            if ch.choices and ch.choices[0].delta.content:
                text += ch.choices[0].delta.content
            if ch.usage:
                usage = ch.usage.total_tokens
        return "chunks=%d text=%r tokens=%s" % (chunks, text[:20], usage)

    def openai_legacy_completions():
        r = oa.completions.create(model=args.fast_model, prompt="只回复两个字：正常",
                                  max_tokens=400)
        if r.object != "text_completion" or not r.choices[0].text.strip():
            raise AssertionError("bad payload: %r" % r)
        parts = []
        for chunk in oa.completions.create(model=args.fast_model, prompt="从1数到3",
                                           max_tokens=400, stream=True):
            if chunk.choices and chunk.choices[0].text:
                parts.append(chunk.choices[0].text)
        if not parts:
            raise AssertionError("no streamed text")
        return "text=%r stream=%r" % (r.choices[0].text[:20], "".join(parts)[:20])

    def openai_tools():
        tools = [{"type": "function", "function": {
            "name": "get_weather", "description": "查天气",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                           "required": ["city"]}}}]
        r = oa.chat.completions.create(model=args.fast_model, max_tokens=300, tools=tools,
                                       messages=[{"role": "user", "content": "查一下北京天气，必须调用工具"}])
        call = r.choices[0].message.tool_calls
        return "finish=%s call=%s" % (
            r.choices[0].finish_reason,
            (call[0].function.name, call[0].function.arguments) if call else None)

    def openai_tool_round_trip():
        tools = [{"type": "function", "function": {
            "name": "get_weather", "description": "查天气",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                           "required": ["city"]}}}]
        first = oa.chat.completions.create(
            model=args.fast_model, max_tokens=300, tools=tools,
            messages=[{"role": "user", "content": "查一下深圳天气，必须调用工具"}])
        call = first.choices[0].message.tool_calls
        if not call:
            raise AssertionError("model did not call the tool: %r" % first.choices[0].message.content)
        messages = [
            {"role": "user", "content": "查一下深圳天气，必须调用工具"},
            {"role": "assistant", "content": first.choices[0].message.content or "",
             "tool_calls": [{"id": call[0].id, "type": "function",
                             "function": {"name": call[0].function.name,
                                          "arguments": call[0].function.arguments}}]},
            {"role": "tool", "tool_call_id": call[0].id,
             "content": '{"temperature": 22, "condition": "sunny"}'},
        ]
        second = oa.chat.completions.create(model=args.fast_model, max_tokens=300,
                                            tools=tools, messages=messages)
        text = second.choices[0].message.content or ""
        if not text:
            raise AssertionError("no final answer after the tool result")
        return "second finish=%s text=%r mentions=%s" % (
            second.choices[0].finish_reason, text[:30], "22" in text)

    def anthropic_tool_round_trip():
        tools = [{"name": "get_weather", "description": "查天气",
                  "input_schema": {"type": "object",
                                   "properties": {"city": {"type": "string"}},
                                   "required": ["city"]}}]
        first = an.messages.create(model=args.fast_model, max_tokens=300, tools=tools,
                                   messages=[{"role": "user", "content": "查一下广州天气，必须调用工具"}])
        block = next((b for b in first.content if b.type == "tool_use"), None)
        if block is None:
            raise AssertionError("model did not use the tool: %r" % [b.type for b in first.content])
        messages = [
            {"role": "user", "content": "查一下广州天气，必须调用工具"},
            {"role": "assistant", "content": first.content},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": block.id,
                                          "content": '{"temperature": 22, "condition": "sunny"}'}]},
        ]
        second = an.messages.create(model=args.fast_model, max_tokens=300, tools=tools,
                                    messages=messages)
        text = "".join(b.text for b in second.content if b.type == "text")
        if not text:
            raise AssertionError("no final answer after the tool result")
        return "stop=%s text=%r mentions=%s" % (second.stop_reason, text[:30], "22" in text)

    def openai_tools_stream():
        tools = [{"type": "function", "function": {
            "name": "get_weather", "description": "查天气",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                           "required": ["city"]}}}]
        with oa.chat.completions.stream(
                model=args.fast_model, max_tokens=400, tools=tools,
                messages=[{"role": "user", "content": "查一下北京天气，必须调用工具"}]) as stream:
            final = stream.get_final_completion()
        call = final.choices[0].message.tool_calls
        if not call:
            raise AssertionError("SDK assembled no tool_calls: %r" % final.choices[0].message.content)
        second = oa.chat.completions.create(
            model=args.fast_model, max_tokens=400, tools=tools,
            messages=[{"role": "user", "content": "查一下北京天气，必须调用工具"},
                      {"role": "assistant", "content": final.choices[0].message.content or "",
                       "tool_calls": [{"id": call[0].id, "type": "function",
                                       "function": {"name": call[0].function.name,
                                                    "arguments": call[0].function.arguments}}]},
                      {"role": "tool", "tool_call_id": call[0].id,
                       "content": '{"temperature": 22, "condition": "sunny"}'}])
        answer = second.choices[0].message.content or ""
        if not answer.strip():
            raise AssertionError("no answer after the streamed tool call")
        return "streamed=%s args=%s answer=%r" % (call[0].function.name,
                                                  call[0].function.arguments[:30], answer[:25])

    def anthropic_cache_control():
        tools = [{"name": "get_weather", "description": "查天气",
                  "input_schema": {"type": "object", "properties": {"city": {"type": "string"}},
                                   "required": ["city"]},
                  "cache_control": {"type": "ephemeral"}}]
        msg = an.messages.create(
            model=args.fast_model, max_tokens=300, tools=tools,
            system=[{"type": "text", "text": "你是一个简洁的助手。",
                     "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": "不要使用 Markdown。",
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": "用一句话说明你现在能做什么"}])
        text = "".join(b.text for b in msg.content if b.type == "text")
        if not text.strip():
            raise AssertionError("empty answer (blocks=%r)" % [b.type for b in msg.content])
        return "stop=%s text=%r cache_read=%s" % (
            msg.stop_reason, text[:20], getattr(msg.usage, "cache_read_input_tokens", None))

    def anthropic_document():
        import base64

        marker = "ZQ7-ALPHA-9"
        body = "内部备忘：本次测试的暗号是 %s。除此之外没有别的内容。" % marker
        document = {"type": "document",
                    "source": {"type": "base64", "media_type": "text/plain",
                               "data": base64.b64encode(body.encode()).decode()},
                    "title": "memo.txt"}
        msg = an.messages.create(
            model=args.model, max_tokens=300,
            messages=[{"role": "user", "content": [
                document, {"type": "text", "text": "文档里的暗号是什么？只回答暗号本身。"}]}])
        text = "".join(b.text for b in msg.content if b.type == "text")
        if marker not in text:
            raise AssertionError("document was not read: %r" % text[:80])
        return "answer=%r" % text[:40]

    def anthropic_thinking_stream():
        chunks = {"thinking": 0, "text": 0}
        with an.messages.stream(model=args.fast_model, max_tokens=1024,
                                thinking={"type": "enabled", "budget_tokens": 512},
                                messages=[{"role": "user", "content": "简述为什么天空是蓝色的"}]) as stream:
            for event in stream:
                if event.type == "content_block_delta":
                    kind = getattr(event.delta, "type", "")
                    if kind == "thinking_delta":
                        chunks["thinking"] += 1
                    elif kind == "text_delta":
                        chunks["text"] += 1
            msg = stream.get_final_message()
        kinds = [b.type for b in msg.content]
        if "thinking" not in kinds:
            raise AssertionError("no thinking block in %r (deltas=%s)" % (kinds, chunks))
        if not any(b.type == "text" and b.text.strip() for b in msg.content):
            raise AssertionError("no text after thinking: %r" % (kinds,))
        return "blocks=%s deltas=%s stop=%s" % (kinds, chunks, msg.stop_reason)

    def anthropic_tools_stream():
        tools = [{"name": "get_weather", "description": "查天气",
                  "input_schema": {"type": "object", "properties": {"city": {"type": "string"}},
                                   "required": ["city"]}}]
        with an.messages.stream(model=args.fast_model, max_tokens=400, tools=tools,
                                messages=[{"role": "user", "content": "查一下广州天气，必须调用工具"}]) as stream:
            msg = stream.get_final_message()
        block = next((b for b in msg.content if b.type == "tool_use"), None)
        if block is None:
            raise AssertionError("SDK assembled no tool_use block: %r" % [b.type for b in msg.content])
        if not isinstance(block.input, dict):
            raise AssertionError("tool input was not assembled into a dict: %r" % (block.input,))
        return "tool=%s input=%s" % (block.name, block.input)

    def openai_reasoning_stream():
        reasoning, text = [], []
        for chunk in oa.chat.completions.create(
                model=args.fast_model, max_tokens=600, stream=True,
                messages=[{"role": "user", "content": "简述为什么天空是蓝色的"}]):
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            think = getattr(delta, "reasoning_content", None)
            if think:
                reasoning.append(think)
            if delta.content:
                text.append(delta.content)
        if not reasoning:
            raise AssertionError("no reasoning_content reached the SDK (extra field dropped?)")
        if not "".join(text).strip():
            raise AssertionError("reasoning arrived but the answer is empty")
        return "reasoning=%d chars text=%d chars" % (len("".join(reasoning)), len("".join(text)))

    def openai_vision():
        import base64
        import struct
        import zlib

        size = 16
        pixel = bytes([255, 0, 0])
        raw = b"".join(bytes([0]) + pixel * size for _ in range(size))

        def chunk(tag, data):
            return (struct.pack(">I", len(data)) + tag + data +
                    struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        png = (bytes([137]) + b"PNG" + bytes([13, 10, 26, 10]) +
               chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)) +
               chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
        uri = "data:image/png;base64," + base64.b64encode(png).decode()
        r = oa.chat.completions.create(model=args.model, max_tokens=200, messages=[{
            "role": "user", "content": [
                {"type": "text", "text": "这张图是什么颜色？只回答颜色名称。"},
                {"type": "image_url", "image_url": {"url": uri}}]}])
        answer = (r.choices[0].message.content or "").strip()
        if not answer:
            raise AssertionError("empty answer for the image")
        if ("红" not in answer) and ("red" not in answer.lower()):
            raise AssertionError("model did not see a red image: %r" % answer[:60])
        return "answer=%r" % answer[:30]


    def openai_models():
        ids = [m.id for m in oa.models.list().data]
        return "models=%s retrieve=%s" % (ids[:3], oa.models.retrieve(ids[0]).id)

    def openai_speech():
        # OpenAI's own model names, as an unmodified SDK client would send them: the
        # gateway maps them onto the account's TTS/ASR models.
        speech = oa.audio.speech.create(model="tts-1", voice="nova",
                                        input="这是通过官方 SDK 合成的语音。", response_format="mp3")
        audio = speech.content if hasattr(speech, "content") else speech.read()
        if not audio.startswith(b"ID3"):
            raise AssertionError("speech returned %d bytes, not an MP3: %r" % (len(audio), audio[:16]))
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as fh:
            fh.write(audio)
            path = fh.name
        with open(path, "rb") as fh:
            text = oa.audio.transcriptions.create(model="whisper-1", file=fh).text
        if not text.strip():
            raise AssertionError("whisper-1 transcription came back empty")
        with open(path, "rb") as fh:
            plain = oa.audio.transcriptions.create(model="whisper-1", file=fh,
                                                   response_format="text")
        if not isinstance(plain, str) or not plain.strip():
            raise AssertionError("response_format=text returned %r" % (type(plain),))
        return "bytes=%d magic=%r transcript=%r text_fmt=%r" % (
            len(audio), audio[:3], text[:30], plain[:20])

    def openai_responses():
        return oa.responses.create(model=args.model, input="用一句话说你好").output_text[:40]

    def responses_tool_stream():
        tools = [{"type": "function", "name": "get_weather", "description": "查天气",
                  "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                                 "required": ["city"]}}]
        call = None
        seen = set()
        with oa.responses.stream(model=args.fast_model, max_output_tokens=400, tools=tools,
                                 input="查一下北京天气，必须调用工具") as stream:
            for event in stream:
                seen.add(event.type)
                if (event.type == "response.output_item.done"
                        and getattr(event.item, "type", "") == "function_call"):
                    call = event.item
            stream.get_final_response()
        if call is None:
            raise AssertionError("no function_call item; events=%s" % sorted(seen)[:8])
        follow = oa.responses.create(
            model=args.fast_model, max_output_tokens=400, tools=tools,
            input=[{"role": "user", "content": "查一下北京天气，必须调用工具"},
                   {"type": "function_call", "call_id": call.call_id,
                    "name": call.name, "arguments": call.arguments},
                   {"type": "function_call_output", "call_id": call.call_id,
                    "output": '{"temperature": 22, "condition": "sunny"}'}])
        if not (follow.output_text or "").strip():
            raise AssertionError("no answer after function_call_output")
        return "call=%s answer=%r" % (call.name, follow.output_text[:30])

    def anthropic_message():
        msg = an.messages.create(model=args.model, max_tokens=300,
                                 messages=[{"role": "user", "content": "用一句话介绍你自己"}])
        text = next((b.text for b in msg.content if b.type == "text"), "")
        return "%s | stop=%s" % (text[:40], msg.stop_reason)

    def anthropic_stream():
        with an.messages.stream(model=args.fast_model, max_tokens=300,
                                messages=[{"role": "user", "content": "从1数到3"}]) as stream:
            text = "".join(stream.text_stream)
            final = stream.get_final_message()
        return "text=%r stop=%s" % (text[:20], final.stop_reason)

    def anthropic_tools():
        msg = an.messages.create(model=args.fast_model, max_tokens=300,
                                 tools=[{"name": "get_weather", "description": "查天气",
                                         "input_schema": {"type": "object",
                                                          "properties": {"city": {"type": "string"}},
                                                          "required": ["city"]}}],
                                 messages=[{"role": "user", "content": "查一下上海天气，必须调用工具"}])
        blocks = [(b.type, getattr(b, "name", None)) for b in msg.content]
        return "stop=%s blocks=%s" % (msg.stop_reason, blocks)

    def anthropic_stop_sequences():
        msg = an.messages.create(
            model=args.fast_model, max_tokens=200, stop_sequences=["5"],
            messages=[{"role": "user", "content": "用空格分隔，从 1 一直数到 10，只输出数字"}])
        text = "".join(b.text for b in msg.content if b.type == "text")
        if msg.stop_reason != "stop_sequence":
            raise AssertionError("stop_reason=%s text=%r" % (msg.stop_reason, text))
        if "5" in text:
            raise AssertionError("stop sequence leaked into the answer: %r" % text)
        return "stop=%s seq=%r text=%r" % (msg.stop_reason, getattr(msg, "stop_sequence", None), text[:20])

    def anthropic_count_tokens():
        """Local estimate (no upstream tokenizer): must answer and grow with the text."""
        short = an.messages.count_tokens(
            model=args.model, messages=[{"role": "user", "content": "hi"}])
        long = an.messages.count_tokens(
            model=args.model, messages=[{"role": "user", "content": "hi" * 200}])
        if short.input_tokens <= 0:
            raise AssertionError("input_tokens=%r" % short.input_tokens)
        if long.input_tokens <= short.input_tokens:
            raise AssertionError("estimate did not grow: %r vs %r"
                                 % (long.input_tokens, short.input_tokens))
        return "tokens %d -> %d" % (short.input_tokens, long.input_tokens)

    def responses_structured():
        schema = {"type": "object",
                  "properties": {"city": {"type": "string"}, "temp_c": {"type": "integer"}},
                  "required": ["city", "temp_c"], "additionalProperties": False}
        r = oa.responses.create(
            model=args.fast_model, max_output_tokens=1500,
            input="只输出一个 JSON 对象，键为 city（字符串）与 temp_c（整数），不要解释",
            reasoning={"effort": "low"},
            text={"format": {"type": "json_schema", "name": "weather", "schema": schema}})
        if not (r.output_text or "").strip():
            # Thinking can eat the whole output budget: the gateway reports that honestly
            # (status=incomplete) rather than inventing text.
            raise AssertionError("empty output_text (status=%s, incomplete=%s); reasoning "
                                 "may have consumed max_output_tokens"
                                 % (r.status, r.incomplete_details))
        data = json.loads(r.output_text)
        if not isinstance(data.get("city"), str) or not isinstance(data.get("temp_c"), int):
            raise AssertionError("schema not followed: %r" % data)
        return "json=%s" % json.dumps(data, ensure_ascii=False)

    def model_aliases():
        # Claude Code and Codex send their own model names; the gateway maps the foreign
        # families onto the upstream default instead of 400ing.
        msg = an.messages.create(model="claude-sonnet-4-5-20250929", max_tokens=200,
                                 messages=[{"role": "user", "content": "只回复两个字：正常"}])
        if not "".join(b.text for b in msg.content if b.type == "text").strip():
            raise AssertionError("empty answer for the claude-* alias")
        r = oa.chat.completions.create(model="gpt-5", max_tokens=200,
                                       messages=[{"role": "user", "content": "只回复两个字：正常"}])
        if not (r.choices[0].message.content or "").strip():
            raise AssertionError("empty answer for the gpt-* alias")
        return "claude->%s gpt->%s" % (msg.model, r.model)

    def error_shapes():
        details = []
        try:
            an.messages.create(model=args.model, max_tokens=16, messages=[])
            raise AssertionError("anthropic: expected a 400")
        except AssertionError:
            raise
        except Exception as exc:                       # noqa: BLE001
            text = str(exc)
            if "non-empty" not in text:
                raise AssertionError("anthropic SDK lost our message: %s" % text[:150])
            details.append("anthropic=%s" % getattr(exc, "status_code", "?"))
        try:
            oa.chat.completions.create(model="definitely-not-a-model", max_tokens=8,
                                       messages=[{"role": "user", "content": "hi"}])
            raise AssertionError("openai: expected a 400")
        except AssertionError:
            raise
        except Exception as exc:                       # noqa: BLE001
            text = str(exc)
            if "not a chat model" not in text:
                raise AssertionError("openai SDK lost our message: %s" % text[:150])
            details.append("openai=%s" % getattr(exc, "status_code", "?"))
        return " ".join(details)

    print("\nstep2api SDK check → %s\n" % args.base)
    check("openai chat", openai_chat)
    check("openai stream", openai_stream)
    check("openai tools", openai_tools)
    check("openai legacy completions", openai_legacy_completions)
    check("openai models", openai_models)
    check("openai speech+asr", openai_speech)
    check("openai responses", openai_responses)
    check("anthropic message", anthropic_message)
    check("anthropic stream", anthropic_stream)
    check("anthropic tools", anthropic_tools)
    check("openai tool round trip", openai_tool_round_trip)
    check("openai tools stream", openai_tools_stream)
    check("openai reasoning stream", openai_reasoning_stream)
    check("openai vision", openai_vision)
    check("anthropic tool round trip", anthropic_tool_round_trip)
    check("anthropic tools stream", anthropic_tools_stream)
    check("anthropic thinking stream", anthropic_thinking_stream)
    check("anthropic document", anthropic_document)
    check("anthropic cache_control", anthropic_cache_control)
    check("error shapes", error_shapes)
    check("model aliases", model_aliases)
    check("anthropic stop_sequences", anthropic_stop_sequences)
    check("anthropic count_tokens", anthropic_count_tokens)
    # The model occasionally answers this one with zero output tokens (upstream quirk);
    # a third attempt keeps the release gate meaningful without hiding real breakage.
    check("responses structured", responses_structured, attempts=3)
    check("responses tool stream", responses_tool_stream)

    print()
    if failures:
        print("%d 项失败\n" % failures)
        return 1
    print("全部通过\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
