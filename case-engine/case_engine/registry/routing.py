"""Cynefin-domain model router (pipeline node_2).

Purpose & role
--------------
The router is **deterministic code, not an LLM decision**: given the
domain classification it ranks registry models by their declared
``domain_affinity`` for that domain and returns the top-N.  The LLM has
no agency over which mental models execute — the routing matrix does.

Contents
--------
* :class:`ModelRouter` — rank-and-select plus an ``explain`` audit view.

Invariants
----------
* Ranking is a stable sort: equal affinity scores preserve the routing
  matrix order from ``case_engine/registry/rule_data.py`` (spec priority).
* ``top_n`` is clamped to at least 1; the result length is
  ``min(len(matrix[domain]), max(1, top_n))`` unless models are forced.
* Operator overrides (``forced_model_ids``) bypass affinity ranking but
  are still validated against the registry (unknown ids raise
  :class:`case_engine.exceptions.RoutingError`).

Cross-references
----------------
* Consumes: :class:`case_engine.registry.models.CognitiveRegistry`.
* Drives: node_3-4 (schema injection / generation); see
  ``docs/PIPELINE.md``.
"""

from __future__ import annotations

from case_engine.exceptions import RoutingError
from case_engine.logging_setup import get_logger
from case_engine.registry.models import CognitiveRegistry, MentalModelDefinition

__all__ = ["ModelRouter"]

log = get_logger(__name__)


class ModelRouter:
    """Rank-and-select mental models for a classified domain.

    Attributes:
        _registry: The consistency-checked registry providing the routing
            matrix and model definitions (private; set once at init).

    Example:
        >>> reg = CognitiveRegistry.load_default()
        >>> router = ModelRouter(reg)
        >>> [m.algorithm_id for m in router.select("Complex", top_n=3)]
        ['MM008', 'MM011', 'MM015']
    """

    def __init__(self, registry: CognitiveRegistry) -> None:
        """Bind the router to a registry.

        Args:
            registry: Consistency-checked cognitive registry.
        """
        log.debug(
            "ModelRouter initialized",
            extra={"registry_models": len(registry)},
        )
        self._registry = registry

    def select(
        self,
        domain: str,
        top_n: int = 3,
        forced_model_ids: list[str] | None = None,
    ) -> list[MentalModelDefinition]:
        """Select the mental models to execute for *domain*.

        Ranking: descending ``domain_affinity[domain]``, ties broken by the
        order the ids appear in the routing matrix (stable sort), which
        itself mirrors spec priority.

        Args:
            domain: Cynefin or specialist domain key, e.g. ``"Complex"``.
            top_n: Maximum number of models when routing automatically
                (spec: "select top 1-3 models").
            forced_model_ids: When provided (operator override), bypass
                affinity ranking and return exactly these models.

        Returns:
            Selected model definitions in execution order.

        Raises:
            RoutingError: If *domain* is not in the routing matrix, a
                forced id is unknown, or no model has affinity for the
                domain.  Miss messages are exactly ``"Domain not present
                in routing matrix"`` (with ``domain`` and ``available``
                context) and ``"Forced model selection failed"`` (with
                ``domain`` and ``reason`` context); tests depend on the
                exception type, not on these strings.
        """
        log.debug(
            "Routing selection started",
            extra={"domain": domain, "top_n": top_n, "forced": forced_model_ids},
        )
        matrix = self._registry.routing_matrix()
        if forced_model_ids:
            log.debug(
                "Operator override active: bypassing affinity ranking",
                extra={"domain": domain, "forced": forced_model_ids},
            )
            try:
                selected = [self._registry.get(mid) for mid in forced_model_ids]
            except Exception as exc:
                log.error(
                    "Forced model selection failed",
                    extra={"domain": domain, "forced": forced_model_ids, "reason": str(exc)},
                )
                raise RoutingError(
                    "Forced model selection failed", domain=domain, reason=str(exc)
                ) from exc
            log.info("Models force-selected by operator", extra={"models": forced_model_ids})
            return selected

        if domain not in matrix:
            log.error(
                "Routing failed: domain not present in routing matrix",
                extra={"domain": domain, "available": sorted(matrix)},
            )
            raise RoutingError(
                "Domain not present in routing matrix",
                domain=domain,
                available=sorted(matrix),
            )

        ranked = sorted(
            matrix[domain],
            key=lambda mid: (-self._registry.get(mid).domain_affinity.get(domain, 0.0),
                             matrix[domain].index(mid)),
        )
        log.debug(
            "Affinity ranking complete (stable order preserved on ties)",
            extra={
                "domain": domain,
                "ranking": ranked,
                "scores": {
                    mid: self._registry.get(mid).domain_affinity.get(domain, 0.0)
                    for mid in ranked
                },
            },
        )
        selected = [self._registry.get(mid) for mid in ranked[: max(1, top_n)]]
        log.info(
            "Models routed for domain",
            extra={
                "domain": domain,
                "selected": [m.algorithm_id for m in selected],
                "scores": {m.algorithm_id: m.domain_affinity.get(domain) for m in selected},
            },
        )
        return selected

    def explain(self, domain: str) -> dict[str, float]:
        """Return the affinity score of every routed model for *domain*.

        Useful for audit payloads and for debugging router decisions.

        Args:
            domain: Domain key present in the routing matrix.

        Returns:
            Mapping ``algorithm_id -> affinity`` (0.0 when undeclared).

        Raises:
            RoutingError: If *domain* is not in the routing matrix —
                message is exactly ``"Domain not present in routing
                matrix"`` with the offending ``domain`` on
                ``RoutingError.context``.

        Example:
            >>> reg = CognitiveRegistry.load_default()
            >>> ModelRouter(reg).explain("Chaotic")["MM001"]
            0.95
        """
        log.debug("Routing explain requested", extra={"domain": domain})
        matrix = self._registry.routing_matrix()
        if domain not in matrix:
            log.error(
                "Routing explain failed: domain not present in routing matrix",
                extra={"domain": domain, "available": sorted(matrix)},
            )
            raise RoutingError("Domain not present in routing matrix", domain=domain)
        scores = {
            mid: self._registry.get(mid).domain_affinity.get(domain, 0.0)
            for mid in matrix[domain]
        }
        log.debug(
            "Routing explain computed",
            extra={"domain": domain, "count": len(scores)},
        )
        return scores
