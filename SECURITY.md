# Security Policy

## Reporting a vulnerability

**Do not open a public PR or issue that demonstrates the vulnerability.**
Issues and Discussions are disabled on this repository.

Use GitHub's private vulnerability reporting instead:

<https://github.com/minc-nice-100/step2api/security/advisories/new>

Please include: affected version/commit, the request or configuration that triggers it,
observed vs expected behaviour, and (if you have one) a minimal reproduction.

You can expect an acknowledgement within a few days. This is a volunteer project, so
there is no paid bug bounty and no formal SLA.

## What is in scope

The gateway is a local proxy that holds upstream session cookies and API keys, so the
interesting attack surface is:

- authentication / CORS / panel-token bypasses (`_authorized`, `_reject_foreign_origin`,
  `/admin/*`), including cross-site requests that can burn quota;
- request framing desync (`Content-Length` vs `Transfer-Encoding`), header injection;
- SSRF through client-supplied attachment URLs (`fetch_url`, redirects, private ranges);
- credential leakage into logs, `usage.jsonl`, exported CSV, error bodies, or the panel;
- XSS in the dashboard via restored / hand-edited `accounts.json`;
- path traversal or arbitrary file write through `--data-dir` handling.

## Hardening notes

- The default bind is `127.0.0.1` and that is the only configuration where a keyless
  gateway is safe. A keyless gateway bound to `0.0.0.0` is usable by anything that can
  reach the port, browsers included: the origin check stops a *foreign* page from spending
  quota, but it cannot tell a DNS-rebinding name (a domain that resolves to this host) from
  a legitimate LAN hostname. Set an API key before widening the bind.
- Panel sessions are bearer tokens held in memory; restarting the process logs every panel
  session out. That is deliberate.
- `accounts.json` contains live session cookies. It is written with the process umask, so
  on a shared machine use a private `--data-dir` and a restrictive umask.

## What is out of scope

- Vulnerabilities in `studio.stepfun.com` itself — report those to StepFun.
- The upstream account being banned or rate-limited for proxying; that is a terms-of-service
  matter, and the README already says "personal use, at your own risk".
- Running the gateway exposed to the public internet without API keys and TLS. The default
  bind is `127.0.0.1` on purpose; `0.0.0.0` is an explicit opt-in.
- Denial of service by someone who already holds a valid API key.
