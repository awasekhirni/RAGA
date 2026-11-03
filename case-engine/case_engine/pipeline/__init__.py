"""Pipeline subpackage — the deterministic control plane of the engine.

Purpose & role
--------------
Wires the eight pure node functions (:mod:`case_engine.pipeline.nodes`)
into a strict, topology-enforced execution graph
(:mod:`case_engine.pipeline.state_machine`) and exposes the shared service
bundle (:class:`EngineContext`) every node receives.  An optional adapter
(:mod:`case_engine.pipeline.langgraph_adapter`) re-expresses the exact same
graph for LangGraph users; both runners must produce identical results and
share one audit trail.

Contents
--------
- :class:`EngineContext` — dependency bundle + fail-closed audit helper.
- :class:`CASEStateMachine` — topology-enforcing runner (semantic reference).
- ``EDGES`` — the declared transition table (single source of truth).
- ``NODES`` — node-id -> pure-function registry; ``ENTRY_NODE`` /
  ``END_NODE`` graph boundary sentinels.
- :func:`build_case_graph` / :func:`run_case_graph` — optional LangGraph
  adapter (requires the ``langgraph`` extra; imported lazily).

Invariants: control flow is data (``EDGES``) — the LLM has zero agency
over transitions (Strategy 2); every node outcome is audited (fail-closed
ledger).

Cross-references: docs/PIPELINE.md (node-by-node contract),
docs/ARCHITECTURE.md (why topology-is-enforcement).
"""

from __future__ import annotations

from case_engine.pipeline.context import EngineContext
from case_engine.pipeline.langgraph_adapter import build_case_graph, run_case_graph
from case_engine.pipeline.nodes import END_NODE, ENTRY_NODE, NODES
from case_engine.pipeline.state_machine import EDGES, CASEStateMachine

__all__ = [
    "EngineContext", "CASEStateMachine", "EDGES", "NODES",
    "build_case_graph", "run_case_graph", "ENTRY_NODE", "END_NODE",
]
