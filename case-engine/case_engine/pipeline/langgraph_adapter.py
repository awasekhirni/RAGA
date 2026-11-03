"""Optional LangGraph adapter (Strategy 2 reference implementation).

Purpose & role
--------------
Wraps the same 8 node functions in a ``langgraph.StateGraph`` so teams
already standardized on LangGraph get identical semantics — same nodes,
same hard edges (wired from the same :data:`EDGES` table), same audit
trail.  ``langgraph`` is an *optional* dependency: importing this module
without it is safe, and the first graph build raises a clear
:class:`LLMProviderUnavailableError`.

Invariants
----------
* Parity: the pure-Python :class:`CASEStateMachine` stays the semantic
  reference; this adapter must produce identical results for identical
  inputs (covered by tests/integration/test_langgraph_parity.py, which is
  skipped when langgraph is not installed).
* Topology: conditional edges are declared from ``EDGES`` only, and
  LangGraph itself rejects undeclared conditional routes — the same
  guarantee as the pure runner's ``TopologyViolationError``.
* Lazy import: the ``langgraph`` package is imported only when a graph is
  actually built, so the rest of the engine never pays for it.

Contents
--------
- :func:`_require_langgraph` — guarded lazy import with a clear error.
- :func:`build_case_graph` — compile the 8-node pipeline as a StateGraph.
- :func:`run_case_graph` — compile + invoke in one call.

Async embeddings (G5): the compiled runnable supports LangGraph's
native ``ainvoke`` for I/O offloading only when node functions are
coroutines; these wrappers are synchronous, so async callers should
prefer :meth:`case_engine.engine.CASEEngine.run_async` (phase 1 —
thread-offloaded nodes, non-blocking event loop, identical semantics).
Wrapping the same ``EDGES``/``NODES`` tables with coroutine adapters is
the documented phase-2 upgrade (``docs/LLM_PROVIDERS.md``).

Cross-references: docs/PIPELINE.md (edge table), docs/ARCHITECTURE.md §1.1
(topology as enforcement), case_engine/pipeline/state_machine.py (EDGES).
"""

from __future__ import annotations

from typing import Any

from case_engine.exceptions import LLMProviderUnavailableError
from case_engine.logging_setup import get_logger
from case_engine.pipeline.context import EngineContext
from case_engine.pipeline.nodes import END_NODE, ENTRY_NODE, NODES
from case_engine.pipeline.state_machine import EDGES

__all__ = ["build_case_graph", "run_case_graph"]

log = get_logger(__name__)


def _require_langgraph() -> Any:
    """Import and return the langgraph module or raise a clear error.

    The import is deliberately lazy and inside the function so the rest of
    the engine (and this module's import) works without the optional
    dependency installed.

    Returns:
        The ``langgraph.graph`` module.

    Raises:
        LLMProviderUnavailableError: If langgraph is not installed
            (logged at ERROR first, chained from the ``ImportError``).
    """
    try:
        from langgraph import graph as lg_graph
    except ImportError as exc:
        log.error(
            "langgraph not installed — optional adapter unavailable",
            extra={"hint": "pip install 'case-engine[langgraph]'"},
        )
        raise LLMProviderUnavailableError(
            "langgraph is not installed. Install with: pip install 'case-engine[langgraph]'"
        ) from exc
    log.debug("langgraph available", extra={"module": "langgraph.graph"})
    return lg_graph


def build_case_graph(ctx: EngineContext) -> Any:
    """Compile the C.A.S.E. pipeline as a LangGraph ``StateGraph``.

    Each LangGraph node executes the corresponding pure-Python node and
    stores its chosen destination in ``state['next_node']``; conditional
    edges then read that key, so the *same* transition semantics are
    enforced (LangGraph raises on undeclared conditional routes).  The
    wiring is generated exclusively from :data:`EDGES` — exactly like the
    pure runner — which is what guarantees parity between the two.

    Args:
        ctx: Engine context (shared services).

    Returns:
        A compiled LangGraph runnable.

    Raises:
        LLMProviderUnavailableError: If langgraph is missing.
    """
    lg_graph = _require_langgraph()

    from case_engine.schemas.pipeline import CASEState

    # CASEState (TypedDict) doubles as LangGraph's channel schema so every
    # state key survives LangGraph's last-value-wins channel merging.
    builder = lg_graph.StateGraph(CASEState)
    log.debug(
        "langgraph builder created",
        extra={"channel_schema": CASEState.__name__, "nodes": len(NODES)},
    )

    def _wrap(node_id: str):
        """Create a LangGraph node shim for pure node *node_id*.

        Args:
            node_id: Registered node id.

        Returns:
            Callable suitable for ``add_node``.
        """
        fn = NODES[node_id]

        def _run(state: dict) -> dict:
            """Execute one pipeline node inside the LangGraph runtime.

            Copies the incoming state (nodes never mutate their input),
            delegates to the pure node function, and stores the chosen
            destination in ``state['next_node']`` for the conditional-edge
            router.

            Args:
                state: LangGraph channel state (all ``CASEState`` keys).

            Returns:
                The updated state dict with ``next_node`` set.
            """
            log.debug(
                "langgraph node entry",
                extra={"node": node_id, "session_id": state.get("session_id")},
            )
            next_node, new_state = fn(dict(state), ctx)
            new_state["next_node"] = next_node
            log.debug(
                "langgraph node exit",
                extra={"node": node_id, "session_id": new_state.get("session_id"),
                       "next_node": next_node},
            )
            return new_state

        return _run

    for node_id in NODES:
        builder.add_node(node_id, _wrap(node_id))
        log.debug("langgraph node wired", extra={"node": node_id})

    def _route(state: dict) -> str:
        """Conditional-edge router reading the node's decision.

        Reads ``state['next_node']`` — written by the ``_run`` shim — and
        maps the pure runner's ``END`` sentinel to LangGraph's.  Because
        the conditional edges are declared from ``EDGES`` (see below),
        LangGraph itself raises if a node ever produces an undeclared
        destination: the same topology guarantee as ``CASEStateMachine``.

        Args:
            state: Post-node state carrying ``next_node``.

        Returns:
            The chosen node id, mapped to LangGraph's END sentinel when the
            pipeline finished.
        """
        next_node = state.get("next_node") or END_NODE
        log.debug(
            "conditional route chosen",
            extra={"next_node": next_node, "session_id": state.get("session_id")},
        )
        return LG_END if next_node == END_NODE else next_node

    LG_END = lg_graph.END  # LangGraph's terminal sentinel ("__end__")
    builder.set_entry_point(ENTRY_NODE)
    for node_id, edges in EDGES.items():
        destinations = {
            (LG_END if dest == END_NODE else dest): dest
            for dest in edges.values()
        }
        if list(destinations) == [LG_END]:
            # Terminal-producing nodes (7, 8) wire straight to END.
            builder.add_edge(node_id, LG_END)
            log.debug("terminal node wired to END", extra={"node": node_id})
            continue
        builder.add_conditional_edges(node_id, _route, destinations)
        log.debug(
            "conditional edges wired",
            extra={"node": node_id, "destinations": sorted(destinations)},
        )

    log.info(
        "LangGraph case graph compiled",
        extra={"entry_point": ENTRY_NODE, "nodes": len(NODES)},
    )
    return builder.compile()


def run_case_graph(ctx: EngineContext, state: dict) -> dict:
    """Compile and run the graph in one call.

    Convenience wrapper mirroring ``CASEStateMachine.run`` — same entry
    node, same edge table, same audit trail, so results are directly
    comparable (see tests/integration/test_langgraph_parity.py).

    Args:
        ctx: Engine context.
        state: Initial pipeline state (not mutated; copied on entry).

    Returns:
        The final state dict.

    Raises:
        LLMProviderUnavailableError: If langgraph is missing.
    """
    graph = build_case_graph(ctx)
    log.debug(
        "langgraph invoke starting",
        extra={"session_id": state.get("session_id"), "recursion_limit": 64},
    )
    final = graph.invoke(dict(state), config={"recursion_limit": 64})
    log.info(
        "LangGraph run complete",
        extra={"session_id": state.get("session_id"),
               "escalated": final.get("escalated", False),
               "all_valid": final.get("all_valid", False)},
    )
    return dict(final)
