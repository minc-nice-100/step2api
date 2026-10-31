"""`python step2api.py --doctor` — end-to-end self check against the real upstream.

Prints a checklist so "网关起来了但客户端一个字都不吐" can be diagnosed in one command.
"""

from __future__ import annotations

import time
from typing import List, Tuple

from .client import StudioError, iter_sse_closing
from .config import Config
from .server import Gateway
from .store import Store, pid_alive, read_pid_file
from .translate import build_chat_config

OK = "✓"
BAD = "✗"
WARN = "!"


def _line(mark: str, label: str, detail: str = "") -> None:
    print("  %s %-22s %s" % (mark, label, detail))


def ping_url(cfg: Config) -> str:
    """The /ping URL for cfg, with a wildcard host mapped to loopback and IPv6 bracketed."""
    host = "127.0.0.1" if cfg.host in ("0.0.0.0", "::", "") else cfg.host
    if ":" in host and not host.startswith("["):
        host = "[%s]" % host          # http://::1:8790/ping does not parse
    return "http://%s:%d/ping" % (host, cfg.port)


def gateway_is_up(cfg: Config, timeout: float = 1.5) -> bool:
    """True when a gateway already answers on cfg.port.

    Its refresh token is the same one doctor is about to rotate, and Studio kills the
    old half on every refresh -- two processes sharing one account can brick it.
    """
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never via proxy
    try:
        with opener.open(ping_url(cfg), timeout=timeout) as resp:
            return resp.read().strip() == b"pong"
    except Exception:  # noqa: BLE001 - any failure means "nothing is listening"
        return False


def _media_check(client, tts_cfg, asr_cfg) -> bool:
    """TTS -> upload -> ASR round trip. Studio's media turns must end with `done`; a
    successful round trip is the live evidence that this account still serves them."""
    from .media import synthesize, transcribe

    started = time.time()
    tts_model = str(tts_cfg.get("default_model") or "")
    voices = [str(v.get("voice")) for v in tts_cfg.get("voices") or []
              if isinstance(v, dict) and v.get("voice")]
    voice = "xiaoyue" if "xiaoyue" in voices or not voices else voices[0]
    asr_model = str(asr_cfg.get("default_model") or "")
    try:
        if not tts_model:
            raise StudioError("上游目录没有 TTS 默认模型")
        if not asr_model:
            raise StudioError("上游目录没有 ASR 默认模型")
        audio, _mime = synthesize(client, "你好，这是 step2api 自检。", voice, tts_model,
                                  "mp3", timeout=180)
        if not audio.startswith(b"ID3"):
            raise StudioError("TTS 返回的不是 MP3（%d 字节）" % len(audio))
        asset_id = client.upload_file("doctor.mp3", "audio/mpeg", audio)
        text = transcribe(client, asset_id, asr_model, timeout=180)
        _line(OK, "tts + asr", "%d 字节 → %r  %.1fs" % (
            len(audio), text[:24], time.time() - started))
        return True
    except StudioError as exc:
        _line(BAD, "tts + asr", str(exc))
        return False


def run(cfg: Config, force: bool = False, media: bool = False) -> int:
    store = Store(cfg.data_dir)
    gateway = Gateway(cfg, store)      # not started: reuse its account plumbing
    accounts = store.accounts
    print("\nstep2api doctor — %s\n" % cfg.data_dir)

    failures = 0
    if not accounts:
        _line(BAD, "accounts", "数据目录里没有账号：打开面板粘贴 Cookie")
        return 1
    _line(OK, "accounts", "%d 个" % len(accounts))
    port_up = gateway_is_up(cfg)
    live_pid = read_pid_file(cfg.pid_file)
    if port_up or pid_alive(live_pid):
        where = ("端口 %d" % cfg.port) if port_up else ("数据目录 %s（pid %d）"
                                                        % (cfg.data_dir, live_pid))
        _line(WARN, "gateway", "%s 上已有网关在跑：续期会与它抢同一个 refresh token，"
                               "先停掉网关再跑 doctor" % where)
        if not force:
            # Refreshing here is exactly what the warning says will brick the pool.
            _line(BAD, "aborted", "未做任何续期；确认不共用 accounts.json 时加 --force 跳过此检查")
            return 1

    usable: List[Tuple[object, object]] = []
    for account in accounts:
        client = gateway.client_for(account)
        try:
            client.refresh()
            # Studio rotates the refresh token on every use and invalidates the
            # old one: a doctor run that forgets to persist the new value bricks
            # the very pool it was supposed to diagnose.
            gateway.sync_account(account, client)
            exp = client.access_exp()
            left = int(exp - time.time()) if exp else 0
            _line(OK, "token · %s" % account.name, "续期成功，access 有效期 %ds" % left)
            usable.append((account, client))
        except StudioError as exc:
            failures += 1
            _line(BAD, "token · %s" % account.name, str(exc))

    if not usable:
        print("\n没有可用账号，后续检查已跳过。\n")
        return 1

    account, client = usable[0]

    try:
        rate = client.rate_limit()
        free = rate.get("free_rate")
        detail = "free_rate=%s" % (round(float(free) * 100, 1) if isinstance(free, (int, float)) else free)
        if isinstance(free, (int, float)) and free < 0.1:
            _line(WARN, "quota", detail + "（额度偏低）")
        else:
            _line(OK, "quota", detail)
    except StudioError as exc:
        failures += 1
        _line(BAD, "quota", str(exc))

    models = []
    payload = {}
    try:
        payload = client.playground_config()
        models = [m for m in (payload.get("chat") or {}).get("models") or [] if m.get("model")]
        _line(OK, "models", ", ".join(m["model"] for m in models))
    except StudioError as exc:
        failures += 1
        _line(BAD, "models", str(exc))

    if not models:
        print("\n拿不到模型目录，对话检查已跳过。\n")
        return 1

    model_id = models[0]["model"]

    # non-stream round trip
    started = time.time()
    try:
        conversation = client.create_conversation()
        config = build_chat_config({"max_tokens": 64}, models[0])
        resp = client.playground_stream(conversation, {
            "model": {"model_id": model_id, "config": config},
            "content": "只回复两个字：正常",
            "asset_ids": [],
            "enable_web_search": False,
        })
        text = ""
        usage = {}
        for event in iter_sse_closing(resp):
            etype = event.get("type")
            data = event.get("data") or {}
            if etype == "text_delta":
                text += str(data.get("delta") or "")
            elif etype == "error":
                raise StudioError(str(data.get("message") or "upstream error"), status=502)
            elif etype == "final":
                usage = data.get("usage") or {}
            elif etype == "done":
                break
        took = time.time() - started
        _line(OK, "chat (non-stream)", "%s → %r  %.1fs  tokens %s/%s" % (
            model_id, text[:20], took, usage.get("tokens_in", "?"), usage.get("tokens_out", "?")))
    except StudioError as exc:
        failures += 1
        _line(BAD, "chat (non-stream)", str(exc))

    # streaming round trip
    started = time.time()
    try:
        conversation = client.create_conversation()
        config = build_chat_config({"max_tokens": 64, "reasoning_effort": "none"}, models[0])
        resp = client.playground_stream(conversation, {
            "model": {"model_id": model_id, "config": config},
            "content": "从1数到3",
            "asset_ids": [],
            "enable_web_search": False,
        })
        ttft = None
        chunks = 0
        for event in iter_sse_closing(resp):
            etype = event.get("type")
            if etype in ("text_delta", "reasoning_delta"):
                chunks += 1
                if ttft is None:
                    ttft = time.time() - started
            elif etype == "error":
                data = event.get("data") or {}
                raise StudioError(str(data.get("message") or "upstream error"), status=502)
            elif etype == "done":
                break
        # A stream that dies before `done` raises from the wrapper (caught below), so
        # reaching this line already proves the terminator arrived; only a turn that
        # ended cleanly with no content at all is left to flag.
        if not chunks:
            failures += 1
            _line(WARN, "chat (stream)", "正常收尾，但没有任何内容")
        else:
            _line(OK, "chat (stream)", "%d 帧, 首字 %s, 收尾 正常" % (
                chunks, ("%.2fs" % ttft) if ttft else "—"))
    except StudioError as exc:
        failures += 1
        _line(BAD, "chat (stream)", str(exc))

    if media:
        if not _media_check(client, payload.get("tts") or {}, payload.get("asr") or {}):
            failures += 1

    # agent surface: workspace -> run -> event feed. Some accounts are not provisioned
    # for workspaces, so a failure here is a warning, not a broken gateway.
    started = time.time()
    try:
        workspace = client.create_workspace()
        conversation = client.create_conversation(workspace)
        run = client.start_run(workspace, conversation, "只回复两个字：正常", model_id)
        if not run.get("assistant_message_id"):
            raise StudioError("StartStudioWorkspaceRun returned no message id")
        text = ""
        events = client.agent_events(run["workspace_id"], run["assistant_message_id"])
        for event in iter_sse_closing(events):
            etype = event.get("type")
            data = event.get("data") or {}
            if etype == "text_delta":
                text += str(data.get("delta") or "")
            elif etype == "error":
                raise StudioError(str(data.get("message") or "upstream error"), status=502)
            elif etype == "done":
                break
        mark = OK if text else WARN
        _line(mark, "chat (agent mode)", "%s → %r  %.1fs" % (
            model_id, text[:20], time.time() - started))
    except StudioError as exc:
        _line(WARN, "chat (agent mode)", "%s（playground 模式不受影响）" % exc)

    print()
    if failures:
        print("有 %d 项检查失败。常见原因：Cookie 过期（重新粘贴）、账号被风控、网络代理不通。\n" % failures)
        return 1
    print("全部检查通过。把 http://127.0.0.1:%d/v1 作为 Base URL 即可。\n" % cfg.port)
    return 0
