"""Message-catalog seam for user-facing strings (G19, v2.3.0).

Purpose & role
--------------
Failure strings produced by the engine are *user-facing*: they enter
reflexion feedback (read by the LLM), escalation handoffs (read by
humans) and CLI output.  Every such string is currently English; this
module gives non-English adopters a **single, opt-in indirection
point** without forcing a translation framework on anyone.

Design contract
---------------
* **Opt-in and inert by default** — with no catalog installed every
  string is byte-identical to pre-2.3.0 output; nothing changes for
  existing integrations or the 240+ tests that assert on messages.
* **Never raises** — a broken catalog (bad template, wrong placeholder,
  throwing ``get``) can never break a validation run or an escalation:
  ``msg`` logs a WARNING and falls back to the rule's own English text.
  Text rendering is the one deliberate fail-open in the engine (a
  missing translation must never stop a session).
* **Process-level catalog** — :func:`set_catalog` swaps the active
  catalog for the whole process (GIL-atomic reference assignment); call
  it once at startup, before creating engines.

Message keys
------------
============== =========================================================
Key            Meaning
============== =========================================================
``vr.<ID>.pass``  Verdict text of validation rule ``<ID>`` (e.g. ``vr.VR005.pass``).
``vr.<ID>.fail``  Rejection text of validation rule ``<ID>`` (feeds reflexion).
``escalation.infra_failure``  Final response of an infrastructure-failure
              escalation.  Placeholders: ``{error_type}``, ``{error}``,
              ``{session_id}``.
============== =========================================================

Rules return fully-interpolated English text, so a catalog entry
*replaces* the whole verdict for that rule id (no re-parameterization
of rule-internal values in v2.3.0; ``escalation.infra_failure`` is the
exception — it is a real template with named placeholders).

Usage
-----
    >>> from case_engine import messages
    >>> catalog = messages.DictMessageCatalog({"vr.VR005.fail": "VR005 rejected: dialectic check failed"})
    >>> messages.set_catalog(catalog)
    >>> messages.msg("vr.VR005.fail", default="VR005 failed: blames a human")
    'VR005 rejected: dialectic check failed'
    >>> messages.reset_catalog()
    >>> messages.msg("vr.VR005.fail", default="VR005 failed: blames a human")
    'VR005 failed: blames a human'

Cross-references
----------------
``case_engine.validation.rules`` (the ``vr.*`` wiring),
``case_engine.engine`` (``escalation.infra_failure`` wiring),
``docs/CONVENTIONS.md`` (log-safety rules apply to translations too).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from case_engine.logging_setup import get_logger

__all__ = [
    "MessageCatalog",
    "DictMessageCatalog",
    "set_catalog",
    "get_catalog",
    "reset_catalog",
    "msg",
]

log = get_logger(__name__)


@runtime_checkable
class MessageCatalog(Protocol):
    """Structural contract for a translation catalog.

    Implementations map stable message keys to templates; ``get`` must
    be side-effect free and safe to call for every message.  A custom
    catalog only has to provide this one method (file-, dict- or
    database-backed — the engine does not care).
    """

    def get(self, key: str) -> str | None:
        """Return the template for *key*, or ``None`` when untranslated.

        Args:
            key: Stable message id, e.g. ``"vr.VR005.fail"``.

        Returns:
            The template string, or ``None`` to fall back to English.
        """
        ...  # pragma: no cover - protocol body


class DictMessageCatalog:
    """Minimal dict-backed :class:`MessageCatalog`.

    Args:
        messages: Mapping of message key to template string.

    Example:
        >>> c = DictMessageCatalog({"vr.VR001.fail": "VR001 rejected"})
        >>> c.get("vr.VR001.fail")
        'VR001 rejected'
        >>> c.get("missing") is None
        True
    """

    def __init__(self, messages: dict[str, str]) -> None:
        """Store a shallow copy of *messages* (caller's dict is safe)."""
        self._messages: dict[str, str] = dict(messages)
        log.debug(
            "message catalog installed",
            extra={"entries": len(self._messages)},
        )

    def get(self, key: str) -> str | None:
        """Return the template for *key* or ``None`` (protocol impl)."""
        return self._messages.get(key)


#: Process-level active catalog; ``None`` means "English defaults only".
_ACTIVE: MessageCatalog | None = None


def set_catalog(catalog: MessageCatalog | None) -> None:
    """Install *catalog* as the process-wide message source.

    Args:
        catalog: The catalog to install, or ``None`` to revert to the
            English defaults (equivalent to :func:`reset_catalog`).

    Side effects:
        INFO log with the catalog type name (never its contents — a
        catalog may embed organization-specific wording).
    """
    global _ACTIVE
    _ACTIVE = catalog
    log.info(
        "message catalog set",
        extra={"catalog": type(catalog).__name__ if catalog else None},
    )


def get_catalog() -> MessageCatalog | None:
    """Return the active catalog (``None`` when using English defaults)."""
    return _ACTIVE


def reset_catalog() -> None:
    """Remove the active catalog (English defaults; used by tests)."""
    global _ACTIVE
    _ACTIVE = None
    log.debug("message catalog reset")


def msg(key: str, default: str, **params: Any) -> str:
    """Resolve a user-facing message through the active catalog.

    Resolution order: catalog template → formatted with *params* →
    English *default* (already interpolated by the caller) → on any
    catalog error, the English default.  Never raises.

    Args:
        key: Stable message id, e.g. ``"vr.VR005.fail"``.
        default: English fallback text (callers pass their fully
            interpolated string).
        **params: Placeholders for a catalog template (ignored when no
            catalog entry exists).

    Returns:
        The resolved message text.

    Example:
        >>> msg("vr.VR001.fail", default="VR001 failed")
        'VR001 failed'
    """
    catalog = _ACTIVE
    template: str | None = None
    if catalog is not None:
        try:
            template = catalog.get(key)
        except Exception as exc:  # text must never break a run (deliberate fail-open)
            log.warning(
                "message catalog get failed - using English default",
                extra={"key": key, "error_type": type(exc).__name__},
            )
            template = None
    if template is None:
        return default
    try:
        # Always format (even with no params) so a placeholder-bearing
        # template without its params falls back cleanly instead of
        # leaking braces into user-facing text.
        return template.format(**params)
    except (KeyError, IndexError, ValueError, AttributeError) as exc:
        log.warning(
            "message template formatting failed - using English default",
            extra={"key": key, "error_type": type(exc).__name__},
        )
        return default
