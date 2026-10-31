"""studio.stepfun.com client: auth, RPC helpers, both streaming chat surfaces.

Protocol notes live in CLAUDE.md; the short version:
  * `Oasis-Token` cookie holds `<accessJWT>...<refreshJWT>`
  * identity headers oasis-appid/oasis-platform/oasis-language/oasis-webid
  * chat surface A ("playground") POSTs a whole chat turn to /api/stdhttp/v1/studio/stream
  * chat surface B ("agent") creates a workspace, then reads a resumable SSE event feed
"""

from __future__ import annotations

import codecs
import http.client
import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, Optional, Tuple

DEFAULT_BASE = "https://studio.stepfun.com"


def base_url() -> str:
    """Resolved per call so tests (and self-hosted mirrors) can point us elsewhere.

    Read at call time rather than import time: importing this module must not freeze the
    upstream for the whole process.
    """
    return os.environ.get("STEP_BASE_URL", DEFAULT_BASE).rstrip("/")


BASE = DEFAULT_BASE  # kept for readability in module-level docs; use base_url()
APP_ID = "10300"
PASSPORT_REFRESH = "/passport/proto.api.passport.v1.PassportService/RefreshToken"

EFFORT_MAP = {"none": "low", "minimal": "low", "low": "low", "medium": "medium", "high": "high",
              "xhigh": "xhigh", "max": "xhigh"}


class StudioError(Exception):
    """Normalised upstream failure."""

    def __init__(self, message: str, status: Optional[int] = None, code: str = "",
                 biz_code: str = "", transient: bool = False, retriable_auth: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code or ""
        self.biz_code = biz_code or ""
        self.transient = transient
        self.retriable_auth = retriable_auth


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-run the SSRF checks on every redirect hop.

    urllib follows redirects blindly, so a public URL that 302s to a metadata endpoint
    (or to ftp://) would walk straight past fetch_url's first-hop check.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme not in ("http", "https"):
            raise StudioError("refusing redirect to %s: %s" % (parts.scheme or "?", newurl),
                              status=400)
        if _is_private_host(parts.hostname or ""):
            raise StudioError("refusing redirect to a private address: %s" % parts.hostname,
                              status=400)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _proxy_opener() -> urllib.request.OpenerDirector:
    """Build an opener that honours HTTP(S)_PROXY env vars only.

    urllib's default opener on Windows also consults the WinINET registry and then
    *freezes* whatever it found on first use — a proxy that is later switched off
    would keep breaking token refresh for the life of the process. Passing an
    explicit (possibly empty) handler map avoids both behaviours.
    """
    proxies: Dict[str, str] = {}

    def pick(*names: str) -> str:
        for name in names:
            value = os.environ.get(name)
            if value:
                return value
        return ""

    for env, scheme in (("HTTP_PROXY", "http"), ("HTTPS_PROXY", "https")):
        value = pick(env, env.lower())
        if value:
            proxies[scheme] = value
    # urllib only ever calls <scheme>_open, so a literal "all" key would be dead weight:
    # ALL_PROXY means "use this for every scheme that has no specific override".
    all_proxy = pick("ALL_PROXY", "all_proxy")
    if all_proxy:
        proxies.setdefault("http", all_proxy)
        proxies.setdefault("https", all_proxy)
    # NO_PROXY needs no entry here: urllib's own proxy_bypass() reads it.
    return urllib.request.build_opener(_GuardedRedirectHandler(),
                                       urllib.request.ProxyHandler(proxies))


def _is_private_host(host: str) -> bool:
    """True for loopback/private/link-local targets (SSRF guard for client URLs).

    Attachment URLs come from the request body, so without this a client could point the
    gateway at `127.0.0.1:*` or a cloud metadata endpoint. Set
    STEP_ALLOW_PRIVATE_FETCH=1 when the media legitimately lives on the LAN.
    """
    if os.environ.get("STEP_ALLOW_PRIVATE_FETCH"):
        return False
    host = (host or "").strip().strip("[]")
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False          # unresolvable: the request itself will fail
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return True
    return False


def _cookie_token(headers) -> str:
    """Pull a rotated `Oasis-Token` out of a response's Set-Cookie headers."""
    try:
        values = headers.get_all("Set-Cookie") or []
    except AttributeError:
        single = headers.get("Set-Cookie") if headers is not None else None
        values = [single] if single else []
    for raw in values:
        name, _, rest = str(raw).partition("=")
        if name.strip().lower() != "oasis-token":
            continue
        value = rest.split(";")[0].strip()
        if "..." in value:
            return value
    return ""


def _json_object(raw: bytes, what: str) -> Dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise StudioError("%s returned non-JSON: %s" % (what, exc), transient=True)
    if not isinstance(payload, dict):
        raise StudioError("%s returned %s, not an object" % (what, type(payload).__name__),
                          transient=True)
    return payload


def _split_token(token: str) -> Tuple[str, str]:
    """`<access>...<refresh>` -> (access, refresh). Either half may be empty."""
    token = (token or "").strip()
    if "..." in token:
        access, _, refresh = token.partition("...")
        return access.strip(), refresh.strip()
    return token, ""


def _jwt_claim(token: str, key: str) -> Any:
    """Decode the JWT payload without verifying the signature (server-side auth)."""
    import base64

    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, UnicodeDecodeError):
        return None
    return data.get(key) if isinstance(data, dict) else None


class StudioClient:
    """One authenticated session. Tokens are refreshed in place and persisted by the caller."""

    def __init__(self, token: str, webid: str = "", oasis_id: str = "",
                 connect_timeout: float = 30.0, read_timeout: float = 600.0,
                 refresh_margin: float = 180.0) -> None:
        self.token = token
        self.webid = webid or ""
        self.oasis_id = oasis_id or ""
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.refresh_margin = refresh_margin
        self._lock = __import__("threading").Lock()

    # -- token plumbing --------------------------------------------------
    @property
    def access_token(self) -> str:
        return _split_token(self.token)[0]

    @property
    def refresh_token(self) -> str:
        return _split_token(self.token)[1]

    def access_exp(self) -> float:
        exp = _jwt_claim(self.access_token, "exp")
        return float(exp) if isinstance(exp, (int, float)) else 0.0

    def needs_refresh(self) -> bool:
        exp = self.access_exp()
        return bool(exp) and (exp - time.time()) < self.refresh_margin

    def refresh_exp(self) -> float:
        exp = _jwt_claim(self.refresh_token, "exp")
        return float(exp) if isinstance(exp, (int, float)) else 0.0

    def set_token(self, token: str) -> None:
        with self._lock:
            self.token = token
            if not self.webid:
                device = _jwt_claim(_split_token(token)[1], "device_id") or _jwt_claim(_split_token(token)[0], "device_id")
                if device:
                    self.webid = device

    def refresh(self) -> str:
        """Rotate the access token. The refresh half is the durable credential."""
        with self._lock:
            access, refresh = _split_token(self.token)
            cookie = "Oasis-Token=%s...%s" % (access, refresh)
            req = urllib.request.Request(
                base_url() + PASSPORT_REFRESH, data=b"{}", method="POST",
                headers=self._headers(cookie=cookie, json_body=True),
            )
            try:
                resp = _proxy_opener().open(req, timeout=self.connect_timeout)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")[:300]
                # Same classification as every other RPC: 429/408/425 are "come back
                # later", not "this credential is dead" (they must not park an account).
                decode = self._decode_error(body, exc.code)
                raise StudioError("token refresh failed (%s): %s" % (exc.code, body),
                                  status=exc.code, transient=decode.transient)
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                raise StudioError("token refresh failed: %s" % exc, transient=True)
            try:
                rotated = _cookie_token(getattr(resp, "headers", None))
                try:
                    raw = resp.read()
                except (OSError, http.client.HTTPException) as exc:
                    # Studio rotates the refresh token *before* it writes the body, and
                    # the old one is dead the moment the new one exists. Losing it here
                    # would brick the account, so adopt the cookie even on a short read.
                    if rotated:
                        self._adopt_token(rotated)
                        return self.token
                    raise StudioError("token refresh failed: %s" % exc, transient=True)
            finally:
                try:
                    resp.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                payload = _json_object(raw, "token refresh")
            except StudioError:
                if rotated:
                    self._adopt_token(rotated)
                    return self.token
                raise
            new_access = (payload.get("accessToken") or {}).get("raw") or ""
            new_refresh = (payload.get("refreshToken") or {}).get("raw") or refresh
            if not new_access:
                if rotated:
                    self._adopt_token(rotated)
                    return self.token
                raise StudioError("token refresh returned no access token", transient=True)
            self._adopt_token("%s...%s" % (new_access, new_refresh))
            return self.token

    def _adopt_token(self, token: str) -> None:
        self.token = token
        if not self.webid:
            access, refresh = _split_token(token)
            self.webid = _jwt_claim(refresh, "device_id") or _jwt_claim(access, "device_id") or ""

    # -- request plumbing ------------------------------------------------
    def _headers(self, cookie: Optional[str] = None, json_body: bool = False,
                 accept: str = "application/json") -> Dict[str, str]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
            "Accept": accept,
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8",
            "Origin": base_url(),
            "Referer": base_url() + "/",
            "oasis-appid": APP_ID,
            "oasis-platform": "web",
            "oasis-language": "zh",
        }
        if self.webid:
            headers["oasis-webid"] = self.webid
        if json_body:
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        return headers

    @property
    def _cookie(self) -> str:
        access, refresh = _split_token(self.token)
        parts = ["Oasis-Token=%s...%s" % (access, refresh)]
        if self.webid:
            parts.append("Oasis-Webid=%s" % self.webid)
        return "; ".join(parts)

    def _open(self, req: urllib.request.Request, timeout: float):
        return _proxy_opener().open(req, timeout=timeout)

    def rpc(self, path: str, body: Optional[Dict[str, Any]] = None, retries: int = 1) -> Dict[str, Any]:
        """JSON RPC on the /api/... surface. Raises StudioError with a decoded envelope."""
        data = json.dumps(body or {}).encode("utf-8")
        last: Optional[StudioError] = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(
                base_url() + path, data=data, method="POST",
                headers=self._headers(cookie=self._cookie, json_body=True),
            )
            try:
                with self._open(req, self.connect_timeout) as resp:
                    return _json_object(resp.read(), path.rsplit("/", 1)[-1])
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                raise self._decode_error(raw, exc.code)
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                last = StudioError("upstream unreachable (%s): %s" % (path, exc), transient=True)
                if attempt < retries:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                raise last
            except ValueError as exc:
                last = StudioError("upstream returned non-JSON (%s): %s" % (path, exc), transient=True)
                if attempt < retries:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                raise last
        raise last or StudioError("rpc failed: %s" % path, transient=True)

    @staticmethod
    def _decode_error(raw: str, status: int) -> StudioError:
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return StudioError(raw[:300] or "HTTP %d" % status, status=status,
                               transient=status >= 500)
        code = str(payload.get("code") or "")
        biz = str(payload.get("biz_code") or "")
        message = str(payload.get("message") or "")
        retriable_auth = code in ("unauthenticated",) or "token" in message.lower()
        # 429 means "come back later", not "this account is broken": treat it as
        # transient so the gateway cools the account down and fails over.
        transient = status >= 500 or status == 429 or code in ("unavailable", "internal")
        # Content-policy refusals are deterministic — retrying never helps.
        if biz in ("CENSORED",) or "DataInspectionFailed" in message:
            transient = False
        return StudioError(message or raw[:300], status=status, code=code, biz_code=biz,
                           transient=transient, retriable_auth=retriable_auth)

    # -- conversations ---------------------------------------------------
    def create_conversation(self, workspace_id: str = "") -> str:
        body: Dict[str, Any] = {"type": "CONVERSATION_TYPE_CHAT"}
        if workspace_id:
            body["workspaceId"] = str(workspace_id)
        payload = self.rpc("/api/step.openapi.devcenter.Dashboard/CreateConversation", body)
        conv = payload.get("conversation") or {}
        conv_id = str(conv.get("conversation_id") or "")
        if not conv_id:
            raise StudioError("CreateConversation returned no id")
        return conv_id

    def stop_run(self, message_id: str) -> None:
        try:
            self.rpc("/api/step.openapi.devcenter.Dashboard/StopRun", {"message_id": str(message_id)})
        except StudioError:
            pass

    # -- chat surface A: playground --------------------------------------
    def playground_stream(self, conversation_id: str, chat: Dict[str, Any]):
        """POST one chat turn; returns the raw streaming response object."""
        return self.stream_turn({"conversation_id": str(conversation_id), "kind": "chat", "chat": chat})

    def stream_turn(self, body: Dict[str, Any], timeout: Optional[float] = None):
        """Any `/api/stdhttp/v1/studio/stream` turn: chat, image, audio_tts, audio_asr."""
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            base_url() + "/api/stdhttp/v1/studio/stream", data=data, method="POST",
            headers=self._headers(cookie=self._cookie, json_body=True, accept="text/event-stream"),
        )
        last: Optional[StudioError] = None
        for attempt in range(2):
            try:
                return self._open(req, timeout or self.read_timeout)
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                raise self._decode_error(raw, exc.code)
            except (urllib.error.URLError, OSError) as exc:
                last = StudioError("upstream unreachable (stream): %s" % exc, transient=True)
                if attempt == 0:
                    time.sleep(0.2)
                    continue
                raise last
        raise last or StudioError("stream open failed", transient=True)

    # -- chat surface B: agent workspace ---------------------------------
    def create_workspace(self) -> str:
        payload = self.rpc("/api/step.openapi.devcenter.AIStudio/CreateStudioWorkspace", {})
        ws = payload.get("workspace") or {}
        ws_id = str(ws.get("workspace_id") or "")
        if not ws_id:
            raise StudioError("CreateStudioWorkspace returned no id")
        return ws_id

    def start_run(self, workspace_id: str, conversation_id: str, text: str,
                  model_id: str, thinking: Optional[bool] = None,
                  effort: Optional[str] = None) -> Dict[str, str]:
        model: Dict[str, Any] = {"modelId": model_id}
        config: Dict[str, Any] = {}
        if thinking is not None:
            config["thinking"] = thinking
        if effort:
            config["effort"] = effort
        if config:
            model["config"] = config
        body = {
            "workspaceId": str(workspace_id),
            "conversationId": str(conversation_id),
            "clientRequestId": _uuid4(),
            "text": text,
            "model": model,
        }
        payload = self.rpc("/api/step.openapi.devcenter.AIStudio/StartStudioWorkspaceRun", body)
        return {
            "workspace_id": str(payload.get("workspace_id") or workspace_id),
            "conversation_id": str(payload.get("conversation_id") or conversation_id),
            "user_message_id": str(payload.get("user_message_id") or ""),
            "assistant_message_id": str(payload.get("assistant_message_id") or ""),
        }

    def agent_events(self, workspace_id: str, message_id: str, last_seq: int = 0):
        url = "%s/api/stdhttp/v1/studio/workspaces/%s/messages/%s/events?last_seq=%d" % (
            base_url(), urllib.parse.quote(str(workspace_id)),
            urllib.parse.quote(str(message_id)), last_seq)
        req = urllib.request.Request(url, method="GET",
                                     headers=self._headers(cookie=self._cookie, accept="text/event-stream"))
        last: Optional[StudioError] = None
        for attempt in range(2):
            try:
                return self._open(req, self.read_timeout)
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                raise self._decode_error(raw, exc.code)
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                last = StudioError("upstream unreachable (events): %s" % exc, transient=True)
                if attempt == 0:
                    time.sleep(0.2)
                    continue
                raise last
        raise last or StudioError("event stream open failed", transient=True)

    def upload_file(self, filename: str, mime: str, data: bytes) -> str:
        """POST /api/stdhttp/v1/studio/file (multipart) -> asset_id."""
        boundary = "----step2api" + _uuid4().replace("-", "")
        # Filename and mime come from client payloads: strip anything that could
        # close the header block or open a new part.
        safe_name = _safe_filename(filename)
        safe_mime = _safe_mime(mime)
        head = ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
                'Content-Type: %s\r\n\r\n') % (boundary, safe_name, safe_mime)
        body = head.encode("utf-8") + data + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
        headers = self._headers(cookie=self._cookie)
        headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
        req = urllib.request.Request(base_url() + "/api/stdhttp/v1/studio/file", data=body,
                                     headers=headers, method="POST")
        try:
            with self._open(req, max(self.connect_timeout, 120.0)) as resp:
                payload = _json_object(resp.read(), "upload")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            raise self._decode_error(raw, exc.code)
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise StudioError("upload failed: %s" % exc, transient=True)
        if payload.get("code") not in (0, None):
            raise StudioError(str(payload.get("message") or "upload rejected"),
                              status=400, biz_code=str(payload.get("biz_code") or ""))
        asset = (payload.get("data") or {}).get("asset") or {}
        asset_id = str(asset.get("asset_id") or "")
        if not asset_id:
            raise StudioError("upload returned no asset id", status=502)
        return asset_id

    @staticmethod
    def fetch_url(url: str, timeout: float = 120.0, max_bytes: Optional[int] = None):
        """GET an absolute URL; returns (bytes, content_type). No auth headers are added —
        signed object-storage links carry their own credentials.

        `max_bytes` bounds the download *while it happens*: an attachment URL comes from
        the request body and may point at a host that streams forever, so `resp.read()`
        would buffer the whole thing before the caller's own size check could run.
        """
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise StudioError("unsupported URL scheme: %s" % (parts.scheme or "?"), status=400)
        if _is_private_host(parts.hostname or ""):
            raise StudioError("refusing to fetch a private address: %s" % parts.hostname,
                              status=400)
        req = urllib.request.Request(url, method="GET")
        try:
            with _proxy_opener().open(req, timeout=timeout) as resp:
                ctype = resp.headers.get("Content-Type") or ""
                declared = (resp.headers.get("Content-Length") or "").strip()
                if max_bytes and declared.isdigit() and int(declared) > max_bytes:
                    raise StudioError("asset download too large (%s bytes)" % declared,
                                      status=400)
                if not max_bytes:
                    return resp.read(), ctype
                chunks = []
                total = 0
                while True:
                    block = resp.read(65536)
                    if not block:
                        break
                    total += len(block)
                    if total > max_bytes:
                        raise StudioError("asset download exceeds %d bytes" % max_bytes,
                                          status=400)
                    chunks.append(block)
                return b"".join(chunks), ctype
        except urllib.error.HTTPError as exc:
            raise StudioError("asset download failed (%s)" % exc.code, status=502, transient=exc.code >= 500)
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise StudioError("asset download failed: %s" % exc, transient=True)

    @classmethod
    def fetch_bytes(cls, url: str, timeout: float = 120.0) -> bytes:
        data, _ctype = cls.fetch_url(url, timeout)
        return data

    # -- catalogue -------------------------------------------------------
    def playground_config(self) -> Dict[str, Any]:
        return self.rpc("/api/step.openapi.devcenter.Dashboard/GetStudioPlaygroundConfig", {})

    def rate_limit(self) -> Dict[str, Any]:
        return self.rpc("/api/step.openapi.devcenter.Dashboard/QueryStudioRateLimit", {})


_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_MIME = re.compile(r"^[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+$")


def _safe_filename(name: str) -> str:
    base = os.path.basename(str(name or "").replace('"', ""))
    cleaned = _UNSAFE_FILENAME.sub("_", base).strip("._") or "file"
    return cleaned[:120]


def _safe_mime(mime: str) -> str:
    value = str(mime or "").strip().lower()
    return value if _SAFE_MIME.match(value) else "application/octet-stream"


def _uuid4() -> str:
    import uuid

    return str(uuid.uuid4())


def iter_sse(resp) -> Iterator[Dict[str, Any]]:
    """Yield decoded `data:` payloads from an SSE response, ignoring `event:`/heartbeats."""
    buf = ""
    seen = False
    carry_cr = False
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    while True:
        try:
            chunk = resp.read1(8192)
        except Exception as exc:  # http.client raises HTTPException subtypes (e.g. IncompleteRead)
            # A reset after bytes have already arrived is just a truncated stream; the
            # translator will close out whatever was received. Before that it is a real
            # connection failure worth retrying on another attempt/account.
            if buf or seen:
                break
            raise StudioError("upstream stream read failed: %s" % exc, transient=True)
        if not chunk:
            buf += decoder.decode(b"", True)
            break
        text = decoder.decode(chunk)
        # The frame separator must match across chunk boundaries, so normalise CRLF
        # here while remembering a lone trailing CR until the next chunk arrives.
        if carry_cr:
            text = "\r" + text
        carry_cr = text.endswith("\r")
        if carry_cr:
            text = text[:-1]
        buf += text.replace("\r\n", "\n")
        seen = True
        while "\n\n" in buf:
            frame, buf = buf.split("\n\n", 1)
            payload = _parse_frame(frame)
            if payload is not None:
                yield payload
    if carry_cr:
        buf += "\r"
    if buf.strip():
        payload = _parse_frame(buf)
        if payload is not None:
            yield payload


def iter_sse_closing(resp) -> Iterator[Dict[str, Any]]:
    """`iter_sse` that also closes the upstream response and enforces the terminator.

    A client that disconnects mid-stream abandons this generator; without the finally the
    upstream socket (and its server-side generation) would linger until GC.
    """
    terminated = False
    try:
        for payload in iter_sse(resp):
            if payload.get("type") in ("done", "error"):
                terminated = True
            yield payload
        if not terminated:
            # Studio always ends a turn with `done`; a connection that dies first would
            # otherwise be dressed up as a complete answer (finish_reason "stop"). An
            # empty body counts as a cut too: a 200 with no events is not a finished turn,
            # and treating it as one handed the client an empty "successful" answer.
            raise StudioError("upstream stream ended before done", transient=True,
                              code="stream_truncated")
    finally:
        try:
            if not getattr(resp, "isclosed", lambda: False)():
                resp.close()
        except Exception:  # noqa: BLE001
            pass


def _parse_frame(frame: str) -> Optional[Dict[str, Any]]:
    lines = [ln for ln in frame.replace("\r\n", "\n").split("\n") if ln.startswith("data:")]
    if not lines:
        return None
    raw = "\n".join(ln[5:].strip() for ln in lines)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None
