#!/usr/bin/env python3
"""PII protection tour (G1 + G4): TEE gate, redaction and encrypted audit.

Demonstrates the four data-protection behaviours added in v2.1.0:

1. **G1 TEE gate** — a query containing PII (an email address) is refused
   before any LLM generation sees it (``pii_policy="escalate"``), and the
   refusal is recorded as a ``TEE_DECISION`` audit event.
2. **G1 allow-with-flags** — the same query under
   ``pii_policy="allow_with_flags"`` runs to completion but is flagged
   (``pii_flagged=True``) in the result.
3. **G4a audit redaction** — with ``redact_pii_in_audit=True`` every PII
   value is tokenized (``jane@corp.com`` → ``[EMAIL_1]``) before it is
   written to the ledger; the raw value appears nowhere on disk.
4. **G4b encryption at rest** — with ``ledger_encryption=True`` payloads
   are AES-256-GCM envelopes; the hash chain commits to the ciphertext,
   so ``verify_audit_chain()`` stays key-free.

Run:  python examples/pii_protection.py
"""

from __future__ import annotations

import base64
import os
import tempfile

from case_engine import CASEEngine
from case_engine.config import EngineConfig, GlobalGuardrails
from case_engine.logging_setup import configure_logging
from case_engine.version import __version__

PII_QUERY = "Contact jane.doe@corp.com about the failed deployment in zone 4"
CLEAN_QUERY = "Zone 4 sensors spike anomalously after the firmware update"


def main() -> None:
    """Run the four demo scenarios in a throwaway directory."""
    configure_logging(level="WARNING")  # keep the demo output readable
    print(f"C.A.S.E. Engine {__version__} - PII protection tour (G1/G4)\n" + "=" * 62)

    # ---- 1) G1: default policy refuses PII before any generation ------ #
    engine = CASEEngine(config=EngineConfig(
        ledger_backend="memory",
        guardrails=GlobalGuardrails(max_reflexion_retries=1),
    ))
    result = engine.run(PII_QUERY)
    print("\n[1] pii_policy='escalate' (default) - PII refused fail-closed")
    print(f"    escalated={result.escalated}  pii_flagged={result.pii_flagged}  "
          f"tee_decision={result.tee_decision}")
    print(f"    categories={result.pii_categories}")
    print(f"    response: {(result.final_response or '').splitlines()[0]}")

    # ---- 2) G1: allow-with-flags proceeds but flags the session -------- #
    engine = CASEEngine(config=EngineConfig(
        ledger_backend="memory",
        guardrails=GlobalGuardrails(pii_policy="allow_with_flags",
                                    max_reflexion_retries=1),
    ))
    result = engine.run(PII_QUERY)
    print("\n[2] pii_policy='allow_with_flags' - session proceeds, flagged")
    print(f"    all_valid={result.all_valid}  pii_flagged={result.pii_flagged}  "
          f"tee_decision={result.tee_decision}")

    # ---- 3) G1: TEE attestation unblocks the session ------------------- #
    result = engine.run(PII_QUERY, tee_attested=True)
    print("\n[3] tee_attested=True - attestation recorded, no flag")
    print(f"    all_valid={result.all_valid}  tee_decision={result.tee_decision}")

    # ---- 4) G4a: audit payloads redacted before persistence ------------ #
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = os.path.join(tmp, "redacted_audit.jsonl")
        engine = CASEEngine(config=EngineConfig(
            ledger_backend="jsonl",
            ledger_path=ledger_path,
            redact_pii_in_audit=True,
            guardrails=GlobalGuardrails(pii_policy="allow_with_flags",
                                        max_reflexion_retries=1),
        ))
        result = engine.run(PII_QUERY)
        raw = open(ledger_path, encoding="utf-8").read()
        print("\n[4] redact_pii_in_audit=True - ledger stores tokens only")
        print(f"    all_valid={result.all_valid}")
        print(f"    raw email on disk: {'jane.doe@corp.com' in raw}")
        print(f"    [EMAIL_1] token on disk: {'[EMAIL_1]' in raw}")
        print(f"    chain still valid: {engine.verify_audit_chain().valid}")

    # ---- 5) G4b: payloads encrypted at rest (AES-256-GCM) -------------- #
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CASE_ENGINE_LEDGER_KEY"] = base64.b64encode(os.urandom(32)).decode()
        ledger_path = os.path.join(tmp, "encrypted_audit.jsonl")
        engine = CASEEngine(config=EngineConfig(
            ledger_backend="jsonl",
            ledger_path=ledger_path,
            ledger_encryption=True,
        ))
        result = engine.run(CLEAN_QUERY)
        raw = open(ledger_path, encoding="utf-8").read()
        print("\n[5] ledger_encryption=True - AESGCM envelopes on disk")
        print(f"    all_valid={result.all_valid}")
        print(f"    plaintext query on disk: {CLEAN_QUERY in raw}")
        print(f"    __enc__ envelopes on disk: {'__enc__' in raw}")
        print(f"    chain verifies WITHOUT the key: "
              f"{engine.verify_audit_chain().valid}")

    print("\nDone - every decision above is recorded in the audit trail.")


if __name__ == "__main__":
    main()
