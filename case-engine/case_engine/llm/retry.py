"""Provider-resilience layer: exponential backoff with jitter (G8).

Transient provider failures — HTTP 429 rate limits, 5xx availability
errors, connection and timeout errors — previously surfaced immediately
as :class:`~case_engine.exceptions.LLMAdapterError` and burned the
pipeline's *reflexion* budget, which exists for content failures (a
rejected payload) rather than transport flakiness.  This module gives
the network adapters a transparent retry loop so a 500 ms backoff can
save a session that would otherwise escalate.

Design rules
------------
* **Transport-only**: only transport-classified failures are retried.
  A response that *parses* but lacks a tool call
  (:class:`~case_engine.exceptions.LLMResponseError`) is deterministic
  model behaviour and stays reflexion-routed — retrying it immediately
  would re-send the identical prompt.
* **Fail-normalizing**: after the final attempt the *original* SDK
  exception propagates to the adapter boundary, which wraps it into
  :class:`~case_engine.exceptions.LLMProviderUnavailableError` (a
  subclass of :class:`~case_engine.exceptions.LLMAdapterError`) exactly
  as before — callers see no new exception types.
* **Budget-bounded**: ``max_attempts`` bounds total tries (1 = retry
  disabled, the legacy behaviour).  Backoff is exponential
  (``initial * 2**attempt``) capped at ``max_backoff_s`` plus uniform
  jitter in ``[0, jitter_s]``.
* **Retry-After honoured**: when the provider supplies a numeric
  ``Retry-After`` header, it overrides the computed backoff (clamped to
  ``max_backoff_s``).
* **Observability**: every retry logs a WARNING with attempt number,
  classified status and sleep seconds — never request content.

Contents
--------
- :class:`RetryPolicy` — immutable retry parameters.
- :func:`classify_transport_failure` — map an SDK exception to a
  retryability verdict.
- :func:`retry_after_seconds` — extract a numeric ``Retry-After`` hint.
- :func:`call_with_retries` — the generic retry executor used by the
  OpenAI/Anthropic adapters.

Cross-references: ``docs/LLM_PROVIDERS.md`` (resilience section),
``case_engine/llm/openai_client.py`` / ``anthropic_client.py`` (call
sites), ``docs/GAP_ANALYSIS.md`` G8.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from case_engine.logging_setup import get_logger

__all__ = ["RetryPolicy", "classify_transport_failure", "retry_after_seconds",
           "call_with_retries", "acall_with_retries"]

log = get_logger(__name__)

#: HTTP status codes classified as transient (retry-worthy).  529 is
#: Anthropic's "overloaded" signal (their capacity analogue of 503).
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504, 529})

#: Substrings (case-insensitive) in an SDK exception's class name that
#: mark connection/timeout-style transport failures across providers.
_TRANSPORT_NAME_HINTS: tuple[str, ...] = (
    "timeout", "connection", "apiconnection", "rate limit", "ratelimit",
    "overloaded", "internal server",
)

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    """Immutable retry parameters for one adapter.

    Attributes:
        max_attempts: Total tries per request (1 disables retrying —
            legacy behaviour; the shipped adapters default to 3).
        initial_backoff_s: Sleep before the first retry.
        max_backoff_s: Ceiling for any single sleep (Retry-After hints
            are clamped to this too, so a hostile header cannot stall
            the pipeline).
        jitter_s: Uniform random jitter added to every sleep
            (thundering-herd dampening).
        retryable_status_codes: HTTP statuses classified as transient.

    Example:
        >>> p = RetryPolicy(max_attempts=3, initial_backoff_s=0.5)
        >>> p.max_attempts
        3
        >>> p.sleep_for(attempt=1) >= 0.5
        True
    """

    max_attempts: int = 3
    initial_backoff_s: float = 0.5
    max_backoff_s: float = 8.0
    jitter_s: float = 0.25
    retryable_status_codes: frozenset[int] = RETRYABLE_STATUS_CODES

    def __post_init__(self) -> None:
        """Validate parameter ranges (fail fast on nonsense policies).

        Raises:
            ValueError: When ``max_attempts < 1`` or any duration is
                negative.  Logged at ERROR first.
        """
        if self.max_attempts < 1:
            log.error("retry policy rejected: max_attempts < 1",
                      extra={"max_attempts": self.max_attempts})
            raise ValueError(f"max_attempts must be >= 1, got {self.max_attempts}")
        negatives = [n for n in ("initial_backoff_s", "max_backoff_s", "jitter_s")
                     if getattr(self, n) < 0]
        if negatives:
            log.error("retry policy rejected: negative duration",
                      extra={"fields": negatives})
            raise ValueError(f"retry durations must be non-negative: {negatives}")

    def sleep_for(self, attempt: int, retry_after: float | None = None) -> float:
        """Return the sleep seconds before retry *attempt*.

        Args:
            attempt: 1-based number of the attempt that just failed.
            retry_after: Optional provider ``Retry-After`` hint in
                seconds; when present it replaces the exponential curve
                (clamped to :attr:`max_backoff_s`).

        Returns:
            Sleep seconds in ``[0, max_backoff_s + jitter_s]``; ``0.0``
            when the hint and the curve both yield nothing (never
            negative).
        """
        if retry_after is not None and retry_after > 0:
            delay = min(retry_after, self.max_backoff_s)
        else:
            delay = min(self.initial_backoff_s * (2 ** max(0, attempt - 1)),
                        self.max_backoff_s)
        delay = min(delay + random.uniform(0, self.jitter_s),
                    self.max_backoff_s + self.jitter_s)
        log.debug("retry backoff computed",
                  extra={"attempt": attempt, "retry_after": retry_after,
                         "sleep_s": round(delay, 4)})
        return max(0.0, delay)


def classify_transport_failure(exc: BaseException,
                               retryable_status_codes: frozenset[int] = RETRYABLE_STATUS_CODES,
                               ) -> str | None:
    """Classify an SDK exception as retryable transport failure or not.

    The classifier is deliberately provider-agnostic and defensive: it
    inspects (in order) a ``status_code`` attribute (both the OpenAI and
    Anthropic SDKs expose it on API status errors), the exception class
    name for transport hints, and the ``args`` for embedded status
    codes.  Anything not recognized as transient returns ``None`` —
    unknown failures are *not* retried (fail-fast).

    Args:
        exc: The raised exception (never logged with content).
        retryable_status_codes: Status set to classify against.

    Returns:
        A short classification tag (``"status_429"``, ``"timeout"``,
        ``"connection"``) when retryable, else ``None``.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status in retryable_status_codes:
            return f"status_{status}"
        return None
    name = type(exc).__name__.lower()
    lowered = f"{name} {str(exc)[:200].lower()}"
    for hint in _TRANSPORT_NAME_HINTS:
        if hint in lowered:
            return hint.split()[0] if " " in hint else hint
    return None


def retry_after_seconds(exc: BaseException) -> float | None:
    """Extract a numeric ``Retry-After`` hint from an SDK exception.

    Looks for ``retry_after``/``Retry-After`` attributes and for a
    ``response.headers`` mapping.  Non-numeric hints (HTTP-date form)
    are ignored rather than guessed at.

    Args:
        exc: The raised exception.

    Returns:
        The hint in seconds, or ``None`` when absent/non-numeric.
    """
    for attr in ("retry_after", "retry_after_s"):
        value = getattr(exc, attr, None)
        if isinstance(value, (int, float)) and value >= 0:
            return float(value)
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    raw = None
    if hasattr(headers, "get"):
        # httpx-style headers are case-insensitive; plain dicts are not,
        # so probe both spellings.
        raw = headers.get("retry-after")
        if raw is None:
            raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        log.debug("non-numeric Retry-After ignored", extra={"raw_type": type(raw).__name__})
        return None


def call_with_retries(
    fn: Callable[[], T],
    *,
    policy: RetryPolicy,
    provider: str,
    what: str,
) -> T:
    """Execute *fn* with the policy's retry loop (synchronous).

    Calls ``fn()`` up to ``policy.max_attempts`` times.  A retryable
    transport failure sleeps per :meth:`RetryPolicy.sleep_for` (with a
    WARNING log naming attempt, classification and sleep) and tries
    again; any other exception, or exhaustion of the budget, re-raises
    the *original* exception so the adapter boundary can wrap it as
    usual.

    Args:
        fn: Zero-argument callable performing one transport attempt.
        policy: Retry parameters.
        provider: Provider key for log fields (never the request body).
        what: Short operation tag for log fields (e.g. ``"chat.completions"``).

    Returns:
        Whatever ``fn()`` returned on the successful attempt.

    Raises:
        BaseException: Whatever ``fn()`` raised — either a
            non-retryable failure (immediately) or the last failure
            after the attempt budget is exhausted.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return fn()
        except Exception as exc:
            classification = classify_transport_failure(
                exc, policy.retryable_status_codes)
            if classification is None or attempt >= policy.max_attempts:
                if classification is not None:
                    log.warning(
                        "transport failure - retry budget exhausted",
                        extra={"provider": provider, "op": what,
                               "attempt": attempt, "classification": classification},
                    )
                raise
            last_exc = exc
            sleep_s = policy.sleep_for(attempt, retry_after_seconds(exc))
            log.warning(
                "transient transport failure - retrying",
                extra={"provider": provider, "op": what, "attempt": attempt,
                       "max_attempts": policy.max_attempts,
                       "classification": classification,
                       "sleep_s": round(sleep_s, 4)},
            )
            time.sleep(sleep_s)
    # Unreachable in practice: every iteration either returns on success or
    # stores the transient failure before sleeping; kept as a safety net.
    assert last_exc is not None, "retry loop exits only via return or raise"
    raise last_exc  # pragma: no cover - loop always returns or raises


async def acall_with_retries(
    fn: Callable[[], Any],
    *,
    policy: RetryPolicy,
    provider: str,
    what: str,
) -> Any:
    """Async twin of :func:`call_with_retries` (awaits sleep, not threads).

    ``fn`` may be sync or async; both are executed on the event loop.
    Semantics, logging and error propagation are identical to the
    synchronous executor.

    Args:
        fn: Zero-argument callable (sync or coroutine-returning).
        policy: Retry parameters.
        provider: Provider key for log fields.
        what: Short operation tag for log fields.

    Returns:
        Whatever ``fn()`` produced on the successful attempt.

    Raises:
        BaseException: Identical propagation rules to
            :func:`call_with_retries`.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            result = fn()
            if asyncio.iscoroutine(result):
                result = await result
            return result
        except Exception as exc:
            classification = classify_transport_failure(
                exc, policy.retryable_status_codes)
            if classification is None or attempt >= policy.max_attempts:
                if classification is not None:
                    log.warning(
                        "transport failure - retry budget exhausted",
                        extra={"provider": provider, "op": what,
                               "attempt": attempt, "classification": classification},
                    )
                raise
            last_exc = exc
            sleep_s = policy.sleep_for(attempt, retry_after_seconds(exc))
            log.warning(
                "transient transport failure - retrying (async)",
                extra={"provider": provider, "op": what, "attempt": attempt,
                       "max_attempts": policy.max_attempts,
                       "classification": classification,
                       "sleep_s": round(sleep_s, 4)},
            )
            await asyncio.sleep(sleep_s)
    # Unreachable in practice: every iteration either returns on success or
    # stores the transient failure before sleeping; kept as a safety net.
    assert last_exc is not None, "retry loop exits only via return or raise"
    raise last_exc  # pragma: no cover - loop always returns or raises
