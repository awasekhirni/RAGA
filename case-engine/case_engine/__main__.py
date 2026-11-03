"""``python -m case_engine`` entry point (delegates to the CLI).

Role in the architecture: the thinnest possible launcher — it exists so
operators can run the engine as a module without going through the
console script.  All behaviour (argument parsing, logging setup, exit
codes) lives in :mod:`case_engine.cli`; this module only forwards the
interpreter's exit status.

Contents
--------
- :func:`case_engine.cli.main` — the dispatched entry point, invoked
  with ``sys.argv`` semantics when run as a script.

Invariants
----------
- Adds no behaviour of its own: exit codes and stdout/stderr parity with
  :func:`case_engine.cli.main` are exact.
- Import side-effect free: importing :mod:`case_engine.__main__` never
  executes the CLI (the ``if __name__ == "__main__"`` guard).

See also: docs/ARCHITECTURE.md, case_engine/cli.py (command reference
and exit codes), pyproject.toml (the ``case-engine`` console script
wired to the same :func:`~case_engine.cli.main`).
"""

from __future__ import annotations

import sys

from case_engine.cli import main

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
