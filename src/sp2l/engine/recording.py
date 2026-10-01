"""Recorder interface: the engine reports what happened; it never reads anything back.

Persistence is a pure side effect (DATA-01..03). Recorders must not influence runtime
decisions (SHD-06 for counterfactuals).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from sp2l.marketdata.m1_builder import M1Result
from sp2l.marketdata.m5_aggregator import M5Result

if TYPE_CHECKING:
    from sp2l.counterfactual.simulator import CounterfactualRun
    from sp2l.engine.setup_machine import SetupMachine
    from sp2l.engine.symbol_engine import PGapLog, ShadowSymbolEngine
    from sp2l.execution.shadow_broker import ShadowBroker


class Recorder(Protocol):
    def on_m1(self, m1: M1Result) -> None: ...
    def on_m5(self, m5: M5Result) -> None: ...
    def on_indicators(self, snapshot: dict[str, Any]) -> None: ...
    def on_pgap(self, log: PGapLog) -> Any: ...
    def on_candidate_created(self, machine: SetupMachine, pgap_id: Any) -> None: ...
    def on_archived(self, machine: SetupMachine, broker: ShadowBroker) -> None: ...
    def on_counterfactual(self, run: CounterfactualRun) -> None: ...
    def on_probe(self, record: dict[str, Any]) -> None: ...
    def on_setup_event(self, machine: SetupMachine, kind: str, event: dict[str, Any]) -> None: ...
    def checkpoint(self, engine: ShadowSymbolEngine) -> None: ...


class NullRecorder:
    def on_m1(self, m1: M1Result) -> None:
        pass

    def on_m5(self, m5: M5Result) -> None:
        pass

    def on_indicators(self, snapshot: dict[str, Any]) -> None:
        pass

    def on_pgap(self, log: PGapLog) -> Any:
        return None

    def on_candidate_created(self, machine: SetupMachine, pgap_id: Any) -> None:
        pass

    def on_archived(self, machine: SetupMachine, broker: ShadowBroker) -> None:
        pass

    def on_counterfactual(self, run: CounterfactualRun) -> None:
        pass

    def on_probe(self, record: dict[str, Any]) -> None:
        pass

    def on_setup_event(self, machine: SetupMachine, kind: str, event: dict[str, Any]) -> None:
        pass

    def checkpoint(self, engine: ShadowSymbolEngine) -> None:
        pass
