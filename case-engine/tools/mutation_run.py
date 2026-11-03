#!/usr/bin/env python3
"""Deterministic mutation runner for the rule logic (G28).

The 16 VR* rules encode legal-ish semantics; a subtle operator flip
(``<=`` → ``<``) is exactly the class of bug example-based tests miss.
This tool mutates the rule logic token-by-token, re-runs the rule test
subset for every mutant and reports **killed / survived / stillborn**,
producing the per-release baseline in ``docs/MUTATION_BASELINE.md``.

Mutation operators (deliberately minimal and auditable):

======================  ============
``<``                   ⇄ ``<=``
``>``                   ⇄ ``>=``
``==``                  ⇄ ``!=``
``and``                 ⇄ ``or``
``True``                ⇄ ``False``
======================  ============

Usage
-----
    python tools/mutation_run.py                    # full run, writes baseline
    python tools/mutation_run.py --list-only        # enumerate mutants, no runs
    python tools/mutation_run.py --start 40 --end 60  # resume a chunked run

Design notes
------------
- **Token-level, not regex**: the :mod:`tokenize` module gives exact
  ``(row, col)`` spans, so ``<`` inside strings/never matches; every
  mutant is compile-checked (``ast.parse``) before it runs — mutants
  that break syntax are *stillborn*, not "survived".
- **Restore-or-die**: the original file is restored in a ``finally``
  block; a crashed run can never leave mutated sources behind.
- **Scope**: defaults to the validation rules and the enforcement gate
  (the trust boundary).  Mutating the whole package is not a goal —
  the runtime is dominated by plumbing, not semantics.
- For the canonical community toolchain the same scope is configured
  for ``mutmut`` in ``pyproject.toml`` (``[tool.mutmut]``); this runner
  exists so the baseline is reproducible offline with zero extra
  dependencies and deterministic mutant ordering.

Exit codes: ``0`` clean (all mutants killed), ``1`` surviving mutants
found, ``2`` harness error.

Cross-references: ``docs/GAP_ANALYSIS.md`` §G28,
``docs/MUTATION_BASELINE.md``, ``pyproject.toml`` ``[tool.mutmut]``.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import time
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_engine.logging_setup import configure_logging, get_logger

__all__ = ["Mutation", "MutantResult", "enumerate_mutants", "run_mutation_suite", "main"]

log = get_logger("tools.mutation_run")  # explicit: the script runs as __main__

#: Operator flips applied to ``OP`` tokens (order matters for docs only).
OP_FLIPS: dict[str, str] = {
    "<": "<=",
    "<=": "<",
    ">": ">=",
    ">=": ">",
    "==": "!=",
    "!=": "==",
}

#: Keyword/literal flips applied to ``NAME`` tokens.
NAME_FLIPS: dict[str, str] = {
    "and": "or",
    "or": "and",
    "True": "False",
    "False": "True",
}


@dataclass(frozen=True)
class Mutation:
    """One candidate mutant (a single token replacement).

    Attributes:
        index: Stable 1-based index (ordering: line, then column).
        path: Target file, repo-relative.
        line / col: 1-based line and 0-based column of the token.
        original / replacement: The token flip applied.
    """

    index: int
    path: str
    line: int
    col: int
    original: str
    replacement: str

    def describe(self) -> str:
        """Return a one-line human-readable description for reports."""
        return f"{self.path}:{self.line}:{self.col} {self.original!r}->{self.replacement!r}"


@dataclass
class MutantResult:
    """Outcome of running the test suite against one mutant.

    Attributes:
        mutation: The mutated site.
        status: ``killed`` | ``survived`` | ``stillborn`` (mutant failed
            to compile — never counted as survived) | ``timeout``.
        duration_s: Wall-clock test duration (0 for stillborn).
        detail: Failure excerpt or empty string.
    """

    mutation: Mutation
    status: str
    duration_s: float = 0.0
    detail: str = ""


def enumerate_mutants(path: Path, start_index: int = 1) -> list[Mutation]:
    """Enumerate every viable token-flip mutant in *path*.

    Args:
        path: Source file to scan (relative paths resolve against the
            repository root).
        start_index: First index for the returned mutants, so callers
            can build a **globally consistent** numbering across
            multiple target files (chunked ``--start/--end`` runs).

    Returns:
        Mutants ordered deterministically by ``(line, col)`` with
        stable indices starting at *start_index*.

    Raises:
        FileNotFoundError: When *path* does not exist.
    """
    if not path.exists():
        log.error("mutation target missing", extra={"path": str(path)})
        raise FileNotFoundError(str(path))
    path = path if path.is_absolute() else REPO_ROOT / path
    candidates: list[tuple[int, int, str, str]] = []
    with open(path, "rb") as fh:
        for token in tokenize.tokenize(fh.readline):
            replacement = None
            if token.type == tokenize.OP and token.string in OP_FLIPS:
                replacement = OP_FLIPS[token.string]
            elif token.type == tokenize.NAME and token.string in NAME_FLIPS:
                replacement = NAME_FLIPS[token.string]
            if replacement is not None:
                candidates.append((token.start[0], token.start[1], token.string, replacement))

    candidates.sort(key=lambda item: (item[0], item[1]))
    rel = path.relative_to(REPO_ROOT).as_posix()
    mutants = [
        Mutation(index=start_index + i, path=rel, line=line, col=col,
                 original=orig, replacement=repl)
        for i, (line, col, orig, repl) in enumerate(candidates)
    ]
    log.info("mutants enumerated", extra={"path": rel, "count": len(mutants)})
    return mutants


def _apply(text_lines: list[str], mutation: Mutation) -> str:
    """Return the mutated source text for *mutation* (pure)."""
    lines = list(text_lines)
    target = lines[mutation.line - 1]
    col = mutation.col
    assert target[col:col + len(mutation.original)] == mutation.original, (
        f"token span drift at {mutation.describe()}"
    )
    lines[mutation.line - 1] = (
        target[:col] + mutation.replacement + target[col + len(mutation.original):]
    )
    return "".join(lines)


def _run_tests(test_paths: list[str], timeout_s: int) -> tuple[int, float, str]:
    """Run the scoped pytest subset; return ``(exit, seconds, tail)``."""
    cmd = [sys.executable, "-m", "pytest", *test_paths,
           "-x", "-q", "--no-header", "-p", "no:cacheprovider"]
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        log.warning("mutant timed out (counted as killed)", extra={"timeout_s": timeout_s})
        tail = (exc.stdout or "")[-400:]
        return 124, time.perf_counter() - started, tail
    duration = time.perf_counter() - started
    tail = (proc.stdout or "")[-400:]
    return proc.returncode, duration, tail


def run_mutation_suite(
    targets: list[Path],
    test_paths: list[str],
    start: int = 1,
    end: int | None = None,
    timeout_s: int = 120,
    list_only: bool = False,
    tests_by_target: dict[str, list[str]] | None = None,
) -> tuple[list[MutantResult], dict[str, Any]]:
    """Enumerate and (optionally) run every mutant; restore sources always.

    Args:
        targets: Source files to mutate.
        test_paths: Default pytest paths run against each mutant (used
            when *tests_by_target* has no entry for the target).
        start: 1-based mutant index to start from (chunked resumption).
        end: Exclusive upper index bound (``None`` = to the end).
        timeout_s: Per-mutant test timeout.
        list_only: Enumerate without running (dry run).
        tests_by_target: Optional per-target test scoping (target path
            -> pytest paths), which roughly halves the runtime because
            rule mutants only need the rule tests.

    Returns:
        ``(results, summary)`` where *summary* carries the counts and
        the mutation score (killed / (killed + survived)).

    Raises:
        RuntimeError: If a target file was left mutated (defensive;
            the ``finally`` restore makes this unreachable short of a
            process kill).
    """
    all_mutants: list[Mutation] = []
    next_index = 1
    for target in targets:
        resolved = target if target.is_absolute() else REPO_ROOT / target
        file_mutants = enumerate_mutants(resolved, start_index=next_index)
        all_mutants.extend(file_mutants)
        next_index += len(file_mutants)
    selected = [m for m in all_mutants if start <= m.index < (end or len(all_mutants) + 1)]
    summary: dict[str, Any] = {
        "total_sites": len(all_mutants),
        "selected": len(selected),
        "killed": 0, "survived": 0, "stillborn": 0, "timeout": 0,
        "score": None,
    }
    results: list[MutantResult] = []
    if list_only:
        log.info("list-only mode", extra={"mutants": len(selected)})
        return results, summary

    # Cache original texts per target path (mutants across targets are
    # interleaved only by index; restore keyed by path keeps this O(1)).
    originals: dict[str, list[str]] = {}
    text_cache: dict[str, list[str]] = {}
    for mutation in selected:
        rel = mutation.path
        if rel not in originals:
            path = REPO_ROOT / rel
            text = path.read_text(encoding="utf-8")
            originals[rel] = text.splitlines(keepends=True)
            text_cache[rel] = text

    try:
        for mutation in selected:
            path = REPO_ROOT / mutation.path
            original_text = text_cache[mutation.path]
            mutated = _apply(originals[mutation.path], mutation)
            if mutated == original_text:
                continue  # flip produced identical text (should not happen)
            try:
                ast.parse(mutated)
            except SyntaxError as exc:
                results.append(MutantResult(mutation, "stillborn", 0.0, str(exc)))
                summary["stillborn"] += 1
                continue

            mutant_tests = (
                (tests_by_target or {}).get(mutation.path, test_paths) or test_paths
            )
            path.write_text(mutated, encoding="utf-8")
            try:
                exit_code, duration, tail = _run_tests(mutant_tests, timeout_s)
            finally:
                path.write_text(original_text, encoding="utf-8")

            status = "killed" if exit_code != 0 else "survived"
            if exit_code == 124:
                status = "timeout"
            result = MutantResult(mutation, status, round(duration, 2), tail if status != "killed" else "")
            results.append(result)
            summary[status] += 1
            log.info(
                "mutant %s", status,
                extra={"mutation": mutation.describe(), "seconds": result.duration_s},
            )
    finally:
        # Defensive restore of every touched target (no-op if untouched).
        for rel, lines in originals.items():
            (REPO_ROOT / rel).write_text("".join(lines), encoding="utf-8")
        # Fail loudly if anything is still mutated (process-kill guard).
        for rel, text in text_cache.items():
            if (REPO_ROOT / rel).read_text(encoding="utf-8") != text:
                raise RuntimeError(f"mutation restore failed for {rel} — sources corrupted")

    effective = summary["killed"] + summary["timeout"] + summary["survived"]
    if effective:
        summary["score"] = round((summary["killed"] + summary["timeout"]) / effective, 4)
    return results, summary


#: Known equivalent-at-verdict mutants, justified in the baseline (G28).
#: Each entry: (path-substring, original->replacement hint, rationale).
JUSTIFIED_EQUIVALENTS: tuple[tuple[str, str, str], ...] = (
    (
        "gate.py:181", "'False'->'True'",
        "exc.errors(include_url=False) — the flattened error strings only read "
        "loc/msg, so include_url has no observable effect (pydantic URL key discarded).",
    ),
    (
        "rules.py:643", "'>'->'>='",
        "VR008 numbered-list heuristic: when numbered==1 the segment count "
        "always dominates max(len(segments), ...), so the flip cannot change "
        "any verdict or message (segments >= 1 whenever numbered >= 1).",
    ),
)


def _render_markdown(summary: dict[str, Any], results: list[MutantResult], targets: list[str], tests: list[str]) -> str:
    """Render the baseline markdown document (deterministic layout)."""
    lines = [
        "# Mutation testing baseline (G28)",
        "",
        "Rule logic is legal-ish code; example-based tests cannot prove a",
        "surviving ``<=`` → ``<`` flip can't slip through. This baseline is",
        "regenerated **once per release** (not per PR) with:",
        "",
        "```bash",
        "python tools/mutation_run.py",
        "```",
        "",
        "| field | value |",
        "| --- | --- |",
        f"| targets | {', '.join('`' + t + '`' for t in targets)} |",
        f"| tests | {', '.join('`' + t + '`' for t in tests)} |",
        f"| mutation sites | {summary['total_sites']} |",
        f"| killed | {summary['killed']} |",
        f"| survived | {summary['survived']} |",
        f"| stillborn (syntax-invalid) | {summary['stillborn']} |",
        f"| timeout (counted killed) | {summary['timeout']} |",
        f"| **mutation score** | **{summary['score']}** |",
        "",
    ]
    survivors = [r for r in results if r.status == "survived"]
    if survivors:
        justified = [
            r for r in survivors
            if any(site in r.mutation.describe() and hint in r.mutation.describe()
                   for site, hint, _ in JUSTIFIED_EQUIVALENTS)
        ]
        unexplained = [r for r in survivors if r not in justified]
        if justified:
            lines += ["## Justified equivalent mutants (no action required)", ""]
            for r in justified:
                rationale = next(
                    why for site, hint, why in JUSTIFIED_EQUIVALENTS
                    if site in r.mutation.describe() and hint in r.mutation.describe()
                )
                lines += [f"- `{r.mutation.describe()}` — {rationale}"]
            lines.append("")
        if unexplained:
            lines += ["## Surviving mutants (review before release)", ""]
            for r in unexplained:
                lines.append(f"- `{r.mutation.describe()}`")
            lines.append("")
        if not unexplained:
            lines += ["No *unexplained* surviving mutants. Any new survivor in a",
                      "future run is a release blocker: either strengthen the",
                      "rules tests or justify it in ``JUSTIFIED_EQUIVALENTS``.", ""]
    else:
        lines += ["No surviving mutants. Any new survivor in a future run is a",
                  "release blocker: either strengthen the rules tests or justify",
                  "the equivalent mutant in this file.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` all killed, ``1`` survivors found, ``2`` harness error.
    """
    parser = argparse.ArgumentParser(
        prog="python tools/mutation_run.py",
        description="Deterministic mutation testing for the C.A.S.E. rule logic (G28).",
    )
    parser.add_argument("--target", action="append", default=None,
                        help="File to mutate (repeatable; default: validation rules + gate).")
    parser.add_argument("--test", action="append", default=None,
                        help="Pytest path to run per mutant (repeatable; default: rule + gate tests).")
    parser.add_argument("--start", type=int, default=1, help="1-based mutant index to start from.")
    parser.add_argument("--end", type=int, default=None, help="Exclusive end index (chunked runs).")
    parser.add_argument("--timeout", type=int, default=120, help="Per-mutant pytest timeout seconds.")
    parser.add_argument("--list-only", action="store_true", help="Enumerate mutants without running.")
    parser.add_argument("--out-md", type=Path, default=REPO_ROOT / "docs" / "MUTATION_BASELINE.md",
                        help="Markdown baseline output path.")
    parser.add_argument("--out-json", type=Path, default=None, help="Raw results JSON path.")
    args = parser.parse_args(argv)

    configure_logging(level="INFO")
    targets = [Path(t) for t in (args.target or [
        "case_engine/validation/rules.py", "case_engine/enforcement/gate.py",
    ])]
    tests = args.test or [
        "tests/unit/test_validation_rules.py", "tests/unit/test_gate_and_factory.py",
    ]
    # Per-target scoping (G28): rule mutants only need the rule tests,
    # gate mutants only the gate/factory tests — roughly halves the
    # per-mutant runtime while keeping the kill signal intact.
    tests_by_target = {
        "case_engine/validation/rules.py": [
            "tests/unit/test_validation_rules.py",
            "tests/unit/test_p3_mutation_pins.py",
        ],
        "case_engine/enforcement/gate.py": [
            "tests/unit/test_gate_and_factory.py", "tests/unit/test_property_gate.py",
        ],
    } if not args.test else {}

    try:
        if args.list_only:
            for target in targets:
                for mutation in enumerate_mutants(REPO_ROOT / target):
                    print(f"{mutation.index:4}  {mutation.describe()}")
            return 0
        results, summary = run_mutation_suite(
            targets, tests, start=args.start, end=args.end,
            timeout_s=args.timeout, tests_by_target=tests_by_target,
        )
    except Exception as exc:
        log.error("mutation run failed", extra={"error": str(exc)}, exc_info=True)
        print(f"MUTATION RUN ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    markdown = _render_markdown(
        summary, results, [t.as_posix() for t in targets], tests,
    )
    args.out_md.write_text(markdown, encoding="utf-8")
    print(f"baseline written -> {args.out_md}")
    if args.out_json:
        payload = {
            "summary": summary,
            "results": [
                {"mutation": r.mutation.describe(), "status": r.status,
                 "seconds": r.duration_s} for r in results
            ],
        }
        args.out_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"raw results -> {args.out_json}")

    print(
        f"sites={summary['total_sites']} killed={summary['killed']} "
        f"survived={summary['survived']} stillborn={summary['stillborn']} "
        f"score={summary['score']}"
    )
    return 1 if summary["survived"] else 0


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    raise SystemExit(main())
