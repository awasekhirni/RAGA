"""Cognitive registry subpackage: model/rule definitions + Cynefin router.

Purpose & role
--------------
Single source of truth for *what the engine may do*: which mental models
exist (MM001–MM015), which validation rules gate them (VR001–VR016) and
which models the deterministic node_2 router picks per Cynefin or
specialist domain.

Contents
--------
* :class:`CognitiveRegistry` — immutable, consistency-checked registry
  view with O(1) id lookups and tool-name resolution.
* :class:`MentalModelDefinition` /
  :class:`ValidationRuleDefinition` — frozen Pydantic entry contracts.
* :class:`ModelRouter` — deterministic affinity rank-and-select.
* :func:`load_registry_pack` / :func:`dump_registry_pack` — external
  JSON pack contract (G15); also exposed as
  ``CognitiveRegistry.load_pack`` / ``.dump_pack``.

Invariants
----------
* Model/rule ids are stable identifiers used in audit records — never
  rename; registry construction fails fast (RegistryError) on duplicate
  ids or dangling references.
* Raw data lives in the sibling data modules (``model_data_a.py``,
  ``model_data_b.py``, ``rule_data.py``) and is validated at load time.

Cross-references
----------------
* Consumers: ``case_engine.enforcement`` (gate + tool factory),
  ``case_engine.validation`` (rule executors), ``case_engine.pipeline``
  (node_2 routing); see ``docs/MENTAL_MODELS.md``,
  ``docs/VALIDATION_RULES.md``, ``docs/PIPELINE.md``.
"""

from __future__ import annotations

from case_engine.registry.models import (
    CognitiveRegistry,
    MentalModelDefinition,
    ValidationRuleDefinition,
)
from case_engine.registry.pack import (
    PACK_TOP_LEVEL_KEYS,
    dump_registry_pack,
    load_registry_pack,
)
from case_engine.registry.routing import ModelRouter

__all__ = [
    "CognitiveRegistry",
    "MentalModelDefinition",
    "ValidationRuleDefinition",
    "ModelRouter",
    "PACK_TOP_LEVEL_KEYS",
    "dump_registry_pack",
    "load_registry_pack",
]
