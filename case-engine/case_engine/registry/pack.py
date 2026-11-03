"""External JSON registry packs (G15, v2.3.0).

Purpose & role
--------------
The default constitution data is hardcoded Python
(``registry/model_data_a.py`` / ``model_data_b.py`` / ``rule_data.py``).
That is perfect for the shipped MM001–MM015 / VR001–VR016 set, but
organizations cannot version, review or diff *their own* model packs
without forking Python.  This module defines a **JSON pack contract**
for the registry data and the load/dump machinery around it, reusing
the exact same frozen Pydantic entry models and the exact same
consistency checker as the default loader — a pack can never introduce
data the engine would not have accepted from code.

Pack contract
-------------
A pack is one UTF-8 JSON object::

    {
      "schema_version": "2.0.0",       // registry JSON contract version
      "generated_by": "case-engine 2.3.0",   // optional provenance
      "models":  [ { ...MentalModelDefinition... }, ... ],
      "rules":   [ { ...ValidationRuleDefinition... }, ... ],
      "routing_matrix": { "Clear": ["MM001", ...], ... }
    }

Contract rules:

* ``schema_version`` must equal :data:`~case_engine.version.SCHEMA_VERSION`
  (packs are contract documents, not loose config — a mismatch is an
  error, not a warning, because rule/model ids are stable identifiers).
* Entries are validated with :class:`MentalModelDefinition` /
  :class:`ValidationRuleDefinition` (both ``extra="forbid"``, so typos
  fail fast) and the whole pack passes
  :meth:`CognitiveRegistry._check_consistency`.
* Two load modes: **complete** (the pack *is* the registry) and
  **overlay** (entries are merged over the default pack by id before
  the consistency check — overlay a single tuned model onto the
  standard constitution).

Invariants
----------
* Loading is fail-closed: any problem raises
  :class:`~case_engine.exceptions.RegistryPackError` (a
  :class:`RegistryError` subclass) after an ERROR log; nothing is ever
  partially merged.
* Dumping is pure projection: ``dump_pack`` output loaded back with
  ``load_pack`` yields an equivalent registry (round-trip property,
  regression-tested).

Cross-references
----------------
``case_engine/registry/models.py`` (entry models + consistency checker),
``case_engine/exceptions.py`` (:class:`RegistryPackError`),
``docs/EXTENDING.md`` (authoring guide), ``examples/registry_pack_demo.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from case_engine.exceptions import RegistryPackError
from case_engine.logging_setup import get_logger
from case_engine.registry.models import (
    CognitiveRegistry,
    MentalModelDefinition,
    ValidationRuleDefinition,
)
from case_engine.version import SCHEMA_VERSION

__all__ = [
    "PACK_TOP_LEVEL_KEYS",
    "dump_registry_pack",
    "load_registry_pack",
]

log = get_logger(__name__)

#: Keys allowed at the top level of a pack document (``extra=forbid``
#: semantics for the pack itself, mirroring the entry models).
PACK_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {"schema_version", "generated_by", "models", "rules", "routing_matrix"}
)


def dump_registry_pack(
    registry: CognitiveRegistry,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Project a registry into the JSON pack document shape.

    Pure projection: model/rule entries are emitted exactly as
    ``model_dump()`` renders them and the routing matrix is copied, so
    the result re-loads to an equivalent registry (round-trip tested).

    Args:
        registry: The registry to export (typically
            ``CognitiveRegistry.load_default()``).
        path: When given, also write the document to this path
            (UTF-8, ``indent=2``); parent directories are created.

    Returns:
        The pack document (also written to *path* when provided).

    Raises:
        OSError: When *path* is given and the file cannot be written
            (propagated unchanged; logged at ERROR first).
    """
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_by": "case-engine dump_registry_pack",
        "models": [m.model_dump() for m in registry.all_models()],
        "rules": [r.model_dump() for r in registry.all_rules()],
        "routing_matrix": registry.routing_matrix(),
    }
    log.debug(
        "registry pack projected",
        extra={"models": len(document["models"]),
               "rules": len(document["rules"]),
               "path": str(path) if path else None},
    )
    if path is not None:
        p = Path(path)
        log.info("writing registry pack", extra={"path": str(p)})
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps(document, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError:
            log.error("cannot write registry pack",
                      extra={"path": str(p)}, exc_info=True)
            raise
    return document


def load_registry_pack(
    path: str | Path,
) -> tuple[list[MentalModelDefinition], list[ValidationRuleDefinition], dict[str, list[str]]]:
    """Read and validate a JSON registry pack (fail-closed).

    Sequence: read file → parse JSON → check top-level keys → check
    ``schema_version`` → validate every entry through the frozen
    Pydantic models.  The consistency check itself runs in
    :class:`CognitiveRegistry`'s constructor, after any overlay merge.

    Args:
        path: Path to the pack JSON document.

    Returns:
        ``(models, rules, routing_matrix)`` — validated entries ready
        for the :class:`CognitiveRegistry` constructor.

    Raises:
        RegistryPackError: On any failure — missing/unreadable file,
            invalid JSON, unknown top-level keys, wrong
            ``schema_version``, or an entry that fails its Pydantic
            model.  Logged at ERROR (with traceback) before raising;
            the pack path travels in ``RegistryPackError.context``.
    """
    p = Path(path)
    log.info("loading registry pack", extra={"path": str(p)})

    # 1. Read + parse -------------------------------------------------------
    try:
        raw_text = p.read_text(encoding="utf-8")
    except OSError as exc:
        log.error("registry pack unreadable", extra={"path": str(p)}, exc_info=True)
        raise RegistryPackError(f"Cannot read registry pack: {p}", pack_path=str(p)) from exc
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        log.error("registry pack is not valid JSON",
                  extra={"path": str(p)}, exc_info=True)
        raise RegistryPackError(
            f"Registry pack is not valid JSON: {p} ({exc})", pack_path=str(p),
        ) from exc
    if not isinstance(raw, dict):
        log.error("registry pack root must be a JSON object",
                  extra={"path": str(p), "root": type(raw).__name__})
        raise RegistryPackError(
            f"Registry pack root must be a JSON object: {p}", pack_path=str(p),
        )

    # 2. Top-level shape ----------------------------------------------------
    unknown = set(raw) - PACK_TOP_LEVEL_KEYS
    if unknown:
        log.error("registry pack has unknown top-level keys",
                  extra={"path": str(p), "keys": sorted(unknown)})
        raise RegistryPackError(
            f"Registry pack has unknown top-level keys {sorted(unknown)}: {p}",
            pack_path=str(p), keys=sorted(unknown),
        )
    declared = raw.get("schema_version")
    if declared != SCHEMA_VERSION:
        log.error(
            "registry pack schema_version mismatch",
            extra={"path": str(p), "declared": declared,
                   "expected": SCHEMA_VERSION},
        )
        raise RegistryPackError(
            f"Registry pack schema_version {declared!r} != engine contract "
            f"{SCHEMA_VERSION!r}: {p}",
            pack_path=str(p), declared=declared, expected=SCHEMA_VERSION,
        )
    for required in ("models", "rules", "routing_matrix"):
        if required not in raw:
            log.error("registry pack missing required section",
                      extra={"path": str(p), "section": required})
            raise RegistryPackError(
                f"Registry pack is missing the {required!r} section: {p}",
                pack_path=str(p), section=required,
            )

    # 3. Entry validation (frozen entry models; extra=forbid) ---------------
    try:
        models = [MentalModelDefinition.model_validate(m) for m in raw["models"]]
        rules = [ValidationRuleDefinition.model_validate(r) for r in raw["rules"]]
    except ValidationError as exc:
        log.error("registry pack entry failed validation",
                  extra={"path": str(p)}, exc_info=True)
        raise RegistryPackError(
            f"Registry pack entry failed validation: {p} ({exc.error_count()} "
            f"error(s), first: {exc.errors()[0]['msg']})",
            pack_path=str(p),
        ) from exc

    routing_matrix = raw["routing_matrix"]
    if not isinstance(routing_matrix, dict):
        log.error("registry pack routing_matrix must be an object",
                  extra={"path": str(p)})
        raise RegistryPackError(
            f"Registry pack routing_matrix must be an object: {p}",
            pack_path=str(p),
        )
    log.debug(
        "registry pack validated",
        extra={"path": str(p), "models": len(models), "rules": len(rules)},
    )
    return models, rules, routing_matrix


def _load_pack(
    cls: type[CognitiveRegistry],
    path: str | Path,
    *,
    overlay: bool = False,
) -> CognitiveRegistry:
    """Implementation of :meth:`CognitiveRegistry.load_pack`.

    Kept here so the merge logic is testable next to the pack contract;
    :meth:`CognitiveRegistry.load_pack` (models.py) lazily imports and
    delegates to avoid an import cycle (pack.py imports models.py).

    Args:
        cls: The :class:`CognitiveRegistry` class.
        path: Pack JSON path.
        overlay: Merge the pack over the default data by id first.

    Returns:
        A fully validated registry.

    Raises:
        RegistryPackError: Propagated from :func:`load_registry_pack`.
        RegistryError: From the consistency checker.
    """
    models, rules, routing_matrix = load_registry_pack(path)
    if overlay:
        default = cls.load_default()
        by_id = {m.algorithm_id: m for m in default.all_models()}
        merged = 0
        for m in models:
            if m.algorithm_id in by_id:
                merged += 1
            by_id[m.algorithm_id] = m
        rule_by_id = {r.rule_id: r for r in default.all_rules()}
        for r in rules:
            rule_by_id[r.rule_id] = r
        log.debug(
            "pack overlay merged",
            extra={"path": str(path), "pack_models": len(models),
                   "overridden_models": merged},
        )
        return cls(models=list(by_id.values()), rules=list(rule_by_id.values()),
                   routing_matrix=routing_matrix)
    return cls(models=models, rules=rules, routing_matrix=routing_matrix)
