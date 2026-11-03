"""Payload-at-rest encryption for audit ledgers (G4b, optional).

Purpose & role
--------------
Complements :mod:`case_engine.privacy` (redaction) with **envelope
encryption at rest**: when configured, every ledger payload is encrypted
with AES-256-GCM before it is written, and the hash chain is computed
over the *ciphertext* (encrypt-then-chain).  Consequences worth stating
plainly:

* ``verify_chain`` needs **no key** — integrity verification of the
  ledger stays available to any operator (and to ``case-engine verify``)
  because the chain hashes exactly the bytes on disk.
* Confidentiality requires the key; a leaked ledger file without the
  key reveals event metadata (seq, type, session, node, timestamp) but
  not payload content.
* Tampering with ciphertext breaks the chain like any other mutation.

Wire format: an encrypted payload replaces the original dict with
``{"__enc__": "AESGCM", "nonce": <b64>, "data": <b64>}`` — a JSON-safe
dict, so every existing backend (JSONL, SQLite) stores it unchanged and
``canonical_json``/``compute_event_hash`` keep working untouched.

Contents
--------
- :class:`PayloadCipher` — structural protocol for cipher implementations.
- :class:`AEADPayloadCipher` — AES-256-GCM implementation (optional
  ``cryptography`` dependency, imported lazily).
- :func:`load_key` — key resolution (explicit bytes → base64 env var).

Invariants
----------
* **Fail-closed on decrypt**: a wrong key, a truncated blob or a tampered
  ciphertext raises :class:`~case_engine.exceptions.LedgerError` — a
  payload is never silently returned empty or partially decrypted.
* **Key handling**: keys are never logged; only the key *source*
  (``"explicit"`` / ``"env:NAME"``) appears in records.
* The module is import-safe without the ``cryptography`` package; the
  dependency is only required when a cipher is actually constructed.

Cross-references: ``docs/GAP_ANALYSIS.md`` (G4),
``docs/AUDIT_LEDGER.md`` (encryption section),
``case_engine/audit/jsonl_ledger.py`` / ``sqlite_ledger.py`` (wiring).
"""

from __future__ import annotations

import base64
import os
import secrets
from typing import Any, Protocol, runtime_checkable

from case_engine.exceptions import LedgerError
from case_engine.logging_setup import get_logger

__all__ = ["PayloadCipher", "AEADPayloadCipher", "load_key", "KEY_ENV_VAR"]

#: Environment variable consulted when no explicit key is provided.
KEY_ENV_VAR = "CASE_ENGINE_LEDGER_KEY"

#: Marker key distinguishing an encrypted payload envelope.
_ENVELOPE_TAG = "__enc__"

log = get_logger(__name__)


@runtime_checkable
class PayloadCipher(Protocol):
    """Structural protocol for at-rest payload ciphers.

    Implementations turn a JSON-safe payload dict into a JSON-safe
    *envelope dict* and back; ledgers treat the envelope as an opaque
    payload (hash chain included).
    """

    def encrypt(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Encrypt *payload* into an envelope dict.

        Args:
            payload: JSON-safe dict (the caller's event body).

        Returns:
            JSON-safe envelope dict safe to persist and hash.
        """
        ...  # pragma: no cover

    def decrypt(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """Decrypt an envelope dict produced by :meth:`encrypt`.

        Args:
            envelope: Previously encrypted envelope.

        Returns:
            The original payload dict.

        Raises:
            LedgerError: On any tamper/format/key failure (fail-closed).
        """
        ...  # pragma: no cover


def load_key(key: bytes | str | None, key_env: str = KEY_ENV_VAR) -> bytes:
    """Resolve a 32-byte AES key from explicit input or the environment.

    Resolution order: explicit *key* (raw 32 bytes or base64 string) →
    ``key_env`` environment variable (base64) → error.

    Args:
        key: Explicit key material (raw bytes, base64 string, or ``None``).
        key_env: Env var name consulted when *key* is ``None``.

    Returns:
        Exactly 32 bytes of key material.

    Raises:
        LedgerError: When no key can be resolved or the material has the
            wrong length (both logged at ERROR without ever logging the
            key contents).
    """
    source = "explicit"
    material: bytes | str | None = key
    if material is None:
        material = os.environ.get(key_env)
        source = f"env:{key_env}"
    if not material:
        log.error(
            "ledger encryption requested but no key available",
            extra={"key_env": key_env, "hint": f"set {key_env} to a base64 32-byte key"},
        )
        raise LedgerError(
            f"No ledger encryption key available: pass 32 raw bytes / base64 "
            f"explicitly or set the {key_env} environment variable"
        )
    try:
        raw = base64.b64decode(material) if isinstance(material, str) else material
    except Exception as exc:
        log.error("ledger key is not valid base64", extra={"source": source})
        raise LedgerError("Ledger encryption key must be 32 raw bytes or valid base64") from exc
    if len(raw) != 32:
        log.error("ledger key has wrong length", extra={"source": source, "length": len(raw)})
        raise LedgerError(
            f"Ledger encryption key must be exactly 32 bytes (AES-256); got {len(raw)}"
        )
    log.info("ledger encryption key loaded", extra={"source": source, "bits": 256})
    return raw


class AEADPayloadCipher:
    """AES-256-GCM payload cipher (encrypt-then-chain at-rest protection).

    Uses the optional ``cryptography`` package, imported lazily so the
    core engine never requires it.  Every ``encrypt`` call draws a fresh
    12-byte nonce from the OS CSPRNG; authenticated-tag failures on
    decrypt are surfaced as :class:`LedgerError` (tamper evidence), never
    swallowed.

    Args:
        key: Explicit 32-byte key or base64 string; ``None`` resolves via
            :func:`load_key` from the environment.
        key_env: Env var name used when *key* is ``None``.

    Raises:
        LedgerError: When ``cryptography`` is not installed (with an
            actionable install hint), or key resolution fails.

    Example:
        >>> from case_engine.audit.crypto import AEADPayloadCipher
        >>> import os, base64
        >>> key = base64.b64encode(os.urandom(32)).decode()
        >>> cipher = AEADPayloadCipher(key=key)
        >>> env = cipher.encrypt({"secret": "jane@corp.com"})
        >>> env["__enc__"]
        'AESGCM'
        >>> cipher.decrypt(env)["secret"]
        'jane@corp.com'
    """

    def __init__(self, key: bytes | str | None = None, key_env: str = KEY_ENV_VAR) -> None:
        """Resolve the key and lazily bind the AESGCM primitive.

        Args:
            key: Explicit key material (see :func:`load_key`).
            key_env: Env var fallback name.

        Raises:
            LedgerError: Missing dependency or unusable key.
        """
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            log.error(
                "cryptography package not installed - cannot enable ledger encryption",
                extra={"hint": "pip install 'case-engine[crypto]' or pip install cryptography"},
            )
            raise LedgerError(
                "Ledger encryption requires the 'cryptography' package "
                "(pip install 'case-engine[crypto]')"
            ) from exc
        self._aesgcm = AESGCM(load_key(key, key_env))
        log.info("AEADPayloadCipher initialised", extra={"algorithm": "AES-256-GCM"})

    def encrypt(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Encrypt *payload* into a fresh-nonce AESGCM envelope.

        Args:
            payload: JSON-safe dict.  Non-dict or non-JSON-serializable
                payloads raise :class:`LedgerError` (the chain must hash
                exactly what is written; nothing is "best-efforted").

        Returns:
            Envelope dict ``{"__enc__": "AESGCM", "nonce": b64, "data": b64}``.

        Raises:
            LedgerError: On serialization failure (logged at ERROR).
        """
        from cryptography.exceptions import InvalidTag  # noqa: F401  (symmetry w/ decrypt)

        if not isinstance(payload, dict):
            log.error("encrypt expects a dict payload", extra={"got": type(payload).__name__})
            raise LedgerError("AEADPayloadCipher.encrypt requires a dict payload")
        try:
            import json as _json

            plaintext = _json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            log.error("payload is not JSON-serializable - cannot encrypt", exc_info=True)
            raise LedgerError(f"Payload not JSON-serializable: {exc}") from exc
        nonce = secrets.token_bytes(12)
        ciphertext = self._aesgcm.encrypt(nonce, plaintext, None)
        log.debug(
            "payload encrypted",
            extra={"plaintext_bytes": len(plaintext), "ciphertext_bytes": len(ciphertext)},
        )
        return {
            _ENVELOPE_TAG: "AESGCM",
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "data": base64.b64encode(ciphertext).decode("ascii"),
        }

    def decrypt(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """Decrypt an AESGCM envelope back into the original payload.

        Args:
            envelope: Dict carrying the ``__enc__`` marker, ``nonce`` and
                ``data`` base64 fields.

        Returns:
            The original payload dict.

        Raises:
            LedgerError: When the envelope is malformed, the key is
                wrong, or the ciphertext fails authentication — every
                case is logged at ERROR and fails closed.
        """
        import json as _json

        from cryptography.exceptions import InvalidTag

        if not isinstance(envelope, dict) or envelope.get(_ENVELOPE_TAG) != "AESGCM":
            log.error("decrypt called on a non-envelope payload")
            raise LedgerError("Payload is not an AESGCM envelope (missing __enc__ marker)")
        try:
            nonce = base64.b64decode(envelope["nonce"])
            ciphertext = base64.b64decode(envelope["data"])
            plaintext = self._aesgcm.decrypt(nonce, ciphertext, None)
        except KeyError as exc:
            log.error("envelope missing required field", extra={"field": str(exc)})
            raise LedgerError(f"Corrupt encryption envelope: missing {exc}") from exc
        except InvalidTag as exc:
            log.error(
                "AEAD authentication FAILED - ciphertext tampered or wrong key",
            )
            raise LedgerError(
                "Ledger payload authentication failed (wrong key or tampered ciphertext)"
            ) from exc
        except Exception as exc:  # binascii.Error etc.
            log.error("envelope decode failure", extra={"error": type(exc).__name__})
            raise LedgerError(f"Corrupt encryption envelope: {exc}") from exc
        try:
            return _json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, _json.JSONDecodeError) as exc:
            log.error("decrypted payload is not valid JSON", exc_info=True)
            raise LedgerError(f"Decrypted payload is not valid JSON: {exc}") from exc
