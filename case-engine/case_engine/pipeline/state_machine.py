"""The deterministic state machine (Strategy 2: topology as enforcement).

Purpose & role
--------------
This module is the single source of truth for pipeline control flow.  The
declared edge set (:data:`EDGES`) is the **only** legal way through the
pipeline: a node that returns a destination outside its declared
transitions raises :class:`TopologyViolationError` instead of executing.
The LLM never chooses control flow — it can only populate payloads, and
only valid payloads advance the machine (docs/ARCHITECTURE.md §1.1).

Invariants
----------
* The ``EDGES`` table is the single source of truth for control flow; the
  LLM has zero agency over transitions (Strategy 2).  Every node's key set
  matches ``EDGES``' key set (asserted by tests/unit/test_state_machine.py).
* Every transition returned by a node is validated against ``EDGES``
  *before* the next node runs; violations are logged at ERROR with the
  from/to nodes and then raised.
* Runtime is bounded: ``run`` trips a ``PipelineError`` step guard once
  ``max_steps`` node executions are exceeded — a runaway loop is always an
  engine bug, never a retryable condition.
* Identical semantics in the LangGraph adapter: the same ``EDGES`` table
  drives ``case_engine.pipeline.langgraph_adapter.build_case_graph``, so
  both runners must produce identical results.

Contents
--------
- ``EDGES``: node -> {edge_name -> destination} transition table.
- :class:`CASEStateMachine`: runner that dispatches the pure node functions
  and enforces the topology.

Cross-references: docs/PIPELINE.md (edge table + node-by-node contracts),
docs/ARCHITECTURE.md §1.1 (determinism is a topology property).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from case_engine.exceptions import PipelineError, TopologyViolationError
from case_engine.logging_setup import get_logger
from case_engine.observability import safe_incr, safe_observe
from case_engine.pipeline.context import EngineContext
from case_engine.pipeline.nodes import END_NODE, ENTRY_NODE, NODES

__all__ = ["EDGES", "CASEStateMachine"]

log = get_logger(__name__)

#: Declared transition table: node -> {edge_name -> destination}.
#: Mirrors the spec execution_pipeline with the documented reflexion
#: refinement (node_6 may return to node_1 or node_4 — the failing stage).
#: This table is the single source of truth for control flow: the state
#: machine raises :class:`TopologyViolationError` for any destination not
#: listed here, and the LangGraph adapter wires its conditional edges from
#: the same table so both runners enforce identical semantics.
EDGES: dict[str, dict[str, str]] = {
    "node_1_intent_classifier": {
        "success": "node_2_model_router",
        "failure": "node_6_reflexion_loop",
        # G1: PII detected without TEE attestation and pii_policy=escalate —
        # the session is refused BEFORE any LLM generation happens.
        "pii_escalation": "node_8_human_escalation",
    },
    "node_2_model_router": {
        "success": "node_3_schema_injection",
        "failure": "node_8_human_escalation",
    },
    "node_3_schema_injection": {
        "success": "node_4_llm_generation",
        "failure": "node_8_human_escalation",
    },
    "node_4_llm_generation": {
        "success": "node_5_ast_validator",
        "schema_violation": "node_6_reflexion_loop",
    },
    "node_5_ast_validator": {
        "all_valid": "node_7_audit_and_synthesis",
        "invalid": "node_6_reflexion_loop",
    },
    "node_6_reflexion_loop": {
        "retry": "node_4_llm_generation",
        "retry_classify": "node_1_intent_classifier",
        "exhausted": "node_8_human_escalation",
        # G2: fallback_behavior="passthrough_with_flags" — failed traces
        # proceed to synthesis, loudly flagged (FALLBACK_APPLIED event).
        "passthrough": "node_7_audit_and_synthesis",
    },
    "node_7_audit_and_synthesis": {"success": END_NODE},
    "node_8_human_escalation": {"success": END_NODE},
}


class CASEStateMachine:
    """Runner enforcing the declared topology over the 8 nodes.

    The machine owns no reasoning logic of its own: it dispatches the pure
    node functions from :data:`case_engine.pipeline.nodes.NODES`, validates
    each returned destination against :data:`EDGES`, and stops at ``END``.
    All side effects (LLM calls, audits) happen inside the nodes through
    the injected :class:`~case_engine.pipeline.context.EngineContext`.

    Attributes:
        _ctx: Engine context threaded into every node call.
        _nodes: Copy of the node registry (node id -> callable); a copy so
            later mutations of ``NODES`` cannot alter a running machine.

    Example:
        >>> machine = CASEStateMachine(ctx)        # doctest: +SKIP
        >>> final_state = machine.run(state)       # doctest: +SKIP
        >>> final_state["final_response"] is not None  # doctest: +SKIP
        True
    """

    def __init__(self, ctx: EngineContext) -> None:
        """Bind the machine to an engine context.

        The constructor never touches the network and never validates the
        registry — wiring is exercised lazily on first dispatch.

        Args:
            ctx: Shared services (registry, llm, ledger, ...).
        """
        self._ctx = ctx
        self._nodes: dict[str, Callable[[dict, EngineContext], tuple[str, dict]]] = dict(NODES)

    def run(self, state: dict, max_steps: int = 64) -> dict:
        """Execute the pipeline until ``END`` or the step guard trips.

        The runner is a strict dispatcher: pick the node, call it with
        ``(state, ctx)``, validate the returned destination against
        :data:`EDGES`, repeat.  It never interprets node results and never
        swallows exceptions — infrastructure failures (e.g. a broken
        ledger) propagate unchanged to the caller.

        Args:
            state: Initial state (must contain ``session_id`` and
                ``user_query``).  Not mutated; nodes copy-on-write.
            max_steps: Hard ceiling on node executions; a value below the
                happy-path length (7) immediately fails, and any larger
                runaway loop is a bug that must not spin forever.

        Returns:
            The final state (with ``final_response`` or escalation).

        Raises:
            PipelineError: On unknown node dispatch or step exhaustion
                (both logged at ERROR first — they indicate engine bugs,
                not validation rejections).
            TopologyViolationError: If a node returns an illegal edge
                (logged at ERROR with the from/to nodes before raising).
            LedgerError: Propagated from ``ctx.audit`` — audit is
                fail-closed, so the run halts rather than continuing
                unaudited.
            RegistryError: Propagated if a node cannot resolve a model id
                (deploy-time bug; never retried).

        Example:
            >>> final = CASEStateMachine(ctx).run(state)  # doctest: +SKIP
            >>> final["escalated"]                        # doctest: +SKIP
            False
        """
        node = ENTRY_NODE
        steps = 0
        log.info(
            "State machine start",
            extra={"session_id": state.get("session_id"),
                   "entry_node": ENTRY_NODE, "max_steps": max_steps},
        )
        while node != END_NODE:
            steps += 1
            if steps > max_steps:
                log.error(
                    "step guard tripped — runaway loop",
                    extra={"session_id": state.get("session_id"),
                           "steps": steps, "max_steps": max_steps,
                           "last_node": node},
                )
                raise PipelineError(
                    f"State machine exceeded max_steps={max_steps}",
                    session_id=state.get("session_id"), last_node=node,
                )
            node_fn = self._nodes.get(node)
            if node_fn is None:
                log.error(
                    "unknown node id — dispatch failure",
                    extra={"session_id": state.get("session_id"), "node": node},
                )
                raise PipelineError(f"No node registered for {node!r}")
            # G11: per-node call counter + latency observation (fail-open).
            safe_incr(self._ctx.metrics, "case_engine_node_calls_total", node=node)
            node_started = time.perf_counter()
            next_node, state = node_fn(state, self._ctx)
            safe_observe(self._ctx.metrics, "case_engine_node_latency_ms",
                         (time.perf_counter() - node_started) * 1000.0, node=node)
            allowed = set(EDGES[node].values())
            if next_node not in allowed:
                log.error(
                    "topology violation attempted",
                    extra={"session_id": state.get("session_id"), "step": steps,
                           "from": node, "to": next_node,
                           "allowed": sorted(allowed)},
                )
                raise TopologyViolationError(
                    f"Node {node} attempted illegal transition to {next_node!r}",
                    allowed=sorted(allowed),
                )
            log.debug("Transition", extra={"from": node, "to": next_node, "step": steps})
            node = next_node
        log.info(
            "State machine end",
            extra={"session_id": state.get("session_id"), "steps": steps,
                   "escalated": state.get("escalated", False)},
        )
        return state

    async def run_async(self, state: dict, max_steps: int = 64) -> dict:
        """Async twin of :meth:`run` — non-blocking event loop (G5, phase 1).

        Identical dispatch, topology guard, step bound and error
        semantics; each node executes via :func:`asyncio.to_thread` so
        the awaiting event loop stays free while nodes run (including
        their LLM round-trips — the whole point for FastAPI/LangGraph
        embeddings).  Determinism is untouched: nodes are pure functions
        and the thread boundary adds no shared mutable state (nodes are
        copy-on-write; the ledger is thread-safe).

        Phase 2 (documented in ``docs/LLM_PROVIDERS.md``) will dispatch
        native-async adapters directly on the loop; phase 1 already
        guarantees the property integrators need — *the loop never
        blocks*.

        Args:
            state: Initial state (must contain ``session_id`` and
                ``user_query``).  Not mutated; nodes copy-on-write.
            max_steps: Hard ceiling on node executions (as :meth:`run`).

        Returns:
            The final state (with ``final_response`` or escalation).

        Raises:
            PipelineError: On unknown node dispatch or step exhaustion
                (logged at ERROR first — engine bugs, not rejections).
            TopologyViolationError: If a node returns an illegal edge.
            LedgerError: Propagated from ``ctx.audit`` (fail-closed).

        Example:
            >>> final = await CASEStateMachine(ctx).run_async(state)  # doctest: +SKIP
            >>> final["final_response"] is not None          # doctest: +SKIP
            True
        """
        node = ENTRY_NODE
        steps = 0
        log.info(
            "State machine start (async)",
            extra={"session_id": state.get("session_id"),
                   "entry_node": ENTRY_NODE, "max_steps": max_steps},
        )
        while node != END_NODE:
            steps += 1
            if steps > max_steps:
                log.error(
                    "step guard tripped — runaway loop",
                    extra={"session_id": state.get("session_id"),
                           "steps": steps, "max_steps": max_steps,
                           "last_node": node},
                )
                raise PipelineError(
                    f"State machine exceeded max_steps={max_steps}",
                    session_id=state.get("session_id"), last_node=node,
                )
            node_fn = self._nodes.get(node)
            if node_fn is None:
                log.error(
                    "unknown node id — dispatch failure",
                    extra={"session_id": state.get("session_id"), "node": node},
                )
                raise PipelineError(f"No node registered for {node!r}")
            safe_incr(self._ctx.metrics, "case_engine_node_calls_total", node=node)
            node_started = time.perf_counter()
            next_node, state = await asyncio.to_thread(node_fn, state, self._ctx)
            safe_observe(self._ctx.metrics, "case_engine_node_latency_ms",
                         (time.perf_counter() - node_started) * 1000.0, node=node)
            allowed = set(EDGES[node].values())
            if next_node not in allowed:
                log.error(
                    "topology violation attempted",
                    extra={"session_id": state.get("session_id"), "step": steps,
                           "from": node, "to": next_node,
                           "allowed": sorted(allowed)},
                )
                raise TopologyViolationError(
                    f"Node {node} attempted illegal transition to {next_node!r}",
                    allowed=sorted(allowed),
                )
            log.debug("Transition (async)",
                      extra={"from": node, "to": next_node, "step": steps})
            node = next_node
        log.info(
            "State machine end (async)",
            extra={"session_id": state.get("session_id"), "steps": steps,
                   "escalated": state.get("escalated", False)},
        )
        return state
