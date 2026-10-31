# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and commits follow
[Conventional Commits](https://www.conventionalcommits.org/).

## [Unreleased]

### Added

- Open-source project policy: pull requests only (Issues and Discussions disabled),
  protected `main`, and a CI gate (`.github/workflows/policy.yml`) that rejects a PR unless
  AI-generated code arrives with its unit-test status.
- `CONTRIBUTING.md`, `SECURITY.md`, `PULL_REQUEST_TEMPLATE.md`, `.editorconfig`.
- Test that the runtime package imports nothing outside the standard library, so the
  "zero dependencies" promise cannot rot silently.

### Changed

- Licensed under the [Unlicense](LICENSE) (public domain) instead of MIT.
- CI now installs the built wheel and exercises both entry points (`step2api -V`,
  `python -m step2api -V`) and asserts the wheel declares no runtime requirements.

### Fixed

- A legitimate cross-origin caller holding the `--api-key` / `STEP_API_KEY` credential was
  rejected with 403: `_cors_headers` treated that key as "gateway is protected" and echoed
  the origin, while `_reject_foreign_origin` only inspected the panel key store. Both now
  use the same notion of "a key is configured".
- `tests/test_panel_security.py` and the panel syntax test failed on a non-UTF-8 console
  (zh-CN Windows, GBK): Node's UTF-8 output was decoded with the process locale. Both
  subprocesses now pin `encoding="utf-8"`.

## [1.0.0] - 2026-10-31

First public release.

- OpenAI-compatible `/v1/chat/completions`, `/v1/completions`, `/v1/responses`,
  Anthropic-compatible `/v1/messages` (+ `count_tokens`), `/v1/models`, audio
  `/v1/audio/speech` and `/v1/audio/transcriptions`, `/v1/audio/voices`.
- Playground and agent upstream modes, prompted tool protocol with full call/result
  round trips, streaming SSE, reasoning passthrough, built-in web search, image/document
  attachments, JSON-mode emulation, client-side `stop` handling.
- Account pool with round-robin, cooldown, automatic token rotation and a background
  keepalive; per-account conversation affinity with transcript replay on divergence.
- Web panel (accounts, API keys, models, usage, request log, CSV export) and the
  `/admin/api/*` surface, with PBKDF2 password storage and a 15 s auto-refresh.
- Zero runtime dependencies; Python 3.9–3.14.
