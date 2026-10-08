"""The signed trading client sends requests the way Tabdeal's own client does (tabdeal-python
Client.request): GET / DELETE parameters in the query string, POST parameters in a form body;
the signature covers the parameters, the timestamp and recvWindow; the key is a header."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

from sp2l.secrets import Credentials
from sp2l.trading.client import TabdealTrade


class Resp(io.BytesIO):
    status = 200

    def __enter__(self) -> Resp:
        return self

    def __exit__(self, *a: Any) -> None:
        pass


def client(seen: list[Any]) -> TabdealTrade:
    def opener(req: Any, timeout: float) -> Resp:
        seen.append(req)
        return Resp(json.dumps({"ok": True}).encode())

    return TabdealTrade(Credentials("KEY", "SECRET"), opener=opener, clock=lambda: 1_700_000_000.0)


def _check(qs: str) -> dict[str, str]:
    pairs = parse_qsl(qs, keep_blank_values=True)
    signed = qs.rsplit("&signature=", 1)[0]
    sig = dict(pairs)["signature"]
    assert sig == hmac.new(b"SECRET", signed.encode(), hashlib.sha256).hexdigest()
    return dict(pairs)


def test_post_sends_the_signed_parameters_in_a_form_body():
    seen: list[Any] = []
    client(seen).set_leverage("XRP_USDT", 20)
    req = seen[0]
    assert req.get_method() == "POST" and urlsplit(req.full_url).query == ""
    assert req.get_header("Content-type") == "application/x-www-form-urlencoded"
    assert req.get_header("X-mbx-apikey") == "KEY"
    p = _check(req.data.decode())
    assert (p["symbol"], p["leverage"], p["timestamp"]) == ("XRP_USDT", "20", "1700000000000")


@pytest.mark.parametrize("call", ["get", "delete"])
def test_get_and_delete_send_them_in_the_query_string(call):
    seen: list[Any] = []
    c = client(seen)
    c.get_leverage("XRP_USDT") if call == "get" else c.cancel("XRP_USDT", 42)
    req = seen[0]
    assert req.data is None and req.get_method() == call.upper()
    p = _check(urlsplit(req.full_url).query)
    assert p["symbol"] == "XRP_USDT"
