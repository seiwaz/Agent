"""Dashboard login: one or more users from a server-side file, a signed session cookie.

The file (`auth.users_file`, mode 600 in a mode-700 directory, written by
`python -m sp2l set-login <user>`) holds each user's scrypt password hash and the secret that
signs session cookies; no password is ever stored in the repo or the config. A session cookie is
`<user, hex>.<expiry>.<hmac>` over the user, the expiry and the user's password hash, so changing a
password ends that user's sessions. HttpOnly, SameSite=Strict, Secure over HTTPS.

Repeated wrong passwords from one address are refused for a while (MAX_FAILS per FAIL_WINDOW_S).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("sp2l.auth")
COOKIE = "ets_session"
MAX_FAILS = 5
FAIL_WINDOW_S = 15 * 60
SCRYPT = (2**14, 8, 1)  # n, r, p


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    n, r, p = SCRYPT
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${dk.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, want = stored.split("$")
        if kind != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r),
                            p=int(p), dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), want)


def set_login(path: Path, user: str, password: str) -> None:
    """Add or change a user in the users file (created 600 in a 700 directory)."""
    if not user or any(c in user for c in ".$ ") or len(password) < 8:
        raise ValueError("user: no spaces, dots or $; password: at least 8 characters")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    data: dict[str, Any] = {}
    if path.exists():
        data = json.loads(path.read_text())
    data.setdefault("secret", secrets.token_hex(32))
    data.setdefault("users", {})[user] = hash_password(password)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(json.dumps(data, indent=1) + "\n")


@dataclass(frozen=True)
class AuthConfig:
    enabled: bool = False
    users_file: str = "~/.config/sp2l/dashboard.auth"
    session_hours: float = 12.0

    @classmethod
    def from_mapping(cls, m: dict[str, Any]) -> AuthConfig:
        unknown = sorted(set(m) - set(cls.__dataclass_fields__))
        if unknown:
            raise ValueError(f"auth: unknown key(s) {', '.join(unknown)}")
        enabled = m.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("auth.enabled must be true or false")
        c = cls(enabled, str(m.get("users_file", cls.users_file)),
                float(m.get("session_hours", cls.session_hours)))
        if not 0.1 <= c.session_hours <= 24 * 30:
            raise ValueError("auth.session_hours must be 0.1 .. 720")
        return c


class Auth:
    def __init__(self, cfg: AuthConfig, clock: Callable[[], float] = time.time) -> None:
        self.cfg, self.clock = cfg, clock
        self.path = Path(cfg.users_file).expanduser()
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}
        self._cache: tuple[float, dict[str, Any]] | None = None

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def _data(self) -> dict[str, Any] | None:
        """The users file (re-read when it changes); None if missing or open to others."""
        try:
            st = self.path.stat()
            if st.st_mode & 0o077:
                log.error("%s must be mode 600; nobody can log in", self.path)
                return None
            if self._cache is None or self._cache[0] != st.st_mtime:
                self._cache = (st.st_mtime, json.loads(self.path.read_text()))
            return self._cache[1]
        except (OSError, ValueError):
            return None

    def ready(self) -> str | None:
        """None when someone can log in, else why not."""
        d = self._data()
        if d is None or not d.get("users") or not d.get("secret"):
            return f"no login is set up: run `python -m sp2l set-login admin` ({self.path})"
        return None

    def _sig(self, d: dict[str, Any], user: str, exp: int) -> str:
        msg = f"{user}|{exp}|{d['users'].get(user, '')}".encode()
        return hmac.new(bytes.fromhex(d["secret"]), msg, hashlib.sha256).hexdigest()

    def user(self, cookie: str | None) -> str | None:
        """The logged-in user of a session cookie, or None."""
        if not self.enabled:
            return "-"
        d = self._data()
        if not cookie or d is None or not d.get("secret"):
            return None
        try:
            uhex, exp_s, sig = cookie.strip('"').split(".")
            user = bytes.fromhex(uhex).decode()
            exp = int(exp_s)
        except (ValueError, UnicodeDecodeError):
            return None
        if user not in d.get("users", {}) or exp < self.clock():
            return None
        return user if hmac.compare_digest(sig, self._sig(d, user, exp)) else None

    def login(self, user: str, password: str, addr: str) -> tuple[str | None, str | None]:
        """(cookie, None) on success, else (None, the message to show)."""
        now = self.clock()
        with self._lock:
            fails = [t for t in self._fails.get(addr, []) if now - t < FAIL_WINDOW_S]
            self._fails[addr] = fails
            if len(fails) >= MAX_FAILS:
                return None, "Too many wrong attempts. Try again in 15 minutes."
        d = self._data()
        if d is None or not d.get("users"):
            return None, "No login is set up on the server (python -m sp2l set-login admin)."
        stored = d["users"].get(user)
        ok = check_password(password, stored or hash_password("x", b"0" * 16))  # same cost
        if not (stored and ok):
            with self._lock:
                self._fails.setdefault(addr, []).append(now)
            log.warning("failed login for %r from %s", user[:40], addr)
            return None, "Wrong user or password."
        with self._lock:
            self._fails.pop(addr, None)
        exp = int(now + self.cfg.session_hours * 3600)
        log.info("login %s from %s", user, addr)
        return f"{user.encode().hex()}.{exp}.{self._sig(d, user, exp)}", None
