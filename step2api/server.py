"""HTTP front end: OpenAI-compatible routes, SSE framing, dashboard API."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import select
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlparse

from . import __version__
from .adapters import (AnthropicAdapter, CompletionsAdapter, ResponsesAdapter,
                       anthropic_to_internal, has_web_search, responses_to_internal)
from .client import StudioClient, StudioError, iter_sse_closing
from .config import Config
from .dashboard import DASHBOARD_HTML
from .media import (load_document, load_image, normalize_format, resolve_voice,
                    synthesize, transcribe)
from .store import Account, Store, hash_password, verify_password
from .translate import (ASR_MODEL_ALIASES, TTS_MODEL_ALIASES, Msg, Translator,
                        build_chat_config, build_system_prompt, effective_model,
                        effort_for_thinking_budget, error_payload, map_audio_model,
                        map_upstream_error, normalize_messages, render_delta, render_transcript,
                        split_system)

# ---------------------------------------------------------------------------
# Conversation affinity
# ---------------------------------------------------------------------------


class Slot:
    """Idempotent release for one concurrency permit (also feeds the /health counter)."""

    __slots__ = ("_gw", "_done")

    def __init__(self, gateway) -> None:
        self._gw = gateway
        self._done = False

    def release(self) -> None:
        if self._done:
            return
        self._done = True
        try:
            self._gw.inflight.release()
        except ValueError:
            pass
        with self._gw.inflight_lock:
            self._gw.inflight_now = max(0, self._gw.inflight_now - 1)


class Session:
    __slots__ = ("mode", "conversation_id", "workspace_id", "sent", "updated_at", "account_id")

    def __init__(self, mode: str, conversation_id: str = "", workspace_id: str = "") -> None:
        self.mode = mode
        self.conversation_id = conversation_id
        self.workspace_id = workspace_id
        self.sent: List[Tuple[str, str, str, str]] = []
        self.updated_at = time.time()
        self.account_id = ""


class SessionCache:
    """Maps an OpenAI history prefix to a live upstream conversation.

    OpenAI clients resend the whole transcript every turn while Studio keeps history
    server-side. When the incoming history still starts with what we already sent, only
    the new tail goes upstream; otherwise we start a fresh conversation and replay.
    """

    def __init__(self, ttl: float = 1800.0, max_entries: int = 1024) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self._data: Dict[str, Session] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Session]:
        with self._lock:
            session = self._data.get(key)
            if not session:
                return None
            if time.time() - session.updated_at > self.ttl:
                self._data.pop(key, None)
                return None
            return session

    def take(self, key: str) -> Optional[Session]:
        """Check a session out: removed until put() publishes it again.

        Two concurrent identical requests must not write turns into the same upstream
        conversation -- the second answer would be conditioned on the first, and
        `session.sent` would end up describing whichever finished last. While checked
        out, a look-alike request misses and starts a fresh conversation instead.
        """
        with self._lock:
            session = self._data.pop(key, None)
        if session and time.time() - session.updated_at > self.ttl:
            return None
        return session

    def put(self, key: str, session: Session) -> None:
        with self._lock:
            session.updated_at = time.time()
            self._data[key] = session
            if len(self._data) > self.max_entries:
                oldest = sorted(self._data.items(), key=lambda kv: kv[1].updated_at)
                for stale_key, _ in oldest[: len(self._data) - self.max_entries]:
                    self._data.pop(stale_key, None)

    def drop(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def count(self) -> int:
        with self._lock:
            return len(self._data)


def session_key(tenant: str, model: str, system_prompt: str, messages: List[Msg], mode: str,
                user: str = "") -> str:
    first_user = next((m.text for m in messages if m.role == "user"), "")
    blob = "\x00".join((tenant, user, mode, model, system_prompt, first_user))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Gateway (accounts, models, chat orchestration)
# ---------------------------------------------------------------------------


def _client_fault(exc: "StudioError") -> bool:
    """True when a 4xx blames the *request*, not the account.

    Retrying or cooling an account for a bad model id / attachment / prompt only makes
    the pool look broken to everyone else, and the same request would fail identically
    on every account anyway.
    """
    status = int(getattr(exc, "status", 0) or 0)
    return 400 <= status < 500 and status not in (401, 403, 429)


class Gateway:
    def __init__(self, cfg: Config, store: Store) -> None:
        self.cfg = cfg
        self.store = store
        self.sessions = SessionCache(ttl=cfg.session_ttl)
        self._clients: Dict[str, StudioClient] = {}
        self._clients_lock = threading.Lock()
        self._models_cache: Tuple[float, List[Dict[str, Any]], Dict[str, Dict[str, Any]]] = (0.0, [], {})
        self._models_lock = threading.Lock()
        self._quota_cache = (0.0, {})
        self._asset_cache = {}
        self._asset_lock = threading.Lock()
        self.inflight = threading.BoundedSemaphore(max(1, cfg.max_concurrency))
        self.inflight_lock = threading.Lock()
        self.inflight_now = 0
        self._keepalive_stop = threading.Event()
        self.panel_sessions: Dict[str, float] = {}
        self._panel_lock = threading.Lock()
        # (failures, locked_until) per client address: the panel password is the only
        # thing between a LAN/local attacker and the user's Studio quota.
        self.panel_failures: Dict[str, tuple] = {}
        # One refresh at a time per account: Studio kills the old refresh token on every
        # rotation, so two concurrent refreshes of the same chain can lose the winner.
        self._refresh_locks: Dict[str, Any] = {}
        self._refresh_locks_guard = threading.Lock()
        # Serialises "look up by oasis_id then insert" in the panel's add flow.
        self.add_lock = threading.Lock()

    def no_account_reason(self) -> str:
        """Why no account can serve right now, phrased for the caller's log/error body."""
        accounts = self.store.accounts
        if not accounts:
            return "none configured (add one in the dashboard)"
        parked = sum(1 for a in accounts if a.disabled)
        cooling = sum(1 for a in accounts if a.cooling and not a.disabled)
        return "%d account(s): %d disabled, %d cooling down" % (len(accounts), parked, cooling)

    def refresh_lock(self, key: str) -> threading.Lock:
        with self._refresh_locks_guard:
            lock = self._refresh_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._refresh_locks[key] = lock
            return lock

    # -- accounts --------------------------------------------------------
    def client_for(self, account: Account) -> StudioClient:
        with self._clients_lock:
            client = self._clients.get(account.id)
            if client is None:
                client = StudioClient(account.token, account.webid, account.oasis_id,
                                      connect_timeout=self.cfg.connect_timeout,
                                      read_timeout=self.cfg.upstream_timeout,
                                      refresh_margin=self.cfg.refresh_margin)
                self._clients[account.id] = client
            # Deliberately NOT pushing account.token back into a live client: a concurrent
            # refresh() may have just rotated it, and overwriting would lose the new
            # refresh token (Studio invalidates the old one). sync_account() is the only
            # writer of account.token.
            if account.webid and not client.webid:
                client.webid = account.webid
            return client

    def sync_account(self, account: Account, client: StudioClient) -> None:
        if account.token != client.token:
            account.token = client.token
        if not account.oasis_id:
            from .client import _jwt_claim, _split_token

            access, refresh = _split_token(client.token)
            oasis_id = _jwt_claim(refresh, "oasis_id") or _jwt_claim(access, "oasis_id")
            if oasis_id:
                account.oasis_id = str(oasis_id)
        if client.webid and account.webid != client.webid:
            account.webid = client.webid
        account.updated_at = time.time()
        try:
            self.store.touch(account)
        except OSError as exc:
            # The turn itself is fine; failing it would not recover the file. Keep the
            # rotated token in memory and let the next successful save persist it.
            sys.stderr.write(("[step2api] could not persist account %s: %s"
                              "\n") % (account.name, exc))

    def _with_auth_retry(self, account: Account, client: StudioClient, call):
        """Run `call`, healing a stale access token and retrying once.

        An RPC answering `unauthenticated` while the JWT still looks valid means the
        upstream revoked or rotated early: the credential is the problem, not the account,
        so refreshing and retrying beats cooling the account down.
        """
        try:
            return call()
        except StudioError as exc:
            if not exc.retriable_auth:
                raise
            self._ensure_fresh(account, client, force=True)
            return call()

    def _ensure_fresh(self, account: Account, client: StudioClient, force: bool = False) -> None:
        if not (force or client.needs_refresh()):
            return
        with self.refresh_lock(account.id):
            # Re-check under the lock: a concurrent turn may have refreshed already, and
            # refreshing twice in a row would rotate away a token nobody has read yet.
            if force or client.needs_refresh():
                client.refresh()
                self.sync_account(account, client)

    def candidate_accounts(self, exclude: Optional[List[str]] = None) -> List[Account]:
        exclude = set(exclude or [])
        accounts = [a for a in self.store.accounts if a.available() and a.id not in exclude]
        # round-robin start point
        for _ in range(len(accounts)):
            picked = self.store.pick()
            if picked and picked.id in {a.id for a in accounts}:
                accounts.sort(key=lambda a: 0 if a.id == picked.id else 1)
                break
        return accounts

    # -- background token keep-alive -------------------------------------
    def should_keepalive(self, account: Account, now: Optional[float] = None,
                         refresh_window: float = 10 * 86400, access_window: float = 600.0) -> bool:
        """True when an account's credentials are close enough to expiry to renew now.

        Studio rotates the refresh token on every refresh, so touching it regularly keeps a
        session alive indefinitely — without it, an idle account dies 30 days after issue.
        """
        if account.disabled or not account.token:
            return False
        now = time.time() if now is None else now
        client = self.client_for(account)
        access_exp = client.access_exp()
        refresh_exp = client.refresh_exp()
        if access_exp and access_exp - now < access_window:
            return True
        if refresh_exp and refresh_exp - now < refresh_window:
            return True
        return False

    def keepalive_once(self) -> int:
        renewed = 0
        for account in self.store.accounts:
            if not self.should_keepalive(account):
                continue
            client = self.client_for(account)
            try:
                with self.refresh_lock(account.id):
                    client.refresh()
                    self.sync_account(account, client)
                account.mark_ok()
                renewed += 1
            except StudioError as exc:
                account.mark_error(exc.message, cooldown=0)
                try:
                    self.store.touch(account)      # persist the failure, best effort
                except OSError:
                    pass
                print("[step2api] keepalive failed for %s: %s" % (account.name, exc.message),
                      file=sys.stderr)
        return renewed

    def prewarm(self) -> None:
        """Warm tokens and the model catalogue before the first client request."""
        if not self.store.accounts:
            print("[step2api] prewarm: no accounts yet", file=sys.stderr)
            return
        renewed = 0
        try:
            renewed = self.keepalive_once()
        except Exception as exc:  # noqa: BLE001
            print("[step2api] prewarm token error: %s" % exc, file=sys.stderr)
        models_ok = False
        try:
            models, _ = self.models()
            models_ok = bool(models)
        except Exception as exc:  # noqa: BLE001
            print("[step2api] prewarm catalogue error: %s" % exc, file=sys.stderr)
        print("[step2api] prewarm: %d token(s) renewed, catalogue %s"
              % (renewed, "ready" if models_ok else "unavailable"), file=sys.stderr)

    def start_keepalive(self) -> None:
        interval = float(self.cfg.keepalive_interval or 0)
        if interval <= 0:
            return

        def loop() -> None:
            while not self._keepalive_stop.wait(interval):
                try:
                    renewed = self.keepalive_once()
                    if renewed:
                        print("[step2api] keepalive renewed %d account(s)" % renewed, file=sys.stderr)
                except Exception as exc:  # noqa: BLE001 - a background task must never die
                    print("[step2api] keepalive error: %s" % exc, file=sys.stderr)

        threading.Thread(target=loop, name="step2api-keepalive", daemon=True).start()

    # -- permits + media accounting --------------------------------------
    def acquire_slot(self) -> Slot:
        """Take a concurrency permit (shared by chat and audio turns)."""
        if not self.inflight.acquire(timeout=60):
            raise StudioError("gateway busy: too many concurrent requests", status=429)
        with self.inflight_lock:
            self.inflight_now += 1
        return Slot(self)

    def log_media(self, kind: str, account, model: str, started: float, status: str,
                  tenant: str = "", error: str = "", client_ua: str = "",
                  request_id: str = "") -> None:
        if not self.cfg.usage_log:
            return
        self.store.log_usage({
            "ts": time.time(),
            "account": account.name if account else "",
            "account_id": account.id if account else "",
            "model": model,
            "mode": kind,
            "tokens_in": 0, "tokens_out": 0, "cached_tokens": 0,
            "duration": round(time.time() - started, 3),
            "ttft": None,
            "status": status,
            "finish_reason": "",
            "error": (error or "")[:300],
            "client": (client_ua or "")[:80],
            "tenant": tenant,
            "request_id": request_id,
        }, self.cfg.usage_file, self.cfg.usage_max_bytes)

    # -- image assets ----------------------------------------------------
    def resolve_assets(self, account: Account, client: StudioClient, images, documents=None):
        """Upload inline/remote attachments (images and documents) -> Studio asset ids.

        Distinct attachments only: a client that replays a transcript repeats the same
        image in every turn, and each repeat used to count against a per-request cap.
        """
        asset_ids = []
        seen = set()
        items = []
        for kind, item in ([("image", src) for src in list(images)]
                           + [("doc", doc) for doc in list(documents or [])]):
            key = json.dumps(item, sort_keys=True) if isinstance(item, dict) else str(item)
            if key in seen:
                continue
            seen.add(key)
            items.append((kind, item))
        if len(items) > MAX_ASSETS_PER_REQUEST:
            # Dropping the extras silently would let the model answer about a request it
            # never fully saw.
            raise StudioError("too many distinct attachments: %d (limit %d per request)"
                              % (len(items), MAX_ASSETS_PER_REQUEST), status=400)
        for kind, item in items:
            loaded = load_image(item) if kind == "image" else load_document(item)
            if not loaded:
                label = item if isinstance(item, str) else type(item).__name__
                raise StudioError("attachment could not be read (%s: %s)" % (kind, str(label)[:80]),
                                  status=400)
            data, mime, name = loaded
            digest = hashlib.sha256(data).hexdigest()
            key = (account.id, digest)
            with self._asset_lock:
                cached = self._asset_cache.get(key)
            if cached:
                asset_ids.append(cached)
                continue
            asset_id = client.upload_file(name, mime, data)
            with self._asset_lock:
                self._asset_cache[key] = asset_id
                if len(self._asset_cache) > 256:
                    for stale in list(self._asset_cache)[:64]:
                        self._asset_cache.pop(stale, None)
            asset_ids.append(asset_id)
        return asset_ids

    # -- quota -----------------------------------------------------------
    def cached_models(self):
        """The catalogue already in memory; never performs a network call (panel polls)."""
        with self._models_lock:
            _stamp, models, index = self._models_cache
        return list(models), dict(index)

    def cached_quota(self):
        """Last known rate-limit snapshot; never performs a network call."""
        return dict(self._quota_cache[1]) if self._quota_cache[1] else {}

    def quota(self, force: bool = False):
        """Upstream Studio rate-limit snapshot (cached for a minute)."""
        stamp, cached = self._quota_cache
        if not force and cached and time.time() - stamp < 60:
            return cached
        for account in self.candidate_accounts()[:2]:
            client = self.client_for(account)
            try:
                self._ensure_fresh(account, client)
                payload = client.rate_limit()
                self.sync_account(account, client)
                self._quota_cache = (time.time(), dict(payload, account=account.name))
                return self._quota_cache[1]
            except StudioError:
                continue
        return cached or {}

    # -- speech ----------------------------------------------------------
    def tts(self, text: str, voice: str, model: str = "", fmt: str = "mp3", tenant: str = "",
            client_ua: str = "", request_id: str = ""):
        started = time.time()
        models, index = self.models()
        meta = index.get("__meta__") or {}
        voice_id = resolve_voice(voice, meta.get("tts_voices") or [])
        fmt_id = normalize_format(fmt, meta.get("tts_formats") or [])
        tts_ids = [m["id"] for m in models if not m.get("chat", True)]
        model_id = effective_model(
            map_audio_model(model, meta.get("tts_model") or "stepaudio-2.5-tts",
                            TTS_MODEL_ALIASES), tts_ids)
        errors = []
        denial = None
        # A name the catalogue does not have is a client typo: every account rejects it
        # identically. A *known* model denied by one account is an entitlement issue and
        # another account may still have it (the catalogue came from just one account).
        known = (not tts_ids) or model_id in tts_ids
        slot = self.acquire_slot()
        try:
            for account in self.candidate_accounts()[:2]:
                client = self.client_for(account)
                try:
                    self._ensure_fresh(account, client)
                    audio, mime = self._with_auth_retry(
                        account, client,
                        lambda: synthesize(client, text, voice_id, model_id, fmt_id))
                    self.sync_account(account, client)
                    account.mark_ok()
                    self.log_media("tts", account, model_id, started, "ok", tenant,
                                   client_ua=client_ua, request_id=request_id)
                    return audio, mime, voice_id, model_id, fmt_id
                except StudioError as exc:
                    errors.append("%s: %s" % (account.name, exc.message))
                    self.log_media("tts", account, model_id, started, "error", tenant,
                                   errors[-1], client_ua=client_ua, request_id=request_id)
                    if _client_fault(exc):
                        if not known:
                            raise
                        denial = denial or exc
                        continue
                    if not exc.transient:
                        account.mark_error(exc.message, cooldown=15)
                        self.store.touch(account)
        finally:
            slot.release()
        if denial is not None:
            # Every candidate account denied a real model: the request is answerable on
            # no account here, so 400 it is — but nobody gets cooled for an entitlement.
            raise denial
        raise StudioError("tts failed (%s)" % ("; ".join(errors) or self.no_account_reason()),
                          status=503)

    def transcribe(self, filename: str, mime: str, data: bytes, model: str = "",
                   tenant: str = "", client_ua: str = "",
                   request_id: str = "") -> Tuple[str, str]:
        started = time.time()
        models, index = self.models()
        meta = index.get("__meta__") or {}
        audio_ids = [m["id"] for m in models if m.get("task") == "audio"]
        model_id = effective_model(
            map_audio_model(model, meta.get("asr_model") or "stepaudio-2.5-asr",
                            ASR_MODEL_ALIASES), audio_ids)
        errors = []
        denial = None
        known = (not audio_ids) or model_id in audio_ids
        slot = self.acquire_slot()
        try:
            for account in self.candidate_accounts()[:2]:
                client = self.client_for(account)
                try:
                    self._ensure_fresh(account, client)
                    def _run_asr():
                        asset_id = client.upload_file(filename or "audio.bin", mime, data)
                        return transcribe(client, asset_id, model_id)

                    text = self._with_auth_retry(account, client, _run_asr)
                    self.sync_account(account, client)
                    account.mark_ok()
                    self.log_media("asr", account, model_id, started, "ok", tenant,
                                   client_ua=client_ua, request_id=request_id)
                    return text, model_id
                except StudioError as exc:
                    errors.append("%s: %s" % (account.name, exc.message))
                    self.log_media("asr", account, model_id, started, "error", tenant,
                                   errors[-1], client_ua=client_ua, request_id=request_id)
                    if _client_fault(exc):
                        if not known:
                            raise
                        denial = denial or exc
                        continue
                    if not exc.transient:
                        account.mark_error(exc.message, cooldown=15)
                        self.store.touch(account)
        finally:
            slot.release()
        if denial is not None:
            raise denial
        raise StudioError("transcription failed (%s)"
                          % ("; ".join(errors) or self.no_account_reason()), status=503)

    # -- model catalogue -------------------------------------------------
    def models(self, force: bool = False):
        with self._models_lock:
            stamp, models, index = self._models_cache
            if not force and models and time.time() - stamp < 300:
                return models, index
        errors = []
        for account in self.candidate_accounts():
            client = self.client_for(account)
            payload = None
            failure = None
            try:
                self._ensure_fresh(account, client)
                payload = client.playground_config()
            except StudioError as exc:
                failure = exc
                if exc.retriable_auth:
                    try:
                        self._ensure_fresh(account, client, force=True)
                        payload = client.playground_config()
                        failure = None
                    except StudioError as exc2:
                        failure = exc2
            except Exception as exc:  # noqa: BLE001
                errors.append("%s: %s" % (account.name, exc))
                continue
            if payload is None:
                # A blip must not park the account; only real rejections cool it down.
                if failure is not None and not failure.transient:
                    account.mark_error(failure.message, cooldown=60)
                    self.store.touch(account)
                errors.append("%s: %s" % (account.name, failure.message if failure else "unknown"))
                continue
            try:
                models, index = parse_model_catalogue(payload)
            except Exception as exc:  # noqa: BLE001
                errors.append("%s: bad catalogue (%s)" % (account.name, exc))
                continue
            account.mark_ok()
            self.sync_account(account, client)
            with self._models_lock:
                self._models_cache = (time.time(), models, index)
            return models, index
        with self._models_lock:
            _, stale_models, stale_index = self._models_cache
        if stale_models:
            # Better a slightly old catalogue than a 503 during a network blip.
            return stale_models, stale_index
        raise StudioError("no healthy account for model catalogue (%s)"
                          % ("; ".join(errors) or self.no_account_reason(),), status=503)


def parse_model_catalogue(payload: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    chat = payload.get("chat") or {}
    models: List[Dict[str, Any]] = []
    index: Dict[str, Dict[str, Any]] = {}
    for meta in chat.get("models") or []:
        if not isinstance(meta, dict) or not meta.get("model"):
            continue
        model_id = str(meta["model"])
        limits = {}
        for item in meta.get("config") or []:
            if isinstance(item, dict) and item.get("key"):
                limits[item["key"]] = {"default": item.get("default_value"),
                                       "min": item.get("min"), "max": item.get("max")}
        thinking = meta.get("thinking") or {}
        models.append({
            "id": model_id,
            "object": "model",
            "created": 0,
            "owned_by": "stepfun",
            "chat": True,          # explicit: the discovery entries below are chat=False
            "display_name": meta.get("display_name") or model_id,
            "context_window": (limits.get("max_tokens") or {}).get("max"),
            "max_output_tokens": (limits.get("max_tokens") or {}).get("max"),
            "multimodal": bool(meta.get("is_multimodal")),
            "thinking": {
                "default_enabled": bool(thinking.get("default_enabled")),
                "effort_options": list(thinking.get("effort_options") or []),
            },
        })
        index[model_id] = meta
    tts = payload.get("tts") or {}
    index["__meta__"] = {
        "chat_default": chat.get("default_model") or "",
        "tts_voices": list(tts.get("voices") or []),
        "tts_formats": list(tts.get("formats") or ["mp3", "wav", "opus"]),
        "tts_model": tts.get("default_model") or "stepaudio-2.5-tts",
        "asr_model": ((payload.get("asr") or {}).get("default_model")) or "stepaudio-2.5-asr",
        "image_model": ((payload.get("image") or {}).get("default_model")) or "step-image-edit-2",
    }
    # image / tts / asr catalogues are advertised for discovery only
    for section, kind in (("image", "image"), ("tts", "audio"), ("asr", "audio")):
        for meta in (payload.get(section) or {}).get("models") or []:
            name = meta.get("model") if isinstance(meta, dict) else None
            if not name:
                continue
            models.append({"id": str(name), "object": "model", "created": 0, "owned_by": "stepfun",
                           "display_name": name, "task": kind, "chat": False})
    return models, index


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


# Seconds a client has to wait after the 3rd, 4th, ... consecutive wrong panel password.
PANEL_LOCK_STEPS = (2.0, 5.0, 15.0, 60.0, 300.0)

_ANTHROPIC_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    500: "api_error",
    502: "api_error",
    503: "overloaded_error",
    504: "api_error",
    529: "overloaded_error",
}


def _metric_value(value: Any) -> str:
    """Prometheus wants a bare number; garbage or NaN must not corrupt the scrape."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0"
    if number != number or number in (float("inf"), float("-inf")):
        return "0"
    return str(int(number)) if number.is_integer() else repr(number)


def _anthropic_error_type(status: int) -> str:
    return _ANTHROPIC_ERROR_TYPES.get(int(status), "api_error")


PEER_CHECK_INTERVAL = 1.0   # seconds between liveness probes while streaming


def peer_closed(sock) -> bool:
    """Has the client hung up? Uses select+MSG_PEEK, so no request data is consumed."""
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        if not readable:
            return False
        return sock.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


def process_rss_mb() -> Optional[float]:
    """Resident set size in MB, stdlib only (used by /health and the soak script)."""
    try:
        if os.name == "nt":
            import ctypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]

            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            psapi = ctypes.WinDLL("psapi")
            kernel32 = ctypes.WinDLL("kernel32")
            # Without restype the HANDLE is truncated to 32 bits and the call silently fails.
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p,
                                                   ctypes.POINTER(Counters), ctypes.c_ulong]
            psapi.GetProcessMemoryInfo.restype = ctypes.c_int
            ok = psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(),
                                            ctypes.byref(counters), counters.cb)
            if ok:
                return round(counters.WorkingSetSize / (1024.0 * 1024.0), 1)
            return None
        with open("/proc/self/statm", "r", encoding="ascii") as fh:
            pages = int(fh.read().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / (1024.0 * 1024.0), 1)
    except Exception:  # noqa: BLE001 - purely informational
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "step2api/" + __version__
    protocol_version = "HTTP/1.1"
    # Per-socket timeout (StreamRequestHandler applies it in setup()). Without it a
    # client that connects and sends nothing parks a thread forever -- and every
    # connection costs a thread on ThreadingHTTPServer.
    timeout = 65

    gw: Gateway  # injected by make_server

    # -- plumbing --------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - quiet by default
        if os.environ.get("STEP_VERBOSE"):
            super().log_message(fmt, *args)

    def version_string(self) -> str:
        """Without this, `BaseHTTPRequestHandler` appends the exact Python version."""
        return "step2api/%s" % __version__

    def handle_one_request(self) -> None:  # noqa: N802 - stdlib naming
        # One handler instance serves every request on a keep-alive connection, so
        # per-request state must be cleared here or only the first request gets it.
        self._body_consumed = False
        self._head_only = False
        self.request_id = ""
        try:
            super().handle_one_request()
        except (ConnectionError, socket.timeout):
            # A closed tab, a proxy reset or an idle keep-alive timeout. There is nobody
            # left to answer, and socketserver would otherwise dump a traceback per event.
            self.close_connection = True

    def _drain_unread_body(self) -> None:
        """Consume a POST body we are not going to parse.

        Every early return (401, 404, 400) still has the request body sitting in the
        socket. Leaving it there desynchronises the next request on a pooled connection.
        """
        if getattr(self, "_body_consumed", False):
            return
        encoding = (self.headers.get("Transfer-Encoding") or "").strip().lower()
        if encoding and encoding != "identity":
            # A chunked body cannot be framed by Content-Length: never let its bytes be
            # read back as the next request line (request smuggling behind a proxy).
            self.close_connection = True
            self._body_consumed = True
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._body_consumed = True
            return
        if length > MAX_JSON_BODY:
            self.close_connection = True
            return
        try:
            self.rfile.read(length)
        except (OSError, ValueError):
            self.close_connection = True
        self._body_consumed = True

    @staticmethod
    def _safe_header(name: str, value: str) -> Dict[str, str]:
        """Only echo a client-supplied model/voice id when it is header-safe.

        `map_audio_model` passes unknown ids through (so typos surface upstream); an id
        containing CR/LF must never reach `send_header`.
        """
        if value and re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", str(value)):
            return {name: str(value)}
        return {}

    def _reject_foreign_origin(self) -> None:
        """A keyless gateway must not be drivable from a foreign web page.

        CORS headers only stop the page from *reading* the reply; a "simple" POST is
        still executed and spends quota (and its timing/size is observable). With an API
        key configured -- either tier: `--api-key`/`STEP_API_KEY` or a panel key -- the key
        is the authorisation and browser callers are expected, so only the keyless mode is
        gated here. (Checking only the store 403'd a legitimately-keyed cross-origin caller
        that `_cors_headers` had already told to proceed.)
        """
        origin = (self.headers.get("Origin") or "").strip()
        if not origin or self.gw.store.api_keys() or self.gw.cfg.api_key:
            return
        if origin == "null":
            raise StudioError("cross-origin request refused: this gateway has no API key",
                              status=403)
        try:
            host = (urlparse(origin).hostname or "").lower()
        except ValueError:
            host = ""
        request_host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        allowed = bool(host) and (host in ("127.0.0.1", "localhost", "::1")
                                  or host == request_host)
        if not allowed:
            raise StudioError(
                "cross-origin request refused: this gateway has no API key; call it from "
                "localhost or generate one in the panel", status=403)

    def _reject_chunked(self) -> None:
        """We frame requests by Content-Length only; a chunked body would be parsed as
        one empty request and then re-parsed as garbage on the same connection."""
        encoding = (self.headers.get("Transfer-Encoding") or "").strip().lower()
        if encoding and encoding != "identity":
            self.close_connection = True
            raise ValueError("chunked request bodies are not supported; send Content-Length")

    def _read_json(self) -> Dict[str, Any]:
        self._reject_chunked()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length < 0:
            length = 0
        if length == 0:
            self._body_consumed = True
            return {}
        if length > MAX_JSON_BODY:
            # Leave the body unread and drop the connection: draining 32 MB+ just to
            # reject it would be worse than a single reconnect.
            self.close_connection = True
            raise ValueError("request body too large (limit %d bytes)" % MAX_JSON_BODY)
        try:
            raw = self.rfile.read(length)
        except socket.timeout:
            self.close_connection = True
            raise ValueError("request body timed out")
        self._body_consumed = True
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("invalid JSON body: %s" % exc)
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _request_headers(self) -> Dict[str, str]:
        """A per-request id the client (and the usage log) can quote in a bug report."""
        rid = getattr(self, "request_id", "")
        return {"X-Request-Id": rid} if rid else {}

    def _send_json(self, status: int, payload: Any, headers: Optional[Dict[str, str]] = None) -> None:
        self._drain_unread_body()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in self._request_headers().items():
            self.send_header(name, value)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        # Never cache API answers: a cached /admin/api/state would keep a dead panel
        # session looking alive, and usage numbers would lag behind reality.
        self.send_header("Cache-Control", "no-store")
        for name, value in self._cors_headers().items():
            self.send_header(name, value)
        self.end_headers()
        if getattr(self, "_head_only", False):
            return
        try:
            self.wfile.write(body)
        except (ConnectionError, socket.timeout):
            self.close_connection = True

    def _send_text(self, status: int, text: str, content_type: str = "text/plain; charset=utf-8",
                   headers: Optional[Dict[str, str]] = None) -> None:
        self._drain_unread_body()
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The dashboard is generated from code in memory; caching it means a restarted
        # gateway keeps serving yesterday's panel.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in self._request_headers().items():
            self.send_header(name, value)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if getattr(self, "_head_only", False):
            return
        try:
            self.wfile.write(body)
        except (ConnectionError, socket.timeout):
            self.close_connection = True

    def _cors_headers(self) -> Dict[str, str]:
        """CORS for the API surface only.

        The panel is same-origin. `/v1` stays open to non-browser clients; a browser
        request is answered only when it comes from a loopback origin or when an API key
        guards the gateway — otherwise any web page the user visits could spend their
        quota through a keyless local gateway.
        """
        if urlparse(self.path).path.startswith("/admin"):
            return {}
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return {"Access-Control-Allow-Origin": "*"}
        host = (urlparse(origin).hostname or "").lower()
        if host in ("localhost", "127.0.0.1", "::1") or self.gw.store.api_keys() or self.gw.cfg.api_key:
            return {"Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Headers": "*",
                    "Access-Control-Allow-Methods": "GET, POST, DELETE, PATCH, OPTIONS",
                    "Vary": "Origin"}
        return {}

    def _error_response(self, exc: Exception) -> None:
        """Send the error in the shape the calling protocol expects.

        The Anthropic SDK reads `error.type` from a `{"type": "error"}` envelope; handing
        it an OpenAI body still raises, but the message is lost.
        """
        status = getattr(exc, "status", None)
        if not status:
            # Gateway-local failures (upstream cut before `done`, a malformed upstream
            # reply, no id in a create call) are not the client's fault: answering 400
            # sent SDK users hunting for a bad parameter that never existed.
            status = 502
        status = int(status)
        message = str(getattr(exc, "message", "") or exc)
        headers = None
        if status in (429, 503):
            # The official SDKs honour Retry-After when backing off; without it a 503
            # (all accounts cooling) turns into a hot retry loop at the client.
            headers = {"Retry-After": str(max(1, int(self.gw.cfg.cooldown_wait)))}
        if urlparse(self.path).path.startswith(("/v1/messages", "/messages")):
            return self._send_json(status, {
                "type": "error",
                "error": {"type": _anthropic_error_type(status), "message": message},
            }, headers)
        return self._send_json(status, error_payload(exc), headers)

    # -- auth ------------------------------------------------------------
    def _presented_key(self) -> str:
        header = self.headers.get("Authorization") or ""
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return (self.headers.get("x-api-key") or "").strip()

    def _authorized(self) -> bool:
        key = self._presented_key()
        stored = self.gw.store.api_keys()
        if stored:
            # Once keys exist they are the ONLY way in. Filtering to enabled keys first
            # meant disabling the last one silently reopened the gateway to everyone.
            return any(entry.get("enabled", True)
                       and hmac.compare_digest(str(entry.get("key") or ""), key)
                       for entry in stored)
        if self.gw.cfg.api_key:
            return hmac.compare_digest(self.gw.cfg.api_key, key)
        return True  # unauthenticated mode: bind to localhost

    def _tenant(self) -> str:
        """Which credential served the request: used for session isolation and the log.

        The key's *label*, never the key itself: usage.jsonl and the CSV export are files
        people hand around, and a key prefix is still key material.
        """
        key = self._presented_key()
        if not key:
            return "anon"
        entry = self.gw.store.find_api_key(key)
        if entry:
            # Name for humans, short hash so two keys sharing a label stay isolated.
            label = str(entry.get("name") or "key")[:32]
            return "%s-%s" % (label, hashlib.sha256(key.encode("utf-8")).hexdigest()[:6])
        if self.gw.cfg.api_key and key == self.gw.cfg.api_key:
            return "env-key"
        return "anon"

    # -- panel auth ------------------------------------------------------
    def _panel_ok(self) -> bool:
        token = self.headers.get("X-Panel-Token") or ""
        if not token:
            return False
        with self.gw._panel_lock:
            expiry = self.gw.panel_sessions.get(token)
            if expiry and expiry > time.time():
                return True
            self.gw.panel_sessions.pop(token, None)
        return False

    def _issue_panel_token(self) -> str:
        import secrets

        token = secrets.token_urlsafe(24)
        with self.gw._panel_lock:
            now = time.time()
            for stale in [k for k, v in self.gw.panel_sessions.items() if v <= now]:
                self.gw.panel_sessions.pop(stale, None)
            self.gw.panel_sessions[token] = now + 12 * 3600
        return token

    # -- routing ---------------------------------------------------------
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        for name, value in self._cors_headers().items():
            self.send_header(name, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    @contextlib.contextmanager
    def _guarded(self):
        """Turn any exception into a response: without this socketserver just drops the
        connection with no status line, which clients read as a protocol error."""
        try:
            yield
        except (ConnectionError, socket.timeout):
            self.close_connection = True
        except StudioError as exc:
            self._error_response(exc)
        except ValueError as exc:
            self._error_response(StudioError(str(exc), status=400))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send_json(500, {"error": {"message": str(exc), "type": "internal_error"}})

    def do_HEAD(self) -> None:  # noqa: N802
        # Probes (curl -I, uptime monitors, reverse proxies) use HEAD: answer with the
        # same status and headers as GET, minus the body.
        self._head_only = True
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        self.request_id = "req_" + os.urandom(8).hex()
        try:
            self._reject_chunked()
            self._reject_foreign_origin()
            if path in ("/ping", "/healthz", "/livez", "/readyz"):
                return self._send_text(200, "pong")
            if path == "/health":
                return self._send_json(200, self._health())
            if path == "/metrics":
                return self._metrics()
            if path == "/":
                # The panel holds a live session token in sessionStorage: a hostile page
                # must not be able to frame it and trick clicks through it.
                return self._send_text(200, DASHBOARD_HTML, "text/html; charset=utf-8",
                                       headers={"X-Frame-Options": "DENY",
                                                "Referrer-Policy": "no-referrer",
                                                # Defence in depth: even an injected script
                                                # cannot load or exfiltrate to another host.
                                                "Content-Security-Policy": PANEL_CSP})
            if path == "/v1/models":
                return self._models()
            if path == "/v1/models/catalogue":
                if not self._authorized():
                    return self._send_json(401, {"error": {"message": "invalid API key",
                                                           "type": "invalid_request_error"}})
                models, _ = self.gw.models()
                return self._send_json(200, {"object": "list", "data": models})
            if path.startswith("/v1/models/"):
                return self._model_detail(path.rsplit("/", 1)[-1])
            if path in ("/v1/audio/voices", "/audio/voices"):
                return self._audio_voices()
            if path == "/admin/api/state":
                return self._admin_state()
            if path == "/admin/api/logs":
                return self._admin_logs()
            if path == "/admin/api/export":
                return self._admin_export()
            if path == "/admin/api/usage.csv":
                return self._admin_usage_csv()
            if path == "/admin/api/panel-check":
                return self._send_json(200, {"ok": self._panel_ok(), "password_default": self._password_is_default()})
            return self._send_json(404, {"error": {"message": "unknown path %s" % path, "type": "invalid_request_error"}})
        except StudioError as exc:
            # Same envelope + Retry-After logic as the POST paths (a 503 from the model
            # catalogue should tell the client how long to wait, too).
            return self._error_response(exc)
        except ValueError as exc:
            return self._error_response(StudioError(str(exc), status=400))
        except (ConnectionError, socket.timeout):
            # Reading the body raced a client that vanished (closed tab, proxy cut).
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._send_json(500, {"error": {"message": str(exc), "type": "internal_error"}})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        self.request_id = "req_" + os.urandom(8).hex()
        try:
            self._reject_chunked()
            self._reject_foreign_origin()
            if path in ("/v1/chat/completions", "/chat/completions"):
                return self._chat_completions()
            if path in ("/v1/messages", "/messages"):
                return self._anthropic_messages()
            if path in ("/v1/messages/count_tokens", "/messages/count_tokens"):
                return self._count_tokens()
            if path in ("/v1/responses", "/responses"):
                return self._responses()
            if path in ("/v1/audio/speech", "/audio/speech"):
                return self._audio_speech()
            if path in ("/v1/audio/transcriptions", "/audio/transcriptions"):
                return self._audio_transcriptions()
            if path in ("/v1/audio/translations", "/audio/translations"):
                return self._send_json(400, {"error": {
                    "message": "audio translations are not available: Studio's ASR "
                               "transcribes without translating; use /v1/audio/transcriptions",
                    "type": "invalid_request_error"}})
            if path in ("/v1/completions", "/completions"):
                return self._completions()
            if path in ("/v1/embeddings", "/embeddings"):
                return self._send_json(400, {"error": {
                    "message": "embeddings are not available: StepFun Studio exposes no "
                               "embedding model", "type": "invalid_request_error"}})
            if path in ("/v1/moderations", "/moderations"):
                return self._send_json(400, {"error": {
                    "message": "moderations are not available: no moderation endpoint upstream",
                    "type": "invalid_request_error"}})
            if path in ("/v1/images/generations", "/images/generations"):
                return self._send_json(400, {"error": {
                    "message": "image generation is unavailable: the upstream account gets "
                               "\"image generation is temporarily unavailable\"",
                    "type": "invalid_request_error"}})
            if path in ("/admin/login",):
                return self._admin_login()
            if path == "/admin/logout":
                return self._admin_logout()
            if path == "/admin/api/accounts":
                return self._admin_add_account()
            if path == "/admin/api/accounts/test":
                return self._admin_test_account()
            if path == "/admin/api/keys":
                return self._admin_add_key()
            if path == "/admin/api/accounts/refresh":
                return self._admin_refresh_all()
            if path == "/admin/api/settings":
                return self._admin_settings()
            if path.startswith("/admin/api/accounts/"):
                return self._admin_account_action(path)
            return self._send_json(404, {"error": {"message": "unknown path %s" % path, "type": "invalid_request_error"}})
        except StudioError as exc:
            return self._error_response(exc)
        except ValueError as exc:
            return self._error_response(StudioError(str(exc), status=400))
        except (ConnectionError, socket.timeout):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._send_json(500, {"error": {"message": str(exc), "type": "internal_error"}})

    def do_DELETE(self) -> None:  # noqa: N802
        with self._guarded():
            self._delete()

    def _delete(self) -> None:
        self._reject_chunked()
        path = urlparse(self.path).path.rstrip("/")
        if not self._panel_ok():
            return self._send_json(401, {"error": {"message": "panel login required"}})
        if path.startswith("/admin/api/accounts/"):
            account_id = path.rsplit("/", 1)[-1]
            return self._send_json(200, {"ok": self.gw.store.remove_account(account_id)})
        if path.startswith("/admin/api/keys/"):
            key = path.rsplit("/", 1)[-1]
            return self._send_json(200, {"ok": self.gw.store.remove_api_key(key)})
        return self._send_json(404, {"error": {"message": "unknown path"}})

    def do_PATCH(self) -> None:  # noqa: N802
        with self._guarded():
            self._patch()

    def _patch(self) -> None:
        self._reject_chunked()
        path = urlparse(self.path).path.rstrip("/")
        if not self._panel_ok():
            return self._send_json(401, {"error": {"message": "panel login required"}})
        if path.startswith("/admin/api/keys/"):
            key = path.rsplit("/", 1)[-1]
            body = self._read_json()
            return self._send_json(200, {"ok": self.gw.store.set_api_key(key, bool(body.get("enabled", True)))})
        if path.startswith("/admin/api/accounts/"):
            account_id = path.rsplit("/", 1)[-1]
            body = self._read_json()
            account = self.gw.store.get_account(account_id)
            if not account:
                return self._send_json(404, {"error": {"message": "no such account"}})
            if "disabled" in body:
                account.disabled = bool(body["disabled"])
                account.cooldown_until = 0.0
                self.gw.store.touch(account)
            return self._send_json(200, {"ok": True, "account": public_account(account)})
        return self._send_json(404, {"error": {"message": "unknown path"}})

    # -- endpoints -------------------------------------------------------
    def _health(self) -> Dict[str, Any]:
        accounts = self.gw.store.accounts
        return {
            "status": "ok",
            "version": __version__,
            "accounts": len(accounts),
            "healthy": sum(1 for a in accounts if a.available()),
            "mode": self.gw.cfg.default_mode,
            "uptime": int(time.time() - START_TIME),
            "inflight": self.gw.inflight_now,
            "threads": threading.active_count(),
            "sessions": self.gw.sessions.count(),
            "rss_mb": process_rss_mb(),
        }

    def _metrics(self) -> None:
        """Prometheus text format: the numbers `/health` reports, plus account totals.

        Deliberately aggregate-only: account names/ids are user data and would need label
        escaping, while the useful signals (is it up, are accounts available, how much has
        been spent) are sums. Scraping does not touch the upstream.
        """
        if not self._authorized():
            return self._send_json(401, {"error": {"message": "invalid API key",
                                                   "type": "invalid_request_error"}})
        accounts = self.gw.store.accounts

        def total(attr: str) -> int:
            return sum(int(getattr(account, attr, 0) or 0) for account in accounts)

        def gauge(name: str, help_text: str, value: float) -> List[str]:
            return ["# HELP %s %s" % (name, help_text), "# TYPE %s gauge" % name,
                    "%s %s" % (name, _metric_value(value))]

        def counter(name: str, help_text: str, value: float) -> List[str]:
            return ["# HELP %s %s" % (name, help_text), "# TYPE %s counter" % name,
                    "%s %s" % (name, _metric_value(value))]

        lines: List[str] = []
        lines += gauge("step2api_up", "1 while the gateway process is serving HTTP.", 1)
        lines += gauge("step2api_uptime_seconds", "Seconds since this process started.",
                       int(time.time() - START_TIME))
        lines += gauge("step2api_inflight_requests",
                       "Upstream turns currently in flight (concurrency permits held).",
                       self.gw.inflight_now)
        lines += gauge("step2api_sessions", "Entries in the conversation-affinity cache.",
                       self.gw.sessions.count())
        lines += gauge("step2api_threads", "Live Python threads in the process.",
                       threading.active_count())
        lines += gauge("step2api_accounts_total", "Accounts in the pool.", len(accounts))
        lines += gauge("step2api_accounts_available", "Accounts that may serve a request now.",
                       sum(1 for account in accounts if account.available()))
        lines += gauge("step2api_accounts_disabled", "Accounts parked (invalid credential).",
                       sum(1 for account in accounts if getattr(account, "disabled", False)))
        lines += gauge("step2api_accounts_cooling", "Accounts in a short error cooldown.",
                       sum(1 for account in accounts
                           if getattr(account, "cooling", False) and not account.disabled))
        lines += counter("step2api_requests_total", "Turns served, counted per account.",
                         total("requests"))
        lines += counter("step2api_tokens_in_total", "Prompt tokens reported by the upstream.",
                         total("tokens_in"))
        lines += counter("step2api_tokens_out_total", "Completion tokens reported by the upstream.",
                         total("tokens_out"))
        lines += counter("step2api_cached_tokens_total", "Prompt tokens served from upstream cache.",
                         total("cached_tokens"))
        body = "\n".join(lines) + "\n"
        return self._send_text(200, body, "text/plain; version=0.0.4; charset=utf-8")

    def _model_detail(self, model_id: str) -> None:
        if not self._authorized():
            return self._send_json(401, {"error": {"message": "invalid API key", "type": "invalid_request_error"}})
        models, _ = self.gw.models()
        for entry in models:
            if entry.get("id") == model_id or entry.get("id") == effective_model(model_id, [m["id"] for m in models]):
                return self._send_json(200, entry)
        return self._send_json(404, {"error": {"message": "model %r not found" % model_id,
                                               "type": "invalid_request_error", "code": "model_not_found"}})

    def _models(self) -> None:
        if not self._authorized():
            return self._send_json(401, {"error": {"message": "invalid API key", "type": "invalid_request_error"}})
        models, _ = self.gw.models()
        return self._send_json(200, {"object": "list", "data": models})

    # -- endpoints -------------------------------------------------------
    # Foreign model families (Claude Code, Codex, generic OpenAI SDK samples) are mapped
    # onto the Studio default so those clients work without editing their model name.
    _FOREIGN_PREFIXES = ("claude", "gpt-", "gpt4", "gpt3", "o1", "o3", "o4", "codex", "chatgpt")

    def _resolve_model(self, requested):
        models, index = self.gw.models()
        chat_ids = [m["id"] for m in models if m.get("chat", True)]
        raw = str(requested or "").strip()
        model = effective_model(raw, chat_ids)
        mapped = False
        if chat_ids and model not in chat_ids:
            lowered = raw.lower()
            if not raw or lowered.startswith(self._FOREIGN_PREFIXES):
                meta = index.get("__meta__") or {}
                fallback = meta.get("chat_default") or chat_ids[0]
                model = fallback if fallback in chat_ids else chat_ids[0]
                mapped = True
            else:
                raise ValueError("model %r is not a chat model; available: %s"
                                 % (model, ", ".join(chat_ids)))
        return model, index.get(model), mapped

    def _resolve_mode(self, body):
        mode = str(body.get("step_mode") or self.headers.get("X-Step-Mode") or self.gw.cfg.default_mode).lower()
        return mode if mode in ("playground", "agent") else "playground"

    def _chat_completions(self) -> None:
        started = time.time()
        if not self._authorized():
            return self._send_json(401, {"error": {"message": "invalid API key", "type": "invalid_request_error"}})
        body = self._read_json()
        try:
            n = int(body.get("n") or 1)
        except (TypeError, ValueError):
            n = 1
        if n > 1:
            raise ValueError("n>1 is not supported: the upstream returns a single candidate")
        messages = normalize_messages(body.get("messages"))
        if not messages:
            raise ValueError("messages must be a non-empty array")

        mode = self._resolve_mode(body)
        model, meta, _mapped = self._resolve_model(body.get("model"))
        thinking = body.get("step_thinking")
        if thinking is None and self.headers.get("X-Step-Thinking") is not None:
            thinking = self.headers.get("X-Step-Thinking", "").lower() not in ("0", "false", "off", "no")
        system_text, rest = split_system(messages)
        tool_choice = body.get("tool_choice")
        tools = [] if _tools_suppressed(tool_choice) else body.get("tools")
        tools_given = bool(tools)
        system_prompt = build_system_prompt(system_text, tools)
        system_prompt = _apply_response_format(system_prompt, body.get("response_format"))
        chat_config = build_chat_config(body, meta, thinking=thinking, effort=body.get("reasoning_effort"))
        web_search = bool(body.get("web_search") or body.get("enable_web_search"))
        stream = bool(body.get("stream"))
        stream_options = body.get("stream_options")
        include_usage = bool(stream_options.get("include_usage")) if isinstance(stream_options, dict) else False
        allowed = _allowed_tool_names(body.get("tools"))
        translator = Translator(model, chunked=stream,
                                include_reasoning=self.gw.cfg.expose_reasoning,
                                include_usage=include_usage, allowed_tools=allowed,
                                stop=_stop_sequences(body.get("stop")))

        run = self._run_upstream(rest, system_prompt, mode, model, chat_config, web_search,
                                 user=str(body.get("user") or ""))
        run["json_mode"] = _is_json_mode(body.get("response_format"))
        max_tokens = chat_config.get("max_tokens")
        if stream:
            self._stream_response(translator, run, rest, model, mode, "openai",
                                  max_tokens, tools_given, started)
        else:
            self._collect_response(translator, run, rest, model, mode, "openai",
                                   max_tokens, tools_given, started)

    def _anthropic_messages(self) -> None:
        started = time.time()
        if not self._authorized():
            return self._send_json(401, {"type": "error", "error": {
                "type": "authentication_error", "message": "invalid x-api-key"}})
        body = self._read_json()
        messages, system_text, tools = anthropic_to_internal(body)
        if not messages:
            raise ValueError("messages must be a non-empty array")
        thinking = None
        if isinstance(body.get("thinking"), dict):
            thinking = bool(body["thinking"].get("type") not in (None, "disabled"))
        mode = self._resolve_mode(body)
        model, meta, _mapped = self._resolve_model(body.get("model"))
        tool_choice = body.get("tool_choice")
        if _tools_suppressed(tool_choice):
            tools = []
        system_prompt = build_system_prompt(system_text, tools)
        # Anthropic SDKs (Claude Code included) express depth as budget_tokens, not as a
        # named effort; dropping it silently made every thinking request run at the default.
        effort = body.get("reasoning_effort") or effort_for_thinking_budget(body.get("thinking"))
        chat_config = build_chat_config(body, meta, thinking=thinking, effort=effort)
        stream = bool(body.get("stream"))
        tools_given = bool(tools)
        adapter = AnthropicAdapter(model, expose_thinking=self.gw.cfg.expose_reasoning
                                   and bool(body.get("thinking")),
                                   allowed_tools=_allowed_tool_names(tools),
                                   stop_sequences=_stop_sequences(body.get("stop_sequences")))
        run = self._run_upstream(messages, system_prompt, mode, model, chat_config,
                                 bool(body.get("web_search")) or has_web_search(body.get("tools")),
                                 user=_nested_user(body.get("metadata")))
        max_tokens = chat_config.get("max_tokens")
        if stream:
            self._stream_response(adapter, run, messages, model, mode, "anthropic",
                                  max_tokens, tools_given, started)
        else:
            self._collect_response(adapter, run, messages, model, mode, "anthropic",
                                   max_tokens, tools_given, started)

    def _responses(self) -> None:
        started = time.time()
        if not self._authorized():
            return self._send_json(401, {"error": {"message": "invalid API key", "type": "invalid_request_error"}})
        body = self._read_json()
        messages, system_text, tools = responses_to_internal(body)
        if not messages:
            raise ValueError("input must not be empty")
        if body.get("previous_response_id"):
            raise ValueError("previous_response_id is not supported: send the full "
                             "conversation in `input`")
        mode = self._resolve_mode(body)
        model, meta, _mapped = self._resolve_model(body.get("model"))
        tool_choice = body.get("tool_choice")
        if _tools_suppressed(tool_choice):
            tools = []
        system_prompt = build_system_prompt(system_text, tools)
        system_prompt = _apply_response_format(system_prompt, _responses_format(body.get("text")))
        reasoning = body.get("reasoning") if isinstance(body.get("reasoning"), dict) else {}
        effort = body.get("reasoning_effort") or reasoning.get("effort")
        chat_config = build_chat_config(body, meta, thinking=None, effort=effort)
        stream = bool(body.get("stream"))
        tools_given = bool(tools)
        adapter = ResponsesAdapter(model, tools_given, allowed_tools=_allowed_tool_names(tools))
        run = self._run_upstream(messages, system_prompt, mode, model, chat_config,
                                 bool(body.get("web_search")) or has_web_search(body.get("tools")),
                                 user=str(body.get("user") or ""))
        run["json_mode"] = _is_json_mode(_responses_format(body.get("text")))
        max_tokens = chat_config.get("max_tokens")
        if stream:
            self._stream_response(adapter, run, messages, model, mode, "responses",
                                  max_tokens, tools_given, started)
        else:
            self._collect_response(adapter, run, messages, model, mode, "responses",
                                   max_tokens, tools_given, started)

    def _completions(self) -> None:
        """Legacy text completions: one prompt in, one text out."""
        started = time.time()
        if not self._authorized():
            return self._send_json(401, {"error": {"message": "invalid API key",
                                                   "type": "invalid_request_error"}})
        body = self._read_json()
        for field, why in (("suffix", "fill-in-the-middle is not supported by the upstream"),
                           ("echo", "echo is not supported"),
                           ("logprobs", "logprobs are not available")):
            if body.get(field):
                raise ValueError("%s: %s; use /v1/chat/completions" % (field, why))
        if int(body.get("n") or 1) > 1:
            raise ValueError("n > 1 is not supported (the upstream returns one candidate)")
        prompt = body.get("prompt")
        if isinstance(prompt, list):
            if len(prompt) != 1 or not isinstance(prompt[0], str):
                raise ValueError("prompt must be a string, or a list holding exactly one string")
            prompt = prompt[0]
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt is required")
        mode = self._resolve_mode(body)
        model, meta, _mapped = self._resolve_model(body.get("model"))
        chat_config = build_chat_config(body, meta, thinking=None)
        stream = bool(body.get("stream"))
        messages = [Msg(role="user", text=prompt)]
        adapter = CompletionsAdapter(model, chunked=stream,
                                     stop=_stop_sequences(body.get("stop")))
        run = self._run_upstream(messages, "", mode, model, chat_config, False,
                                 user=str(body.get("user") or ""))
        max_tokens = chat_config.get("max_tokens")
        if stream:
            self._stream_response(adapter, run, messages, model, mode, "completions",
                                  max_tokens, False, started)
        else:
            self._collect_response(adapter, run, messages, model, mode, "completions",
                                   max_tokens, False, started)

    def _audio_voices(self) -> None:
        if not self._authorized():
            return self._send_json(401, {"error": {"message": "invalid API key", "type": "invalid_request_error"}})
        _, index = self.gw.models()
        meta = index.get("__meta__") or {}
        voices = []
        for item in meta.get("tts_voices") or []:
            if not isinstance(item, dict):
                continue
            voices.append({
                "id": item.get("voice"),
                "name": (item.get("display_name") or {}).get("zh_cn") or item.get("voice"),
                "name_en": (item.get("display_name") or {}).get("en_us") or item.get("voice"),
                "description": (item.get("description") or {}).get("zh_cn") or "",
                "sample_url": item.get("sample_url") or "",
            })
        return self._send_json(200, {"object": "list", "data": voices,
                                     "formats": meta.get("tts_formats") or ["mp3", "wav", "opus"]})

    def _read_multipart(self):
        """Small multipart/form-data reader (cgi was removed in Python 3.13)."""
        import email
        import email.policy

        self._reject_chunked()
        ctype = self.headers.get("Content-Type") or ""
        if "multipart/form-data" not in ctype.lower():
            raise ValueError("expected multipart/form-data")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_UPLOAD_BODY:
            self.close_connection = True
            raise ValueError("upload too large (limit %d bytes)" % MAX_UPLOAD_BODY)
        try:
            raw = self.rfile.read(length) if length > 0 else b""
        except socket.timeout:
            self.close_connection = True
            raise ValueError("upload timed out")
        self._body_consumed = True
        blob = b"Content-Type: " + ctype.encode("utf-8") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw
        message = email.message_from_bytes(blob, policy=email.policy.default)
        fields = {}
        files = {}
        if not message.is_multipart():
            return fields, files
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            name = part.get_param("name", header="content-disposition") or ""
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename:
                files[name or "file"] = (filename, part.get_content_type(), payload)
            else:
                charset = part.get_content_charset() or "utf-8"
                try:
                    fields[name] = payload.decode(charset, "replace")
                except LookupError:            # bogus charset in the part header
                    fields[name] = payload.decode("utf-8", "replace")
        return fields, files

    def _audio_transcriptions(self) -> None:
        if not self._authorized():
            return self._send_json(401, {"error": {"message": "invalid API key", "type": "invalid_request_error"}})
        fields, files = self._read_multipart()
        if not files:
            raise ValueError("file is required (multipart field 'file')")
        filename, mime, data = files.get("file") or next(iter(files.values()))
        if not data:
            raise ValueError("uploaded file is empty")
        text, model_id = self.gw.transcribe(filename, mime, data, str(fields.get("model") or ""),
                                            tenant=self._tenant(),
                                            client_ua=self.headers.get("User-Agent") or "",
                                            request_id=self.request_id)
        # Symmetry with /v1/audio/speech: the alias mapping (`whisper-1` -> the account's
        # ASR model) must be visible without turning on debug logging.
        model_header = self._safe_header("X-Step-Model", model_id)
        fmt = str(fields.get("response_format") or "json").lower()
        if fmt == "text":
            return self._send_text(200, text, headers=model_header)
        if fmt == "verbose_json":
            # Studio returns text only: no timestamps, no per-segment data. Say so with
            # nulls rather than inventing numbers; whisper-style clients still read `text`.
            return self._send_json(200, {"text": text, "task": "transcribe",
                                         "language": None, "duration": None,
                                         "segments": []}, headers=model_header)
        if fmt not in ("json", ""):
            raise ValueError("response_format %r is not supported: Studio returns plain text "
                             "without timestamps (srt/vtt need them)" % fmt)
        return self._send_json(200, {"text": text}, headers=model_header)

    def _audio_speech(self) -> None:
        if not self._authorized():
            return self._send_json(401, {"error": {"message": "invalid API key", "type": "invalid_request_error"}})
        body = self._read_json()
        text = str(body.get("input") or body.get("text") or "").strip()
        if not text:
            raise ValueError("input is required")
        audio, mime, voice_id, model_id, fmt = self.gw.tts(
            text, str(body.get("voice") or ""), str(body.get("model") or ""),
            str(body.get("response_format") or "mp3"), tenant=self._tenant(),
            client_ua=self.headers.get("User-Agent") or "", request_id=self.request_id)
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(audio)))
        for name, value in self._request_headers().items():
            self.send_header(name, value)
        for name, value in (("X-Step-Voice", voice_id), ("X-Step-Model", model_id),
                            ("X-Step-Format", fmt)):
            for header, safe in self._safe_header(name, value).items():
                self.send_header(header, safe)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if getattr(self, "_head_only", False):
            return
        try:
            self.wfile.write(audio)
        except ConnectionError:
            self.close_connection = True

    def _count_tokens(self) -> None:
        """Anthropic pre-flight helper. Studio exposes no tokenizer, so this is an estimate."""
        if not self._authorized():
            return self._send_json(401, {"type": "error", "error": {
                "type": "authentication_error", "message": "invalid x-api-key"}})
        body = self._read_json()
        messages, system_text, tools = anthropic_to_internal(body)
        chars = len(system_text) + sum(len(m.text) for m in messages)
        chars += len(json.dumps(tools, ensure_ascii=False)) if tools else 0
        estimate = max(1, int(chars / 2.2))
        return self._send_json(200, {"input_tokens": estimate})

    # -- shared upstream runner ------------------------------------------
    def _run_upstream(self, messages, system_prompt, mode, model, chat_config, web_search,
                      user="", prepaid_slot=None):
        if prepaid_slot is not None:
            # A non-stream replay continues the *same* client request: re-queuing for a
            # permit would turn a retry into a 60 s wait (then 429) on a busy gateway.
            slot = prepaid_slot
        else:
            if not self.gw.inflight.acquire(timeout=60):
                raise StudioError("gateway busy: too many concurrent requests", status=429)
            with self.gw.inflight_lock:
                self.gw.inflight_now += 1
            slot = Slot(self.gw)
        tenant = self._tenant()
        key = session_key(tenant, model, system_prompt, messages, mode, user=user)
        session = self.gw.sessions.take(key)
        reuse = bool(session and session.conversation_id and _prefix_matches(session.sent, messages))
        if reuse and session.account_id:
            owner = self.gw.store.get_account(session.account_id)
            # Conversation ids are scoped to the account that created them: if that
            # account is unavailable we must start over instead of handing the id
            # to a different account.
            reuse = bool(owner and owner.available())
        if reuse:
            content = render_delta(messages[len(session.sent):])
            pinned_account = session.account_id
        else:
            session = Session(mode)
            content = render_transcript(messages)
            pinned_account = ""
        if not content:
            content = "continue"

        attempts = max(1, self.gw.cfg.max_retries)
        tried = []
        last_error = None
        events = None
        first = None
        account = None
        connected = False      # only a validated first event counts as a served turn

        try:
            for attempt in range(attempts):
                account = self._pick_account(tried, prefer=pinned_account)
                if account is None and not tried:
                    self._wait_for_cooldown()
                    account = self._pick_account(tried, prefer=pinned_account)
                if account is None and pinned_account:
                    # owner vanished between the check and the pick: degrade to a fresh
                    # conversation on whatever account is left rather than failing.
                    session = Session(mode)
                    content = render_transcript(messages) or "continue"
                    pinned_account = ""
                    reuse = False
                    account = self._pick_account(tried)
                if not account:
                    break
                tried.append(account.id)
                client = self.gw.client_for(account)
                try:
                    self.gw._ensure_fresh(account, client)
                    if not reuse or not session.conversation_id:
                        if mode == "agent":
                            session.workspace_id = session.workspace_id or client.create_workspace()
                            session.conversation_id = client.create_conversation(session.workspace_id)
                        else:
                            session.conversation_id = client.create_conversation()
                    turn_text = content
                    surface_prompt = system_prompt
                    if mode == "agent":
                        # the agent surface has no system slot: fold it into the first turn
                        surface_prompt = ""
                        if system_prompt and not reuse:
                            turn_text = system_prompt + "\n\n" + content
                    asset_ids = []
                    if mode == "agent":
                        selected = messages[len(session.sent):] if reuse else messages
                        attachments = sum(len(m.images) + len(m.documents) for m in selected)
                        if attachments:
                            # The workspace has no upload slot; answering anyway would let
                            # the model talk about a file it never saw.
                            raise StudioError(
                                "agent mode accepts no uploaded attachments: %d image/document "
                                "input(s) would be ignored. Send this request with "
                                "step_mode: playground (or change the default mode) for vision "
                                "or document input." % attachments, status=400)
                    else:
                        selected = messages[len(session.sent):] if reuse else messages
                        for msg in selected:
                            if len(msg.images) + len(msg.documents) > MAX_ATTACHMENTS_PER_MESSAGE:
                                raise StudioError(
                                    "too many attachments in one message: %d (limit %d)"
                                    % (len(msg.images) + len(msg.documents),
                                       MAX_ATTACHMENTS_PER_MESSAGE), status=400)
                        images = [src for msg in selected for src in msg.images]
                        documents = [doc for msg in selected for doc in msg.documents]
                        if images or documents:
                            asset_ids = self.gw.resolve_assets(account, client, images, documents)
                    events = self._open_upstream(client, session, mode, turn_text, model,
                                                 chat_config, web_search, surface_prompt, asset_ids)
                    first = next(events, None)
                    if first is None:
                        raise StudioError("upstream closed the stream immediately", transient=True)
                    if first.get("type") == "error":
                        raise map_upstream_error(first.get("data") or {})
                    self.gw.sync_account(account, client)
                    session.account_id = account.id
                    account.mark_ok()
                    connected = True
                    break
                except StudioError as exc:
                    last_error = exc
                    more = attempt + 1 < attempts
                    if exc.retriable_auth and more:
                        try:
                            self.gw._ensure_fresh(account, client, force=True)
                            self.gw.sync_account(account, client)
                            # The credential was the problem, not the account: let the same
                            # one be picked again instead of failing over (a single-account
                            # pool would otherwise give up right after healing itself).
                            tried.pop()
                            continue
                        except StudioError:
                            pass
                    if exc.transient and more:
                        # A single blip must not park the account: cool it down only once the
                        # retry budget is spent.
                        continue
                    # A 4xx (other than auth/rate limits) means the *request* was bad:
                    # cooling the account would punish every other client for one bad
                    # attachment or an unknown model.
                    client_fault = _client_fault(exc)
                    if not client_fault:
                        account.mark_error(exc.message, cooldown=15 if exc.transient else 30)
                        self.gw.store.touch(account)
                    raise
                except ConnectionError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if attempt + 1 < attempts:
                        continue
                    account.mark_error(str(exc), cooldown=15)
                    self.gw.store.touch(account)
                    continue

        except BaseException:
            # Never leak the concurrency permit: a non-retryable upstream error used
            # to bypass the release below and slowly starve the gateway.
            if events is not None:
                try:
                    events.close()
                except Exception:  # noqa: BLE001
                    pass
            slot.release()
            raise
        if not connected:
            # Also covers the path where an attempt failed and a later one found no
            # account left to try: `events` was already assigned, so guarding on it alone
            # returned `first=None` and the caller dressed that up as an empty answer
            # (a 200 with no content) instead of the upstream failure it was.
            if events is not None:
                try:
                    events.close()
                except Exception:  # noqa: BLE001
                    pass
            slot.release()
            if last_error is not None:
                raise last_error
            raise StudioError("no available account: %s" % self.gw.no_account_reason(),
                              status=503)
        # The turn parameters ride along so a *non-stream* retry can replay the request
        # without re-deriving them at the call site (see `_collect_response`).
        return {"events": events, "first": first, "session": session, "key": key,
                "account": account, "reuse": reuse, "slot": slot,
                "system_prompt": system_prompt, "chat_config": chat_config,
                "web_search": web_search, "user": user}

    def _wait_for_cooldown(self) -> None:
        """Sleep out a short cooldown when every account is briefly parked."""
        waiting = [a for a in self.gw.store.accounts
                   if a.token and not a.disabled and not a.available()]
        if not waiting:
            return
        soonest = min(a.cooldown_until for a in waiting) - time.time()
        if soonest <= 0 or soonest > self.gw.cfg.cooldown_wait:
            return
        time.sleep(min(soonest + 0.05, self.gw.cfg.cooldown_wait))

    def _pick_account(self, exclude, prefer=""):
        candidates = [a for a in self.gw.store.accounts if a.available() and a.id not in exclude]
        if not candidates:
            return None
        if prefer:
            for candidate in candidates:
                if candidate.id == prefer:
                    return candidate
            # The conversation owner is unavailable: the caller must start a new
            # conversation because conversation ids are scoped to an account.
            return None
        picked = self.gw.store.pick()
        if picked and picked.id in {a.id for a in candidates}:
            return picked
        return candidates[0]

    def _open_upstream(self, client, session, mode, content, model, chat_config, web_search,
                       system_prompt="", asset_ids=None):
        if mode == "agent":
            run = client.start_run(session.workspace_id, session.conversation_id, content, model,
                                   thinking=chat_config.get("thinking"), effort=chat_config.get("effort"))
            session.workspace_id = run["workspace_id"]
            session.conversation_id = run["conversation_id"]
            resp = client.agent_events(session.workspace_id, run["assistant_message_id"])
        else:
            chat = {
                "model": {"model_id": model, "config": chat_config},
                "content": content,
                "asset_ids": list(asset_ids or []),
                "enable_web_search": web_search,
            }
            if system_prompt:
                chat["system_prompt"] = system_prompt
            resp = client.playground_stream(session.conversation_id, chat)
        return iter_sse_closing(resp)

    # -- response writers ------------------------------------------------
    def _write_error(self, protocol: str, message: str) -> None:
        if protocol in ("anthropic", "responses"):
            if protocol == "anthropic":
                payload = {"type": "error", "error": {"type": "api_error", "message": message}}
            else:
                payload = {"type": "error", "message": message}
            frame = "event: error\ndata: %s\n\n" % json.dumps(payload, ensure_ascii=False)
            self._sse_chunk(frame.encode("utf-8"))
        else:
            self._sse_data({"error": {"message": message, "type": "upstream_error"}})
            self._sse_data("[DONE]")

    def _write_frame(self, protocol, frame):
        if protocol in ("openai", "completions"):
            self._sse_data(frame)
            return
        if protocol in ("anthropic", "responses"):
            body = json.dumps(frame.get("data") if isinstance(frame, dict) else frame, ensure_ascii=False)
            name = frame.get("event") if isinstance(frame, dict) else "message"
            self._sse_chunk(("event: %s\ndata: %s\n\n" % (name, body)).encode("utf-8"))
            return
        self._sse_data(frame)

    def _first_payload_seen(self, protocol, frame):
        if protocol == "completions":
            choices = frame.get("choices") or []
            return bool(choices and str(choices[0].get("text") or ""))
        if protocol == "openai":
            choices = frame.get("choices") or []
            if not choices:
                return False
            delta = choices[0].get("delta") or {}
            return bool(delta.get("content") or delta.get("reasoning_content") or delta.get("tool_calls"))
        name = frame.get("event") if isinstance(frame, dict) else ""
        return name in ("content_block_delta", "response.output_text.delta",
                        "response.function_call_arguments.delta")

    def _stream_response(self, adapter, run, messages, model, mode, protocol,
                         max_tokens, tools_given, started) -> None:
        events, first = run["events"], run["first"]
        session, key, account = run["session"], run["key"], run["account"]

        stop_heartbeat = threading.Event()
        write_lock = threading.Lock()
        last_write = [time.time()]
        ttft = None
        status, error_text = "ok", ""
        headers_sent = False

        try:
            # Inside the try: a client that disconnects while we are opening the upstream
            # stream makes send_response/end_headers raise, and that must still release
            # the concurrency permit and close the upstream socket.
            self._sse_start()
            headers_sent = True

            def heartbeat():
                while not stop_heartbeat.wait(self.gw.cfg.sse_heartbeat):
                    if time.time() - last_write[0] < self.gw.cfg.sse_heartbeat:
                        continue
                    with write_lock:
                        try:
                            self._sse_comment("ping")
                            last_write[0] = time.time()
                        except (ConnectionError, OSError):
                            return

            if self.gw.cfg.sse_heartbeat > 0:
                threading.Thread(target=heartbeat, daemon=True).start()

            def emit(frame):
                nonlocal ttft
                with write_lock:
                    self._write_frame(protocol, frame)
                    last_write[0] = time.time()
                if ttft is None and self._first_payload_seen(protocol, frame):
                    ttft = time.time() - started

            for frame in adapter.feed(first):
                emit(frame)
            last_peer_check = time.time()
            for event in events:
                for frame in adapter.feed(event):
                    emit(frame)
                if adapter.error:
                    break
                # Stop burning upstream tokens as soon as the client walks away: a
                # closed peer keeps accepting buffered writes, so write errors alone can
                # take a whole generation to notice.
                now = time.time()
                if now - last_peer_check >= PEER_CHECK_INTERVAL:
                    last_peer_check = now
                    if peer_closed(self.connection):
                        raise ConnectionAbortedError("client closed the connection")
            if adapter.error:
                # The upstream failed mid-stream: send exactly one terminal sequence and
                # stop. Emitting finish()/[DONE] afterwards would put frames after the
                # terminal marker, which strict SSE clients reject.
                status = "error"
                message = (adapter.error.get("message")
                           or (adapter.error.get("error") or {}).get("message") or "upstream error")
                error_text = str(message)
                with write_lock:
                    self._write_error(protocol, str(message))
                    self._sse_end()
            else:
                for frame in adapter.finish(max_tokens=max_tokens, tools_given=tools_given):
                    emit(frame)
                stop_heartbeat.set()
                with write_lock:
                    if protocol in ("openai", "completions"):
                        self._sse_data("[DONE]")
                    self._sse_end()
        except (ConnectionError, socket.timeout):
            # socket.timeout on write == the client stopped reading; that is its problem,
            # not an upstream failure, and must not pollute the error counter.
            status = "client_closed"
            self.close_connection = True
        except StudioError as exc:
            status = "error"
            error_text = getattr(exc, "message", str(exc))
            if headers_sent:
                with write_lock:
                    try:
                        self._write_error(protocol, getattr(exc, "message", str(exc)))
                        self._sse_end()
                    except OSError:
                        pass
        except Exception as exc:  # noqa: BLE001
            status = "error"
            error_text = str(exc)
            traceback.print_exc()
            if headers_sent:
                with write_lock:
                    try:
                        self._write_error(protocol, str(exc))
                        self._sse_end()
                    except OSError:
                        pass
        finally:
            stop_heartbeat.set()
            try:
                events.close()
            except Exception:  # noqa: BLE001
                pass
            if run.get("slot"):
                run["slot"].release()
            self._record(account, model, mode, adapter, started, ttft, status, error_text)
            if status in ("ok", "client_closed") and adapter.message_id:
                session.sent = [m.key() for m in messages]
                session.sent.append(adapter.assistant_key())
                self.gw.sessions.put(key, session)
            elif status == "error":
                self.gw.sessions.drop(key)


    def _collect_response(self, adapter, run, messages, model, mode, protocol,
                          max_tokens, tools_given, started) -> None:
        events, first = run["events"], run["first"]
        session, key, account = run["session"], run["key"], run["account"]
        status, error_text = "ok", ""
        retried = False
        try:
            while True:
                try:
                    # Buffer the stream before touching the adapter: a cut then raises
                    # while the adapter is still pristine, so replaying the turn on a
                    # fresh conversation is safe. Streaming cannot do this — its bytes
                    # are already on the wire. Stop at the terminal event: Studio may keep
                    # the socket open with heartbeats afterwards, and an upstream `error`
                    # must be raised now rather than after the read timeout.
                    batch = []
                    last_peer = time.time()
                    for event in _chain(first, events):
                        batch.append(event)
                        if event.get("type") in ("done", "error"):
                            break
                        # A non-stream client that walked away is only noticed at the final
                        # write otherwise: the gateway would keep reading (and paying for)
                        # the whole answer, and a cut would even replay it. Same probe as
                        # the streaming path; MSG_PEEK consumes nothing.
                        now = time.time()
                        if now - last_peer >= PEER_CHECK_INTERVAL:
                            last_peer = now
                            if peer_closed(self.connection):
                                raise ConnectionAbortedError("client closed the connection")
                    try:
                        events.close()
                    except Exception:  # noqa: BLE001
                        pass
                    break
                except StudioError as exc:
                    try:
                        events.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self.gw.sessions.drop(key)
                    if (exc.code == "stream_truncated" and not retried
                            and self.gw.cfg.max_retries > 1):
                        # The upstream cut mid-answer. `_run_upstream` takes a fresh
                        # session (the old one was dropped), so the transcript is
                        # replayed into a new conversation instead of appending a
                        # duplicate user turn to the half-written one.
                        retried = True
                        previous = run
                        # Hand the permit over instead of releasing and re-acquiring it.
                        prepaid = previous.get("slot")
                        previous["slot"] = None
                        run = self._run_upstream(
                            messages, previous["system_prompt"], mode, model,
                            previous["chat_config"], previous["web_search"],
                            user=previous["user"], prepaid_slot=prepaid)
                        # Whatever the caller attached to the run (json_mode, …) belongs
                        # to the request, not to the attempt: a replay that lost it would
                        # quietly skip the JSON recovery for this one answer.
                        run.update({k: v for k, v in previous.items()
                                    if k not in ("events", "first", "session", "key",
                                                 "account", "reuse", "slot")})
                        events, first = run["events"], run["first"]
                        session, key, account = (run["session"], run["key"], run["account"])
                        continue
                    if run.get("slot"):
                        run["slot"].release()
                        run["slot"] = None
                    status = "error"
                    error_text = getattr(exc, "message", str(exc))
                    raise

            for event in batch:
                adapter.feed(event)
                if adapter.error:
                    err = adapter.error
                    if isinstance(err.get("error"), dict):
                        err = dict(err["error"], biz_code=err.get("biz_code", ""))
                    raise map_upstream_error(err)
            if protocol == "anthropic":
                adapter.finish(max_tokens=max_tokens, tools_given=tools_given)
                payload = adapter.message(max_tokens=max_tokens)
            elif protocol == "completions":
                payload = adapter.completion(max_tokens=max_tokens, tools_given=tools_given)
            elif protocol == "responses":
                adapter.finish(max_tokens=max_tokens, tools_given=tools_given)
                payload = adapter.response(max_tokens=max_tokens)
                if run.get("json_mode"):
                    for item in payload.get("output") or []:
                        for part in item.get("content") or []:
                            if part.get("type") != "output_text":
                                continue
                            fixed = _extract_json_object(part.get("text") or "")
                            if fixed is not None:
                                part["text"] = fixed
                    payload["output_text"] = "".join(
                        p.get("text") or "" for i in payload.get("output") or []
                        for p in i.get("content") or [] if p.get("type") == "output_text")
            else:
                payload = adapter.completion(max_tokens=max_tokens, tools_given=tools_given)
                if run.get("json_mode"):
                    fixed = _extract_json_object(payload["choices"][0]["message"].get("content") or "")
                    if fixed is not None:
                        payload["choices"][0]["message"]["content"] = fixed
            self._send_json(200, payload)
            session.sent = [m.key() for m in messages]
            session.sent.append(adapter.assistant_key())
            self.gw.sessions.put(key, session)
        except StudioError as exc:
            status = "error"
            error_text = getattr(exc, "message", str(exc))
            self.gw.sessions.drop(key)
            raise
        except ConnectionError:
            # The client left while we were collecting (or while writing the answer):
            # finishing the generation would burn quota for nobody, and a replay would
            # burn a second one. Record it as `client_closed`, like the streaming path.
            status = "client_closed"
            self.close_connection = True
            return
        except Exception as exc:  # noqa: BLE001 - truncated body, decoder bug, anything
            status = "error"
            error_text = str(exc)
            self.gw.sessions.drop(key)
            raise StudioError("upstream failed: %s" % exc, status=502, transient=True)
        finally:
            try:
                events.close()
            except Exception:  # noqa: BLE001
                pass
            if run.get("slot"):
                run["slot"].release()
            self._record(account, model, mode, adapter, started, None, status, error_text,
                         replayed=retried)



    def _record(self, account: Optional[Account], model: str, mode: str, translator: Translator,
                started: float, ttft: Optional[float], status: str, error: str = "",
                replayed: bool = False) -> None:
        usage = translator.usage or {}
        if account:
            account.add_usage(usage.get("tokens_in", 0), usage.get("tokens_out", 0), usage.get("cached_tokens", 0))
        if not self.gw.cfg.usage_log:
            return
        record = {
            "ts": time.time(),
            "account": account.name if account else "",
            "account_id": account.id if account else "",
            "model": model,
            "mode": mode,
            "tokens_in": usage.get("tokens_in", 0),
            "tokens_out": usage.get("tokens_out", 0),
            "cached_tokens": usage.get("cached_tokens", 0),
            "duration": round(time.time() - started, 3),
            "ttft": round(ttft, 3) if ttft else None,
            "status": status,
            # Only a turn that actually finished has a meaningful reason; a client
            # hang-up mid-stream would otherwise be labelled "end_turn".
            "finish_reason": finish_reason_of(translator) if status == "ok" else "",
            "error": (error or "")[:300],
            # Which client actually hit us: the first thing to check when someone asks
            # "is Claude Code / Cline really going through the gateway?".
            "client": str(self.headers.get("User-Agent") or "")[:80],
            "tenant": self._tenant(),
            "request_id": getattr(self, "request_id", ""),
        }
        if replayed:
            # A non-stream cut costs a full extra upstream generation; without this flag
            # the row just says "ok" and the waste is invisible in the log.
            record["replayed"] = True
        self.gw.store.log_usage(record, self.gw.cfg.usage_file, self.gw.cfg.usage_max_bytes)

    # -- SSE framing -----------------------------------------------------
    def _sse_start(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        for name, value in self._request_headers().items():
            self.send_header(name, value)
        for name, value in self._cors_headers().items():
            self.send_header(name, value)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _sse_chunk(self, payload: bytes) -> None:
        self.wfile.write(("%X\r\n" % len(payload)).encode("ascii") + payload + b"\r\n")
        self.wfile.flush()

    def _sse_data(self, data: Any) -> None:
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        self._sse_chunk(("data: " + text + "\n\n").encode("utf-8"))

    def _sse_comment(self, text: str) -> None:
        self._sse_chunk((": " + text + "\n\n").encode("utf-8"))

    def _sse_end(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    # -- admin -----------------------------------------------------------
    def _password_is_default(self) -> bool:
        stored = self.gw.store.settings().get("panel_password")
        return not stored and self.gw.cfg.panel_password == "admin"

    def _check_panel_password(self, password: str) -> bool:
        stored = self.gw.store.settings().get("panel_password")
        if stored:
            return verify_password(password, stored)
        return bool(password) and hmac.compare_digest(password, self.gw.cfg.panel_password)

    def _login_throttle(self, ok: bool) -> int:
        """Record a login attempt; return the number of seconds the client must wait.

        Wrong passwords back off exponentially per client address, so a script cannot
        grind through the panel password even though PBKDF2 already slows each try.
        """
        client = self.client_address[0] if self.client_address else "?"
        now = time.time()
        with self.gw._panel_lock:
            failures: Dict[str, tuple] = self.gw.panel_failures
            if ok:
                failures.pop(client, None)
                return 0
            count, locked_until = failures.get(client, (0, 0.0))
            if locked_until > now:
                return max(1, int(locked_until - now + 0.5))
            count += 1
            delay = 0.0
            if count >= 3:
                delay = PANEL_LOCK_STEPS[min(count - 3, len(PANEL_LOCK_STEPS) - 1)]
                if count == 3:
                    sys.stderr.write(("[step2api] panel login: too many failures from %s, "
                                      "backing off\n") % client)
            failures[client] = (count, now + delay)
            if len(failures) > 512:            # never let the map grow without bound
                for stale in sorted(failures, key=lambda k: failures[k][1])[:128]:
                    failures.pop(stale, None)
        return int(delay + 0.5) if delay else 0

    def _login_gate(self) -> int:
        """Seconds the caller must still wait, without doing any hashing work."""
        client = self.client_address[0] if self.client_address else "?"
        now = time.time()
        with self.gw._panel_lock:
            locked_until = self.gw.panel_failures.get(client, (0, 0.0))[1]
        return max(1, int(locked_until - now + 0.5)) if locked_until > now else 0

    def _admin_login(self) -> None:
        body = self._read_json()
        password = str(body.get("password") or "")
        wait = self._login_gate()
        if wait:
            return self._send_json(
                429,
                {"error": {"message": "too many attempts, retry in %ds" % wait,
                           "type": "rate_limited"}},
                headers={"Retry-After": str(wait)})
        ok = self._check_panel_password(password)
        wait = self._login_throttle(ok)
        if not ok:
            if wait:
                return self._send_json(
                    429,
                    {"error": {"message": "too many attempts, retry in %ds" % wait,
                               "type": "rate_limited"}},
                    headers={"Retry-After": str(wait)})
            return self._send_json(401, {"error": {"message": "wrong password"}})
        token = self._issue_panel_token()
        return self._send_json(200, {"ok": True, "token": token, "password_default": self._password_is_default()})

    def _admin_state(self) -> None:
        if not self._panel_ok():
            return self._send_json(401, {"error": {"message": "panel login required"}})
        accounts = [public_account(a) for a in self.gw.store.accounts]
        keys = [{k: v for k, v in entry.items()} for entry in self.gw.store.api_keys()]
        rows = self.gw.store.read_usage(self.gw.cfg.usage_file, 500)
        # Never touch the upstream from a panel poll: with a stalled upstream each 15 s
        # refresh parked another handler thread for `upstream_timeout` (10 minutes).
        models, _index = self.gw.cached_models()
        model_error = "" if models else "模型目录尚未加载（有请求或重启后会自动拉取）"
        quota = self.gw.cached_quota()
        settings = {k: v for k, v in self.gw.store.settings().items()
                    if k != "panel_password"}
        return self._send_json(200, {
            "version": __version__,
            "mode": self.gw.cfg.default_mode,
            "quota": quota,
            "accounts": accounts,
            "api_keys": keys,
            "models": models,
            "model_error": model_error,
            "usage": rows,
            "summary": self.gw.store.summary(rows),
            "password_default": self._password_is_default(),
            # Never ship the password hash to the browser (offline cracking material).
            "settings": settings,
        })

    def _admin_export(self) -> None:
        """Download accounts.json (contains live credentials — panel auth only)."""
        if not self._panel_ok():
            return self._send_json(401, {"error": {"message": "panel login required"}})
        try:
            with open(self.gw.store.path, "rb") as fh:
                payload = fh.read()
        except OSError as exc:
            return self._send_json(500, {"error": {"message": "cannot read store: %s" % exc}})
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Disposition", 'attachment; filename="step2api-accounts.json"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if getattr(self, "_head_only", False):
            return
        try:
            self.wfile.write(payload)
        except ConnectionError:
            self.close_connection = True

    USAGE_COLUMNS = ("ts", "account", "model", "mode", "tokens_in", "tokens_out",
                     "cached_tokens", "ttft", "duration", "status", "finish_reason", "error",
                     "client", "tenant", "request_id", "replayed")

    def _admin_usage_csv(self) -> None:
        if not self._panel_ok():
            return self._send_json(401, {"error": {"message": "panel login required"}})
        rows = self.gw.store.read_usage(self.gw.cfg.usage_file, 100000)
        lines = [",".join(self.USAGE_COLUMNS)]
        for row in rows:
            cells = []
            for column in self.USAGE_COLUMNS:
                value = row.get(column)
                text = "" if value is None else str(value)
                if text[:1] in ("=", "+", "-", "@", chr(9), chr(13)):
                    # Spreadsheets evaluate a leading =/+/-/@ even inside quotes; the
                    # User-Agent column is client-controlled, so neutralise it.
                    text = "'" + text
                cells.append('"' + text.replace('"', '""') + '"' if any(c in text for c in ',"\r\n') else text)
            lines.append(",".join(cells))
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Disposition", 'attachment; filename="step2api-usage.csv"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if getattr(self, "_head_only", False):
            return
        try:
            self.wfile.write(payload)
        except ConnectionError:
            self.close_connection = True

    def _admin_logs(self) -> None:
        if not self._panel_ok():
            return self._send_json(401, {"error": {"message": "panel login required"}})
        rows = self.gw.store.read_usage(self.gw.cfg.usage_file, 200)
        return self._send_json(200, {"items": rows[::-1]})

    def _admin_logout(self) -> None:
        """Revoke this panel token server-side; the panel clears it client-side too."""
        token = self.headers.get("X-Panel-Token") or ""
        if token:
            with self.gw._panel_lock:
                self.gw.panel_sessions.pop(token, None)
        return self._send_json(200, {"ok": True})

    def _admin_add_account(self) -> None:
        if not self._panel_ok():
            return self._send_json(401, {"error": {"message": "panel login required"}})
        body = self._read_json()
        cookie = str(body.get("cookie") or "").strip()
        token = str(body.get("token") or "").strip()
        webid = str(body.get("webid") or "").strip()
        name = str(body.get("name") or "").strip()
        if cookie:
            parsed_token, parsed_webid = parse_cookie_header(cookie) or ("", "")
            token = parsed_token or token
            webid = parsed_webid or webid
        if not token:
            raise ValueError("需要 Oasis-Token（或粘贴完整 Cookie）")
        if len(token) > 8192:
            # A real Oasis-Token pair is ~600 bytes; anything much larger is a pasted
            # HAR/header dump and would only become a parked row full of junk.
            raise ValueError("Cookie 过长（%d 字节），请只粘贴 Oasis-Token 与 Oasis-Webid"
                             % len(token))
        if not webid:
            webid = jwt_device_id(token)
        oasis_id = jwt_oasis_id(token)
        # Adds are rare but race each other over "is this session already in the pool?",
        # and a refresh of an existing row must not overlap a live turn on that account.
        with self.gw.add_lock:
            existing = None
            if oasis_id:
                # Rows imported before oasis_id was recorded (or added by hand) still
                # belong to the same Studio user: parse it from their stored token.
                existing = next((a for a in self.gw.store.accounts
                                 if a.oasis_id == oasis_id
                                 or (not a.oasis_id and jwt_oasis_id(a.token) == oasis_id)), None)
                if existing is not None and not existing.oasis_id:
                    existing.oasis_id = oasis_id
                    self.gw.store.touch(existing)
            ctx = (self.gw.refresh_lock(existing.id) if existing is not None
                   else contextlib.nullcontext())
            with ctx:
                return self._add_account_locked(token, webid, name, oasis_id, existing)

    def _add_account_locked(self, token: str, webid: str, name: str, oasis_id: str,
                            existing: Optional[Account]) -> None:
        """Validate a pasted credential and commit it. Caller holds the add/refresh locks."""
        account_name = name or ("acct-%d" % (len(self.gw.store.accounts) + 1))
        probe = StudioClient(token, webid, oasis_id,
                             connect_timeout=self.gw.cfg.connect_timeout,
                             read_timeout=self.gw.cfg.upstream_timeout,
                             refresh_margin=self.gw.cfg.refresh_margin)
        rotated = False
        failure: Optional[StudioError] = None
        if existing is None:
            # A brand-new import: rotate up front, because adopting the session means
            # owning its refresh chain (and a dead refresh half must surface here).
            try:
                probe.refresh()
                rotated = True
            except StudioError as exc:
                failure = exc
        if failure is None:
            # Use the pasted access token as-is first: a refresh that turns out to be
            # unnecessary invalidates the very session the user copied (their browser
            # keeps using the old chain and gets logged out).
            try:
                probe.rate_limit()
            except StudioError as first:
                failure = first
                status = int(getattr(first, "status", 0) or 0)
                if first.retriable_auth or status in (401, 403):
                    try:
                        probe.refresh()
                        rotated = True
                        probe.rate_limit()
                        failure = None
                    except StudioError as second:
                        failure = second
        device = jwt_device_id(probe.token) or webid
        if rotated:
            # The old refresh token is dead upstream: probe.token is the only live
            # credential and must be persisted even when validation failed.
            new_token = probe.token
        else:
            new_token = token
        if existing is None:
            account = self.gw.store.add_account(account_name, new_token, device, oasis_id)
            if failure is None:
                account.mark_ok()
                self.gw.store.touch(account)
                return self._send_json(200, {"ok": True, "updated": False,
                                             "detail": "token refreshed",
                                             "account": public_account(account)})
            account.mark_error(failure.message, cooldown=0)
            # Park the row instead of letting a bad paste take turns in the pool and fail
            # requests another (good) account could have served. A transient upstream 5xx
            # says nothing about the credential, so that one stays enabled.
            account.disabled = not failure.transient
            self.gw.store.touch(account)
            detail = failure.message + ("" if failure.transient else "（已停用，修正后重新添加或启用）")
            return self._send_json(200, {"ok": False, "updated": False, "detail": detail,
                                         "account": public_account(account)})
        if failure is not None and not rotated:
            # Nothing was rotated, so the row's own credential is untouched.
            return self._send_json(200, {"ok": False, "updated": True,
                                         "detail": failure.message + "（现有账号未被修改）",
                                         "account": public_account(existing)})
        if failure is not None and existing.token != token:
            # A different session that had to be refreshed to be usable, and failed:
            # the row is fine, but the pasted session itself was spent.
            return self._send_json(200, {"ok": False, "updated": True,
                                         "detail": failure.message
                                         + "（现有账号未被修改；粘贴的会话已刷新，勿再使用旧 Cookie）",
                                         "account": public_account(existing)})
        if name:
            existing.name = name
        client = self.gw.client_for(existing)
        client.set_token(new_token)
        if device:
            client.webid = device
        self.gw.sync_account(existing, client)
        if failure is None:
            existing.mark_ok()
            existing.disabled = False
            self.gw.store.touch(existing)
            return self._send_json(200, {"ok": True, "updated": True,
                                         "detail": "existing account updated",
                                         "account": public_account(existing)})
        existing.mark_error(failure.message, cooldown=0)
        existing.disabled = not failure.transient
        self.gw.store.touch(existing)
        detail = failure.message + ("" if failure.transient else "（已停用，修正后重新添加或启用）")
        return self._send_json(200, {"ok": False, "updated": True, "detail": detail,
                                     "account": public_account(existing)})

    def _admin_test_account(self) -> None:
        if not self._panel_ok():
            return self._send_json(401, {"error": {"message": "panel login required"}})
        body = self._read_json()
        account = self.gw.store.get_account(str(body.get("id") or ""))
        if not account:
            return self._send_json(404, {"error": {"message": "no such account"}})
        client = self.gw.client_for(account)
        try:
            self.gw._ensure_fresh(account, client, force=True)
            self.gw.sync_account(account, client)
            rate = client.rate_limit()
            account.mark_ok()
            # A passing test is the same evidence as a successful add: the credential
            # works, so a parked account goes back into the pool (the FAQ promises that
            # "测试" restores an account, and it already clears the cooldown).
            was_disabled = account.disabled
            account.disabled = False
            self.gw.store.touch(account)
            return self._send_json(200, {"ok": True, "rate_limit": rate,
                                         "re_enabled": was_disabled,
                                         "account": public_account(account)})
        except StudioError as exc:
            account.mark_error(exc.message, cooldown=0)
            self.gw.store.touch(account)
            return self._send_json(200, {"ok": False, "detail": exc.message, "account": public_account(account)})

    def _admin_refresh_all(self) -> None:
        if not self._panel_ok():
            return self._send_json(401, {"error": {"message": "panel login required"}})
        results = []
        for account in self.gw.store.accounts:
            client = self.gw.client_for(account)
            try:
                with self.gw.refresh_lock(account.id):
                    client.refresh()
                    self.gw.sync_account(account, client)
                account.mark_ok()
                results.append({"id": account.id, "ok": True})
            except StudioError as exc:
                account.mark_error(exc.message, cooldown=0)
                results.append({"id": account.id, "ok": False, "detail": exc.message})
            self.gw.store.touch(account)
        return self._send_json(200, {"items": results})

    def _admin_add_key(self) -> None:
        if not self._panel_ok():
            return self._send_json(401, {"error": {"message": "panel login required"}})
        body = self._read_json()
        entry = self.gw.store.add_api_key(str(body.get("name") or "key"))
        return self._send_json(200, {"ok": True, "key": entry})

    def _admin_settings(self) -> None:
        if not self._panel_ok():
            return self._send_json(401, {"error": {"message": "panel login required"}})
        body = self._read_json()
        password = str(body.get("panel_password") or "")
        if password:
            self.gw.store.set_setting("panel_password", hash_password(password))
            # Old sessions must not outlive the password they were created under; keep
            # only the caller's own token so the dashboard does not bounce to login.
            current = self.headers.get("X-Panel-Token") or ""
            with self.gw._panel_lock:
                for token in list(self.gw.panel_sessions):
                    if token != current:
                        self.gw.panel_sessions.pop(token, None)
        if "default_mode" in body:
            mode = str(body["default_mode"])
            if mode in ("playground", "agent"):
                self.gw.cfg.default_mode = mode
                self.gw.store.set_setting("default_mode", mode)
        return self._send_json(200, {"ok": True})

    def _admin_account_action(self, path: str) -> None:
        if not self._panel_ok():
            return self._send_json(401, {"error": {"message": "panel login required"}})
        # /admin/api/accounts/<id>/<action> -> ['', 'admin', 'api', 'accounts', <id>, <action>]
        parts = path.split("/")
        if len(parts) < 5:
            return self._send_json(404, {"error": {"message": "unknown path"}})
        account_id = parts[4]
        action = parts[5] if len(parts) > 5 else ""
        account = self.gw.store.get_account(account_id)
        if not account:
            return self._send_json(404, {"error": {"message": "no such account"}})
        if action == "refresh":
            client = self.gw.client_for(account)
            try:
                with self.gw.refresh_lock(account.id):
                    client.refresh()
                    self.gw.sync_account(account, client)
                account.mark_ok()
                self.gw.store.touch(account)
                return self._send_json(200, {"ok": True, "account": public_account(account)})
            except StudioError as exc:
                account.mark_error(exc.message, cooldown=0)
                self.gw.store.touch(account)
                return self._send_json(200, {"ok": False, "detail": exc.message})
        return self._send_json(404, {"error": {"message": "unknown action"}})


_JSON_INSTRUCTION = (
    "Critical output contract: the entire reply is parsed by json.loads(). Emit exactly one "
    "JSON object and nothing else — no markdown fences, no prose before or after. "
    "\u4f60\u7684\u56de\u7b54\u5fc5\u987b\u4e14\u53ea\u80fd\u662f\u4e00\u4e2a JSON \u5bf9\u8c61\u3002"
)


def _responses_format(text_cfg) -> Optional[Dict[str, Any]]:
    """Translate the Responses API `text.format` block into our `response_format`."""
    if not isinstance(text_cfg, dict):
        return None
    fmt = text_cfg.get("format")
    if not isinstance(fmt, dict):
        return None
    kind = str(fmt.get("type") or "")
    if kind == "json_schema":
        return {"type": "json_schema",
                "json_schema": {"name": fmt.get("name") or "output",
                                "schema": fmt.get("schema") or {}}}
    if kind == "json_object":
        return {"type": "json_object"}
    return None


def _apply_response_format(system_prompt: str, response_format) -> str:
    """Studio has no JSON mode; steer it from the system prompt instead."""
    if not isinstance(response_format, dict):
        return system_prompt
    kind = response_format.get("type")
    if kind == "json_object":
        extra = _JSON_INSTRUCTION
    elif kind == "json_schema":
        schema = (response_format.get("json_schema") or {}).get("schema")
        extra = _JSON_INSTRUCTION
        if schema:
            extra += "\nFollow this JSON Schema exactly:\n" + json.dumps(schema, ensure_ascii=False)
    else:
        return system_prompt
    return (system_prompt + "\n\n" + extra).strip() if system_prompt else extra


def _nested_user(metadata) -> str:
    return str(metadata.get("user_id") or "") if isinstance(metadata, dict) else ""


def _tools_suppressed(choice) -> bool:
    if isinstance(choice, str):
        return choice.strip().lower() == "none"
    if isinstance(choice, dict):
        return str(choice.get("type") or "").lower() == "none"
    return False


def _is_json_mode(response_format) -> bool:
    return isinstance(response_format, dict) and response_format.get("type") in ("json_object", "json_schema")


def _extract_json_object(text: str):
    """Best-effort recovery for non-stream JSON mode (fences, leading prose, trailing notes)."""
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        first_newline = candidate.find("\n")
        if first_newline > 0:
            candidate = candidate[first_newline + 1:]
        if candidate.rstrip().endswith("```"):
            candidate = candidate.rstrip()[:-3]
        candidate = candidate.strip()
    try:
        json.loads(candidate)
        return candidate
    except ValueError:
        pass
    start = candidate.find("{")
    if start < 0:
        return None
    end = candidate.rfind("}")
    while end > start:
        chunk = candidate[start:end + 1]
        try:
            json.loads(chunk)
            return chunk
        except ValueError:
            end = candidate.rfind("}", start, end)
    return None


def _stop_sequences(raw):
    if raw is None:
        return None
    if isinstance(raw, str):
        return [raw] if raw else None
    if isinstance(raw, list):
        stops = [str(item) for item in raw if isinstance(item, str) and item]
        return stops or None
    return None


def _allowed_tool_names(tools):
    """Names the client actually declared — always a set, possibly empty.

    An empty set means "filter everything": a tool call the client never declared
    (Studio's own `web_search`, `document_parser`, or an agent-mode `ls`) has no
    implementation on the client side, so forwarding it hands the caller a call it
    cannot answer.
    """
    names = set()
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = fn.get("name") if isinstance(fn, dict) else None
        if name:
            names.add(str(name))
    return names


def _chain(first: Optional[Dict[str, Any]], rest: Iterator[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
    if first is not None:
        yield first
    yield from rest


def _prefix_matches(sent: List[Tuple[str, str, str, str]], messages: List[Msg]) -> bool:
    if not sent or len(messages) < len(sent):
        return False
    return all(messages[i].key() == sent[i] for i in range(len(sent)))


def parse_cookie_header(raw: str) -> Optional[Tuple[str, str]]:
    """Extract (Oasis-Token, Oasis-Webid) from a pasted Cookie header."""
    token = ""
    webid = ""
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        # A pasted header line ("Cookie: Oasis-Token=...") would otherwise fall through
        # to the bare-token branch and be stored as one giant unusable token.
        raw = raw[len("cookie:"):].strip()
    for part in raw.replace("\n", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip().lower()
        if name == "oasis-token":
            token = value.strip()
        elif name == "oasis-webid":
            webid = value.strip()
    if not token:
        # allow pasting just the token value
        token = raw.strip()
    if not token:
        return None
    return token, webid


def jwt_device_id(token: str) -> str:
    from .client import _jwt_claim, _split_token

    access, refresh = _split_token(token)
    for part in (refresh, access):
        device = _jwt_claim(part, "device_id") if part else None
        if device:
            return str(device)
    return ""


def jwt_oasis_id(token: str) -> str:
    """Studio user id from either half of the token, or "" when it is not a JWT pair."""
    from .client import _jwt_claim, _split_token

    access, refresh = _split_token(token)
    for part in (refresh, access):
        oasis = _jwt_claim(part, "oasis_id") if part else None
        if oasis:
            return str(oasis)
    return ""


def finish_reason_of(translator: Any) -> str:
    """Terminal reason for the usage row; every adapter exposes a ``finish_reason``.

    The vocabulary stays protocol-native (``stop``/``length``/``tool_calls`` for chat,
    ``end_turn``/``tool_use``/``max_tokens`` for messages, ``completed``/``incomplete``
    for responses). Before this, only chat rows had a reason at all.
    """
    value = getattr(translator, "finish_reason", "")
    return str(value) if value else ""


def public_account(account: Account) -> Dict[str, Any]:
    return {
        "id": account.id,
        "name": account.name,
        "oasis_id": account.oasis_id,
        "webid": account.webid,
        "disabled": account.disabled,
        "cooling": account.cooling,
        "cooldown_until": account.cooldown_until,
        "last_ok_at": account.last_ok_at,
        "last_error": account.last_error,
        "error_count": account.error_count,
        "requests": account.requests,
        "tokens_in": account.tokens_in,
        "tokens_out": account.tokens_out,
        "cached_tokens": account.cached_tokens,
        "token_exp": jwt_exp(account.token),
        "refresh_exp": jwt_refresh_exp(account.token),
    }


def jwt_refresh_exp(token: str) -> float:
    from .client import _jwt_claim, _split_token

    _, refresh = _split_token(token)
    exp = _jwt_claim(refresh, "exp") if refresh else None
    return float(exp) if isinstance(exp, (int, float)) else 0.0


def jwt_exp(token: str) -> float:
    from .client import _jwt_claim, _split_token

    access, _ = _split_token(token)
    exp = _jwt_claim(access, "exp") if access else None
    return float(exp) if isinstance(exp, (int, float)) else 0.0


START_TIME = time.time()

PANEL_CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
             "script-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; "
             "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
MAX_JSON_BODY = 32 * 1024 * 1024
MAX_UPLOAD_BODY = 40 * 1024 * 1024
# A single message may carry at most this many attachment parts; a replayed transcript
# may carry more in total, because repeats of the same file are uploaded once.
MAX_ATTACHMENTS_PER_MESSAGE = 8
MAX_ASSETS_PER_REQUEST = 64


class GatewayServer(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind a port that is already
    # listening, so two gateways would silently share traffic -- and, worse, rotate
    # the same account's refresh token against each other. Refuse the steal instead.
    allow_reuse_address = os.name != "nt"


def make_server(cfg: Config, store: Store) -> ThreadingHTTPServer:
    gateway = Gateway(cfg, store)
    handler = type("BoundHandler", (Handler,), {"gw": gateway})
    httpd = GatewayServer((cfg.host, cfg.port), handler)
    httpd.daemon_threads = True
    httpd.gateway = gateway  # type: ignore[attr-defined]
    gateway.start_keepalive()
    threading.Thread(target=gateway.prewarm, name="step2api-prewarm", daemon=True).start()
    return httpd
