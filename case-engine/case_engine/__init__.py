"""C.A.S.E. — Cognitive Architectural Scaffolding & Enforcement Engine.

Top-level facade and public API::

    from case_engine import CASEEngine, create_llm_client, create_ledger

    engine = CASEEngine()                      # mock provider, JSONL ledger
    result = engine.run("Why are users churning?")
    print(result.final_response)
    assert engine.verify_audit_chain().valid

Contents
--------
- :class:`~case_engine.engine.CASEEngine` — the one-object facade
  (run / verify / introspect).
- :class:`~case_engine.config.EngineConfig`,
  :class:`~case_engine.config.GlobalGuardrails` — runtime configuration.
- :class:`~case_engine.schemas.pipeline.CASEState`,
  :class:`~case_engine.schemas.pipeline.CASEResult` — pipeline
  state/result schemas.
- :class:`~case_engine.registry.models.CognitiveRegistry`,
  :class:`~case_engine.registry.routing.ModelRouter` — mental-model
  registry and Cynefin router.
- :class:`~case_engine.llm.base.BaseLLMClient`,
  :class:`~case_engine.llm.mock_client.MockLLMClient`,
  :func:`~case_engine.llm.create_llm_client` — provider adapters and
  their factory.
- :class:`~case_engine.audit.AuditLedger`,
  :func:`~case_engine.audit.create_ledger` — hash-chained audit backends
  and their factory.
- :class:`~case_engine.validation.engine.ValidationEngine`,
  :class:`~case_engine.validation.llm_judge.LLMBackedJudge` — trace
  validation (rule engine + optional LLM judge).
- :class:`~case_engine.observability.Metrics`,
  :class:`~case_engine.observability.NoopMetrics`,
  :class:`~case_engine.observability.InMemoryMetrics` — observability
  sinks (G11; default no-op).
- :class:`~case_engine.storage.ResultStore`,
  :func:`~case_engine.storage.create_result_store` — optional
  session-result persistence (G12).
- :class:`~case_engine.llm.retry.RetryPolicy` — provider transport
  resilience parameters (G8).
- :class:`~case_engine.schemas.pipeline.TokenUsage` — per-session token
  accounting (G7).
- :class:`~case_engine.exceptions.CASEError` — root of the engine
  exception hierarchy.
- ``ENGINE_NAME`` / ``SCHEMA_VERSION`` / ``__version__`` — identity
  constants (see :mod:`case_engine.version`).

Invariants
----------
- The names in ``__all__`` are the supported public API; everything else
  in submodules is internal and may change without notice.
- Importing this package performs no network or filesystem side effects;
  registries, ledgers and LLM clients are only built when an engine is
  constructed.

See ``docs/ARCHITECTURE.md`` for the design, ``docs/EXTENDING.md`` to add
mental models, rules, providers and ledger backends.
"""

from __future__ import annotations

from case_engine.audit import AuditLedger, create_ledger
from case_engine.config import EngineConfig, GlobalGuardrails
from case_engine.engine import CASEEngine
from case_engine.exceptions import CASEError
from case_engine.llm import BaseLLMClient, MockLLMClient, create_llm_client
from case_engine.llm.retry import RetryPolicy
from case_engine.observability import InMemoryMetrics, Metrics, NoopMetrics
from case_engine.privacy import (
    NoopPIIDetector,
    NoopRedactor,
    PIIDetector,
    PIIFinding,
    PIIRedactor,
    RegexPIIDetector,
)
from case_engine.registry import CognitiveRegistry, ModelRouter
from case_engine.schemas.pipeline import CASEResult, CASEState, TokenUsage
from case_engine.storage import ResultStore, create_result_store
from case_engine.validation import LLMBackedJudge, ValidationEngine
from case_engine.version import ENGINE_NAME, SCHEMA_VERSION, __version__

__all__ = [
    "CASEEngine", "CASEState", "CASEResult", "TokenUsage",
    "EngineConfig", "GlobalGuardrails",
    "CognitiveRegistry", "ModelRouter",
    "BaseLLMClient", "MockLLMClient", "create_llm_client", "RetryPolicy",
    "AuditLedger", "create_ledger",
    "ValidationEngine", "LLMBackedJudge",
    "CASEError",
    "PIIDetector", "PIIFinding", "RegexPIIDetector", "NoopPIIDetector",
    "PIIRedactor", "NoopRedactor",
    "Metrics", "NoopMetrics", "InMemoryMetrics",
    "ResultStore", "create_result_store",
    "ENGINE_NAME", "SCHEMA_VERSION", "__version__",
]
