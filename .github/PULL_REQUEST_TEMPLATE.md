## What this changes

<!-- One paragraph. Link the behaviour, not the file list. -->

## Why

<!-- The failure mode or gap this closes. "It looked nicer" is not a reason. -->

## How it was verified

<!--
Commands you actually ran, pasted output preferred. At minimum:

    python -m unittest discover -s tests

Anything touching the wire format should also show the relevant targeted run, e.g.

    python -m unittest tests.test_adapters -v
-->

```
<test output / commands>
```

## AI assistance disclosure (required)

<!--
Policy: code generated with an AI assistant must ship with its unit-test status.
Tick one, and in both cases state which parts were AI-generated and which tests cover them.
-->

- [ ] No AI-generated code in this PR
- [ ] This PR contains AI-generated code — the section below is filled in

**AI-generated scope:** <!-- files/functions -->

**Tests covering that code:** <!-- test file::test name, plus pass/fail -->

**Test status:** <!-- e.g. `351 passed, 0 failed` on CPython 3.12, commit <sha> -->

## Checklist

- [ ] `python -m unittest discover -s tests` passes locally
- [ ] `python -m pyflakes step2api step2api.py tests scripts` is clean
- [ ] No credentials, cookies, `accounts.json`, `usage.jsonl`, or `.probe/` content in the diff
- [ ] Docs updated when behaviour or the unsupported-parameter table changed
- [ ] Commits follow Conventional Commits; no unrelated reformatting
