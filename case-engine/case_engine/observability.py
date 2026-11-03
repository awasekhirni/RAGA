"""Observability hooks: counters, timers and Prometheus text export (G11).

Structured logging (see ``docs/CONVENTIONS.md``) answers "what happened
in session X?"; metrics answer "how is the engine behaving across
sessions?" — reflexion rate, gate rejections, escalation rate, LLM
latency.  This module provides a tiny, dependency-free metrics surface
that is wired into :class:`~case_engine.pipeline.context.EngineContext`
and instrumented at the pipeline's decision points.

Design rules
------------
* **Zero hard dependencies** — the default sink is :class:`NoopMetrics`
  (no-op, allocation-free); :class:`InMemoryMetrics` is enough for tests
  and single-process dashboards.  OpenTelemetry / StatsD shippers can be
  added by implementing the :class:`Metrics` protocol — no engine code
  changes (see ``docs/EXTENDING.md``).
* **Labels are bounded** — metric names and label *keys* are engine
  constants; label *values* come from fixed vocabularies (node ids,
  model ids, event types, booleans).  Free-text (queries, payloads,
  errors) is never a label value — that is the log-safety rule applied
  to metrics.
* **Fail-open** — a misbehaving sink must never break a reasoning
  session.  :func:`safe_incr` / :func:`safe_observe` wrap sink calls in
  a try/except that demotes sink failures to one WARNING each; the
  instrumentation call-sites use these helpers exclusively.
* **Prometheus text format** — :func:`render_prometheus` renders an
  :class:`InMemoryMetrics` snapshot in the official text exposition
  format (counters + summary), scrape-ready without the
  ``prometheus_client`` package.

Contents
--------
- :class:`Metrics` — the structural protocol every sink implements.
- :class:`NoopMetrics` — default sink; compiles to nothing.
- :class:`InMemoryMetrics` — dict-backed sink for tests/dashboards.
- :func:`safe_incr` / :func:`safe_observe` — fail-open call wrappers.
- :func:`render_prometheus` — text-exposition rendering.

Cross-references: ``docs/GAP_ANALYSIS.md`` G11 (origin),
``case_engine/pipeline/context.py`` (the wiring point),
``case_engine/pipeline/nodes.py`` (instrumentation call-sites).
"""

from __future__ import annotations

import threading
from typing import Any, Protocol, runtime_checkable

from case_engine.logging_setup import get_logger

__all__ = ["Metrics", "NoopMetrics", "InMemoryMetrics",
           "safe_incr", "safe_observe", "render_prometheus"]

log = get_logger(__name__)


@runtime_checkable
class Metrics(Protocol):
    """Structural contract for a metrics sink (G11).

    Implementations must be cheap and exception-safe at the call site;
    engine code wraps calls in :func:`safe_incr` / :func:`safe_observe`
    anyway, so a raising sink degrades to log noise rather than a failed
    session.

    Counter naming convention: ``case_engine_<subject>_total`` for
    monotonically increasing counts (Prometheus convention) and
    ``case_engine_<subject>`` (no suffix) for observations (timings).
    """

    def incr(self, name: str, value: int = 1, **labels: Any) -> None:
        """Add *value* to counter *name*.

        Args:
            name: Counter name (engine vocabulary, e.g.
                ``"case_engine_reflexion_retries_total"``).
            value: Positive increment (defaults to 1).
            **labels: Bounded-vocabulary labels (``node=``, ``model=``,
                ``event_type=``, ``outcome=``).
        """
        ...  # pragma: no cover - protocol

    def observe(self, name: str, value: float, **labels: Any) -> None:
        """Record one observation (e.g. latency milliseconds) for *name*.

        Args:
            name: Series name (e.g. ``"case_engine_llm_latency_ms"``).
            value: Observed value.
            **labels: Bounded-vocabulary labels.
        """
        ...  # pragma: no cover - protocol

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe copy of the accumulated series.

        Returns:
            ``{"counters": {...}, "observations": {...}}`` — exact shape
            is sink-defined; :class:`InMemoryMetrics` documents its own.
        """
        ...  # pragma: no cover - protocol


class NoopMetrics:
    """Default metrics sink — every operation is a no-op.

    Selected automatically when the operator injects nothing, so the
    engine has zero observability overhead out of the box while all
    instrumentation call-sites stay live.

    Example:
        >>> m = NoopMetrics()
        >>> m.incr("case_engine_sessions_total")
        >>> m.observe("case_engine_llm_latency_ms", 12.5, node="node_1")
        >>> m.snapshot()
        {}
    """

    def incr(self, name: str, value: int = 1, **labels: Any) -> None:
        """Drop the increment (no-op; never raises).

        Args:
            name: Ignored.
            value: Ignored.
            **labels: Ignored.
        """

    def observe(self, name: str, value: float, **labels: Any) -> None:
        """Drop the observation (no-op; never raises).

        Args:
            name: Ignored.
            value: Ignored.
            **labels: Ignored.
        """

    def snapshot(self) -> dict[str, Any]:
        """Return the empty snapshot.

        Returns:
            ``{}`` — nothing is ever recorded.
        """
        return {}


class InMemoryMetrics:
    """Dict-backed metrics sink for tests, demos and single-process runs.

    Thread-safe (a lock serializes mutations); snapshots are deep
    copies so callers cannot corrupt live series.  Counters are stored
    per ``(name, sorted-label-tuple)``; observations accumulate into a
    list per series with ``count``/``sum``/``min``/``max`` projections
    kept for cheap rendering.

    Example:
        >>> m = InMemoryMetrics()
        >>> m.incr("case_engine_sessions_total", outcome="ok")
        >>> m.incr("case_engine_sessions_total", outcome="ok")
        >>> m.snapshot()["counters"]["case_engine_sessions_total|outcome=ok"]
        2
    """

    def __init__(self) -> None:
        """Initialise empty series (no arguments; never raises)."""
        self._lock = threading.Lock()
        self._counters: dict[str, float] = {}
        self._observations: dict[str, list[float]] = {}

    @staticmethod
    def _key(name: str, labels: dict[str, Any]) -> str:
        """Build the stable ``name|k=v,k=v`` series key.

        Args:
            name: Metric name.
            labels: Label dict (values stringified; keys sorted).

        Returns:
            Deterministic series key.
        """
        if not labels:
            return name
        joined = ",".join(f"{k}={labels[k]}" for k in sorted(labels))
        return f"{name}|{joined}"

    def incr(self, name: str, value: int = 1, **labels: Any) -> None:
        """Add *value* to the labelled counter.

        Args:
            name: Counter name.
            value: Increment (negative values are clamped to 0 floor).
            **labels: Bounded-vocabulary labels.
        """
        key = self._key(name, labels)
        with self._lock:
            current = self._counters.get(key, 0)
            updated = current + value
            # Keep integer counters integer (doctest/JSON friendliness);
            # clamp negatives at zero.
            self._counters[key] = max(0, updated) if isinstance(updated, int) \
                else max(0.0, updated)
        log.debug("metric incremented", extra={"metric": key, "value": value})

    def observe(self, name: str, value: float, **labels: Any) -> None:
        """Record one observation for the labelled series.

        Args:
            name: Series name.
            value: Observed value.
            **labels: Bounded-vocabulary labels.
        """
        key = self._key(name, labels)
        with self._lock:
            self._observations.setdefault(key, []).append(float(value))
        log.debug("metric observed", extra={"metric": key, "value": value})

    def snapshot(self) -> dict[str, Any]:
        """Return a deep copy of all series.

        Returns:
            ``{"counters": {key: number}, "observations": {key: [values]}}``.
        """
        with self._lock:
            return {
                "counters": dict(self._counters),
                "observations": {k: list(v) for k, v in self._observations.items()},
            }


def safe_incr(metrics: Any, name: str, value: int = 1, **labels: Any) -> None:
    """Fail-open counter increment (never raises into the pipeline).

    Args:
        metrics: Any sink (or ``None`` — a no-op).
        name: Counter name.
        value: Increment.
        **labels: Bounded-vocabulary labels.
    """
    if metrics is None:
        return
    try:
        metrics.incr(name, value, **labels)
    except Exception:
        log.warning("metrics sink raised on incr", extra={"metric": name},
                    exc_info=True)


def safe_observe(metrics: Any, name: str, value: float, **labels: Any) -> None:
    """Fail-open observation (never raises into the pipeline).

    Args:
        metrics: Any sink (or ``None`` — a no-op).
        name: Series name.
        value: Observed value.
        **labels: Bounded-vocabulary labels.
    """
    if metrics is None:
        return
    try:
        metrics.observe(name, value, **labels)
    except Exception:
        log.warning("metrics sink raised on observe", extra={"metric": name},
                    exc_info=True)


def _series_from_key(key: str) -> tuple[str, dict[str, str]]:
    """Split an :class:`InMemoryMetrics` series key back into name+labels.

    Args:
        key: ``"name|k=v,k=v"`` (or bare ``"name"``).

    Returns:
        ``(name, labels)`` with stringified label values.
    """
    if "|" not in key:
        return key, {}
    name, _, label_part = key.partition("|")
    labels: dict[str, str] = {}
    for pair in label_part.split(","):
        k, _, v = pair.partition("=")
        labels[k] = v
    return name, labels


def _escape_label(value: str) -> str:
    """Escape a label value for the Prometheus text format.

    Args:
        value: Raw label value.

    Returns:
        Value with backslash, double-quote and newline escaped.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_prometheus(metrics: InMemoryMetrics) -> str:
    """Render an :class:`InMemoryMetrics` snapshot as Prometheus text.

    Counters become ``# TYPE <name> counter`` samples; observation
    series become ``# TYPE <name> summary`` blocks with
    ``_count``/``_sum`` samples (quantiles are omitted — the engine
    never promises pre-computed quantiles).  Metric names are
    sanitized: characters outside ``[a-zA-Z0-9_:]`` become ``_``.

    Args:
        metrics: The sink to render.

    Returns:
        Text-exposition document ending in a newline (possibly empty).

    Example:
        >>> m = InMemoryMetrics()
        >>> m.incr("case_engine_sessions_total", outcome="ok")
        >>> "case_engine_sessions_total" in render_prometheus(m)
        True
    """
    snap = metrics.snapshot()
    lines: list[str] = []
    import re

    def sanitize(name: str) -> str:
        """Restrict a metric name to the Prometheus legal charset.

        Args:
            name: Raw metric name.

        Returns:
            Name with every character outside ``[a-zA-Z0-9_:]`` replaced
            by ``_``.
        """
        return re.sub(r"[^a-zA-Z0-9_:]", "_", name)

    seen_types: set[str] = set()
    for key, value in sorted(snap["counters"].items()):
        name, labels = _series_from_key(key)
        base = sanitize(name)
        if base not in seen_types:
            lines.append(f"# TYPE {base} counter")
            seen_types.add(base)
        label_str = ""
        if labels:
            inner = ",".join(f'{k}="{_escape_label(v)}"' for k, v in sorted(labels.items()))
            label_str = "{" + inner + "}"
        rendered_value = int(value) if float(value).is_integer() else value
        lines.append(f"{base}{label_str} {rendered_value}")
    for key, values in sorted(snap["observations"].items()):
        name, labels = _series_from_key(key)
        base = sanitize(name)
        if base not in seen_types:
            lines.append(f"# TYPE {base} summary")
            seen_types.add(base)
        label_str = ""
        extra_labels = ""
        if labels:
            inner = ",".join(f'{k}="{_escape_label(v)}"' for k, v in sorted(labels.items()))
            label_str = "{" + inner + "}"
            extra_labels = "," + inner
        lines.append(f"{base}_count{label_str} {len(values)}")
        lines.append(f"{base}_sum{label_str} {round(sum(values), 6)}")
        if len(values) > 1:
            lines.append(f'{base}{{quantile="0.5"{extra_labels}}} '
                         f'{round(sorted(values)[len(values) // 2], 6)}')
    text = "\n".join(lines)
    if text:
        text += "\n"
    log.debug("prometheus text rendered",
              extra={"series": len(snap["counters"]) + len(snap["observations"]),
                     "chars": len(text)})
    return text
