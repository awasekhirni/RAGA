"""Privacy primitives: PII detection (G1) and deterministic redaction (G4).

Purpose & role
--------------
This module implements the two data-protection halves of the P0 gap
closure.  **Detection** feeds the TEE enforcement path: node_1 runs a
:class:`PIIDetector` over the user query (and, when present, the operator
context) and the pipeline decides — per ``guardrails.pii_policy`` —
whether a PII-bearing session without TEE attestation is escalated or
allowed with flags (see ``case_engine.pipeline.nodes``).  **Redaction**
feeds the audit-at-rest protection: :class:`PIIRedactor` performs
deterministic tokenization (``john@x.com`` → ``[EMAIL_1]``) on audit
payloads at :meth:`EngineContext.audit` time, so the hash-chained ledger
never stores raw PII while remaining a faithful evidence chain.

Contents
--------
- :class:`PIICategory` — the PII kinds the default detector recognises.
- :class:`PIIFinding` — detection result (categories + truncated evidence).
- :class:`PIIDetector` — structural protocol for detectors.
- :class:`RegexPIIDetector` — default, fully-offline regex detector
  (email, phone, SSN, credit card with Luhn check, IPv4 address).
- :class:`NoopPIIDetector` — always-negative detector (explicit opt-out).
- :class:`PIIRedactor` — deterministic, session-scoped tokenizing
  redactor used for audit payloads.
- :class:`NoopRedactor` — identity redactor (default; behavior-neutral).

Invariants
----------
* **Never log raw PII.** Findings carry *categories* and truncated
  evidence previews only; the redactor logs counts and categories, never
  matched values.
* **Determinism**: the same input always yields the same token
  (``EMAIL_1`` stays ``EMAIL_1`` within one redactor instance), so
  redacted evidence remains joinable across events of one session.
* **The token map never leaves memory.** The ``original → token`` map is
  held on the redactor instance only; it is never persisted, logged, or
  included in any payload.
* **Fail-permissive**: detection failures degrade to "no PII found" with
  an ERROR log — a broken detector must not crash the pipeline, but it
  *is* loudly visible (deployers who need hard failure can wrap the
  protocol themselves).

Cross-references: ``docs/GAP_ANALYSIS.md`` (G1, G4),
``case_engine/pipeline/context.py`` (redaction hook),
``case_engine/pipeline/nodes.py`` (node_1 detection gate),
``case_engine/audit/crypto.py`` (complementary encryption at rest).
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from case_engine.logging_setup import get_logger

__all__ = [
    "PIICategory",
    "PIIFinding",
    "PIIDetector",
    "RegexPIIDetector",
    "NoopPIIDetector",
    "PIIRedactor",
    "NoopRedactor",
]

log = get_logger(__name__)


class PIICategory(str, Enum):
    """PII kinds recognised by the shipped regex detector.

    Values are stable audit-contract strings (they appear in
    ``TEE_DECISION`` events and redaction logs) — never rename them.

    Example:
        >>> PIICategory.EMAIL.value
        'email'
    """

    EMAIL = "email"
    PHONE = "phone"
    SSN = "ssn"
    CREDIT_CARD = "credit_card"
    IP_ADDRESS = "ip_address"


class PIIFinding(BaseModel):
    """Result of scanning one text for PII.

    Attributes:
        detected: ``True`` when at least one category matched.
        categories: Distinct matched categories (stable value strings).
        evidence: Truncated previews (first 40 chars, category-tagged).
            Previews intentionally keep only enough shape for operators
            to sanity-check a detection — never the full value.

    Example:
        >>> PIIFinding(detected=False, categories=[], evidence=[])
        PIIFinding(detected=False, categories=[], evidence=[])
    """

    model_config = ConfigDict(extra="forbid")

    detected: bool = Field(description="True when any PII category matched")
    categories: list[str] = Field(
        default_factory=list,
        description="Distinct matched PIICategory value strings",
    )
    evidence: list[str] = Field(
        default_factory=list,
        description="Truncated, category-tagged evidence previews (not raw values)",
    )


@runtime_checkable
class PIIDetector(Protocol):
    """Structural protocol any PII detector must satisfy.

    Implementations may use regexes, NER models or call out to a DLP
    service; the pipeline only depends on :meth:`detect`.
    """

    def detect(self, text: str) -> PIIFinding:
        """Scan *text* and report PII categories found.

        Args:
            text: Free text to scan (never ``None``).

        Returns:
            A :class:`PIIFinding` (``detected=False`` on error-free absence).
        """
        ...  # pragma: no cover


class RegexPIIDetector:
    """Fully-offline, conservative regex detector for common PII kinds.

    Deliberately **conservative**: patterns require separators/structure
    (``name@host.tld``, ``123-45-6789``, Luhn-valid card numbers, dotted
    IPv4, phone numbers with 10+ digits) so ordinary technical prose —
    sensor counts, version strings, model names — does not trigger false
    positives.  Deployers needing broader coverage implement the
    :class:`PIIDetector` protocol (NER, DLP service) and inject it.

    Attributes:
        patterns: Mapping of category → compiled regex (read-only use).

    Example:
        >>> det = RegexPIIDetector()
        >>> det.detect("contact jane@corp.com today").detected
        True
        >>> det.detect("Zone 4 sensors spike anomalously").detected
        False
    """

    #: Conservative patterns; each compiled once at construction.
    patterns: dict[PIICategory, re.Pattern[str]] = {
        PIICategory.EMAIL: re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
        PIICategory.SSN: re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        PIICategory.CREDIT_CARD: re.compile(r"\b(?:\d{4}[ -]){3}\d{4}\b"),
        PIICategory.IP_ADDRESS: re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
        PIICategory.PHONE: re.compile(r"\+?\d{1,3}[-. ]\(?\d{2,4}\)?[-. ]\d{3,4}[-. ]\d{3,4}\b"),
    }

    def __init__(self) -> None:
        """Compile the pattern set (no I/O; safe to build per engine)."""
        log.debug(
            "RegexPIIDetector ready",
            extra={"categories": sorted(c.value for c in self.patterns)},
        )

    @staticmethod
    def _luhn_ok(candidate: str) -> bool:
        """Return ``True`` when *candidate* passes the Luhn checksum.

        Prevents ordinary digit runs (``2024 1234 5678 9010``-style noise)
        from being reported as card numbers.

        Args:
            candidate: Digit-and-separator string matching the card regex.

        Returns:
            Checksum verdict.
        """
        digits = [int(d) for d in re.sub(r"\D", "", candidate)]
        if len(digits) != 16:
            return False
        checksum = 0
        for i, digit in enumerate(reversed(digits)):
            if i % 2 == 1:
                digit *= 2
                if digit > 9:
                    digit -= 9
            checksum += digit
        return checksum % 10 == 0

    def detect(self, text: str) -> PIIFinding:
        """Scan *text* for every category; never raises on bad input.

        Args:
            text: Free text to scan.

        Returns:
            :class:`PIIFinding` with distinct categories and truncated
            evidence previews.  On unexpected internal failure a
            negative finding is returned after an ERROR log (fail-
            permissive; the pipeline continues, the problem is visible).
        """
        if not isinstance(text, str) or not text:
            return PIIFinding(detected=False)
        categories: list[str] = []
        evidence: list[str] = []
        try:
            for category, pattern in self.patterns.items():
                match = pattern.search(text)
                if match is None:
                    continue
                if category is PIICategory.CREDIT_CARD and not self._luhn_ok(match.group(0)):
                    log.debug(
                        "card-like digits rejected by Luhn",
                        extra={"category": category.value},
                    )
                    continue
                categories.append(category.value)
                evidence.append(f"{category.value}:{match.group(0)[:40]}")
        except Exception as exc:  # pragma: no cover - defensive
            log.error(
                "PII detector failed - degrading to negative finding",
                extra={"error": type(exc).__name__},
                exc_info=True,
            )
            return PIIFinding(detected=False)
        finding = PIIFinding(
            detected=bool(categories), categories=categories, evidence=evidence
        )
        log.debug(
            "PII scan complete",
            extra={"detected": finding.detected, "categories": categories},
        )
        return finding


class NoopPIIDetector:
    """Always-negative detector (explicit deployment opt-out).

    Use when PII screening is handled upstream and node_1 scanning is
    unwanted.  Every call logs at DEBUG so the opt-out stays visible in
    session traces.

    Example:
        >>> NoopPIIDetector().detect("jane@corp.com").detected
        False
    """

    def detect(self, text: str) -> PIIFinding:
        """Return a negative finding unconditionally.

        Args:
            text: Ignored (accepted for protocol compatibility).

        Returns:
            A negative :class:`PIIFinding`.
        """
        log.debug("noop detector consulted (PII screening disabled upstream)")
        return PIIFinding(detected=False)


class PIIRedactor:
    """Deterministic, session-scoped tokenizing redactor for audit payloads.

    Recursively walks JSON-safe payloads and replaces every PII match
    with a stable token (``[EMAIL_1]``, ``[EMAIL_2]``, …).  The same
    original value always maps to the same token **within one redactor
    instance** (one session), which keeps redacted evidence joinable for
    reviewers; the mapping table itself stays in memory and is never
    persisted, logged, or exported.

    Thread-safety: instances are **not** thread-safe; one redactor per
    engine instance (matching the engine's single-session-at-a-time
    contract) is the supported usage.

    Example:
        >>> red = PIIRedactor()
        >>> out, n = red.redact_payload({"contact": "jane@corp.com"})
        >>> n
        1
        >>> out["contact"]
        '[EMAIL_1]'
        >>> "jane@corp.com" in str(out)
        False
    """

    def __init__(self, detector: RegexPIIDetector | None = None) -> None:
        """Build the redactor around the given (or a fresh) detector.

        Args:
            detector: Scanner used to find PII values; defaults to a
                private :class:`RegexPIIDetector`.
        """
        self._detector = detector or RegexPIIDetector()
        self._map: dict[str, str] = {}
        self._counters: dict[str, int] = {}
        log.info("PIIRedactor initialised (audit-payload redaction ACTIVE)")

    def redact_text(self, text: str) -> tuple[str, int]:
        """Tokenize every PII match in *text*.

        Args:
            text: Arbitrary string (``""``/non-str pass through).

        Returns:
            ``(redacted_text, replacements_made)``.
        """
        if not isinstance(text, str) or not text:
            return text, 0
        replacements = 0

        def _sub(match: re.Match[str]) -> str:
            """Replace one regex match with its stable token.

            Args:
                match: The regex match found by ``pattern.sub``.

            Returns:
                The deterministic ``[CATEGORY_n]`` token for the value.
            """
            nonlocal replacements
            replacements += 1
            return self._token_for(match.group(0))

        out = text
        for category, pattern in self._detector.patterns.items():
            if category is PIICategory.CREDIT_CARD:
                out = pattern.sub(
                    lambda m: self._token_for(m.group(0))
                    if self._detector._luhn_ok(m.group(0))
                    else m.group(0),
                    out,
                )
                continue
            out = pattern.sub(_sub, out)
        return out, replacements

    def redact_payload(self, payload: Any) -> tuple[Any, int]:
        """Recursively redact every string in a JSON-safe payload.

        Dict keys are **never** modified (only values), so event shapes
        stay stable for SIEM queries.

        Args:
            payload: Any JSON-safe structure (dict/list/str/scalars).

        Returns:
            ``(redacted_payload, total_replacements)`` — the input is
            never mutated; containers are rebuilt along modified paths.
        """
        total = 0

        def _walk(node: Any) -> Any:
            """Recursively redact one payload node (dict/list/str/scalars).

            Dict keys are never modified — only values — so event shapes
            stay stable for downstream queries.

            Args:
                node: Current payload node.

            Returns:
                The redacted node (containers rebuilt along modified paths).
            """
            nonlocal total
            if isinstance(node, str):
                out, n = self.redact_text(node)
                total += n
                return out
            if isinstance(node, dict):
                return {k: _walk(v) for k, v in node.items()}
            if isinstance(node, list):
                return [_walk(item) for item in node]
            return node

        redacted = _walk(payload)
        if total:
            log.info(
                "PII redacted from audit payload",
                extra={"replacements": total, "categories": sorted(self._counters)},
            )
        return redacted, total

    def _token_for(self, value: str) -> str:
        """Return the stable token for *value*, assigning one if new.

        Args:
            value: Raw matched PII value.

        Returns:
            ``[<CATEGORY>_<n>]`` — deterministic per instance.
        """
        if value in self._map:
            return self._map[value]
        category = "PII"
        for candidate, pattern in self._detector.patterns.items():
            if pattern.fullmatch(value):
                category = candidate.value.upper()
                break
        self._counters[category] = self._counters.get(category, 0) + 1
        token = f"[{category}_{self._counters[category]}]"
        self._map[value] = token
        log.debug(
            "pii value tokenized",
            extra={"category": category.lower(), "token": token},
        )
        return token


class NoopRedactor:
    """Identity redactor — the behavior-neutral default.

    Returned payloads are the input object itself (no copy), matching
    the pre-G4 audit behavior byte-for-byte so enabling nothing changes
    nothing.

    Example:
        >>> payload = {"a": "jane@corp.com"}
        >>> out, n = NoopRedactor().redact_payload(payload)
        >>> out is payload
        True
        >>> n
        0
    """

    def redact_payload(self, payload: Any) -> tuple[Any, int]:
        """Return the payload untouched with a zero replacement count.

        Args:
            payload: Any value.

        Returns:
            ``(payload, 0)``.
        """
        return payload, 0
