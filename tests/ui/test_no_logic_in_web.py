"""UI-02: the frontend must contain no strategy logic, thresholds or state derivation."""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "web"
FILES = [WEB / "index.html", WEB / "app.js", WEB / "app.css"]

# every numeric strategy threshold of SP2L_RULES.yaml, and gate/indicator vocabulary that
# would indicate client-side computation
FORBIDDEN_LITERALS = [
    "61.8",
    "38.2",
    "0.3333",
    "0.6666",
    "1/3",
    "2/3",
    "0.90",
    "0.10",
    "0.9",
    "0.1",
    "1.5",
    "2.0",
    "0.5",
    "150",
    "20",
    "14",
    "1R",
]
FORBIDDEN_CODE = [
    r"\bADX\b",
    r"\bCHOP\b",
    r"\bEMA\b",
    r"\bATR\b",
    r"median",
    r"Math\.(abs|max|min)\(.*(e1|sl|tp)",
    r"(range_position|room_to_tp|stretch|spike_atr)\w*\s*[<>]=?",
    r"\bwilder\b",
]


def test_app_js_has_no_threshold_literals():
    js = (WEB / "app.js").read_text()
    code = re.sub(r"/\*.*?\*/", "", js, flags=re.S)  # comments may mention the rule
    code = re.sub(r"<path[^>]*>|<rect[^>]*>|<circle[^>]*>", "", code)  # SVG geometry
    code = re.sub(r"\b(5000|15000|240|180|100|1000)\b", "", code)  # refresh/limit params
    for lit in FORBIDDEN_LITERALS:
        assert not re.search(rf"(?<![\w.]){re.escape(lit)}(?![\w.])", code), lit


def test_no_indicator_or_gate_computation_in_web():
    for f in FILES:
        text = f.read_text()
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        for pat in FORBIDDEN_CODE:
            assert not re.search(pat, text, flags=re.I), f"{f.name}: {pat}"


def test_no_credential_references_in_web():
    for f in FILES:
        assert not re.search(
            r"api[_-]?key|api[_-]?secret|signature|tabdeal\.env", f.read_text(), re.I
        )


def test_frontend_derives_no_trading_decision():
    """Levels, prices and gate values are only displayed: never compared, summed or used to
    choose a state. Every state/tone/label arrives from the backend."""
    js = re.sub(r"/\*.*?\*/", "", (WEB / "app.js").read_text(), flags=re.S)
    level = (
        r"(e1|e2|sl|tp|r|price|last_price|avg_entry|qty"
        r"|room_to_tp_r|stretch_atr|spike_atr|adx14|chop14)"
    )
    assert not re.search(rf"\.{level}\s*[<>]=?|[<>]=?\s*[\w.]*\.{level}\b", js)
    assert not re.search(rf"\.{level}\s*[-+*/]\s*[\w(]", js)
    assert "thresholds" not in js  # threshold values are rendered from gate rows, never read
