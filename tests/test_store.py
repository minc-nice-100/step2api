"""Unit tests for the persistence layer."""

import contextlib
import io
import json
import os
import tempfile
import time
import unittest

from step2api.store import Account, Store, hash_password, verify_password


class PasswordTest(unittest.TestCase):
    def test_hash_roundtrip(self):
        stored = hash_password("s3cret")
        self.assertTrue(verify_password("s3cret", stored))
        self.assertFalse(verify_password("wrong", stored))

    def test_hash_is_salted(self):
        self.assertNotEqual(hash_password("x"), hash_password("x"))

    def test_garbage_stored_value(self):
        self.assertFalse(verify_password("x", "not-a-hash"))


class AccountTest(unittest.TestCase):
    def test_available_and_cooldown(self):
        acc = Account("id", "name", "token")
        self.assertTrue(acc.available())
        acc.mark_error("boom", cooldown=0.3)
        self.assertFalse(acc.available())
        self.assertTrue(acc.cooling)
        time.sleep(0.35)
        self.assertTrue(acc.available())

    def test_mark_ok_clears(self):
        acc = Account("id", "n", "t")
        acc.mark_error("boom", cooldown=10)
        acc.mark_ok()
        self.assertEqual(acc.last_error, "")
        self.assertEqual(acc.error_count, 0)
        self.assertFalse(acc.cooling)

    def test_disabled_account_never_available(self):
        acc = Account("id", "n", "t")
        acc.disabled = True
        self.assertFalse(acc.available())

    def test_usage_accumulates(self):
        acc = Account("id", "n", "t")
        acc.add_usage(10, 5, 2)
        acc.add_usage(1, 1, 0)
        self.assertEqual((acc.requests, acc.tokens_in, acc.tokens_out, acc.cached_tokens), (2, 11, 6, 2))

    def test_roundtrip(self):
        acc = Account("id", "n", "t", webid="w", oasis_id="42")
        acc.requests = 3
        clone = Account.from_dict(acc.to_dict())
        self.assertEqual((clone.id, clone.name, clone.token, clone.webid, clone.oasis_id),
                         ("id", "n", "t", "w", "42"))
        self.assertEqual(clone.requests, 3)


class UsageLogFailureTest(unittest.TestCase):
    """A disk error must not crash a request, but it must not be invisible either."""

    def test_warns_once_and_never_raises(self):
        import sys
        import tempfile
        from unittest import mock

        import step2api.store as store_module

        tmp = tempfile.mkdtemp()
        store = store_module.Store(tmp)
        path = os.path.join(tmp, "usage.jsonl")
        io.open(path, "w", encoding="utf-8").close()      # exists, so read_usage tries to open it
        buf = io.StringIO()
        real_open = open

        def failing_open(*args, **kwargs):
            if args and str(args[0]) == path:
                raise OSError("disk full")
            return real_open(*args, **kwargs)

        saved = store_module._USAGE_WARNED
        saved_read = store_module._READ_WARNED
        store_module._USAGE_WARNED = False
        store_module._READ_WARNED = False
        try:
            with mock.patch("builtins.open", failing_open), mock.patch.object(sys, "stderr", buf):
                store.log_usage({"ts": 1}, path)
                store.log_usage({"ts": 2}, path)
                store.read_usage(path)
        finally:
            store_module._USAGE_WARNED = saved
            store_module._READ_WARNED = saved_read
        text = buf.getvalue()
        self.assertEqual(text.count("cannot write the usage log"), 1, text)
        self.assertEqual(text.count("cannot read the usage log"), 1, text)
        self.assertIn("disk full", text)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="step2api-store-")
        self.store = Store(self.tmp)

    def test_persists_across_reload(self):
        acc = self.store.add_account("a", "tok", "wid")
        reloaded = Store(self.tmp)
        self.assertEqual(len(reloaded.accounts), 1)
        self.assertEqual(reloaded.accounts[0].id, acc.id)
        self.assertEqual(reloaded.accounts[0].token, "tok")

    def test_remove_account(self):
        acc = self.store.add_account("a", "tok")
        self.assertTrue(self.store.remove_account(acc.id))
        self.assertFalse(self.store.remove_account(acc.id))
        self.assertEqual(Store(self.tmp).accounts, [])

    def test_pick_round_robin_skips_unusable(self):
        a = self.store.add_account("a", "t1")
        b = self.store.add_account("b", "t2")
        self.store.add_account("c", "")              # no token -> never picked
        b.disabled = True
        seen = set()
        for _ in range(6):
            picked = self.store.pick()
            self.assertIsNotNone(picked)
            seen.add(picked.id)
        self.assertEqual(seen, {a.id})

    def test_pick_without_accounts(self):
        self.assertIsNone(self.store.pick())

    def test_api_key_lifecycle(self):
        entry = self.store.add_api_key("client")
        key = entry["key"]
        self.assertTrue(key.startswith("sf-"))
        self.assertIsNotNone(self.store.find_api_key(key))
        self.assertTrue(self.store.set_api_key(key, False))
        self.assertFalse(self.store.find_api_key(key)["enabled"])
        self.assertTrue(self.store.remove_api_key(key))
        self.assertIsNone(self.store.find_api_key(key))
        self.assertFalse(self.store.remove_api_key(key))

    def test_settings_persist(self):
        self.store.set_setting("default_mode", "agent")
        self.assertEqual(Store(self.tmp).settings().get("default_mode"), "agent")

    def test_usage_log_roundtrip(self):
        path = os.path.join(self.tmp, "usage.jsonl")
        self.store.log_usage({"a": 1}, path)
        self.store.log_usage({"a": 2}, path)
        rows = self.store.read_usage(path)
        self.assertEqual([r["a"] for r in rows], [1, 2])
        self.assertEqual(self.store.summary(rows)["requests"], 2)

    def test_corrupt_store_falls_back_to_empty(self):
        with open(os.path.join(self.tmp, "accounts.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        store = Store(self.tmp)
        self.assertEqual(store.accounts, [])


if __name__ == "__main__":
    unittest.main()


class UsageRotationRaceTest(unittest.TestCase):
    """Rotation and append share one lock: concurrent writers must not lose rows."""

    def test_concurrent_writers_lose_nothing_across_one_rotation(self):
        import json as _json
        import tempfile
        import threading

        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "usage.jsonl")
        store = Store(tmp)
        record = {"ts": 1.0, "model": "m", "status": "ok", "pad": "x" * 60}
        row_bytes = len(_json.dumps(record, ensure_ascii=False)) + 1
        max_bytes = row_bytes * 60          # exactly one rotation over 100 rows

        def worker(n):
            for _ in range(n):
                store.log_usage(dict(record), path, max_bytes)

        threads = [threading.Thread(target=worker, args=(25,)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total = 0
        for candidate in (path, path + ".1"):
            if os.path.exists(candidate):
                total += len(store.read_usage(candidate, 10 ** 6))
        self.assertEqual(total, 100)


class StoreShapeTest(unittest.TestCase):
    """A JSON-valid but wrongly-shaped accounts.json must be parked, not crash startup."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="step2api-store-shape-")
        self.path = os.path.join(self.tmp, "accounts.json")

    def _write(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_wrong_shape_is_preserved_and_the_store_stays_empty(self):
        for payload in ('{"accounts": 5}', '{"settings": "oops"}', '[]',
                        '{"api_keys": {"sf-1": true}}'):
            self._write(payload)
            with contextlib.redirect_stderr(io.StringIO()):
                store = Store(self.tmp)
            self.assertEqual(store.accounts, [], payload)
            self.assertEqual(store.api_keys(), [], payload)
            self.assertEqual(store.settings(), {}, payload)
            backups = [n for n in os.listdir(self.tmp) if ".corrupt-" in n]
            self.assertTrue(backups, payload)
            for name in backups:
                os.remove(os.path.join(self.tmp, name))

    def test_non_dict_rows_are_skipped(self):
        self._write(json.dumps({"accounts": [{"id": "a", "token": "t"}, "junk", 7],
                                "api_keys": [{"key": "sf-1"}, "junk"]}))
        store = Store(self.tmp)
        self.assertEqual([a.id for a in store.accounts], ["a"])
        self.assertEqual([k["key"] for k in store.api_keys()], ["sf-1"])

    def test_string_stats_are_coerced(self):
        """Hand-edited stats used to make `add_usage` raise inside `_record`."""
        self._write(json.dumps({"accounts": [{
            "id": "a", "token": "t", "requests": "7", "tokens_in": "<img src=x>",
            "tokens_out": 3.9, "cached_tokens": None, "cooldown_until": "soon",
            "error_count": True, "disabled": "yes"}]}))
        store = Store(self.tmp)
        acc = store.accounts[0]
        self.assertEqual(acc.requests, 7)
        self.assertEqual(acc.tokens_in, 0)
        self.assertEqual(acc.tokens_out, 3)
        self.assertEqual(acc.cached_tokens, 0)
        self.assertEqual(acc.cooldown_until, 0.0)
        self.assertEqual(acc.error_count, 1)
        self.assertTrue(acc.disabled)
        acc.add_usage(1, 2, 3)          # must not raise
        self.assertEqual((acc.requests, acc.tokens_in), (8, 1))


if __name__ == "__main__":
    unittest.main()
