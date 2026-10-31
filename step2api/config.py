"""Runtime configuration: env vars + CLI, resolved once at startup."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List


def _bool(raw: str | None, default: bool = False) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _text(raw, default: str) -> str:
    """Missing or blank text keeps the default (`--data-dir ""` crashed startup)."""
    if raw is None:
        return default
    value = str(raw).strip()
    return value or default


def _int(raw: str | None, default: int) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _size(raw: str | None, default: int) -> int:
    """Byte size: 0 legitimately means "never rotate", a negative value is a typo."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _float(raw: str | None, default: float, minimum: float = 0.0) -> float:
    """Tolerant float parse: a typo in one env var must not abort startup."""
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value != value:                 # NaN
        return default
    return value if value >= minimum else default


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8790
    data_dir: str = "accounts"
    api_key: str = ""                # single pre-shared key ("" = open, warns at startup)
    panel_password: str = "admin"    # dashboard password, PBKDF2-hashed at rest
    # upstream behaviour
    upstream_timeout: float = 600.0
    connect_timeout: float = 30.0
    sse_heartbeat: float = 5.0       # seconds between client-facing `: ping` comments
    refresh_margin: float = 180.0    # seconds before access-token expiry to pre-emptively refresh
    max_retries: int = 3
    max_concurrency: int = 8      # in-flight upstream turns across all clients
    cooldown_wait: float = 10.0   # seconds we are willing to wait out a short account cooldown
    session_ttl: float = 1800.0   # how long an idle conversation is kept for prefix reuse
    keepalive_interval: float = 21600.0  # 6h: refresh tokens in the background so idle sessions never lapse
    default_mode: str = "playground"  # playground | agent
    # feature switches
    expose_reasoning: bool = True
    usage_log: bool = True
    usage_max_bytes: int = 8 * 1024 * 1024
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def accounts_file(self) -> str:
        return os.path.join(self.data_dir, "accounts.json")

    @property
    def settings_file(self) -> str:
        return os.path.join(self.data_dir, "settings.json")

    @property
    def usage_file(self) -> str:
        return os.path.join(self.data_dir, "usage.jsonl")

    @property
    def pid_file(self) -> str:
        return os.path.join(self.data_dir, "gateway.pid")

    @classmethod
    def from_env(cls, argv: List[str] | None = None) -> "Config":
        cfg = cls()

        # Environment first, command line second: an explicit flag must win over an
        # inherited env var, not the other way round.
        cfg.host = _text(os.environ.get("STEP_HOST"), cfg.host)
        cfg.port = _int(os.environ.get("STEP_PORT"), cfg.port)
        cfg.data_dir = _text(os.environ.get("STEP_DATA_DIR"), cfg.data_dir)
        cfg.api_key = os.environ.get("STEP_API_KEY", cfg.api_key)
        cfg.panel_password = _text(os.environ.get("STEP_PANEL_PASSWORD"),
                                   cfg.panel_password)
        cfg.upstream_timeout = _float(os.environ.get("STEP_UPSTREAM_TIMEOUT"),
                                      cfg.upstream_timeout, minimum=1.0)
        cfg.connect_timeout = _float(os.environ.get("STEP_CONNECT_TIMEOUT"),
                                     cfg.connect_timeout, minimum=1.0)
        cfg.sse_heartbeat = _float(os.environ.get("STEP_SSE_HEARTBEAT"), cfg.sse_heartbeat)
        cfg.refresh_margin = _float(os.environ.get("STEP_REFRESH_MARGIN"), cfg.refresh_margin)
        cfg.max_retries = max(0, _int(os.environ.get("STEP_MAX_RETRIES"), cfg.max_retries))
        cfg.max_concurrency = max(1, _int(os.environ.get("STEP_MAX_CONCURRENCY"), cfg.max_concurrency))
        cfg.cooldown_wait = _float(os.environ.get("STEP_COOLDOWN_WAIT"), cfg.cooldown_wait)
        cfg.session_ttl = _float(os.environ.get("STEP_SESSION_TTL"), cfg.session_ttl, minimum=1.0)
        cfg.keepalive_interval = _float(os.environ.get("STEP_KEEPALIVE"), cfg.keepalive_interval)
        cfg.default_mode = os.environ.get("STEP_DEFAULT_MODE", cfg.default_mode)
        cfg.expose_reasoning = _bool(os.environ.get("STEP_EXPOSE_REASONING"), cfg.expose_reasoning)
        cfg.usage_log = _bool(os.environ.get("STEP_USAGE_LOG"), cfg.usage_log)
        cfg.usage_max_bytes = _size(os.environ.get("STEP_USAGE_MAX_BYTES"),
                                    cfg.usage_max_bytes)

        argv = list(argv or [])
        i = 0
        while i < len(argv):
            arg = argv[i]
            if arg in ("--host", "-H") and i + 1 < len(argv):
                cfg.host = _text(argv[i + 1], cfg.host); i += 2; continue
            if arg in ("--port", "-p") and i + 1 < len(argv):
                cfg.port = _int(argv[i + 1], cfg.port); i += 2; continue
            if arg in ("--api-key", "-k") and i + 1 < len(argv):
                cfg.api_key = argv[i + 1]; i += 2; continue
            if arg in ("--panel-password",) and i + 1 < len(argv):
                cfg.panel_password = _text(argv[i + 1], cfg.panel_password); i += 2; continue
            if arg in ("--data-dir",) and i + 1 < len(argv):
                cfg.data_dir = _text(argv[i + 1], cfg.data_dir); i += 2; continue
            if arg in ("--mode",) and i + 1 < len(argv):
                cfg.default_mode = argv[i + 1]; i += 2; continue
            i += 1
        if cfg.default_mode not in ("playground", "agent"):
            cfg.default_mode = "playground"
        if not 0 <= cfg.port <= 65535:
            # An out-of-range port is a typo, not a request to bind port 65535.
            cfg.port = cls.port
        return cfg
