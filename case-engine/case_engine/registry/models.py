"""Typed registry primitives: mental-model and validation-rule definitions.

Purpose & role
--------------
The registry is the single source of truth the whole engine reads from:
routing, schema injection, validation and audit all dereference entries
from here.  It is loaded from the spec-transcribed data modules and then
**consistency-checked** at construction time (fail-fast on broken data).

Contents
--------
* :class:`MentalModelDefinition` — one frozen registry entry (MM001–MM015).
* :class:`ValidationRuleDefinition` — one frozen rule entry (VR001–VR016).
* :class:`CognitiveRegistry` — immutable, consistency-checked view with
  O(1) lookups by algorithm id / tool name / rule id.

Invariants
----------
* ``algorithm_id`` (MM001–MM015) and ``rule_id`` (VR001–VR016) are stable
  identifiers used in audit records and the routing matrix — never rename.
* ``schema_class`` doubles as the LLM tool name (Strategy 1) and must
  match a class in :mod:`case_engine.schemas.mental_models`.
* Construction fails fast via :class:`case_engine.exceptions.RegistryError`
  on duplicate ids or dangling references (unknown rule, unknown model,
  unknown affinity domain).

Cross-references
----------------
* Raw data: ``case_engine/registry/model_data_a.py`` / ``model_data_b.py``
  / ``rule_data.py``; router: :mod:`case_engine.registry.routing`.
* Docs: ``docs/MENTAL_MODELS.md``, ``docs/VALIDATION_RULES.md``,
  ``docs/ARCHITECTURE.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from case_engine.exceptions import RegistryError
from case_engine.logging_setup import get_logger
from case_engine.registry.model_data_a import MODELS_A
from case_engine.registry.model_data_b import MODELS_B
from case_engine.registry.rule_data import ROUTING_MATRIX, RULES

__all__ = ["MentalModelDefinition", "ValidationRuleDefinition", "CognitiveRegistry"]

log = get_logger(__name__)


class MentalModelDefinition(BaseModel):
    """One entry of the cognitive registry (one mental model).

    Why it exists: it is the machine-readable description the router
    (node_2), the tool factory / prompt injection (node_3-4), the
    validator (node_5) and the synthesizer (node_7) all read.  Instances
    are frozen — treat them as immutable reference data.

    Attributes:
        algorithm_id: Canonical id, e.g. ``"MM005"``.
        algorithm_name: Human name, e.g. ``"Root Cause Analysis (5 Whys)"``.
        schema_class: Name of the authoritative Pydantic contract class in
            :mod:`case_engine.schemas.mental_models`; also used as the
            LLM tool name.
        version: Model definition version (semver).
        category / tags: Classification metadata for discovery & UI.
        description_for_llm: One-paragraph instruction injected into the
            system prompt when the model is executed.
        domain_affinity: ``domain -> [0,1]`` affinity scores used by the
            router for ranking.
        input_contract: Declared required/optional inputs (documentation).
        output_contract: Declared outputs and downstream chaining.
        pydantic_schema: Spec-verbatim schema string (reference only —
            never executed by the engine; see docs/ARCHITECTURE.md).
        validation_rules: Rule ids (VR*) that gate this model's traces.
        examples: Gold examples used by tests, demos and few-shot prompts.
        chain_with: Suggested follow-up model ids (or ``"ROUTER"``).
        confidence_threshold: Per-model confidence floor (VR016).
        tee_compatible: Whether the trace may be processed inside a TEE.
        audit_hooks: Hook names fired at synthesis time (node_7).
        summary_fields: Fields extracted by the synthesizer for the final
            user-facing response.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    algorithm_id: str = Field(
        pattern=r"^MM\d{3}$",
        description='Canonical stable id, e.g. "MM005"; used in audit records — never rename',
    )
    algorithm_name: str = Field(..., description='Human-readable name, e.g. "Root Cause Analysis (5 Whys)"')
    schema_class: str = Field(
        ...,
        description="Authoritative contract class in case_engine.schemas.mental_models; doubles as the LLM tool name",
    )
    version: str = Field(default="1.0.0", description="Model definition version (semver)")
    category: str = Field(..., description="Spec category grouping for discovery & UI")
    tags: list[str] = Field(default_factory=list, description="Free-form discovery/UI tags")
    description_for_llm: str = Field(
        ..., description="Instruction injected into the system prompt at node_4"
    )
    domain_affinity: dict[str, float] = Field(
        ...,
        description="domain -> [0,1] affinity scores ranked by the node_2 router",
    )
    input_contract: dict[str, list[str]] = Field(
        ..., description="Declared required/optional inputs (documentation only)"
    )
    output_contract: dict[str, Any] = Field(
        ..., description="Declared outputs and downstream chaining (documentation only)"
    )
    pydantic_schema: str = Field(
        ...,
        description="Spec-verbatim schema string; reference only, never executed",
    )
    validation_rules: list[str] = Field(
        ..., description="VR* rule ids gating this model's traces at node_5"
    )
    examples: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Gold examples used by tests, demos and few-shot prompts",
    )
    chain_with: list[str] = Field(
        default_factory=list,
        description='Suggested follow-up model ids (or "ROUTER")',
    )
    confidence_threshold: float = Field(
        ge=0.0, le=1.0, description="Per-model confidence floor enforced by VR016"
    )
    tee_compatible: bool = Field(
        default=True, description="Whether the trace may be processed inside a TEE"
    )
    audit_hooks: list[str] = Field(
        default_factory=list, description="Hook names fired at synthesis time (node_7)"
    )
    summary_fields: list[str] = Field(
        default_factory=list,
        description="Fields extracted by the synthesizer for the final response",
    )

    @property
    def tool_name(self) -> str:
        """Return the LLM tool name for this model (its schema class name).

        The class name doubles as the tool name (Strategy 1), so this is
        the join key between registry entries and the typed contracts in
        :mod:`case_engine.schemas.mental_models`.

        Returns:
            e.g. ``"RootCauseAnalysisTrace"`` for MM005.

        Example:
            >>> reg = CognitiveRegistry.load_default()
            >>> reg.get("MM005").tool_name
            'RootCauseAnalysisTrace'
        """
        return self.schema_class


class ValidationRuleDefinition(BaseModel):
    """One entry of the validation-rules registry.

    Why it exists: it declares *what* is enforced per model; the
    executable heuristics keyed by ``rule_id`` live in
    ``case_engine/validation/rules.py``.  Frozen like the model entries.

    Attributes:
        rule_id: Canonical id, e.g. ``"VR005"``.
        name: snake_case rule name.
        type: ``semantic`` | ``structural`` | ``mathematical`` |
            ``conditional`` | ``lookup``.
        applies_to: Model ids the rule gates; ``["UNIVERSAL"]`` for VR016.
        description: Human-readable enforcement statement.
        auditor_prompt: Prompt for the optional LLM-as-judge upgrade
            (present for semantic rules).
        implementation_hint: Spec-verbatim hint describing the heuristic.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str = Field(
        pattern=r"^VR\d{3}$",
        description='Canonical stable id, e.g. "VR005"; used in audit records — never rename',
    )
    name: str = Field(..., description="snake_case rule name")
    type: str = Field(
        ...,
        description="semantic | structural | mathematical | conditional | lookup",
    )
    applies_to: list[str] = Field(
        ...,
        description='Model ids the rule gates; ["UNIVERSAL"] for VR016',
    )
    description: str = Field(..., description="Human-readable enforcement statement")
    auditor_prompt: str | None = Field(
        default=None,
        description="Prompt for the optional LLM-as-judge upgrade; None for mechanical rules",
    )
    implementation_hint: str | None = Field(
        default=None,
        description="Spec-verbatim heuristic hint; executable version in validation/rules.py",
    )


class CognitiveRegistry:
    """Immutable, consistency-checked view over the cognitive registry.

    Responsibilities:
        * Load the 15 mental models and 16 validation rules.
        * Provide O(1) lookups by algorithm id, tool name and rule id.
        * Fail fast at construction if the data is inconsistent (unknown
          rule reference, unroutable model id, unknown affinity domain).

    Example:
        >>> reg = CognitiveRegistry.load_default()
        >>> reg.get("MM005").algorithm_name
        'Root Cause Analysis (5 Whys)'
        >>> reg.get_by_tool_name("RootCauseAnalysisTrace").algorithm_id
        'MM005'
    """

    #: Valid affinity / routing domain keys (4 Cynefin + 4 specialist).
    KNOWN_DOMAINS: frozenset[str] = frozenset(
        {
            "Clear", "Complicated", "Complex", "Chaotic",
            "Legacy_Refactoring", "AI_Alignment_Metrics",
            "IoT_Telemetry_Diagnosis", "Team_Org_Design",
        }
    )

    def __init__(
        self,
        models: list[MentalModelDefinition],
        rules: list[ValidationRuleDefinition],
        routing_matrix: dict[str, list[str]],
    ) -> None:
        """Store and validate the registry contents.

        Args:
            models: Parsed mental-model definitions.
            rules: Parsed validation-rule definitions.
            routing_matrix: Domain -> ranked model-id lists.

        Raises:
            RegistryError: On duplicate ids or inconsistent references.
                The exception message is one of ``"Duplicate algorithm_id
                in registry"`` / ``"Duplicate rule_id in registry"`` or
                the consistency-check messages below; structured context
                (``algorithm_id`` / ``rule_id`` / ``domain``) travels on
                ``RegistryError.context``.
        """
        log.debug(
            "Constructing cognitive registry",
            extra={
                "model_count": len(models),
                "rule_count": len(rules),
                "routing_domains": len(routing_matrix),
            },
        )
        self._models: dict[str, MentalModelDefinition] = {}
        for m in models:
            if m.algorithm_id in self._models:
                log.error(
                    "Registry construction failed: duplicate algorithm_id",
                    extra={"algorithm_id": m.algorithm_id},
                )
                raise RegistryError("Duplicate algorithm_id in registry", algorithm_id=m.algorithm_id)
            self._models[m.algorithm_id] = m

        self._rules: dict[str, ValidationRuleDefinition] = {}
        for r in rules:
            if r.rule_id in self._rules:
                log.error(
                    "Registry construction failed: duplicate rule_id",
                    extra={"rule_id": r.rule_id},
                )
                raise RegistryError("Duplicate rule_id in registry", rule_id=r.rule_id)
            self._rules[r.rule_id] = r

        self._routing_matrix: dict[str, list[str]] = {
            domain: list(ids) for domain, ids in routing_matrix.items()
        }

        self._check_consistency()
        log.info(
            "Cognitive registry loaded",
            extra={"models": len(self._models), "rules": len(self._rules)},
        )

    @classmethod
    def load_default(cls) -> CognitiveRegistry:
        """Build the registry from the spec-transcribed default data.

        Parses ``MODELS_A + MODELS_B`` and ``RULES`` through the frozen
        Pydantic entry models and hands the spec routing matrix to the
        consistency-checked constructor.

        Returns:
            A fully validated :class:`CognitiveRegistry` with MM001–MM015,
            VR001–VR016 and the spec routing matrix.

        Raises:
            RegistryError: If the raw data is malformed or fails the
                consistency checks performed in :meth:`__init__`.

        Example:
            >>> reg = CognitiveRegistry.load_default()
            >>> len(reg)
            15
        """
        log.debug(
            "Parsing default registry data",
            extra={"raw_models": len(MODELS_A) + len(MODELS_B), "raw_rules": len(RULES)},
        )
        models = [MentalModelDefinition.model_validate(raw) for raw in (MODELS_A + MODELS_B)]
        rules = [ValidationRuleDefinition.model_validate(raw) for raw in RULES]
        return cls(models=models, rules=rules, routing_matrix=ROUTING_MATRIX)

    @classmethod
    def load_pack(cls, path: Path | str, *, overlay: bool = False) -> CognitiveRegistry:
        """Build the registry from an external JSON pack (G15).

        The pack is validated by :mod:`case_engine.registry.pack`
        (top-level shape, ``schema_version``, entry models) and by this
        constructor's consistency checker, so a pack can never
        introduce data the code-defined registry would not accept.
        With ``overlay=True`` the pack's entries are merged over the
        default data by id first — tune one model without forking the
        constitution.

        Args:
            path: Path to the pack JSON document (see
                :mod:`case_engine.registry.pack` for the contract).
            overlay: Merge over :meth:`load_default` instead of
                replacing it.

        Returns:
            A fully validated registry.

        Raises:
            RegistryPackError: Malformed or version-mismatched pack
                (logged at ERROR before raising).
            RegistryError: Consistency failures (unknown rule refs,
                unknown domains, dangling routing ids).

        Example:
            >>> reg = CognitiveRegistry.load_pack("packs/org_pack.json")  # doctest: +SKIP
        """
        from case_engine.registry.pack import _load_pack

        log.debug("loading registry pack", extra={"path": str(path), "overlay": overlay})
        return _load_pack(cls, path, overlay=overlay)

    def dump_pack(self, path: Path | str | None = None) -> dict[str, Any]:
        """Export this registry as a JSON pack document (G15).

        Convenience wrapper over
        :func:`case_engine.registry.pack.dump_registry_pack`; the
        result round-trips through :meth:`load_pack`.

        Args:
            path: When given, also write the document to this path
                (UTF-8, ``indent=2``; parent dirs are created).

        Returns:
            The pack document (``schema_version``, ``generated_by``,
            ``models``, ``rules``, ``routing_matrix``).
        """
        from case_engine.registry.pack import dump_registry_pack

        return dump_registry_pack(self, path)

    def _check_consistency(self) -> None:
        """Cross-check models, rules and routing matrix.

        Raises:
            RegistryError: If a model references an unknown rule, uses an
                unknown affinity domain, or the routing matrix references
                an unknown model id / domain.
        """
        for m in self._models.values():
            log.debug(
                "Consistency-checking model",
                extra={"algorithm_id": m.algorithm_id, "validation_rules": m.validation_rules},
            )
            for rid in m.validation_rules:
                if rid not in self._rules:
                    log.error(
                        "Registry inconsistent: model references unknown validation rule",
                        extra={"algorithm_id": m.algorithm_id, "rule_id": rid},
                    )
                    raise RegistryError(
                        "Model references unknown validation rule",
                        algorithm_id=m.algorithm_id, rule_id=rid,
                    )
            unknown_domains = set(m.domain_affinity) - self.KNOWN_DOMAINS
            if unknown_domains:
                log.error(
                    "Registry inconsistent: model has unknown affinity domain(s)",
                    extra={"algorithm_id": m.algorithm_id, "domains": sorted(unknown_domains)},
                )
                raise RegistryError(
                    "Model has unknown affinity domain(s)",
                    algorithm_id=m.algorithm_id, domains=sorted(unknown_domains),
                )
        for domain, ids in self._routing_matrix.items():
            if domain not in self.KNOWN_DOMAINS:
                log.error(
                    "Registry inconsistent: routing matrix has unknown domain",
                    extra={"domain": domain},
                )
                raise RegistryError("Routing matrix has unknown domain", domain=domain)
            for mid in ids:
                if mid not in self._models:
                    log.error(
                        "Registry inconsistent: routing matrix references unknown model",
                        extra={"domain": domain, "algorithm_id": mid},
                    )
                    raise RegistryError(
                        "Routing matrix references unknown model",
                        domain=domain, algorithm_id=mid,
                    )
        log.debug(
            "Registry consistency check passed",
            extra={"models": len(self._models), "rules": len(self._rules)},
        )

    # ------------------------------------------------------------------ #
    # Lookups
    # ------------------------------------------------------------------ #
    def get(self, algorithm_id: str) -> MentalModelDefinition:
        """Return the model definition for *algorithm_id*.

        Args:
            algorithm_id: e.g. ``"MM001"``.

        Returns:
            The corresponding definition.

        Raises:
            RegistryError: If the id is unknown — message is exactly
                ``"Unknown algorithm_id"`` with the offending id on
                ``RegistryError.context["algorithm_id"]`` (tests and the
                enforcement gate depend on this exact miss behavior).
        """
        log.debug("Registry lookup by algorithm_id", extra={"algorithm_id": algorithm_id})
        try:
            return self._models[algorithm_id]
        except KeyError:
            log.error(
                "Registry lookup miss: unknown algorithm_id",
                extra={"algorithm_id": algorithm_id},
            )
            raise RegistryError("Unknown algorithm_id", algorithm_id=algorithm_id) from None

    def get_by_tool_name(self, tool_name: str) -> MentalModelDefinition:
        """Return the model whose schema class (tool name) is *tool_name*.

        Linear scan over the (15) entries; the enforcement gate calls this
        on every tool call, which is fine at registry scale.

        Args:
            tool_name: e.g. ``"RootCauseAnalysisTrace"``.

        Returns:
            The corresponding definition.

        Raises:
            RegistryError: If no model declares that tool name — message
                is exactly ``"Unknown tool name"`` with the requested
                name on ``RegistryError.context["tool_name"]``.
        """
        log.debug("Registry lookup by tool name", extra={"tool_name": tool_name})
        for m in self._models.values():
            if m.tool_name == tool_name:
                log.debug(
                    "Registry tool-name lookup hit",
                    extra={"tool_name": tool_name, "algorithm_id": m.algorithm_id},
                )
                return m
        log.error(
            "Registry lookup miss: unknown tool name",
            extra={"tool_name": tool_name},
        )
        raise RegistryError("Unknown tool name", tool_name=tool_name)

    def get_rule(self, rule_id: str) -> ValidationRuleDefinition:
        """Return the rule definition for *rule_id*.

        Args:
            rule_id: e.g. ``"VR016"``.

        Returns:
            The corresponding rule definition.

        Raises:
            RegistryError: If the id is unknown — message is exactly
                ``"Unknown rule_id"`` with the offending id on
                ``RegistryError.context["rule_id"]``.
        """
        log.debug("Registry lookup by rule_id", extra={"rule_id": rule_id})
        try:
            return self._rules[rule_id]
        except KeyError:
            log.error(
                "Registry lookup miss: unknown rule_id",
                extra={"rule_id": rule_id},
            )
            raise RegistryError("Unknown rule_id", rule_id=rule_id) from None

    def all_models(self) -> list[MentalModelDefinition]:
        """Return all model definitions ordered by algorithm id.

        Returns:
            MM001–MM015 in ascending id order (tests assert contiguity).
        """
        models = sorted(self._models.values(), key=lambda m: m.algorithm_id)
        log.debug("All models requested", extra={"count": len(models)})
        return models

    def all_rules(self) -> list[ValidationRuleDefinition]:
        """Return all rule definitions ordered by rule id.

        Returns:
            VR001–VR016 in ascending id order.
        """
        rules = sorted(self._rules.values(), key=lambda r: r.rule_id)
        log.debug("All rules requested", extra={"count": len(rules)})
        return rules

    def routing_matrix(self) -> dict[str, list[str]]:
        """Return a defensive copy of the routing matrix.

        Returns:
            Mapping ``domain -> ranked algorithm_id list``; mutating the
            copy never affects the registry.
        """
        log.debug(
            "Routing matrix copy requested",
            extra={"domains": sorted(self._routing_matrix)},
        )
        return {d: list(ids) for d, ids in self._routing_matrix.items()}

    def __len__(self) -> int:
        """Return the number of registered mental models."""
        return len(self._models)
