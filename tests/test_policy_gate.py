"""The PR-policy gate is code too: `node --check`-style validation is not enough.

`.github/workflows/policy.yml` embeds a Python script in a heredoc. YAML parsing proves the
workflow loads; it does not prove a compliant PR passes or that each rule actually fires.
This extracts the script and runs it against representative PR bodies, including the bug
that shipped in the first version of the gate (it counted the checklist's ticked boxes and
demanded "exactly one tick" from a perfectly good PR).
"""

import io
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "policy.yml")
OPEN = "python - <<'PY'\n"
CLOSE = "\n          PY\n"


def gate_source():
    with io.open(WORKFLOW, encoding="utf-8") as fh:
        text = fh.read()
    start = text.index(OPEN) + len(OPEN)
    block = text[start:text.index(CLOSE, start)]
    # The heredoc body sits 10 spaces deep inside the YAML step.
    return "\n".join(line[10:] if line.startswith(" " * 10) else line
                     for line in block.splitlines()) + "\n"


def run_gate(body):
    env = dict(os.environ)
    env["PR_BODY"] = body
    proc = subprocess.run([sys.executable, "-c", gate_source()], env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def compliant_body():
    return "\n".join([
        "## What this changes",
        "Fix a thing.",
        "",
        "## AI assistance disclosure (required)",
        "- [x] This PR contains AI-generated code — the section below is filled in",
        "- [ ] No AI-generated code in this PR",
        "",
        "**AI-generated scope:** `step2api/server.py`",
        "**Tests covering that code:** `tests/test_e2e.py::GatewayE2E.test_stream`",
        "**Test status:** 353 passed, 0 failed — CPython 3.14, abc1234",
        "",
        "## Checklist",
        "- [x] tests pass",
        "- [x] lint is clean",
        "",
        "```bash",
        "python -m unittest discover -s tests",
        "```",
    ])


class PolicyGateTest(unittest.TestCase):
    def test_the_extracted_script_is_python(self):
        self.assertIn("PR_BODY", gate_source())
        compile(gate_source(), WORKFLOW, "exec")

    def test_a_compliant_pr_passes_despite_checklist_ticks(self):
        code, output = run_gate(compliant_body())
        self.assertEqual(code, 0, output)

    def test_an_unfilled_template_is_rejected(self):
        with io.open(os.path.join(ROOT, ".github", "PULL_REQUEST_TEMPLATE.md"),
                     encoding="utf-8") as fh:
            body = fh.read()
        code, output = run_gate(body)
        self.assertEqual(code, 1, output)
        self.assertIn("AI-disclosure", output)
        self.assertIn("Test status", output)

    def test_two_ticked_disclosure_boxes_are_rejected(self):
        body = compliant_body().replace("- [ ] No AI-generated code in this PR",
                                        "- [x] No AI-generated code in this PR")
        code, output = run_gate(body)
        self.assertEqual(code, 1, output)
        self.assertIn("exactly one", output)

    def test_a_placeholder_test_status_is_rejected(self):
        body = compliant_body().replace(
            "**Test status:** 353 passed, 0 failed — CPython 3.14, abc1234",
            "**Test status:** <paste results>")
        code, output = run_gate(body)
        self.assertEqual(code, 1, output)
        self.assertIn("Test status", output)

    def test_docs_only_change_needs_no_fenced_test_output(self):
        body = "\n".join([
            "## AI assistance disclosure",
            "- [x] No AI-generated code in this PR",
            "**Test status:** no behaviour change — docs only",
        ])
        code, output = run_gate(body)
        self.assertEqual(code, 0, output)

    def test_missing_disclosure_section_is_rejected(self):
        code, output = run_gate("## What this changes\nnothing\n")
        self.assertEqual(code, 1, output)
        self.assertIn("AI assistance disclosure", output)


if __name__ == "__main__":
    unittest.main()
