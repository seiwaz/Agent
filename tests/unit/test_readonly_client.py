"""The read-only Tabdeal client can only GET allow-listed paths, signs like the official SDK,
and never lets credentials into evidence."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import stat
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from sp2l.secrets import CredentialError, Credentials, load_credentials
from sp2l.validation.readonly import READ_ONLY_PATHS, NotReadOnly, ReadOnlyTabdeal

CREDS = Credentials("KEY_abc123", "SECRET_xyz789")


class Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def capture():
    seen = []

    def opener(req, timeout):
        seen.append(req)
        return Resp(json.dumps([{"asset": "USDT", "walletBalance": "1"}]).encode())

    return seen, opener


def test_only_get_on_allow_listed_paths():
    seen, opener = capture()
    c = ReadOnlyTabdeal(CREDS, opener=opener)
    for path in (
        "/fapi/v1/order",
        "/fapi/v1/leverage",
        "/fapi/v1/position",
        "/fapi/v1/positionSlTp",
        "/fapi/v1/transfer",
        "/api/v1/order",
    ):
        with pytest.raises(NotReadOnly):
            c.get(path, signed=True)
    assert seen == []
    assert all(p.startswith("/r/fapi/") for p in READ_ONLY_PATHS)
    assert not hasattr(c, "post") and not hasattr(c, "delete") and not hasattr(c, "put")
    c.get("/r/fapi/v3/balance", signed=True)
    assert seen[0].get_method() == "GET" and seen[0].data is None


def test_signature_matches_official_sdk_scheme():
    seen, opener = capture()
    c = ReadOnlyTabdeal(CREDS, opener=opener, clock=lambda: 1_700_000_000.123)
    c.get("/r/fapi/v1/leverage", {"symbol": "BTCUSDT"}, signed=True)
    req = seen[0]
    q = urlsplit(req.full_url).query
    body, sig = q.rsplit("&signature=", 1)
    assert body == "symbol=BTCUSDT&timestamp=1700000000123&recvWindow=5000"
    assert sig == hmac.new(b"SECRET_xyz789", body.encode(), hashlib.sha256).hexdigest()
    assert req.get_header("X-mbx-apikey") == "KEY_abc123"
    assert "SECRET_xyz789" not in req.full_url


def test_evidence_is_redacted():
    seen, opener = capture()
    c = ReadOnlyTabdeal(CREDS, opener=opener)
    p = c.get("/r/fapi/v3/balance", signed=True)
    ev = c.redact({"x": p.evidence(), "leak": "KEY_abc123 SECRET_xyz789"})
    blob = json.dumps(ev)
    assert "KEY_abc123" not in blob and "SECRET_xyz789" not in blob
    assert "signature" not in json.dumps(p.evidence()["request"]["params"])
    assert dict(parse_qsl(urlsplit(seen[0].full_url).query))["recvWindow"] == "5000"


def test_credentials_redact_themselves_and_require_strict_permissions(tmp_path: Path):
    assert "KEY" not in repr(CREDS) and "SECRET" not in str(CREDS)
    d = tmp_path / "sp2l"
    d.mkdir(mode=0o700)
    f = d / "tabdeal.env"
    f.write_text("SP2L_TABDEAL_API_KEY=k\nSP2L_TABDEAL_API_SECRET=s\n")
    f.chmod(0o644)
    with pytest.raises(CredentialError, match="600"):
        load_credentials(f)
    f.chmod(0o600)
    assert load_credentials(f).api_key == "k"
    d.chmod(0o755)
    with pytest.raises(CredentialError, match="700"):
        load_credentials(f)
    d.chmod(0o700)
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
