"""Regression tests for the defects found in the second review pass.

Each test here is written so that it fails against the *old* code:

* ``Config`` crashed on a typo'd float env var and let env beat an explicit flag.
* ``cli``'s SIGINT handler called ``shutdown()`` on the ``serve_forever`` thread.
* ``--doctor`` refreshed accounts but threw the rotated refresh token away.
"""

import contextlib
import io
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from step2api import cli
from step2api.client import StudioError
from step2api.config import Config
from step2api.diag import run as doctor
from step2api.store import Store


class ConfigRobustnessTest(unittest.TestCase):
    ENV = ("STEP_UPSTREAM_TIMEOUT", "STEP_CONNECT_TIMEOUT", "STEP_SSE_HEARTBEAT",
           "STEP_REFRESH_MARGIN", "STEP_COOLDOWN_WAIT", "STEP_SESSION_TTL",
           "STEP_KEEPALIVE", "STEP_PORT", "STEP_MAX_CONCURRENCY", "STEP_USAGE_MAX_BYTES",
           "STEP_DEFAULT_MODE", "STEP_HOST", "STEP_DATA_DIR", "STEP_PANEL_PASSWORD")

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in self.ENV}
        for key in self.ENV:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_bad_float_env_falls_back_instead_of_raising(self):
        os.environ["STEP_UPSTREAM_TIMEOUT"] = "600s"
        os.environ["STEP_KEEPALIVE"] = "6h"
        cfg = Config.from_env([])                       # must not raise ValueError
        self.assertEqual(cfg.upstream_timeout, 600.0)
        self.assertEqual(cfg.keepalive_interval, 21600.0)

    def test_nan_and_negative_are_rejected(self):
        os.environ["STEP_SSE_HEARTBEAT"] = "nan"
        os.environ["STEP_USAGE_MAX_BYTES"] = "-1"
        os.environ["STEP_SESSION_TTL"] = "-5"
        cfg = Config.from_env([])
        self.assertEqual(cfg.sse_heartbeat, 5.0)
        self.assertEqual(cfg.usage_max_bytes, 8 * 1024 * 1024)
        self.assertEqual(cfg.session_ttl, 1800.0)

    def test_command_line_beats_environment(self):
        os.environ["STEP_PORT"] = "1111"
        os.environ["STEP_HOST"] = "0.0.0.0"
        cfg = Config.from_env(["--port", "9000", "--host", "127.0.0.1"])
        self.assertEqual(cfg.port, 9000)
        self.assertEqual(cfg.host, "127.0.0.1")

    def test_environment_still_applies_without_a_flag(self):
        os.environ["STEP_PORT"] = "1111"
        self.assertEqual(Config.from_env([]).port, 1111)

    def test_blank_and_out_of_range_values_fall_back(self):
        os.environ["STEP_PORT"] = "70000"
        os.environ["STEP_DATA_DIR"] = "   "
        os.environ["STEP_PANEL_PASSWORD"] = ""
        cfg = Config.from_env([])
        self.assertEqual(cfg.port, 8790)
        self.assertEqual(cfg.data_dir, "accounts")
        self.assertEqual(cfg.panel_password, "admin")
        self.assertEqual(Config.from_env(["--data-dir", ""]).data_dir, "accounts")
        self.assertEqual(Config.from_env(["--port", "-5"]).port, 8790)

    def test_unknown_mode_is_normalised(self):
        self.assertEqual(Config.from_env(["--mode", "bogus"]).default_mode, "playground")


class StoredModePrecedenceTest(unittest.TestCase):
    """CLI > env > panel setting: a stored default must not beat an explicit flag."""

    class _Store:
        def __init__(self, mode):
            self.mode = mode

        def settings(self):
            return {"default_mode": self.mode}

    def test_panel_setting_applies_when_nothing_was_asked(self):
        from step2api.cli import _apply_stored_mode

        cfg = Config()
        _apply_stored_mode(cfg, self._Store("agent"), [])
        self.assertEqual(cfg.default_mode, "agent")

    def test_cli_mode_wins_over_the_panel_setting(self):
        from step2api.cli import _apply_stored_mode

        cfg = Config()
        _apply_stored_mode(cfg, self._Store("agent"), ["--mode", "playground"])
        self.assertEqual(cfg.default_mode, "playground")

    def test_env_mode_wins_over_the_panel_setting(self):
        from step2api.cli import _apply_stored_mode

        os.environ["STEP_DEFAULT_MODE"] = "playground"
        try:
            cfg = Config()
            _apply_stored_mode(cfg, self._Store("agent"), [])
            self.assertEqual(cfg.default_mode, "playground")
        finally:
            os.environ.pop("STEP_DEFAULT_MODE", None)


class DoctorMediaCheckTest(unittest.TestCase):
    """`--doctor --media` must exercise TTS -> upload -> ASR for real, not just print."""

    def test_a_successful_round_trip_reports_ok(self):
        from step2api import diag, media

        seen = {}

        class Client:
            def upload_file(self, name, mime, data):
                seen["upload"] = (name, mime, data)
                return "asset-1"

        with mock.patch.object(media, "synthesize",
                               lambda *a, **k: (b"ID3fake", "audio/mpeg")),              mock.patch.object(media, "transcribe", lambda *a, **k: "你好"):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                ok = diag._media_check(Client(),
                                       {"default_model": "tts-x",
                                        "voices": [{"voice": "xiaoyue"}]},
                                       {"default_model": "asr-x"})
        self.assertTrue(ok)
        self.assertIn("tts + asr", out.getvalue())
        self.assertEqual(seen["upload"][1], "audio/mpeg")

    def test_a_missing_tts_model_is_a_failure(self):
        from step2api import diag

        with contextlib.redirect_stdout(io.StringIO()) as out:
            ok = diag._media_check(object(), {"voices": []}, {"default_model": "asr-x"})
        self.assertFalse(ok)
        self.assertIn("TTS", out.getvalue())


class ResponseHeaderSafetyTest(unittest.TestCase):
    """A client-supplied model id is echoed back in a header: no CR/LF may pass."""

    def test_safe_values_are_kept_and_hostile_ones_dropped(self):
        from step2api.server import Handler

        self.assertEqual(Handler._safe_header("X-Step-Model", "stepaudio-2.5-tts"),
                         {"X-Step-Model": "stepaudio-2.5-tts"})
        for bad in ("a" + chr(13) + chr(10) + "X-Evil: 1", "a" + chr(10) + "b",
                    "a b", "", "x" * 200):
            self.assertEqual(Handler._safe_header("X-Step-Model", bad), {}, repr(bad))


class _FakeServer:
    def __init__(self):
        self.calls = []

    def shutdown(self):
        self.calls.append(threading.current_thread().name)


class ShutdownHandlerTest(unittest.TestCase):
    def test_handler_does_not_call_shutdown_inline(self):
        server = _FakeServer()
        handler = cli._shutdown_handler(server)
        started = time.time()
        with contextlib.redirect_stdout(io.StringIO()):
            handler(2, None)
        self.assertLess(time.time() - started, 1.0, "signal handler blocked")
        deadline = time.time() + 5
        while not server.calls and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(server.calls), 1, "shutdown() was never called")
        # The deadlock was shutdown() running on the calling (serve_forever) thread.
        self.assertNotEqual(server.calls[0], threading.current_thread().name)

    def test_second_signal_is_ignored(self):
        server = _FakeServer()
        handler = cli._shutdown_handler(server)
        with contextlib.redirect_stdout(io.StringIO()):
            handler(2, None)
            handler(2, None)
        time.sleep(0.3)
        self.assertEqual(len(server.calls), 1)


class SessionCheckoutTest(unittest.TestCase):
    """A session is checked out while in use so two concurrent turns cannot share it."""

    def test_take_removes_until_put(self):
        from step2api.server import Session, SessionCache

        cache = SessionCache(ttl=60)
        session = Session("playground", conversation_id="conv-1")
        cache.put("k", session)
        self.assertIs(cache.take("k"), session)
        self.assertIsNone(cache.take("k"), "a second concurrent turn got the same session")
        cache.put("k", session)
        self.assertIs(cache.get("k"), session)


class PeerClosedTest(unittest.TestCase):
    """The streaming loop probes the client socket without consuming request data."""

    def test_detects_hangup_and_pending_data(self):
        import socket

        from step2api.server import peer_closed

        mine, theirs = socket.socketpair()
        try:
            self.assertFalse(peer_closed(mine))
            theirs.send(b"x")
            self.assertFalse(peer_closed(mine), "pending data must not look like a hangup")
            self.assertEqual(mine.recv(1), b"x", "the probe consumed a byte")
            theirs.close()
            self.assertTrue(peer_closed(mine))
        finally:
            mine.close()
            try:
                theirs.close()
            except OSError:
                pass


class PrivateFetchGuardTest(unittest.TestCase):
    """Attachment URLs must not be able to point the gateway at local services."""

    ENV = ("STEP_ALLOW_PRIVATE_FETCH", "step_allow_private_fetch")

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in self.ENV}
        for key in self.ENV:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    @staticmethod
    def fetch(url):
        from step2api.client import StudioClient
        return StudioClient.fetch_url(url, timeout=2)

    def test_loopback_is_refused(self):
        with self.assertRaises(StudioError) as ctx:
            self.fetch("http://127.0.0.1:9/assets/x.png")
        self.assertIn("private address", str(ctx.exception))
        self.assertEqual(ctx.exception.status, 400)

    def test_localhost_and_metadata_are_refused(self):
        for url in ("http://localhost:9/x", "http://169.254.169.254/latest/meta-data/"):
            with self.assertRaises(StudioError) as ctx:
                self.fetch(url)
            self.assertEqual(ctx.exception.status, 400, url)

    def test_flag_allows_private_targets(self):
        os.environ["STEP_ALLOW_PRIVATE_FETCH"] = "1"
        with self.assertRaises(StudioError) as ctx:
            self.fetch("http://127.0.0.1:9/assets/x.png")
        # no longer blocked by the guard; it got as far as trying to connect
        self.assertNotIn("private address", str(ctx.exception))

    def test_non_http_schemes_are_refused(self):
        with self.assertRaises(StudioError) as ctx:
            self.fetch("file:///C:/Windows/win.ini")
        self.assertEqual(ctx.exception.status, 400)


class StoreCorruptionTest(unittest.TestCase):
    """A damaged accounts.json must never be silently replaced by an empty store."""

    def test_corrupt_file_is_preserved(self):
        import sys

        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "accounts.json")
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write('{"accounts": [ {"name": "truncated"')
        buf = io.StringIO()
        with mock.patch.object(sys, "stderr", buf):
            store = Store(tmp)
        self.assertEqual(store.accounts, [])
        backups = [n for n in os.listdir(tmp) if n.startswith("accounts.json.corrupt-")]
        self.assertEqual(len(backups), 1, os.listdir(tmp))
        with io.open(os.path.join(tmp, backups[0]), encoding="utf-8") as fh:
            self.assertIn("truncated", fh.read())
        self.assertIn("unreadable", buf.getvalue())


class AtomicWriteTest(unittest.TestCase):
    def test_replace_retries_on_sharing_violation(self):
        from step2api import store as store_module

        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "x.json")
        real_replace = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError("sharing violation")
            return real_replace(src, dst)

        with mock.patch.object(store_module.os, "replace", flaky):
            store_module._atomic_write(path, "hello")
        self.assertEqual(calls["n"], 3)
        with io.open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "hello")
        self.assertEqual([n for n in os.listdir(tmp) if n.endswith(".tmp")], [])


class RedirectGuardTest(unittest.TestCase):
    """urllib follows redirects blindly; the SSRF guard must run on every hop."""

    def setUp(self):
        self.saved = os.environ.pop("STEP_ALLOW_PRIVATE_FETCH", None)

    def tearDown(self):
        if self.saved is not None:
            os.environ["STEP_ALLOW_PRIVATE_FETCH"] = self.saved

    @staticmethod
    def handler():
        import urllib.request
        from step2api.client import _GuardedRedirectHandler
        return _GuardedRedirectHandler(), urllib.request.Request("https://example.com/a.png")

    def test_private_hop_is_refused(self):
        handler, req = self.handler()
        for target in ("http://127.0.0.1:9/x", "http://169.254.169.254/latest/",
                       "http://10.0.0.5/x"):
            with self.assertRaises(StudioError, msg=target):
                handler.redirect_request(req, None, 302, "Found", {}, target)

    def test_non_http_hop_is_refused(self):
        handler, req = self.handler()
        with self.assertRaises(StudioError):
            handler.redirect_request(req, None, 302, "Found", {}, "ftp://example.com/x")

    def test_the_opener_actually_uses_it(self):
        from step2api.client import _GuardedRedirectHandler, _proxy_opener
        handlers = _proxy_opener().handlers
        self.assertTrue(any(isinstance(h, _GuardedRedirectHandler) for h in handlers),
                        [type(h).__name__ for h in handlers])

    def test_public_hop_is_followed(self):
        handler, req = self.handler()
        followed = handler.redirect_request(req, None, 302, "Found", {}, "https://cdn.example.com/x.png")
        self.assertIsNotNone(followed)


class TokenPersistenceTest(unittest.TestCase):
    """A failed save must not fail the turn: the token stays in memory."""

    def test_touch_failure_is_survivable(self):
        import sys
        import tempfile

        from step2api.server import Config, Gateway
        from step2api.store import Store

        tmp = tempfile.mkdtemp()
        store = Store(tmp)
        account = store.add_account("a", "tok", "webid")
        gateway = Gateway(Config(data_dir=tmp), store)
        client = gateway.client_for(account)
        client.token = "new-access...new-refresh"
        buf = io.StringIO()
        with mock.patch.object(store, "touch", side_effect=PermissionError("locked")),                 mock.patch.object(sys, "stderr", buf):
            gateway.sync_account(account, client)      # must not raise
        self.assertEqual(account.token, "new-access...new-refresh")
        self.assertIn("could not persist", buf.getvalue())


class _StubClient:
    """Enough of StudioClient for the doctor's account loop."""

    created = 0

    def __init__(self, token, webid="", oasis_id="", **kwargs):
        self.token = token
        self.webid = webid
        self.oasis_id = oasis_id
        self.refreshes = 0
        _StubClient.created += 1

    def refresh(self):
        self.refreshes += 1
        self.token = "access-rotated-%d...refresh-rotated-%d" % (self.refreshes, self.refreshes)
        self.webid = "webid-rotated"

    def access_exp(self):
        return time.time() + 120

    def rate_limit(self):
        raise StudioError("stub: stop after the account pass")

    def playground_config(self):
        raise StudioError("stub: no model catalogue")


class DoctorPersistenceTest(unittest.TestCase):
    def test_rotated_token_is_persisted(self):
        import step2api.server as server_module

        import socket

        tmp = tempfile.mkdtemp()
        # A free port: doctor refuses to run when something already answers on cfg.port.
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
        probe.close()
        cfg = Config(data_dir=tmp, host="127.0.0.1", port=free_port)
        store = Store(tmp)
        store.add_account("acc", "access-old...refresh-old", webid="webid-old", oasis_id="42")

        original = server_module.StudioClient
        server_module.StudioClient = _StubClient
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                doctor(cfg)
        finally:
            server_module.StudioClient = original

        saved = Store(tmp).accounts[0]
        self.assertEqual(saved.token, "access-rotated-1...refresh-rotated-1")
        self.assertEqual(saved.webid, "webid-rotated")


class SoakScriptTest(unittest.TestCase):
    """A dead worker must show up as a failed sample, never as a clean run."""

    @staticmethod
    def _soak():
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "soak_script", os.path.join(os.path.dirname(__file__), "..", "scripts", "soak.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_connection_errors_are_recorded_as_samples(self):
        import urllib.error
        from unittest import mock

        soak = self._soak()
        with mock.patch.object(soak.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("refused")):
            self.assertEqual(soak.chat("http://x", "m", "hi", stream=False), "exc:URLError")
            self.assertEqual(soak.chat("http://x", "m", "hi", stream=True), "exc:URLError")

    def test_api_key_header_is_sent(self):
        self.assertEqual(self._soak().headers("k")["Authorization"], "Bearer k")
        self.assertNotIn("Authorization", self._soak().headers(""))


class RefreshLockTest(unittest.TestCase):
    """Two threads refreshing one account would rotate the same chain twice; the loser
    ends up holding a refresh token the upstream has already invalidated."""

    def test_one_refresh_at_a_time_per_account(self):
        import tempfile

        from step2api.server import Gateway
        from step2api.store import Store

        tmp = tempfile.mkdtemp(prefix="step2api-lock-")
        store = Store(tmp)
        account = store.add_account("lock", "a.b.c...d.e.f", "dev")
        gw = Gateway(Config(data_dir=tmp, max_retries=1, max_concurrency=1, sse_heartbeat=0),
                     store)

        active = []
        peak = [0]
        gate = threading.Lock()

        class FakeClient:
            token = "a.b.c...d.e.f"
            webid = "dev"

            def needs_refresh(self):
                return True

            def refresh(self):
                with gate:
                    active.append(1)
                    peak[0] = max(peak[0], len(active))
                time.sleep(0.08)
                with gate:
                    active.pop()

        def worker():
            gw._ensure_fresh(account, FakeClient())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(peak[0], 1, "refreshes of one account overlapped")


class NoAccountReasonTest(unittest.TestCase):
    """The 503 body should say which fix applies: add an account, or fix the parked one."""

    def _gateway(self):
        import tempfile

        from step2api.server import Gateway
        from step2api.store import Store

        tmp = tempfile.mkdtemp(prefix="step2api-reason-")
        store = Store(tmp)
        return Gateway(Config(data_dir=tmp, max_retries=1, max_concurrency=1, sse_heartbeat=0),
                       store), store

    def test_empty_store_says_to_add_one(self):
        gateway, _ = self._gateway()
        self.assertIn("add one", gateway.no_account_reason())

    def test_parked_and_cooling_are_counted(self):
        gateway, store = self._gateway()
        parked = store.add_account("parked", "a.b.c...d.e.f", "dev")
        parked.disabled = True
        cooling = store.add_account("cooling", "a.b.c...d.e.g", "dev")
        cooling.cooldown_until = time.time() + 60
        reason = gateway.no_account_reason()
        self.assertIn("2 account(s)", reason)
        self.assertIn("1 disabled", reason)
        self.assertIn("1 cooling down", reason)


class UsageRotationTest(unittest.TestCase):
    """The usage log is append-only, so it must rotate or it grows without bound."""

    def test_rotation_keeps_one_generation(self):
        from step2api.store import Store

        tmp = tempfile.mkdtemp(prefix="step2api-usage-")
        store = Store(tmp)
        path = os.path.join(tmp, "usage.jsonl")
        store.log_usage({"n": 1}, path, max_bytes=100)
        store.log_usage({"pad": "x" * 200}, path, max_bytes=100)   # still under the cap
        self.assertFalse(os.path.exists(path + ".1"))
        store.log_usage({"n": 2}, path, max_bytes=100)             # size >= cap: rotate
        self.assertTrue(os.path.exists(path + ".1"))
        self.assertEqual([r.get("n") for r in store.read_usage(path, 10)], [2])
        self.assertEqual([r.get("n") for r in store.read_usage(path + ".1", 10)], [1, None])

    def test_rotation_can_be_disabled(self):
        from step2api.store import Store

        tmp = tempfile.mkdtemp(prefix="step2api-usage-")
        store = Store(tmp)
        path = os.path.join(tmp, "usage.jsonl")
        for i in range(5):
            store.log_usage({"n": i, "pad": "x" * 80}, path, max_bytes=0)
        self.assertFalse(os.path.exists(path + ".1"))
        self.assertEqual(len(store.read_usage(path, 10)), 5)


class SlotGateTest(unittest.TestCase):
    def test_a_full_gate_answers_429(self):
        """A 60s wait for a permit must end in "gateway busy", not a hung request."""
        from unittest import mock

        from step2api.client import StudioError
        from step2api.server import Gateway
        from step2api.store import Store

        tmp = tempfile.mkdtemp(prefix="step2api-gate-")
        gw = Gateway(Config(data_dir=tmp, max_retries=1, max_concurrency=2, sse_heartbeat=0),
                     Store(tmp))
        with mock.patch.object(gw.inflight, "acquire", return_value=False):
            with self.assertRaises(StudioError) as ctx:
                gw.acquire_slot()
        self.assertEqual(ctx.exception.status, 429)
        self.assertIn("gateway busy", ctx.exception.message)


class UsageReaderTest(unittest.TestCase):
    def test_a_truncated_tail_line_is_skipped(self):
        """A crash mid-append leaves half a JSON line; the panel must still load the rest."""
        from step2api.store import Store

        tmp = tempfile.mkdtemp(prefix="step2api-usage-")
        store = Store(tmp)
        path = os.path.join(tmp, "usage.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"n": 1}' + chr(10) + '{"n": 2, "trunc')
        self.assertEqual([r.get("n") for r in store.read_usage(path, 10)], [1])

    def test_a_missing_file_reads_as_empty(self):
        from step2api.store import Store

        tmp = tempfile.mkdtemp(prefix="step2api-usage-")
        self.assertEqual(Store(tmp).read_usage(os.path.join(tmp, "nope.jsonl"), 10), [])


class DataDirLockTest(unittest.TestCase):
    """Two gateways on one data dir rotate the same refresh token and brick the account."""

    def _tmp_with_live_pid(self):
        from step2api.store import write_pid_file

        tmp = tempfile.mkdtemp(prefix="step2api-dirlock-")
        write_pid_file(os.path.join(tmp, "gateway.pid"))      # this process: alive
        return tmp

    @staticmethod
    def _free_port():
        import socket

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        return port

    def test_a_second_gateway_on_the_same_data_dir_is_refused(self):
        tmp = self._tmp_with_live_pid()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["--port", str(self._free_port()), "--data-dir", tmp])
        self.assertEqual(code, 2, buf.getvalue())
        self.assertIn("已被另一个 step2api 进程使用", buf.getvalue())

    def test_doctor_notices_a_gateway_by_its_pid_file(self):
        """The port can differ from the one doctor probes; the data dir cannot."""
        from step2api import diag
        from step2api.store import Store

        tmp = self._tmp_with_live_pid()
        Store(tmp).add_account("doc", "a.b.c...d.e.f", "dev-1")
        cfg = Config(data_dir=tmp, host="127.0.0.1", port=self._free_port())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = diag.run(cfg)
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("数据目录", out.getvalue())

    def test_a_stale_pid_file_is_ignored(self):
        from step2api.store import read_pid_file, remove_pid_file, write_pid_file

        tmp = tempfile.mkdtemp(prefix="step2api-dirlock-")
        path = os.path.join(tmp, "gateway.pid")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("999999")                     # no such process
        self.assertEqual(read_pid_file(path), 999999)
        remove_pid_file(path)                      # not ours: must survive
        self.assertTrue(os.path.exists(path))
        write_pid_file(path)
        self.assertEqual(read_pid_file(path), os.getpid())
        remove_pid_file(path)
        self.assertFalse(os.path.exists(path))


class BindErrorTest(unittest.TestCase):
    """A taken port and a permission problem need different advice."""

    def _run(self, exc):
        import tempfile
        from unittest import mock

        tmp = tempfile.mkdtemp(prefix="step2api-bind-")
        buf = io.StringIO()
        with mock.patch.object(cli, "make_server", side_effect=exc):
            with contextlib.redirect_stdout(buf):
                code = cli.main(["--port", "8799", "--data-dir", tmp])
        return code, buf.getvalue()

    def test_port_in_use_is_named(self):
        code, out = self._run(OSError(10048, "in use"))
        self.assertEqual(code, 2)
        self.assertIn("端口已被占用", out)

    def test_other_bind_errors_are_not_blamed_on_the_port(self):
        code, out = self._run(OSError(13, "permission denied"))
        self.assertEqual(code, 2)
        self.assertIn("不是端口占用", out)


class CookiePasteTest(unittest.TestCase):
    """The panel accepts whatever the browser hands over: a bare token, a Cookie
    header, or the whole ``Cookie: ...`` line copied from DevTools."""

    HEADER = "Oasis-Token=a.b.c...d.e.f; Oasis-Webid=dev-1"

    def test_bare_token_value(self):
        from step2api.server import parse_cookie_header

        self.assertEqual(parse_cookie_header("a.b.c...d.e.f"), ("a.b.c...d.e.f", ""))

    def test_cookie_header(self):
        from step2api.server import parse_cookie_header

        self.assertEqual(parse_cookie_header(self.HEADER), ("a.b.c...d.e.f", "dev-1"))

    def test_devtools_header_line(self):
        from step2api.server import parse_cookie_header

        self.assertEqual(parse_cookie_header("Cookie: " + self.HEADER),
                         ("a.b.c...d.e.f", "dev-1"))

    def test_multiline_paste(self):
        from step2api.server import parse_cookie_header

        raw = "Oasis-Token=a.b.c...d.e.f" + chr(10) + "Oasis-Webid=dev-1"
        self.assertEqual(parse_cookie_header(raw), ("a.b.c...d.e.f", "dev-1"))

    def test_empty_is_none(self):
        from step2api.server import parse_cookie_header

        self.assertIsNone(parse_cookie_header("   "))


if __name__ == "__main__":
    unittest.main()
