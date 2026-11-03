"""Example 7 — external registry packs (G15): dump, overlay, reload.

Run:  python examples/registry_pack_demo.py

The constitution is data.  Organizations can now version and review
their own model packs as JSON — without forking Python:

1. **Export** the built-in registry to a pack document and reload it
   (complete-pack round trip, byte-for-byte semantics).
2. **Overlay** a tuned copy of one model onto the default constitution
   (e.g. a stricter confidence threshold for MM005) and show that the
   merged registry passes the same consistency checks as the original.
3. **Fail closed** on a tampered pack (wrong ``schema_version``).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from case_engine.exceptions import RegistryPackError
from case_engine.logging_setup import configure_logging
from case_engine.registry import CognitiveRegistry


def main() -> None:
    """Demonstrate the pack round trip, overlay mode and fail-closed load."""
    configure_logging(level="CRITICAL")  # keep the example output clean

    with tempfile.TemporaryDirectory() as tmp:
        # --- 1. complete-pack round trip ---------------------------------
        default = CognitiveRegistry.load_default()
        pack_path = Path(tmp) / "org_pack.json"
        default.dump_pack(pack_path)
        reloaded = CognitiveRegistry.load_pack(pack_path)
        same = (
            len(reloaded) == len(default) == 15
            and [m.algorithm_id for m in reloaded.all_models()]
            == [m.algorithm_id for m in default.all_models()]
            and reloaded.get("MM005").tool_name == default.get("MM005").tool_name
        )
        print(f"[1] dump -> load round trip: {'PASS' if same else 'FAIL'} "
              f"({len(default)} models, pack at {pack_path.name})")

        # --- 2. overlay a tuned model ------------------------------------
        document = json.loads(pack_path.read_text(encoding="utf-8"))
        mm005 = next(m for m in document["models"]
                     if m["algorithm_id"] == "MM005")
        mm005["confidence_threshold"] = 0.95  # organization-specific strictness
        overlay_path = Path(tmp) / "tuned_overlay.json"
        overlay_path.write_text(json.dumps(document, indent=2), encoding="utf-8")

        tuned = CognitiveRegistry.load_pack(overlay_path, overlay=True)
        base_threshold = default.get("MM005").confidence_threshold
        print(f"[2] overlay tuned MM005: "
              f"{'PASS' if tuned.get('MM005').confidence_threshold == 0.95 else 'FAIL'} "
              f"(threshold {base_threshold} -> 0.95, still {len(tuned)} models)")

        # --- 3. tampered pack fails closed -------------------------------
        document["schema_version"] = "0.0.1"
        bad_path = Path(tmp) / "bad_pack.json"
        bad_path.write_text(json.dumps(document), encoding="utf-8")
        try:
            CognitiveRegistry.load_pack(bad_path)
        except RegistryPackError as exc:
            print(f"[3] tampered pack rejected: PASS ({exc})")
        else:
            print("[3] tampered pack rejected: FAIL (was accepted!)")


if __name__ == "__main__":
    main()
