"""Tabdeal API credentials, read from ~/.config/sp2l/tabdeal.env (never from the repo).

The file must be mode 600 inside a mode-700 directory, or loading is refused. The secret is
never logged, persisted, returned by the API or shown in the WebUI: `Credentials` redacts
itself in repr/str.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path.home() / ".config" / "sp2l" / "tabdeal.env"


class CredentialError(RuntimeError):
    pass


@dataclass(frozen=True)
class Credentials:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)

    def __str__(self) -> str:
        return "Credentials(<redacted>)"

    __repr__ = __str__


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def load_credentials(path: Path = DEFAULT_PATH) -> Credentials:
    if not path.is_file():
        raise CredentialError(f"credential file not found: {path}")
    if _mode(path.parent) & 0o077:
        raise CredentialError(f"{path.parent} must be mode 700")
    if _mode(path) & 0o077:
        raise CredentialError(f"{path} must be mode 600")
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip()
    key = values.get("SP2L_TABDEAL_API_KEY")
    secret = values.get("SP2L_TABDEAL_API_SECRET")
    if not key or not secret:
        raise CredentialError("SP2L_TABDEAL_API_KEY / SP2L_TABDEAL_API_SECRET missing")
    return Credentials(key, secret)
