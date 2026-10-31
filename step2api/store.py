"""Account / settings / usage persistence.

Everything lives in a single JSON file plus an append-only usage log, so the whole
gateway is inspectable with a text editor (Keep Knowledge in Plain Text).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

_LOCK = threading.RLock()
_USAGE_WARNED = False      # log an I/O failure of the usage log once per process
_READ_WARNED = False


def _now() -> float:
    return time.time()


def _atomic_write(path: str, text: str) -> None:
    # A fixed ".tmp" name collided between processes (gateway + --doctor) and, on
    # Windows, os.replace fails while any reader holds the target open (the export
    # endpoint, a backup tool, AV). Unique name + a short retry covers both.
    tmp = "%s.%d.%s.tmp" % (path, os.getpid(), secrets.token_hex(4))
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
        os.replace(tmp, path)          # last try: let the error surface
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def pid_alive(pid: int) -> bool:
    """True when `pid` names a live process (stdlib only, Windows included)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
        if not handle:
            # ERROR_ACCESS_DENIED means the pid exists but belongs to another user:
            # fail closed so a second gateway cannot adopt the same data dir.
            return ctypes.get_last_error() == 5
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid_file(path: str) -> int:
    """The pid recorded in `path`, or 0 when the file is missing or unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def write_pid_file(path: str) -> None:
    """Record this process; best effort (a read-only data dir must not stop the gateway)."""
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pass


def remove_pid_file(path: str) -> None:
    """Remove the file only when it still points at this process."""
    try:
        if read_pid_file(path) == os.getpid():
            os.remove(path)
    except OSError:
        pass


def hash_password(password: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 120_000)
    return "pbkdf2_sha256$120000$%s$%s" % (salt, digest.hex())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt, expected = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(rounds))
        return secrets.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


class Account:
    """One StepFun Studio session (cookie + webid)."""

    # accounts.json can be hand-edited or merged from a backup: a stat stored as a string
    # used to crash `add_usage` (and reached the panel's innerHTML unescaped).
    _INT_SLOTS = ("requests", "tokens_in", "tokens_out", "cached_tokens", "error_count")
    _FLOAT_SLOTS = ("created_at", "updated_at", "last_ok_at", "cooldown_until")

    __slots__ = (
        "id", "name", "token", "webid", "oasis_id", "created_at", "updated_at",
        "last_ok_at", "last_error", "error_count", "disabled", "cooldown_until",
        "requests", "tokens_in", "tokens_out", "cached_tokens", "notes",
    )

    def __init__(self, id: str, name: str, token: str, webid: str = "", oasis_id: str = "") -> None:
        self.id = id
        self.name = name
        self.token = token
        self.webid = webid
        self.oasis_id = oasis_id
        self.created_at = _now()
        self.updated_at = self.created_at
        self.last_ok_at = 0.0
        self.last_error = ""
        self.error_count = 0
        self.disabled = False
        self.cooldown_until = 0.0
        self.requests = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.cached_tokens = 0
        self.notes = ""

    # -- serialisation ---------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {slot: getattr(self, slot) for slot in self.__slots__}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Account":
        acc = cls(
            id=str(data.get("id") or secrets.token_hex(8)),
            name=str(data.get("name") or "account"),
            token=str(data.get("token") or ""),
            webid=str(data.get("webid") or ""),
            oasis_id=str(data.get("oasis_id") or ""),
        )
        for slot in cls.__slots__:
            if slot in ("id", "name", "token", "webid", "oasis_id") or slot not in data:
                continue
            value = data[slot]
            if slot in cls._INT_SLOTS:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    value = 0
            elif slot in cls._FLOAT_SLOTS:
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    value = 0.0
            elif slot == "disabled":
                value = bool(value)
            elif slot in ("last_error", "notes"):
                value = str(value or "")[:500]
            setattr(acc, slot, value)
        return acc

    # -- helpers ---------------------------------------------------------
    @property
    def cooling(self) -> bool:
        return _now() < self.cooldown_until

    def available(self) -> bool:
        return bool(self.token) and not self.disabled and not self.cooling

    def mark_ok(self) -> None:
        self.last_ok_at = _now()
        self.last_error = ""
        self.error_count = 0
        self.cooldown_until = 0.0

    def mark_error(self, message: str, cooldown: float = 30.0) -> None:
        self.last_error = str(message)[:500]
        self.error_count += 1
        self.cooldown_until = _now() + cooldown

    def add_usage(self, tokens_in: int, tokens_out: int, cached: int) -> None:
        self.requests += 1
        self.tokens_in += int(tokens_in or 0)
        self.tokens_out += int(tokens_out or 0)
        self.cached_tokens += int(cached or 0)


class Store:
    """Thread-safe JSON store for accounts, API keys and settings."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "accounts.json")
        self._accounts: List[Account] = []
        self._api_keys: List[Dict[str, Any]] = []
        self._settings: Dict[str, Any] = {}
        self._cursor = 0
        os.makedirs(data_dir, exist_ok=True)
        self.load()

    # -- load / save -----------------------------------------------------
    def _park_corrupt(self, reason) -> None:
        """Move a damaged store aside: the next save must not overwrite the user's data."""
        backup = "%s.corrupt-%s-%s" % (self.path, time.strftime("%Y%m%d-%H%M%S"),
                                       secrets.token_hex(2))
        moved = backup
        try:
            os.replace(self.path, backup)
        except OSError:
            moved = "(could not move it)"
        sys.stderr.write(("[step2api] %s is unreadable (%s); preserved as %s and "
                          "starting with an empty store" + chr(10))
                         % (self.path, reason, moved))

    def load(self) -> None:
        if not os.path.exists(self.path):
            self._accounts, self._api_keys, self._settings = [], [], {}
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError) as exc:
            self._park_corrupt(exc)
            raw = {}
        if not isinstance(raw, dict):
            self._park_corrupt("top level is not a JSON object")
            raw = {}
        accounts = raw.get("accounts", [])
        api_keys = raw.get("api_keys", [])
        settings = raw.get("settings", {})
        if (not isinstance(accounts, list) or not isinstance(api_keys, list)
                or not isinstance(settings, dict)):
            # Valid JSON, wrong shape (`{"accounts": 5}`) used to raise a TypeError out
            # of startup instead of being preserved like any other damaged store.
            self._park_corrupt("accounts/api_keys/settings have the wrong type")
            accounts, api_keys, settings = [], [], {}
        self._accounts = [Account.from_dict(a) for a in accounts if isinstance(a, dict)]
        self._api_keys = [k for k in api_keys
                          if isinstance(k, dict) and isinstance(k.get("key"), str)]
        self._settings = dict(settings)

    def save(self) -> None:
        with _LOCK:
            payload = {
                "version": 1,
                "accounts": [a.to_dict() for a in self._accounts],
                "api_keys": self._api_keys,
                "settings": self._settings,
            }
            _atomic_write(self.path, json.dumps(payload, ensure_ascii=False, indent=2))

    # -- accounts --------------------------------------------------------
    @property
    def accounts(self) -> List[Account]:
        return list(self._accounts)

    def get_account(self, account_id: str) -> Optional[Account]:
        for acc in self._accounts:
            if acc.id == account_id:
                return acc
        return None

    def add_account(self, name: str, token: str, webid: str = "", oasis_id: str = "") -> Account:
        with _LOCK:
            acc = Account(id=secrets.token_hex(6), name=name or "account", token=token, webid=webid, oasis_id=oasis_id)
            self._accounts.append(acc)
            self.save()
            return acc

    def remove_account(self, account_id: str) -> bool:
        with _LOCK:
            before = len(self._accounts)
            self._accounts = [a for a in self._accounts if a.id != account_id]
            changed = len(self._accounts) != before
            if changed:
                self.save()
            return changed

    def touch(self, acc: Account) -> None:
        """Persist after a token refresh / status change (cheap: rewrite JSON)."""
        with _LOCK:
            self.save()

    def pick(self) -> Optional[Account]:
        """Round-robin over healthy accounts."""
        with _LOCK:
            healthy = [a for a in self._accounts if a.available()]
            if not healthy:
                return None
            self._cursor = (self._cursor + 1) % len(healthy)
            return healthy[self._cursor]

    # -- api keys --------------------------------------------------------
    def api_keys(self) -> List[Dict[str, Any]]:
        return list(self._api_keys)

    def find_api_key(self, key: str) -> Optional[Dict[str, Any]]:
        for entry in self._api_keys:
            if entry.get("key") == key:
                return entry
        return None

    def add_api_key(self, name: str) -> Dict[str, Any]:
        with _LOCK:
            entry = {"key": "sf-" + secrets.token_urlsafe(24), "name": name or "key",
                     "created_at": _now(), "enabled": True}
            self._api_keys.append(entry)
            self.save()
            return entry

    def set_api_key(self, key: str, enabled: bool) -> bool:
        with _LOCK:
            for entry in self._api_keys:
                if entry.get("key") == key:
                    entry["enabled"] = bool(enabled)
                    self.save()
                    return True
        return False

    def remove_api_key(self, key: str) -> bool:
        with _LOCK:
            before = len(self._api_keys)
            self._api_keys = [e for e in self._api_keys if e.get("key") != key]
            changed = len(self._api_keys) != before
            if changed:
                self.save()
            return changed

    # -- settings --------------------------------------------------------
    def settings(self) -> Dict[str, Any]:
        return dict(self._settings)

    def set_setting(self, key: str, value: Any) -> None:
        with _LOCK:
            self._settings[str(key)] = value
            self.save()

    # -- usage -----------------------------------------------------------
    def log_usage(self, record: Dict[str, Any], path: str, max_bytes: int = 8 * 1024 * 1024) -> None:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        # Rotate and append under one lock: two threads crossing max_bytes together
        # would otherwise race on os.replace and lose the loser's record.
        with _LOCK:
            if max_bytes > 0:
                try:
                    if os.path.getsize(path) >= max_bytes:
                        # single-generation rotation: the dashboard only needs recent rows
                        os.replace(path, path + ".1")
                except OSError:
                    pass
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line)
            except OSError as exc:
                global _USAGE_WARNED
                if not _USAGE_WARNED:
                    _USAGE_WARNED = True
                    sys.stderr.write(("[step2api] cannot write the usage log (%s): %s"
                                      "\n") % (path, exc))

    def read_usage(self, path: str, limit: int = 500) -> List[Dict[str, Any]]:
        if not os.path.exists(path):
            return []
        rows: List[Dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError as exc:
            global _READ_WARNED
            if not _READ_WARNED:
                _READ_WARNED = True
                sys.stderr.write(("[step2api] cannot read the usage log (%s): %s"
                                  "\n") % (path, exc))
            return []
        return rows[-limit:]

    def summary(self, rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        rows = list(rows)
        return {
            "requests": len(rows),
            "tokens_in": sum(int(r.get("tokens_in") or 0) for r in rows),
            "tokens_out": sum(int(r.get("tokens_out") or 0) for r in rows),
            "cached_tokens": sum(int(r.get("cached_tokens") or 0) for r in rows),
            "errors": sum(1 for r in rows if r.get("status") == "error"),
        }
