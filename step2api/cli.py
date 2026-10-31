"""Command line entry point (also exported as the `step2api` console script)."""

from __future__ import annotations

import os
import signal
import sys
import threading
import time

from . import __version__
from .config import Config
from .server import make_server
from .store import Store, pid_alive, read_pid_file, remove_pid_file, write_pid_file

USAGE = """\
step2api — StepFun Studio -> OpenAI/Anthropic compatible gateway

    step2api [--port 8790] [--host 127.0.0.1] [--api-key KEY] [--mode playground|agent]
    step2api --doctor        # self check: accounts / quota / models / chat (real upstream)
    step2api --doctor --media  # also TTS -> upload -> ASR round trip
                             #   refuses to run beside a live gateway; --force overrides
    step2api -V | -h

Environment variables mirror the flags; see step2api/config.py.
"""

BANNER = """
  step2api {version} — StepFun Studio -> OpenAI compatible gateway
  ───────────────────────────────────────────────────────────────
  API      http://{host}:{port}/v1        (OpenAI compatible)
  Panel    http://{host}:{port}/         (password: {password})
  Health   http://{host}:{port}/health
  Accounts {accounts}
"""


def drain(gateway, timeout: float = 10.0, poll: float = 0.2) -> float:
    """Wait (bounded) for in-flight upstream turns to finish before closing the socket."""
    waited = 0.0
    while getattr(gateway, "inflight_now", 0) > 0 and waited < timeout:
        time.sleep(poll)
        waited += poll
    return waited


def _is_addr_in_use(exc: OSError) -> bool:
    """EADDRINUSE across platforms: WSAEADDRINUSE(10048), Linux 98, macOS 48."""
    return getattr(exc, "winerror", None) == 10048 or getattr(exc, "errno", None) in (98, 48, 10048)


def _shutdown_handler(httpd, stopping: threading.Event | None = None):
    """Signal handler -> stop serve_forever() without deadlocking.

    BaseServer.shutdown() blocks until serve_forever() returns, and signal handlers
    always run on the main thread -- the same thread sitting inside serve_forever().
    Calling shutdown() inline there deadlocks the process forever, so it is done on
    a helper thread and the handler returns immediately.
    """
    gate = stopping or threading.Event()

    def handler(signum, frame):  # noqa: ARG001
        if gate.is_set():
            return
        gate.set()
        print("\n[step2api] shutting down...", flush=True)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    return handler


def _apply_stored_mode(cfg: Config, store: Store, argv: list[str]) -> None:
    """Precedence is CLI > env > panel setting: the stored default is only a fallback.

    Applying it unconditionally made `--mode playground` silently launch in agent mode.
    """
    explicit = (bool(os.environ.get("STEP_DEFAULT_MODE", "").strip())
                or any(a == "--mode" or a.startswith("--mode=") for a in argv))
    stored = store.settings().get("default_mode")
    if not explicit and stored in ("playground", "agent"):
        cfg.default_mode = stored


def main(argv: list[str] | None = None) -> int:
    try:
        # When stdout is a pipe (docker logs, nohup, Start-Process) Python block-buffers
        # it and the startup banner only appears at exit.
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(a in ("-h", "--help") for a in argv):
        print(USAGE)
        return 0
    if any(a in ("-V", "--version") for a in argv):
        print(__version__)
        return 0

    cfg = Config.from_env(argv)
    if "--doctor" in argv:
        from .diag import run as doctor

        return doctor(cfg, force="--force" in argv, media="--media" in argv)

    store = Store(cfg.data_dir)
    _apply_stored_mode(cfg, store, argv)

    # Two gateways sharing one data dir rotate the same refresh token against each other
    # and brick the account; the port guard alone misses the "different --port" case.
    running = read_pid_file(cfg.pid_file)
    if pid_alive(running):
        print("[step2api] 数据目录 %s 已被另一个 step2api 进程使用（pid %d）。"
              % (cfg.data_dir, running))
        print("  两个进程共用 accounts.json 会互相把 refresh token 作废；换个 --data-dir、先停掉它，")
        print("  或确认该 pid 已不是 step2api 后删除 %s 再重试。" % cfg.pid_file)
        return 2
    try:
        httpd = make_server(cfg, store)
    except OSError as exc:
        print("[step2api] 无法监听 %s:%s —— %s" % (cfg.host, cfg.port, exc))
        if _is_addr_in_use(exc):
            print("  端口已被占用（多半是另一个 step2api 还在跑）；换一个 --port 或先关掉它。")
        else:
            print("  不是端口占用：检查地址/权限（<1024 端口、证书、防火墙）或换个 --host。")
        return 2
    write_pid_file(cfg.pid_file)
    accounts = len(store.accounts)
    print(BANNER.format(version=__version__, host=cfg.host, port=cfg.port,
                        password="(custom)" if store.settings().get("panel_password") else cfg.panel_password,
                        accounts=accounts))
    if accounts == 0:
        print("  ! 还没有账号：打开面板粘贴 StepFun Studio 的 Cookie 即可入池\n")
    if not store.api_keys() and not cfg.api_key:
        if cfg.host not in ("127.0.0.1", "localhost", "::1"):
            print("  !! 监听在 %s 且没有任何 API Key —— 任何能连上此端口的人都能用你的额度。" % cfg.host)
            print("     请到面板生成 Key，或设置 --api-key / STEP_API_KEY。\n")
        else:
            print("  ! 未配置 API Key：网关当前对本机开放（同机网页也能直接调用它）；"
                  "生成 Key 后才会鉴权，也不要直接暴露到公网\n")

    shutdown = _shutdown_handler(httpd)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, shutdown)
        except (ValueError, OSError):
            pass

    try:
        httpd.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        # Let in-flight turns finish instead of severing them mid-stream.
        gateway = getattr(httpd, "gateway", None)
        if gateway is not None:
            waited = drain(gateway)
            if waited:
                print("[step2api] drained in-flight requests in %.1fs" % waited)
        httpd.server_close()
        remove_pid_file(cfg.pid_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
