"""Unit tests for conversation affinity (session cache + prefix matching)."""

import time
import unittest

from step2api.server import Session, SessionCache, _prefix_matches, session_key
from step2api.translate import normalize_messages


class SessionCacheTest(unittest.TestCase):
    def test_put_get_drop(self):
        cache = SessionCache(ttl=60)
        session = Session("playground", "conv-1")
        cache.put("k", session)
        self.assertIs(cache.get("k"), session)
        cache.drop("k")
        self.assertIsNone(cache.get("k"))

    def test_ttl_expiry(self):
        cache = SessionCache(ttl=0.05)
        cache.put("k", Session("playground", "conv-1"))
        self.assertIsNotNone(cache.get("k"))
        time.sleep(0.1)
        self.assertIsNone(cache.get("k"))

    def test_eviction_keeps_newest(self):
        cache = SessionCache(ttl=600, max_entries=3)
        for i in range(5):
            session = Session("playground", "conv-%d" % i)
            session.updated_at = 1000 + i
            cache.put("k%d" % i, session)
        self.assertIsNone(cache.get("k0"))
        self.assertIsNone(cache.get("k1"))
        self.assertIsNotNone(cache.get("k4"))
        self.assertEqual(len(cache._data), 3)

    def test_missing_key(self):
        self.assertIsNone(SessionCache().get("nope"))


class SessionKeyTest(unittest.TestCase):
    msgs = normalize_messages([{"role": "user", "content": "hello"}])

    def key(self, **kwargs):
        base = dict(tenant="t", model="m", system_prompt="", messages=self.msgs, mode="playground")
        base.update(kwargs)
        return session_key(**base)

    def test_stable(self):
        self.assertEqual(self.key(), self.key())

    def test_isolation(self):
        base = self.key()
        for variant in ({"tenant": "t2"}, {"model": "m2"}, {"system_prompt": "sys"},
                        {"mode": "agent"}, {"user": "alice"}):
            self.assertNotEqual(base, self.key(**variant), variant)

    def test_first_user_message_matters(self):
        other = normalize_messages([{"role": "user", "content": "different"}])
        self.assertNotEqual(self.key(), self.key(messages=other))


class PrefixTest(unittest.TestCase):
    def test_matching_prefix(self):
        sent = [("user", "a", "", "", ""), ("assistant", "b", "", "", "")]
        messages = normalize_messages([
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ])
        self.assertTrue(_prefix_matches(sent, messages))

    def test_diverged_history(self):
        sent = [("user", "a", "", "", "")]
        messages = normalize_messages([
            {"role": "user", "content": "edited"},
            {"role": "user", "content": "c"},
        ])
        self.assertFalse(_prefix_matches(sent, messages))

    def test_shorter_history(self):
        sent = [("user", "a", "", "", ""), ("assistant", "b", "", "", "")]
        messages = normalize_messages([{"role": "user", "content": "a"}])
        self.assertFalse(_prefix_matches(sent, messages))

    def test_empty_sent(self):
        self.assertFalse(_prefix_matches([], normalize_messages([{"role": "user", "content": "a"}])))


if __name__ == "__main__":
    unittest.main()
