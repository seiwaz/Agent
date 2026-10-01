"""B39: Shadow restart = restore checkpoint + replay the journal after its cursor.
With complete coverage the result is identical to an uninterrupted run."""

from __future__ import annotations

from datetime import timedelta

import pytest

from sp2l.engine.model import SetupState
from tests.conftest import T0
from tests.db.conftest import URL
from tests.db.journal import write_journal
from tests.db.runner_utils import crash_mid_input, drain, open_runner, outcome, step_until
from tests.replay.tape import tape

pytestmark = pytest.mark.db
MINUTES = 900


@pytest.fixture(scope="module")
def journal(engine):
    end = T0 + timedelta(minutes=MINUTES)
    write_journal(engine, "BTCUSDT", tape(MINUTES, 3), start=T0, end=end + timedelta(minutes=1))
    return engine


def test_restart_with_complete_coverage_is_identical_to_uninterrupted_run(journal):
    db = journal
    a = open_runner(db, URL, "BTCUSDT", fresh=True)
    drain(a)
    baseline = outcome(db, a.engine.session_id)
    assert any(s[2] == "CLOSED" for s in baseline["setups"]), "scenario needs closed trades"

    b = open_runner(db, URL, "BTCUSDT", fresh=True)
    session = b.engine.session_id
    # crash 1: E1 armed, mid-input (the in-flight event's writes are rolled back)
    assert step_until(b, lambda e: e.active is not None and e.active.state is SetupState.E1_PENDING)
    crash_mid_input(b)
    b = open_runner(db, URL, "BTCUSDT", fresh=False)
    assert b.engine.session_id == session
    # crash 2: position open (no automatic close on restart, B39)
    assert step_until(b, lambda e: e.active is not None and e.broker.position_qty() != 0)
    pos_before = b.engine.broker.position_qty()
    b = open_runner(db, URL, "BTCUSDT", fresh=False)
    assert b.engine.broker.position_qty() == pos_before  # restored, not closed
    drain(b)
    assert outcome(db, session) == baseline
