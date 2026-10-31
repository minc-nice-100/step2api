# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

`step2api` — an OpenAI-compatible gateway in front of **studio.stepfun.com** (阶跃星辰 AI Studio),
modeled on the reference project in `example/` (qoder2api-hub: single-file stdlib Python proxy +
dashboard + account pool). Shape: one Python process, no third-party deps at runtime,
`/v1/chat/completions` + `/v1/messages` + `/v1/responses`, `/v1/models`, `/health`, plus a panel at `/`.

Invariants worth remembering when editing the gateway:

1. **Conversation affinity is per account.** Studio keeps history server-side against a
   `conversation_id`, and that id only exists for the account that created it. `Session.account_id`
   pins a cached conversation to its owner; if the owner is unavailable the request starts a fresh
   conversation and replays the transcript rather than handing the id to another account.
2. **`assistant_key()` must equal `Msg.key()` byte for byte.** Both are 5-tuples and tool-call
   arguments must be normalised to dicts before hashing (`canonical_tool_calls_json`) — upstream
   sends them as JSON strings while echoed client messages are parsed into dicts. A shape or
   encoding mismatch silently disables reuse for that protocol.
3. **Only the tail goes upstream.** OpenAI clients resend the whole transcript; if the incoming
   history still starts with `session.sent`, only `messages[len(sent):]` is sent. A mismatch
   (edited branch, sliding window, restart) means new conversation + `render_transcript`.
4. **`tool_choice` is only honoured for `"none"`.** Removing the tool catalogue works
   deterministically; `required` / a named tool cannot be forced — probing showed every
   imperative phrasing either did nothing or suppressed legitimate calls, so the gateway does
   not pretend otherwise (see the README unsupported table).
5. **No native `stop` / JSON mode.** `StopFilter` truncates text client-side for OpenAI
   `stop` and Anthropic `stop_sequences` (buffering up to `max(len(stop))-1` chars so a
   sequence spanning two chunks cannot leak). **After a hit, `feed()` must return `""`** —
   the model keeps generating past the sequence and those deltas used to be forwarded
   verbatim; the Anthropic adapter also reports `stop_reason: "stop_sequence"` +
   `stop_sequence`;
   `response_format` is a system-prompt contract plus, for non-stream only, `_extract_json_object`
   recovery. Both are emulations — keep them honest in docs.
6. **Client tools are prompted, not native.** Studio has no OpenAI `tools` field; tool schemas are
   injected into the system prompt (`TOOL_PROTOCOL`), the model answers with `{"name","arguments"}`,
   and Studio reports it as `tool_call_start`/`tool_call_delta`. Anything the model calls that the
   client did not declare (Studio's own built-ins such as `document_parser`) is dropped by the
   adapters' `allowed_tools` filter.
7. **Handler state is per request, not per connection.** `BaseHTTPRequestHandler`
   reuses one instance for every request on a keep-alive connection, and `protocol_version`
   is `HTTP/1.1`. Anything request-scoped (`_body_consumed`, `request_id`, and future
   fields) must be reset in `Handler.handle_one_request`; a flag set once made every later
   early-return (401/404/400) skip draining its body, desynchronising the socket into a
   bogus 400. `request_id` is assigned at the top of `do_GET`/`do_POST`, echoed as
   `X-Request-Id` on every response shape and recorded in `usage.jsonl`.
   `Handler.timeout` bounds every socket: without it an idle or stalled connection parks
   a thread for the life of the process.
8. **Config precedence is CLI > env > defaults, and parsing is tolerant.** An env typo
   (`STEP_UPSTREAM_TIMEOUT=600s`) must fall back to the default rather than abort startup
   with a traceback; negative sizes fall back too (`0` legitimately disables rotation),
   blank text (`STEP_DATA_DIR=`, `--data-dir "  "`) keeps the default, and an
   out-of-range port (`STEP_PORT=70000`) falls back instead of reaching `bind()` as an
   `OverflowError`. The panel's stored `default_mode` is a *third*, lower tier: it only
   applies when neither `--mode` nor `STEP_DEFAULT_MODE` was given (an unconditional
   override made `--mode playground` silently launch in agent mode).
9. **A cached session is checked out, not shared.** `SessionCache.take()` removes the
   session while a turn is in flight and `put()` publishes it again. Two overlapping turns
   with the same history must not be appended to one upstream conversation — the second
   answer would be conditioned on the first, and `sent` would describe whichever finished
   last. (The e2e gateway runs `max_concurrency=1`, so overlap tests need their own server.)
10. **Transport failures come in two families.** `urllib.error.URLError/OSError` *and*
   `http.client.HTTPException` (e.g. `IncompleteRead` when the chunked body is cut). Every
   request path catches both. `refresh()` additionally adopts `Set-Cookie: Oasis-Token`
   before reading the body: Studio rotates the token server-side first, so a truncated
   refresh response must not lose the new credential. An auth failure flagged
   `retriable_auth` refreshes and retries **the same account** (`tried.pop()`): marking it
   tried and failing over meant a single-account pool gave up right after healing itself.
   TTS/ASR go through `Gateway._with_auth_retry` for the same reason.

11. **Provider built-ins never reach the client.** Studio runs `web_search` and
    `document_parser` itself; their `tool_call` events arrive on the same stream as client
    tool calls. `_allowed_tool_names()` therefore returns a (possibly empty) set — never
    `None` — so anything the client did not declare is dropped. Declaring
    `web_search_preview` / `web_search_20250305` turns Studio's search on via
    `enable_web_search` instead of advertising a function.

12. **Streaming must notice a dead client.** Writes into a closed socket keep succeeding
    while the kernel buffer has room, so a turned-away client can otherwise keep the
    upstream generating (and burning quota) for the whole answer. The stream loop probes
    with `select` + `MSG_PEEK` every `PEER_CHECK_INTERVAL` and raises
    `ConnectionAbortedError` on EOF; `MSG_PEEK` never consumes pipelined request bytes.
    The **non-stream** collection loop runs the same probe (a departed client would
    otherwise be noticed only at the final write, after paying for the whole answer — and
    a cut would replay it): the abort is recorded as `client_closed` and never replayed.

13. **Client-supplied URLs are untrusted.** `fetch_url` refuses non-http(s) schemes and
    any target that resolves to loopback/private/link-local/reserved space
    (`STEP_ALLOW_PRIVATE_FETCH=1` opts out for LAN-hosted assets). Attachments and
    Anthropic `url` documents flow straight from the request body into this fetch.

14. **Auth never falls back once keys exist.** `_authorized` checks the *store* for any
    key and then requires a match against an enabled one — filtering to enabled keys first
    meant disabling the last key silently reopened the gateway. `_cors_headers()` keeps
    `/admin` same-origin and answers a `/v1` browser request only from a loopback origin
    or when a key exists. **Withholding ACAO is not enough**: a hostile page can still
    send a "simple" POST and burn quota, so `_reject_foreign_origin()` runs first in
    `do_GET`/`do_POST` and **403s** a foreign `Origin` on a keyless gateway (loopback and
    same-host origins pass; `Origin: null` does not). With keys configured the key is the
    authorisation and cross-origin callers are expected. Both functions must agree on what
    "a key is configured" means — `store.api_keys()` **or** `cfg.api_key`: checking only the
    store made `_cors_headers` echo an origin that `_reject_foreign_origin` then 403'd, so a
    `--api-key` client behind a browser was told to proceed and then refused.
15. **Requests are framed by Content-Length only.** `Transfer-Encoding: chunked` is
    rejected with 400 and the connection closed; honouring neither would leave the chunk
    bytes to be parsed as a second request (CL/TE desync behind a proxy).
16. **Store writes are crash- and reader-safe.** Unique `.tmp` name (+ pid/uuid), retry
    `os.replace` on Windows sharing violations, and a corrupt `accounts.json` is moved to
    `.corrupt-<ts>-<rand>` instead of being silently replaced by an empty store on the next
    save. "Corrupt" includes **valid JSON of the wrong shape** (`{"accounts": 5}`, a
    top-level array, `settings` as a string) — those used to raise out of `Store.load`
    and kill startup. Rows that are not dicts are skipped, and `Account.from_dict`
    coerces its numeric slots (`requests`, token counters, timestamps, `error_count`):
    a hand-edited `"tokens_in": "<img…>"` used to crash `add_usage` inside `_record`
    and reached the panel's innerHTML.
    A failed `save()` must never fail the turn: `sync_account` logs and keeps the rotated
    token in memory (failing the request would not recover the file either).
17. **A successful refresh is a state change you must persist.** Studio invalidates the
    old refresh token the moment it mints a new one, so any path that calls `refresh()`
    must `sync_account()` even when the *validation* after it fails. The panel's add flow
    (`_add_account_locked`) therefore validates on a throwaway client: a new import
    refreshes first (it is adopting the chain, and a dead refresh half must surface), an
    *existing* row uses the pasted access token as-is and refreshes only when that token
    is rejected — rotating a live session logs the browser it was copied from out of the
    upstream. Whenever a rotation happened, `probe.token` is what gets persisted.
    `Gateway.refresh_lock(account.id)` serialises refreshes per account (add / test /
    refresh-all / keepalive / `_ensure_fresh` all take it) and `add_lock` covers the
    lookup-then-insert race, because two rotations of one chain lose whichever token the
    loser is holding.
18. **Redirects re-run the SSRF guard.** `_GuardedRedirectHandler` checks every hop's
    scheme and address; urllib would otherwise follow a public URL straight to
    `169.254.169.254`.
19. **Only `done` / `error` ends a stream.** Studio always terminates a turn with `done`
    (`error` is terminal too); a chunked response that stops earlier raises
    `StudioError(transient=True)` from `iter_sse_closing`. Without that check a cut
    upstream connection looked like a complete answer — streaming clients were sent
    `finish_reason: "stop"` and non-stream clients a truncated 200. `iter_sse` stays a
    pure parser and media (TTS/ASR) also consumes the wrapper: a cut ASR stream used to
    be returned as a short transcript "successfully". An **empty 200 body** counts as a
    cut too. Consumers that stop deliberately (`break` on `done`, the doctor probes) are
    unaffected — the check only runs once the iteration runs out. `_run_upstream` returns
    a run only after a first event was validated (`connected`): the "no account left to
    retry" break used to leave `first=None` with `events` assigned, and `_collect_response`
    turned that into an empty 200 answer instead of the upstream failure it was. A
    status-less `StudioError` reaching `_error_response` is a gateway/upstream fault and
    is answered **502**, not 400 — 400 sent SDK users hunting for a parameter that does
    not exist. An empty 200 is retried by the normal open loop (no output yet). A cut
    *after* the first event is replayed **once for non-stream only**: `_collect_response`
    buffers the event list before feeding the adapter, so at the moment of the cut the
    adapter is pristine and `_run_upstream` starts a fresh conversation (the half-written
    one was dropped) — never append to it, or the upstream turn is duplicated. A second
    cut gives up with 502. The replay keeps the request's concurrency permit
    (`prepaid_slot`, never release-then-reacquire: on a busy gateway that wait turns into
    a 429) and marks the usage row `replayed: true` so the extra generation is visible.
    Buffering costs roughly 7× the answer text in event dicts — fine for non-stream
    (bounded by `max_tokens`), do not extend it to streaming. Streaming cannot replay
    (bytes are already on the wire): it sends one protocol-shaped error frame and stops.
    A cut does **not** cool the account: it is as likely to be the network as the session,
    and the retry loop's cooling only sees failures *before* the first event.

20. **Audio errors split into typo vs entitlement.** `/v1/audio/speech` and
    `/v1/audio/transcriptions` map OpenAI names onto the account's model (see
    `map_audio_model`). A 4xx on a model id the catalogue does **not** contain is a client
    typo: answer 400 immediately, cool nothing. A 4xx on a *known* model is an entitlement
    decision that differs per account (the catalogue itself came from one account), so try
    the next candidate account, never cool anyone, and return the denial as 400 only when
    every candidate refused. Chat keeps its own `_client_fault` rule (no failover on 4xx).

21. **Anthropic depth is `budget_tokens`, not an effort name.** `/v1/messages` maps
    `thinking.budget_tokens` onto Studio's effort levels via `effort_for_thinking_budget`
    (`<4096` low, `<16384` medium, else high; missing or unknown keeps the model default).
    Dropping the field silently ran every extended-thinking request at the default effort.
    The bands are a judgement call — Studio has no token budget — so they live in one
    place. `thinking.type: "disabled"` still turns reasoning off, and `reasoning_effort`
    (the OpenAI spelling) wins when both are present.

22. **The panel is a rendering surface, not a trusted one.** Every value that reaches
    `innerHTML` goes through `esc()`, and numeric cells through `fmtNum()`, which must
    coerce (`Number(...)`): `toLocaleString()` on a *string* returns it verbatim, so
    `(n || 0).toLocaleString()` was an injection sink for a restored/hand-edited
    `accounts.json` (the same file the panel exports). `tests/test_panel_security.py`
    runs the real `render()` under a Node DOM shim with hostile state — `node --check`
    only proves it parses. `/admin/api/state` must not ship the panel password hash, must
    not persist state to `localStorage` (the page holds every API key), and must not call
    the upstream from a poll (a stalled upstream turned each 15 s refresh into a parked
    thread — use `cached_models()` / `cached_quota()`). Client-supplied model ids echoed
    in `X-Step-Model` go through the header-safe pattern.

Memory note: request handling creates reference cycles (handler → server, closures), so RSS
sawtooths as the cycle collector runs rather than dropping immediately. Measured offline:
6 × 120 requests with concurrency 6 peaked ~320 MB and fell back to ~89 MB mid-run, and a
manual `gc.collect()` at 322 MB dropped it to 47 MB with only 360 live `Session` objects.
Treat a high reading as garbage awaiting collection, not as a leak — verify with `gc.collect()`.

One operational trap: `--doctor` refreshes every account, and Studio invalidates the old
refresh token on each refresh, so the doctor *must* persist the rotated token
(`gateway.sync_account`) or it bricks the pool it was diagnosing.

## Repo layout

- `step2api/` — the gateway.
  - `client.py` — Studio client: cookie/JWT handling, token refresh, both streaming surfaces, SSE parsing.
  - `translate.py` — pure OpenAI <-> Studio translation (no I/O): message normalisation, prompted
    tool protocol, sampling config, `Translator` (events -> chat.completion chunks).
  - `adapters.py` — Anthropic `/v1/messages` and OpenAI `/v1/responses` adapters over the same events.
  - `server.py` — `ThreadingHTTPServer` handler, routing, chunked SSE framing + heartbeat,
    `SessionCache` (conversation affinity), account rotation/retry, dashboard admin API.
  - `store.py` — `accounts.json` (accounts + API keys + panel password), `usage.jsonl` request log.
  - `config.py` / `dashboard.py` — env+CLI config, single-file dashboard HTML.
- `tests/` — stdlib `unittest`; `test_e2e.py` spins up a fake Studio upstream and drives the real
  gateway over HTTP, so most behaviour is verifiable without credentials.
- `.probe/` — reverse-engineering scratch space (captured cookies, downloaded JS chunks, probe
  scripts). **Untrusted / disposable**: `cookie.txt` holds a live session, never copy it into
  source, docs, or commits. `.probe/all/*.js` are the minified Studio bundles used to recover the
  RPC protos.
- `example/` — upstream reference implementation (qoder2api-hub). Read it for structure
  (routing, SSE framing, account pool, dashboard), **not** for protocol details.

`Gateway.start_keepalive` runs a daemon thread (default every 6h, `STEP_KEEPALIVE`) that renews
any account whose access token is <10 min or refresh token <10 days from expiry. This matters:
Studio rotates the refresh token on every refresh, so a running gateway keeps a session alive
indefinitely, while an idle import dies 30 days after issue.

One deliberate convenience: `Handler._resolve_model` maps foreign model families
(`claude*`, `gpt-*`, `o1/o3/o4*`, `codex*`) onto the upstream default model so Claude Code and
Codex work unedited; anything else unknown still 400s so typos stay visible.

## Commands

```bash
python step2api.py                       # run (http://127.0.0.1:8790, panel password admin)
python -m step2api                       # same, module form; `pip install .` gives `step2api`
python step2api.py --port 8791 --mode agent
python step2api.py --doctor              # real-upstream self check; rotates + persists tokens
python step2api.py --doctor --media      # + TTS -> upload -> ASR round trip (uses quota)
python scripts/smoke.py                  # deployment check, stdlib only (7 HTTP checks)
python scripts/sdk_check.py              # official-SDK wire check (pip install openai anthropic)
python scripts/soak.py --count 16        # mixed load + permit/thread leak check via /health
python -m unittest discover -s tests -v  # full suite, no network needed
python -m unittest tests.test_e2e.GatewayE2E.test_stream -v
```

`STEP_BASE_URL` points the client at another upstream; it is read on every call
(`client.base_url()`), never frozen at import — an import-time snapshot made the test suite
order-dependent and would also have pinned a proxy/mirror for the process lifetime.

## Upstream protocol (recovered — this is the load-bearing knowledge)

All calls are plain JSON POSTs to `https://studio.stepfun.com`. No request signing, no device
fingerprint beyond `Oasis-Webid`.

### Auth

- `Oasis-Token` cookie = `<accessJWT>...<refreshJWT>` (literally two JWTs joined by `...`).
  Access token carries `oasis_id` / `exp` (~30 min); refresh token carries `device_id` / `exp` (~30 days).
- Identity headers on every call: `oasis-appid: 10300`, `oasis-platform: web`,
  `oasis-language: zh`, `oasis-webid: <device id>`.
- **Refresh**: `POST /passport/proto.api.passport.v1.PassportService/RefreshToken`, body `{}`,
  same cookie. Response JSON has `accessToken.raw` / `refreshToken.raw`; the response also sets
  `Set-Cookie: Oasis-Token=...`. A bogus access token still refreshes as long as the refresh
  token is valid — so token renewal is self-healing. `oasis-webid` header is required or the call
  returns `400 oasis header is invalid`.
- Errors are `401 {"code":"unauthenticated", ...}` on the `/api/...` RPC surface.
- **A refresh success does not prove the credential.** `RefreshToken` does not verify the
  refresh JWT: a forged cookie gets `200` with tokens for a *guest* oasis_id (not the one in
  the cookie), and every authenticated RPC then fails `auth failed: not a logined oasis
  account`. `_admin_add_account` therefore validates with `QueryStudioRateLimit` after the
  refresh, and parks (disables) an account that only refreshes — otherwise the pool fills
  with sessions that look healthy and fail every request. Verified against the live
  upstream on 2026-10-25 with a self-signed, unsigned JWT pair.

### Two chat surfaces (both must be supported)

1. **Playground (stateless-ish)** — `POST /api/stdhttp/v1/studio/stream`
   body `{conversation_id, kind:"chat", chat:{model:{model_id, config:{max_tokens, temperature,
   top_p, frequency_penalty, thinking, effort}}, content, asset_ids:[], enable_web_search,
   system_prompt?}}`.
   Requires a `conversation_id` from `Dashboard/CreateConversation` (`{"type":"CONVERSATION_TYPE_CHAT"}`).
   Multi-turn context is server-side keyed by `conversation_id` — history is *not* sent per request.
   `system_prompt` is honored. Unknown `model_id` → `400 INVALID_MODEL`; denied → `MODEL_ACCESS_DENIED`.
2. **Agent workspace (tool-using)** — three steps:
   `AIStudio/CreateStudioWorkspace {}` → `Dashboard/CreateConversation {type, workspaceId}` →
   `AIStudio/StartStudioWorkspaceRun {workspaceId, conversationId, clientRequestId(uuid4), text,
   model:{modelId}}`, then stream `GET /api/stdhttp/v1/studio/workspaces/{ws}/messages/{mid}/events?last_seq=0`
   (`Accept: text/event-stream`). Returns `{workspace_id, conversation_id, user_message_id,
   assistant_message_id}`.
   The agent runs server-side tools (`ls`, `write`, …) and emits `tool_call_start` /
   `tool_call_end` with `TOOL_STATUS_OK` — the client never executes them.
   `config` here only accepts `thinking`/`effort`; passing `effort` while `thinking:false`
   yields stream-level `AGENT_ERROR / Invalid reasoning effort`.

### SSE event vocabulary (identical on both surfaces)

`message_start`, `content_block_start` (`CONTENT_BLOCK_TYPE_REASONING|TEXT`), `reasoning_delta`,
`text_delta`, `content_block_stop`, `tool_call_start`, `tool_call_end`, `message_stop`, `final`
(carries `usage.tokens_in/tokens_out/cached_tokens`), `done`, plus `heartbeat` (seq 0, ignore) and
`error` (`{code, biz_code, message}`). Each frame is `event: message\ndata: <json>\n\n`.

### Media (TTS)

`kind:"audio_tts"` on the same `/studio/stream` endpoint takes
`audio_tts:{model:{model_id, config:{voice, format}}, content}` and answers with
`content_block_start` (`CONTENT_BLOCK_TYPE_ASSETS`) → `media_asset_start` →
`media_asset_ready{asset_id, url}` → `final{asset_ids}`. The `url` is a pre-signed object-storage
link, fetched without auth headers. Voice ids and formats come from the `tts` section of
`GetStudioPlaygroundConfig` (37 voices; mp3/wav/opus).

Documents ride the same asset mechanism as images (`media.load_document` normalises Anthropic
`document` blocks — base64 / text / url — and OpenAI `file` / `input_file` parts into bytes).
Inputs the gateway cannot honour are **rejected with 400, never dropped**: a `file` part that
only carries a `file_id` (no `/v1/files` store), inline `audio` / `input_audio` parts, more
than 8 attachments, and any attachment in agent mode. Answering around a silently discarded
input is worse than failing the request.

Image inputs ride the same asset mechanism: `POST /api/stdhttp/v1/studio/file` (multipart `file`)
returns `asset_id`, then `chat.asset_ids:[id]` on the normal chat turn. The gateway decodes `data:`
URIs / fetches public URLs in `media.load_image`, uploads once per (account, sha256) and reuses the
asset id from `Gateway._asset_cache`. Agent mode has no asset slot — use `file_paths` there, and a request that carries
attachments while `step_mode: agent` is rejected with 400 rather than answered blind.

`kind:"audio_asr"` needs an asset first: `POST /api/stdhttp/v1/studio/file` with multipart field
`file` (optional `meta_data` JSON) returns `{"code":0,"data":{"asset":{"asset_id":...}}}`; then
`{kind:"audio_asr", audio_asr:{model:{model_id}, asset_ids:[id]}}` streams the transcript as
`text_delta` (plus `audio_duration_sec` in `final.usage`).

`thinking:false` is legal on the playground surface but the model can spend *more* tokens than
with thinking on (measured 94 vs 35 for a two-word answer), so a small `max_tokens` with
`reasoning_effort:"none"` can return empty content with `finish_reason:"length"`. That is upstream
behaviour, not a translation bug.

`kind:"image"` exists but the account currently gets
`400 INVALID_REQUEST "image generation is temporarily unavailable"` — do not wire it up expecting it
to work.

### Models & history

- `Dashboard/GetStudioPlaygroundConfig` and `Dashboard/ListStudioModelConfigs` are the source of
  truth for model ids (currently `step-5-preview`, `step-3.7-flash`, `step-3.5-flash-2603`) along
  with per-model `config` bounds, thinking mode/effort options, `is_multimodal`, display names.
  Do not hardcode a model list; fetch it.
- `Dashboard/BatchGetMessages {message_ids:[...]}` returns full message bodies with typed blocks
  (`role` 1=user 2=assistant 3=system; `type` 1=text 2=thinking 3=tool_call 4=assets) — use it to
  replay history when a client needs real multi-turn on a fresh conversation.

## Conventions to carry over from `example/`

- Runtime = Python 3.9+ stdlib only (`http.server`, `urllib`, `ssl`); proxy must be readable by
  env vars (`HTTPS_PROXY`) and must re-read them per request rather than caching an opener.
- Stream with HTTP/1.1 chunked encoding and `0\r\n\r\n` terminator (no `Connection: close`), and
  emit SSE comment heartbeats while waiting on upstream — StepFun can stall >30 s before the first
  token.
- Commit messages in English, Conventional Commits.
