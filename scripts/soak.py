#!/usr/bin/env python3
"""Short soak test: mixed traffic through the gateway, then leak checks via /health.

    python scripts/soak.py [--base http://127.0.0.1:8790] [--count 12] [--concurrency 3]

Verifies that every request succeeds, the in-flight counter returns to 0 (no leaked
concurrency permits) and the thread count settles back (no leaked threads). Uses the
real upstream, so keep --count small when the account has a tight quota.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


def headers(key: str = "") -> Dict[str, str]:
    out = {"Content-Type": "application/json"}
    if key:
        out["Authorization"] = "Bearer " + key
    return out


def get(base: str, path: str) -> Dict[str, Any]:
    with urllib.request.urlopen(base + path, timeout=30) as resp:
        return json.loads(resp.read())


def chat(base: str, model: str, text: str, stream: bool, key: str = "") -> Optional[int]:
    body = {"model": model, "messages": [{"role": "user", "content": text}],
            "max_tokens": 80, "reasoning_effort": "none", "stream": stream}
    req = urllib.request.Request(base + "/v1/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers=headers(key), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            if stream:
                while True:
                    chunk = resp.read1(4096)
                    if not chunk:
                        break
                return resp.status
            resp.read()
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:                       # noqa: BLE001 - never lose a sample
        return "exc:%s" % type(exc).__name__


def abandon(base: str, model: str, key: str = "") -> str:
    """Open a stream and drop it early, exercising the client-disconnect path."""
    body = {"model": model, "messages": [{"role": "user", "content": "写一段两百字的介绍"}],
            "max_tokens": 400, "stream": True}
    req = urllib.request.Request(base + "/v1/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers=headers(key), method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        resp.read1(256)
    return "aborted"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8790")
    parser.add_argument("--model", default="step-3.7-flash")
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--no-abort", action="store_true")
    parser.add_argument("--key", default="", help="API key, if the gateway requires one")
    parser.add_argument("--settle", type=float, default=15.0,
                        help="seconds to wait for in-flight permits to drain after the last request")
    args = parser.parse_args()

    try:
        before = get(args.base, "/health")
    except Exception as exc:                       # noqa: BLE001
        print("cannot reach %s/health: %s" % (args.base, exc))
        return 2
    print("before: inflight=%s threads=%s sessions=%s rss=%sMB accounts=%s" % (
        before.get("inflight"), before.get("threads"), before.get("sessions"),
        before.get("rss_mb"), before.get("accounts")))

    results: List[Any] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        stream = index % 2 == 1
        # chat() never raises, but a bug here must still surface as a failed sample
        # instead of silently shrinking `results` and passing the run.
        try:
            status = chat(args.base, args.model, "只回复两个字：正常", stream, args.key)
        except Exception as exc:                   # noqa: BLE001
            status = "exc:%s" % type(exc).__name__
        with lock:
            results.append(status)

    started = time.time()
    for batch_start in range(0, args.count, args.concurrency):
        batch = list(range(batch_start, min(batch_start + args.concurrency, args.count)))
        threads = [threading.Thread(target=worker, args=(i,)) for i in batch]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    elapsed = time.time() - started

    if not args.no_abort:
        abandoned = [abandon(args.base, args.model, args.key) for _ in range(2)]
        print("aborted: %d stream(s) dropped early" % len(abandoned))

    # Let the server settle after the disconnects. Aborted streams keep their permit until
    # the handler notices the dead socket (which can take a few seconds of buffered writes),
    # so poll instead of assuming a fixed delay -- a *permanent* leak is what matters.
    settle_started = time.time()
    after = get(args.base, "/health")
    while (after.get("inflight") or 0) > 0 and time.time() - settle_started < args.settle:
        time.sleep(0.5)
        after = get(args.base, "/health")
    settled = time.time() - settle_started
    bad = [status for status in results if status != 200]
    print("after : inflight=%s threads=%s sessions=%s rss=%sMB (settled in %.1fs)" % (
        after.get("inflight"), after.get("threads"), after.get("sessions"),
        after.get("rss_mb"), settled))
    print("requests: %d in %.1fs, failures=%d" % (len(results), elapsed, len(bad)))

    failures = 0
    if len(results) != args.count:
        print("  x only %d/%d samples returned (worker died before recording)"
              % (len(results), args.count))
        failures += 1
    if bad:
        print("  ✗ non-200 responses: %s" % bad[:5])
        failures += 1
    if after.get("inflight") != 0:
        print("  ✗ leaked concurrency permits: inflight=%s" % after.get("inflight"))
        failures += 1
    rss_before, rss_after = before.get("rss_mb"), after.get("rss_mb")
    if (isinstance(rss_before, (int, float)) and isinstance(rss_after, (int, float))
            and rss_after - rss_before > 256):
        print("  ✗ memory growth: %.1fMB -> %.1fMB" % (rss_before, rss_after))
        failures += 1
    if after.get("threads", 0) > before.get("threads", 0) + args.concurrency + 4:
        print("  ✗ thread growth: %s -> %s" % (before.get("threads"), after.get("threads")))
        failures += 1
    print("OK" if not failures else "%d check(s) failed" % failures)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
