#!/usr/bin/env python3
"""Deployment smoke test: is this running gateway actually serving clients?

    python scripts/smoke.py [--base http://127.0.0.1:8790] [--key sk-...]

Standard library only (unlike sdk_check.py, which needs the official SDKs), so it works
straight after `docker compose up` or on a machine with nothing installed. It talks to
the public HTTP surface only, and it spends a handful of tokens on the real upstream.

Exit code: 0 all checks passed, 1 at least one failed.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List

OK = "✓"
BAD = "✗"
TIMEOUT = 120.0   # per-request seconds; --timeout overrides it


def post(base: str, path: str, body: Dict[str, Any], key: str = "") -> Any:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(base + path, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def post_stream(base: str, path: str, body: Dict[str, Any], key: str = "") -> str:
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(base + path, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8")


def get(base: str, path: str, key: str = "") -> Any:
    headers = {"Authorization": "Bearer " + key} if key else {}
    req = urllib.request.Request(base + path, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        text = resp.read().decode("utf-8")
    try:
        return json.loads(text)
    except ValueError:
        return text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8790")
    parser.add_argument("--key", default="", help="API key, if the gateway requires one")
    parser.add_argument("--model", default="", help="override the model id")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="per-request timeout in seconds (lower it for a quick reachability test)")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    global TIMEOUT
    TIMEOUT = args.timeout

    failures: List[str] = []

    def check(label, fn):
        try:
            detail = fn()
            print("  %s %-24s %s" % (OK, label, detail))
        except Exception as exc:  # noqa: BLE001 - the point is to report, not raise
            failures.append(label)
            print("  %s %-24s %s" % (BAD, label, exc))

    print("\nstep2api smoke -> %s\n" % base)

    def health():
        payload = get(base, "/health")
        if payload.get("status") != "ok":
            raise AssertionError("status=%r" % payload.get("status"))
        return "version=%s accounts=%s inflight=%s" % (
            payload.get("version"), payload.get("accounts"), payload.get("inflight"))

    def ping():
        return get(base, "/ping")

    model_box: List[str] = []

    def models():
        payload = get(base, "/v1/models", args.key)
        ids = [m["id"] for m in payload.get("data") or []]
        if not ids:
            raise AssertionError("no models in the catalogue")
        model_box.extend(ids)
        return "%d model(s): %s" % (len(ids), ", ".join(ids[:3]))

    def chat():
        payload = post(base, "/v1/chat/completions", {
            "model": args.model or model_box[0], "max_tokens": 200,
            "messages": [{"role": "user", "content": "只回复两个字：正常"}]}, args.key)
        text = (payload["choices"][0]["message"].get("content") or "").strip()
        if not text:
            raise AssertionError("empty content (finish=%s)" % payload["choices"][0]["finish_reason"])
        return "content=%r finish=%s" % (text[:12], payload["choices"][0]["finish_reason"])

    def stream():
        raw = post_stream(base, "/v1/chat/completions", {
            "model": args.model or model_box[0], "max_tokens": 200, "stream": True,
            "messages": [{"role": "user", "content": "从1数到3"}]}, args.key)
        if "[DONE]" not in raw:
            raise AssertionError("no [DONE] terminator")
        chunks = raw.count("data: ")
        if chunks < 2:
            raise AssertionError("only %d frame(s)" % chunks)
        return "%d frame(s), [DONE] present" % chunks

    def anthropic():
        payload = post(base, "/v1/messages", {
            "model": args.model or model_box[0], "max_tokens": 200,
            "messages": [{"role": "user", "content": "只回复两个字：正常"}]}, args.key)
        text = "".join(b.get("text") or "" for b in payload.get("content") or [])
        if payload.get("type") != "message" or not text.strip():
            raise AssertionError("unexpected payload: %r" % (payload.get("type"),))
        return "stop=%s text=%r" % (payload.get("stop_reason"), text[:12])

    def responses():
        payload = post(base, "/v1/responses", {
            # Reasoning eats the budget before any text: a small cap yields
            # status=incomplete with empty output_text (upstream behaviour).
            "model": args.model or model_box[0], "max_output_tokens": 800,
            "input": "只回复两个字：正常"}, args.key)
        text = (payload.get("output_text") or "").strip()
        if not text:
            raise AssertionError("empty output_text (status=%s)" % payload.get("status"))
        return "text=%r" % text[:12]

    started = time.time()
    check("health", health)
    check("ping", ping)
    check("models", models)
    if model_box:
        check("chat (non-stream)", chat)
        check("chat (stream)", stream)
        check("anthropic messages", anthropic)
        check("responses", responses)
    else:
        print("  %s %-24s %s" % (BAD, "chat / messages / responses", "skipped: no model catalogue"))
        failures.append("model catalogue")

    print()
    if failures:
        print("%d 项失败：%s（%.1fs）\n" % (len(failures), ", ".join(failures), time.time() - started))
        return 1
    print("全部通过（%.1fs）\n" % (time.time() - started))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
