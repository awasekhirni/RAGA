"""Engine configuration: global guardrails + runtime options.

This module implements the ``global_guardrails`` block of the C.A.S.E.
spec JSON as a validated Pydantic model, plus an :class:`EngineConfig`
container that wires guardrails together with ledger, logging and
provider defaults.  It is the single source of truth for *how the
engine is allowed to behave*: pipeline nodes read these values at run
time and enforce them — they are never advisory.

Configuration precedence (highest wins):

1. Explicit constructor arguments.
2. ``CASE_ENGINE_CONFIG`` environment variable pointing at a JSON file.
3. Built-in defaults taken verbatim from the spec.

Contents
--------
- :class:`GlobalGuardrails` — non-negotiable safety rails (retries,
  token budget, fallback behaviour, audit posture).
- :class:`EngineConfig` — provider/ledger/logging wiring, with
  :meth:`EngineConfig.from_file` (JSON file loader) and
  :meth:`EngineConfig.load` (precedence resolver).

Invariants
----------
- Both models use ``extra="forbid"`` and ``validate_assignment=True``:
  unknown or misspelled options fail fast at construction time.
- ``allow_unvalidated_passthrough`` defaults to ``False`` and emits a
  loud WARNING when enabled (fail-closed posture).
- File loading wraps missing-file / invalid-JSON OS errors into
  :class:`~case_engine.exceptions.ConfigurationError` with the offending
  path in the message and ``from exc`` chaining; other ``OSError``
  subclasses and Pydantic ``ValidationError`` propagate unchanged.
- Secrets: ``llm_kwargs`` may carry provider API keys; this module logs
  file paths and top-level section names only — never values.

Example
-------
>>> cfg = EngineConfig(guardrails=GlobalGuardrails(max_reflexion_retries=2))
>>> cfg.guardrails.max_reflexion_retries
2
>>> cfg = EngineConfig.load("case_config.json")   # doctest: +SKIP

See also: docs/ARCHITECTURE.md, case_engine/engine.py (the consumer),
case_engine/pipeline/nodes.py (where each guardrail binds),
case_engine/audit/__init__.py and case_engine/llm/__init__.py (factories
keyed by ``ledger_backend`` / ``provider``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from case_engine.exceptions import ConfigurationError
from case_engine.logging_setup import get_logger
from case_engine.version import ENGINE_NAME

__all__ = ["GlobalGuardrails", "EngineConfig"]

_ENV_VAR = "CASE_ENGINE_CONFIG"

log = get_logger(__name__)


class GlobalGuardrails(BaseModel):
    """Non-negotiable engine-wide safety rails (spec ``global_guardrails``).

    These values are enforced by the pipeline nodes; they are never
    advisory.  See ``docs/PIPELINE.md`` for where each rail binds.

    Attributes:
        max_reasoning_tokens: Token ceiling forwarded to LLM providers for
            each reasoning/tool-call request.
        schema_validation_timeout_ms: Soft wall-clock budget for validating
            one trace (schema + rules).  Exceeding it logs a WARNING; the
            validation result is still honoured (fail-closed semantics).
        max_reflexion_retries: Maximum number of reject-and-regenerate
            cycles after the initial generation attempt.
        fallback_behavior: What to do when reflexion is exhausted.  Four
            policies: ``escalate_to_human_with_partial_trace`` (default —
            node_6 routes to node_8 with all partial evidence),
            ``abort_session`` (node_6 routes to node_8 which closes the
            session as ``aborted`` — no human escalation is claimed),
            ``passthrough_with_flags`` (node_6 proceeds to node_7 with the
            failed traces flagged ``passthrough=True`` in the result and
            a loud ``FALLBACK_APPLIED`` audit event — never silent) and
            ``raise`` (infrastructure failures in :meth:`CASEEngine.run`
            re-raise instead of packaging an escalation).
        security_posture: Declarative posture string; the engine implements
            ``zero_trust_tool_execution`` (every tool call is re-validated
            server-side regardless of provider-side schema enforcement).
        default_confidence_threshold: Floor applied when a mental model
            does not declare its own ``confidence_threshold``.
        allow_unvalidated_passthrough: MUST remain ``False`` in production.
            When ``True`` the engine emits a loud warning and skips rule
            validation (useful only for debugging the engine itself).
        audit_every_trace: When ``True`` every generated trace is written
            to the hash-chained ledger; when ``False`` only rejections and
            the final synthesis are written.
        tee_required_for_pii: Enforces the spec's data-protection clause
            (G1).  When ``True`` and node_1 detects PII in the query
            without TEE attestation, the session follows
            ``pii_policy`` (escalate or allow-with-flags) and a
            ``TEE_DECISION`` audit event records the decision and the
            detected categories.
        pii_policy: What to do with a PII-bearing session that has no TEE
            attestation: ``escalate`` (default — fail-closed, the session
            routes to node_8 without any LLM generation) or
            ``allow_with_flags`` (the session proceeds but is flagged
            ``pii_flagged`` in the result and in the ``TEE_DECISION``
            audit event).  Ignored when ``tee_required_for_pii=False``.
    """

    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    max_reasoning_tokens: int = Field(default=4096, ge=256, le=200_000)
    schema_validation_timeout_ms: int = Field(default=2000, ge=50, le=60_000)
    max_reflexion_retries: int = Field(default=3, ge=0, le=20)
    fallback_behavior: Literal[
        "escalate_to_human_with_partial_trace",
        "abort_session",
        "passthrough_with_flags",
        "raise",
    ] = "escalate_to_human_with_partial_trace"
    security_posture: Literal["zero_trust_tool_execution"] = "zero_trust_tool_execution"
    default_confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    allow_unvalidated_passthrough: bool = False
    audit_every_trace: bool = True
    tee_required_for_pii: bool = True
    pii_policy: Literal["escalate", "allow_with_flags"] = "escalate"

    @field_validator("allow_unvalidated_passthrough")
    @classmethod
    def _warn_on_passthrough(cls, value: bool) -> bool:
        """Log a loud warning when unvalidated passthrough is enabled.

        Args:
            value: The parsed flag value.

        Returns:
            The flag unchanged (validation-only hook).
        """
        if value:
            log.warning(
                "allow_unvalidated_passthrough=True - schema/rule enforcement is "
                "DISABLED. Never use this outside engine debugging."
            )
        return value


class EngineConfig(BaseModel):
    """Top-level runtime configuration for :class:`case_engine.engine.CASEEngine`.

    Attributes:
        engine_name: Static identity string (informational).
        guardrails: The :class:`GlobalGuardrails` in force for this engine.
        provider: Default LLM provider key used by ``create_llm_client``.
        llm_kwargs: Provider-specific kwargs (``model``, ``base_url``, ...).
            May carry provider credentials (e.g. API keys); values are
            never logged — only kwarg names appear in log records.
        ledger_backend: ``"jsonl"`` | ``"sqlite"`` | ``"memory"``.
        ledger_path: File path for persistent ledgers; ``None`` uses
            ``./case_audit.jsonl`` / ``./case_audit.sqlite3`` respectively.
        log_level: Logging level applied by ``configure_logging``.
        log_json: Emit JSON log lines when ``True``.
        tee_attested: Operator attestation that this deployment runs
            inside a Trusted Execution Environment; when ``True``, PII
            detection no longer escalates/flags (the ``TEE_DECISION``
            audit event records the attestation instead).  Also
            overridable per call via ``CASEEngine.run(tee_attested=...)``.
        redact_pii_in_audit: When ``True``, a :class:`PIIRedactor`
            tokenizes PII out of every audit payload before it reaches
            the ledger (G4a).  Default ``False`` keeps the pre-existing
            byte-identical behavior; enable for GDPR/CCPA-sensitive
            deployments.
        ledger_encryption: When ``True``, payloads are envelope-encrypted
            at rest with AES-256-GCM (G4b; requires the ``cryptography``
            package and a key via ``CASE_ENGINE_LEDGER_KEY``).  The hash
            chain is computed over ciphertext, so ``verify_chain`` stays
            key-free.
        result_store_backend: Session-result persistence (G12):
            ``"none"`` (default — legacy behaviour, results are returned
            but not stored), ``"memory"`` (volatile) or ``"sqlite"``
            (durable; enables ``case-engine history``).  The store is
            best-effort and never fails a session.
        result_store_path: File path for the ``"sqlite"`` result store;
            ``None`` uses ``./case_results.sqlite3``.
        escalation_sink_kind: node_8 escalation delivery (G6):
            ``"none"`` (default — ledger-only), ``"file"`` (JSONL
            handoff file) or ``"logging"`` (structured WARNING log).
            HTTP webhooks are available programmatically via
            :class:`~case_engine.escalation.WebhookEscalationSink` and
            engine injection (not config, to keep credentials out of
            config files).
        escalation_sink_path: Handoff JSONL path for
            ``escalation_sink_kind="file"``; ``None`` uses
            ``./case_escalations.jsonl``.

    Example:
        >>> cfg = EngineConfig(provider="mock", ledger_backend="memory")
        >>> (cfg.provider, cfg.ledger_backend)
        ('mock', 'memory')
    """

    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    engine_name: str = Field(default=ENGINE_NAME, frozen=True)
    guardrails: GlobalGuardrails = Field(default_factory=GlobalGuardrails)
    provider: Literal["mock", "openai", "anthropic", "ollama", "vllm"] = "mock"
    llm_kwargs: dict[str, Any] = Field(default_factory=dict)
    ledger_backend: Literal["jsonl", "sqlite", "memory"] = "jsonl"
    ledger_path: str | None = None
    log_level: str = "INFO"
    log_json: bool = False
    tee_attested: bool = False
    redact_pii_in_audit: bool = False
    ledger_encryption: bool = False
    result_store_backend: Literal["none", "memory", "sqlite"] = "none"
    result_store_path: str | None = None
    escalation_sink_kind: Literal["none", "file", "logging"] = "none"
    escalation_sink_path: str | None = None

    @classmethod
    def from_file(cls, path: str | Path) -> EngineConfig:
        """Build a configuration from a JSON file.

        The document must be a single JSON object matching this model's
        schema; unknown keys are rejected (``extra="forbid"``) so
        misspelled options fail fast.  The read is logged at INFO before
        it happens; parse failures are logged at ERROR (with traceback)
        before the wrapped error is raised with ``from exc`` chaining.

        Args:
            path: Path to a JSON document matching this model's schema.

        Returns:
            Parsed :class:`EngineConfig` (fully validated).

        Raises:
            ConfigurationError: If the file is missing or invalid JSON
                (raised with ``from exc`` chaining and logged at ERROR
                first).
            pydantic.ValidationError: If the JSON does not match the
                schema (propagated unchanged).
            OSError: Filesystem errors other than "missing file" (e.g.
                permission denied) propagate unchanged.
        """
        p = Path(path)
        log.info("Loading engine config", extra={"path": str(p)})
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            log.error("config file not found", extra={"path": str(p)}, exc_info=True)
            raise ConfigurationError(f"Config file not found: {p}") from exc
        except json.JSONDecodeError as exc:
            log.error("config file is not valid JSON",
                      extra={"path": str(p)}, exc_info=True)
            raise ConfigurationError(f"Config file is not valid JSON: {p} ({exc})") from exc
        config = cls.model_validate(raw)
        log.debug("engine config validated", extra={"path": str(p),
                                                    "top_level": type(raw).__name__})
        return config

    @classmethod
    def load(cls, path: str | Path | None = None) -> EngineConfig:
        """Load configuration from *path*, the env var, or defaults.

        Resolution order: explicit *path* → ``CASE_ENGINE_CONFIG`` env var
        → default-constructed config.  Each resolution branch is logged
        (INFO when a file is used, DEBUG for the defaults path) so
        operators can see which configuration source won.

        Args:
            path: Optional explicit config path.

        Returns:
            A fully-validated :class:`EngineConfig`.

        Raises:
            ConfigurationError: Propagated from :meth:`from_file` when an
                explicit or env-var-provided file cannot be read or
                parsed (logged at ERROR before the raise).
        """
        log.debug(
            "resolving engine configuration",
            extra={"explicit_path": str(path) if path is not None else None},
        )
        if path is not None:
            return cls.from_file(path)
        env_path = os.environ.get(_ENV_VAR)
        if env_path:
            log.info("Using config from environment", extra={"env": _ENV_VAR, "path": env_path})
            return cls.from_file(env_path)
        log.debug("No config file supplied - using built-in defaults")
        return cls()
