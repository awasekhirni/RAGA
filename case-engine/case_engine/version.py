"""Version and identity constants for the C.A.S.E. engine.

This module is intentionally dependency-free so that any other module in
the package (and external tooling such as packaging metadata) can import
it without triggering the rest of the engine.

Contents
--------
- ``ENGINE_NAME`` — human-readable engine identity (spec metadata, CLI
  banner, audit ``SESSION_START`` payloads).
- ``__version__`` — semantic version of the engine implementation.
- ``SCHEMA_VERSION`` — version of the cognitive-registry JSON contract
  this build implements.
- ``COMPATIBILITY`` — declared compatibility floors for runtime deps.

Invariants
----------
- Import-safe and side-effect free: constants only, no imports, no I/O,
  so every engine module may pull it in freely.
- ``__version__`` must stay in lockstep with ``project.version`` in
  ``pyproject.toml`` (release checklist item).
- ``SCHEMA_VERSION`` changes only when the registry JSON contract breaks
  backwards compatibility.

See also: pyproject.toml (packaging metadata), case_engine/__init__.py
(public re-exports), docs/ARCHITECTURE.md.
"""

from __future__ import annotations

#: Human-readable engine identity, as declared in the C.A.S.E. spec JSON.
ENGINE_NAME: str = "C.A.S.E. (Cognitive Architectural Scaffolding & Enforcement)"

#: Semantic version of the engine implementation.
__version__: str = "2.4.0"

#: Version of the cognitive-registry JSON contract this build implements.
SCHEMA_VERSION: str = "2.0.0"

#: Compatibility floor declared in the spec metadata block.
COMPATIBILITY: dict[str, str] = {
    "python": ">=3.11",
    "pydantic": ">=2.5",
    "langgraph": ">=0.2 (optional)",
    "outlines": ">=0.0.40 (optional, not required for the tool-call gate)",
}
