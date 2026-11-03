"""Command-line interface for the C.A.S.E. engine.

This module is the operator-facing surface of the engine: it parses
arguments, builds a :class:`~case_engine.engine.CASEEngine` from CLI
flags and delegates to one command handler per subcommand.  Handlers
print their results to stdout (machine-readable with ``--json``) and
return process exit codes; diagnostics flow through the structured
logging system (clamped to ``WARNING`` in :func:`main` and routed to
**stderr**, so stdout carries command output only — hence all handler
log records are DEBUG level).

Commands
--------
``run``       Execute one query through the full pipeline.
``demo``      Run the built-in offline demo (mock provider).
``models``    List the cognitive registry (optionally filtered by domain).
``show``      Show the full registry entry for one mental model.
``verify``    Verify the hash chain of an audit ledger file.
``config``    Print the resolved configuration.
``history``   List stored session results (G12 result store).
``doctor``    Health-check registry, ledger and provider (G17).
``init``      Write a commented config.json template (G17).
``export``    Export stored session results to JSON/CSV (G17).

Exit codes: ``0`` success · ``1`` reasoning/verification failure ·
``2`` usage error.

Contents
--------
- :func:`main` — entry point; installs logging and dispatches to the
  ``_cmd_*`` handlers.
- :func:`_build_parser` — argparse tree for all subcommands.
- :func:`_make_engine` — CLI flags → :class:`EngineConfig` → engine.
- :func:`_print_result` — human/JSON rendering of a :class:`CASEResult`.
- ``_cmd_run`` / ``_cmd_demo`` / ``_cmd_models`` / ``_cmd_show`` /
  ``_cmd_verify`` / ``_cmd_config`` / ``_cmd_history`` / ``_cmd_doctor`` /
  ``_cmd_init`` / ``_cmd_export`` — one handler per subcommand.

Invariants
----------
- Handlers communicate results only via ``print`` + return code; every
  new diagnostic is a DEBUG log, and engine logs are routed to stderr
  so ``--json`` stdout stays byte-pure regardless of log level.
- Provider secrets (values inside ``llm_kwargs``, e.g. API keys) are
  never logged — only kwarg *names* appear in debug records.
- :class:`~case_engine.exceptions.CASEError` is caught in :func:`main`
  and converted to exit code ``1`` with the message on stderr.

See also: docs/ARCHITECTURE.md, case_engine/config.py (flag → config
mapping), case_engine/logging_setup.py (log routing).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from case_engine.audit import create_ledger
from case_engine.config import EngineConfig, GlobalGuardrails
from case_engine.engine import CASEEngine
from case_engine.exceptions import CASEError
from case_engine.llm import MockLLMClient
from case_engine.logging_setup import configure_logging, get_logger
from case_engine.registry import CognitiveRegistry
from case_engine.registry.routing import ModelRouter
from case_engine.version import ENGINE_NAME, __version__

__all__ = ["main"]

log = get_logger(__name__)

_EXIT_OK, EXIT_FAIL, _EXIT_USAGE = 0, 1, 2


def _build_parser() -> argparse.ArgumentParser:
    """Create the top-level argument parser (pure construction, no I/O).

    Builds the full subcommand tree: ``run``, ``demo``, ``models``,
    ``show``, ``verify`` and ``config``.  Defaults mirror
    :class:`~case_engine.config.EngineConfig` (mock provider, JSONL
    ledger); ``--quiet`` later clamps engine logging to ``ERROR``.

    Side effects: emits a DEBUG record (nothing else).

    Returns:
        Configured :class:`argparse.ArgumentParser` with ``required``
        subparsers (usage errors exit with code 2 via argparse).
    """
    log.debug("building cli argument parser")
    parser = argparse.ArgumentParser(
        prog="case-engine",
        description=f"{ENGINE_NAME} v{__version__} — deterministic mental-model reasoning",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run one query through the pipeline")
    p_run.add_argument("query", help="User problem statement")
    p_run.add_argument("--provider", default="mock",
                       choices=["mock", "openai", "anthropic", "ollama", "vllm"])
    p_run.add_argument("--model", default=None, help="Provider model id")
    p_run.add_argument("--base-url", default=None, help="Custom endpoint (ollama/vllm/gateway)")
    p_run.add_argument("--ledger", default="jsonl", choices=["jsonl", "sqlite", "memory"])
    p_run.add_argument("--ledger-path", default=None, help="Ledger file path")
    p_run.add_argument("--max-retries", type=int, default=None,
                       help="Reflexion retries (default from guardrails: 3)")
    p_run.add_argument("--top-n", type=int, default=3, help="Models routed per domain")
    p_run.add_argument("--domain", default=None,
                       help="Preset domain (skips classification)")
    p_run.add_argument("--models", default=None,
                       help="Force models, comma-separated (bypasses affinity routing)")
    p_run.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p_run.add_argument("--quiet", action="store_true", help="Suppress logs (errors only)")
    p_run.add_argument("--result-store", default=None,
                       help="Persist the session result to this SQLite result store (G12)")

    p_demo = sub.add_parser("demo", help="Run the built-in offline demo")
    p_demo.add_argument("--ledger-path", default="case_audit_demo.jsonl")
    p_demo.add_argument("--json", action="store_true")
    p_demo.add_argument("--reflexion", action="store_true",
                        help="Inject a scripted failure to demo the reflexion loop")
    p_demo.add_argument("--quiet", action="store_true", help="Suppress logs (errors only)")

    p_models = sub.add_parser("models", help="List registered mental models")
    p_models.add_argument("--domain", default=None, help="Only models routed for this domain")
    p_models.add_argument("--json", action="store_true")

    p_show = sub.add_parser("show", help="Show one mental model's full entry")
    p_show.add_argument("model_id", help="e.g. MM005")

    p_verify = sub.add_parser("verify", help="Verify an audit ledger's hash chain")
    p_verify.add_argument("--ledger", default="jsonl", choices=["jsonl", "sqlite", "memory"])
    p_verify.add_argument("--ledger-path", default="case_audit.jsonl")

    sub.add_parser("config", help="Print the resolved configuration")

    p_history = sub.add_parser("history",
                               help="List stored session results (G12 result store)")
    p_history.add_argument("--store", default="case_results.sqlite3",
                           help="SQLite result-store path (written by run --result-store)")
    p_history.add_argument("--since", default=None,
                           help="ISO-8601 lower bound on session start (inclusive)")
    p_history.add_argument("--limit", type=int, default=20,
                           help="Maximum sessions to list (default 20; 0 = unlimited)")
    p_history.add_argument("--session", default=None,
                           help="Show one full session result by id")
    p_history.add_argument("--json", action="store_true",
                           help="Emit machine-readable JSON")

    p_doctor = sub.add_parser("doctor",
                              help="Health-check registry, ledger and provider (G17)")
    p_doctor.add_argument("--ledger", default="jsonl", choices=["jsonl", "sqlite", "memory"])
    p_doctor.add_argument("--ledger-path", default="case_audit.jsonl",
                          help="Ledger file to health-check")
    p_doctor.add_argument("--provider", default="mock",
                          choices=["mock", "openai", "anthropic", "ollama", "vllm"])
    p_doctor.add_argument("--ping", action="store_true",
                          help="Also construct the provider client (SDK import only, no API call)")
    p_doctor.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    p_init = sub.add_parser("init",
                            help="Write a commented config.json template (G17)")
    p_init.add_argument("--path", default="config.json",
                        help="Where to write the template (default ./config.json)")
    p_init.add_argument("--force", action="store_true",
                        help="Overwrite an existing file")

    p_export = sub.add_parser("export",
                              help="Export stored session results to JSON/CSV (G17)")
    p_export.add_argument("--store", default="case_results.sqlite3",
                          help="SQLite result-store path (written by run --result-store)")
    p_export.add_argument("--format", default="json", choices=["json", "csv"],
                          help="Output format")
    p_export.add_argument("--since", default=None,
                          help="ISO-8601 lower bound on session start (inclusive)")
    p_export.add_argument("--limit", type=int, default=None,
                          help="Maximum sessions to export (default: all)")
    p_export.add_argument("--out", default="-",
                          help="Output file path ('-' = stdout)")
    return parser


def _make_engine(args: argparse.Namespace) -> CASEEngine:
    """Build an engine from parsed CLI arguments.

    Maps flags → :class:`~case_engine.config.EngineConfig`: ``--model``
    and ``--base-url`` (only for ``ollama``/``vllm``) become
    ``llm_kwargs``, ``--max-retries`` overrides the guardrail default,
    and ``--quiet`` clamps the log level to ``ERROR``.  Attributes that
    only exist on some subcommand namespaces are read via ``getattr``
    with defaults; the engine constructor performs no network I/O.

    Side effects: emits a DEBUG record; provider credentials inside
    ``llm_kwargs`` are never logged (only kwarg names).

    Args:
        args: Parsed namespace (expected to carry at least ``provider``,
            ``ledger`` and ``ledger_path``).

    Returns:
        Ready :class:`CASEEngine`.
    """
    guardrails = GlobalGuardrails(
        max_reflexion_retries=args.max_retries if getattr(args, "max_retries", None) is not None else 3
    )
    llm_kwargs: dict = {}
    if getattr(args, "model", None):
        llm_kwargs["model"] = args.model
    if getattr(args, "base_url", None) and args.provider in {"ollama", "vllm"}:
        llm_kwargs["base_url"] = args.base_url

    config = EngineConfig(
        guardrails=guardrails,
        provider=args.provider,
        llm_kwargs=llm_kwargs,
        ledger_backend=args.ledger,
        ledger_path=args.ledger_path,
        log_level="ERROR" if getattr(args, "quiet", False) else "INFO",
        result_store_backend=("sqlite" if getattr(args, "result_store", None) else "none"),
        result_store_path=getattr(args, "result_store", None),
    )
    log.debug(
        "building engine from cli arguments",
        extra={"provider": config.provider, "ledger_backend": config.ledger_backend,
               "ledger_path": config.ledger_path,
               "max_retries": guardrails.max_reflexion_retries,
               "llm_kwarg_keys": sorted(llm_kwargs),
               "quiet": getattr(args, "quiet", False)},
    )
    return CASEEngine(config=config)


def _print_result(result, as_json: bool) -> None:
    """Print a :class:`CASEResult` humanly or as JSON.

    The canonical stdout rendering of a result: JSON mode dumps the full
    ``model_dump()`` for ``--json`` consumers; human mode prints the
    final response framed by rules plus a one-line session summary.
    Response payloads are never logged — DEBUG records carry the session
    id and mode only.

    Args:
        result: The engine result.
        as_json: Emit JSON when True.
    """
    log.debug("printing result", extra={"as_json": as_json,
                                        "session_id": result.session_id,
                                        "escalated": result.escalated})
    if as_json:
        print(json.dumps(result.model_dump(), indent=2, ensure_ascii=False, default=str))
        return
    print("=" * 72)
    print(result.final_response or "(no response)")
    print("=" * 72)
    print(
        f"session={result.session_id[:8]}  domain={result.domain_classification}  "
        f"models={','.join(result.selected_model_ids)}  retries={result.retry_count}  "
        f"escalated={result.escalated}  audit_events={result.audit_event_count}"
    )


def _cmd_run(args: argparse.Namespace) -> int:
    """Implement ``case-engine run``.

    Builds the engine, optionally bypasses the router with ``--models``
    (comma-separated, upper-cased), executes one pipeline run and prints
    the result.  Exit code ``1`` when the session escalated (reasoning
    failure surfaced to a human), else ``0``.

    Side effects: DEBUG-only log records; result printed to stdout;
    audit ledger written per ``--ledger``/``--ledger-path``.

    Args:
        args: Parsed arguments.

    Returns:
        Process exit code.

    Raises:
        CASEError: Infrastructure failures propagate to :func:`main`,
            which converts them to exit code ``1`` + stderr message.
    """
    engine = _make_engine(args)
    forced = [m.strip().upper() for m in args.models.split(",")] if args.models else None
    log.debug(
        "run command invoked",
        extra={"query_preview": args.query[:120], "domain": args.domain,
               "forced_model_ids": forced, "as_json": args.json},
    )
    result = engine.run(
        args.query,
        domain_hint=args.domain,
        forced_model_ids=forced,
    )
    _print_result(result, args.json)
    return _EXIT_OK if not result.escalated else EXIT_FAIL


def _cmd_demo(args: argparse.Namespace) -> int:
    """Implement ``case-engine demo`` (fully offline).

    Runs the packaged mock provider against the sample IoT-telemetry
    query, writes a JSONL ledger at ``--ledger-path`` and verifies the
    chain afterwards.  With ``--reflexion`` the mock is scripted to fail
    its first tool call so the reject-and-regenerate loop is exercised.

    Side effects: DEBUG-only log records; result and chain summary
    printed to stdout; ledger file created/overwritten.

    Args:
        args: Parsed arguments.

    Returns:
        Process exit code (``1`` when the demo escalates).

    Raises:
        CASEError: Infrastructure failures propagate to :func:`main`.
    """
    log.debug(
        "demo command invoked",
        extra={"ledger_path": args.ledger_path, "reflexion": args.reflexion,
               "as_json": args.json},
    )
    registry = CognitiveRegistry.load_default()
    mock = MockLLMClient.from_registry_examples(registry)
    if args.reflexion:
        mock.fail_next(times=1, mode="invalid_args")
    engine = CASEEngine(
        config=EngineConfig(
            guardrails=GlobalGuardrails(max_reflexion_retries=3),
            ledger_backend="jsonl",
            ledger_path=args.ledger_path,
        ),
        llm_client=mock,
    )
    result = engine.run(
        "SycliQ Zone 4 soil sensors are reporting anomalous moisture spikes; "
        "diagnose the anomaly and plan the response."
    )
    _print_result(result, args.json)
    verification = engine.verify_audit_chain()
    log.debug("demo chain verified", extra={"valid": verification.valid,
                                            "total_entries": verification.total_entries})
    print(f"Audit ledger: {args.ledger_path} — chain valid: {verification.valid} "
          f"({verification.total_entries} events)")
    return _EXIT_OK if not result.escalated else EXIT_FAIL


def _cmd_models(args: argparse.Namespace) -> int:
    """Implement ``case-engine models``.

    Lists every registered mental model as a fixed-width table (or a
    JSON array with ``--json``).  With ``--domain`` the rows are
    filtered to the models the router would pick for that domain
    (``top_n=99`` → effectively all with affinity).

    Side effects: DEBUG-only log records; table/JSON printed to stdout.

    Args:
        args: Parsed arguments.

    Returns:
        Process exit code (always ``0``).
    """
    log.debug("models command invoked", extra={"domain": args.domain,
                                               "as_json": args.json})
    registry = CognitiveRegistry.load_default()
    rows = [
        {"algorithm_id": m.algorithm_id, "name": m.algorithm_name,
         "category": m.category, "threshold": m.confidence_threshold,
         "rules": ",".join(m.validation_rules)}
        for m in registry.all_models()
    ]
    if args.domain:
        router = ModelRouter(registry)
        selected = {m.algorithm_id for m in router.select(args.domain, top_n=99)}
        rows = [r for r in rows if r["algorithm_id"] in selected]
    log.debug("registry rows prepared", extra={"count": len(rows),
                                               "domain_filter": bool(args.domain)})
    if args.json:
        print(json.dumps(rows, indent=2))
        return _EXIT_OK
    print(f"{'ID':<7} {'MODEL':<32} {'CATEGORY':<48} {'THR':<5} RULES")
    for r in rows:
        print(f"{r['algorithm_id']:<7} {r['name']:<32} {r['category']:<48} "
              f"{r['threshold']:<5} {r['rules']}")
    return _EXIT_OK


def _cmd_show(args: argparse.Namespace) -> int:
    """Implement ``case-engine show``.

    Prints the complete registry entry for one mental model as
    pretty-printed JSON (the same dump returned by
    :meth:`case_engine.engine.CASEEngine.describe_model`).

    Side effects: DEBUG-only log records; JSON printed to stdout.

    Args:
        args: Parsed arguments.

    Returns:
        Process exit code (always ``0`` on success).

    Raises:
        RegistryError: Unknown model id — a :class:`CASEError` subclass,
            so :func:`main` converts it to exit code ``1`` with a
            stderr message.
    """
    log.debug("show command invoked", extra={"model_id": args.model_id})
    registry = CognitiveRegistry.load_default()
    entry = registry.get(args.model_id.upper())
    print(json.dumps(entry.model_dump(), indent=2, ensure_ascii=False))
    return _EXIT_OK


def _cmd_verify(args: argparse.Namespace) -> int:
    """Implement ``case-engine verify``.

    Replays the ledger at ``--ledger-path`` and recomputes the hash
    chain; prints ``OK`` or ``TAMPERED`` (plus the first invalid
    sequence number).  This is the operator's tamper-evidence sweep —
    the same logic as
    :meth:`case_engine.engine.CASEEngine.verify_audit_chain`.

    Side effects: DEBUG-only log records; verdict printed to stdout.

    Args:
        args: Parsed arguments.

    Returns:
        Process exit code (0 when the chain is valid).

    Raises:
        LedgerError: Unreadable or corrupt ledger (propagates to
            :func:`main` → exit code ``1``).
    """
    log.debug("verify command invoked", extra={"backend": args.ledger,
                                               "ledger_path": args.ledger_path})
    ledger = create_ledger(args.ledger, args.ledger_path)
    verification = ledger.verify_chain()
    ledger.close()
    log.debug("ledger chain verified", extra={"valid": verification.valid,
                                              "total_entries": verification.total_entries,
                                              "first_invalid_seq": verification.first_invalid_seq})
    if verification.valid:
        print(f"OK — {verification.total_entries} events verified, hash chain intact "
              f"({args.ledger_path})")
        return _EXIT_OK
    print(f"TAMPERED — {verification.reason} (first bad seq: {verification.first_invalid_seq})")
    return EXIT_FAIL


def _cmd_config(_args: argparse.Namespace) -> int:
    """Implement ``case-engine config``.

    Resolves the effective configuration (explicit path →
    ``CASE_ENGINE_CONFIG`` env var → defaults) and prints it as JSON so
    operators can diff what the engine would actually use.  This
    command never logs configuration *values* — the loader logs file
    paths and section names only.

    Side effects: DEBUG-only log record; JSON printed to stdout.

    Args:
        _args: Parsed arguments (unused).

    Returns:
        Process exit code (always ``0``).
    """
    log.debug("config command invoked")
    config = EngineConfig.load()
    print(json.dumps(config.model_dump(), indent=2, ensure_ascii=False))
    return _EXIT_OK


def _cmd_history(args: argparse.Namespace) -> int:
    """Implement ``case-engine history`` (G12 result store).

    Reads a SQLite result store written by ``run --result-store`` and
    prints stored session results newest-first: one summary line per
    session (``--session`` prints the full result for one id, ``--json``
    emits machine-readable output).  The store is an operational index;
    the audit ledger remains the authoritative record.

    Side effects: DEBUG-only log records; results printed to stdout.

    Args:
        args: Parsed arguments (``store``, ``since``, ``limit``,
            ``session``, ``json``).

    Returns:
        Process exit code: ``0`` when sessions were found (or a single
        requested session), ``1`` when the store is empty / the session
        is missing / the store cannot be opened.
    """
    from case_engine.storage import SQLiteResultStore, StorageError

    log.debug("history command invoked",
              extra={"store": args.store, "since": args.since,
                     "limit": args.limit, "session": args.session})
    try:
        store = SQLiteResultStore(args.store)
    except StorageError as exc:
        print(f"ERROR: cannot open result store: {exc}", file=sys.stderr)
        return EXIT_FAIL
    try:
        if args.session:
            result = store.get(args.session)
            if result is None:
                print(f"No stored session {args.session!r}", file=sys.stderr)
                return EXIT_FAIL
            if args.json:
                print(json.dumps(result.model_dump(), indent=2,
                                 ensure_ascii=False, default=str))
            else:
                _print_result(result, as_json=False)
            return _EXIT_OK
        limit = None if args.limit == 0 else args.limit
        rows = list(store.iter_since(since_iso=args.since, limit=limit))
        if not rows:
            print("(no stored sessions)")
            return EXIT_FAIL
        if args.json:
            print(json.dumps([r.model_dump() for r in rows], indent=2,
                             ensure_ascii=False, default=str))
        else:
            for r in rows:
                print(
                    f"{r.started_at}  {r.session_id[:8]}  "
                    f"domain={r.domain_classification}  "
                    f"models={','.join(r.selected_model_ids)}  "
                    f"retries={r.retry_count}  valid={r.all_valid}  "
                    f"escalated={r.escalated}  tokens={r.token_usage.total_tokens}"
                )
        return _EXIT_OK
    finally:
        store.close()


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Implement ``case-engine doctor`` (G17).

    Runs three offline health checks and reports each as
    ``[ok]/[warn]/[fail]`` (or a JSON document with ``--json``):

    * **registry** — loads the default cognitive registry, then checks
      that MM001–MM015 / VR001–VR016 are contiguous and every routing
      domain resolves;
    * **ledger** — verifies the hash chain of ``--ledger-path``
      (missing file is a *warn* for fresh installs, not a failure);
    * **provider** — ``mock`` is always ok; network providers report
      *configured* and, with ``--ping``, construct the client (SDK
      import only — deliberately **no API call**, so doctor never
      spends tokens or needs secrets).

    Exit code is ``0`` when nothing failed (warnings allowed), ``1``
    when any check failed.

    Side effects: DEBUG-only log records; report printed to stdout;
    read-only with respect to the ledger.

    Args:
        args: Parsed arguments (``ledger``, ``ledger_path``,
            ``provider``, ``ping``, ``json``).

    Returns:
        Process exit code.
    """
    checks: list[dict[str, str]] = []

    def _check(name: str, status: str, detail: str) -> None:
        """Record one check result (internal helper)."""
        checks.append({"check": name, "status": status, "detail": detail})
        log.debug("doctor check", extra={"check": name, "status": status})

    # --- 1. registry -----------------------------------------------------
    try:
        registry = CognitiveRegistry.load_default()
        models = registry.all_models()
        rules = registry.all_rules()
        contiguous_models = [m.algorithm_id for m in models] == [
            f"MM{i:03d}" for i in range(1, len(models) + 1)
        ]
        contiguous_rules = [r.rule_id for r in rules] == [
            f"VR{i:03d}" for i in range(1, len(rules) + 1)
        ]
        domains = sorted(registry.routing_matrix())
        if contiguous_models and contiguous_rules:
            _check("registry", "ok",
                   f"{len(models)} models (MM001–MM{len(models):03d}), "
                   f"{len(rules)} rules (VR001–VR{len(rules):03d}), "
                   f"{len(domains)} routing domains")
        else:
            _check("registry", "fail",
                   "model/rule ids are not contiguous — registry data corrupted")
    except CASEError as exc:
        _check("registry", "fail", f"registry failed to load: {exc}")

    # --- 2. ledger -------------------------------------------------------
    ledger_path = Path(args.ledger_path)
    if args.ledger == "memory":
        _check("ledger", "warn",
               "in-memory ledger selected — audit evidence is not persisted")
    elif not ledger_path.exists():
        _check("ledger", "warn",
               f"no ledger at {ledger_path} yet (normal for fresh installs)")
    else:
        try:
            ledger = create_ledger(args.ledger, str(ledger_path))
            verification = ledger.verify_chain()
            ledger.close()
            if verification.valid:
                _check("ledger", "ok",
                       f"{verification.total_entries} events, hash chain intact")
            else:
                _check("ledger", "fail",
                       f"TAMPERED: {verification.reason} "
                       f"(first bad seq: {verification.first_invalid_seq})")
        except CASEError as exc:
            _check("ledger", "fail", f"cannot verify ledger: {exc}")

    # --- 3. provider -----------------------------------------------------
    if args.provider == "mock":
        _check("provider", "ok", "mock provider — fully offline, always available")
    else:
        if not args.ping:
            _check("provider", "warn",
                   f"{args.provider} configured; pass --ping to construct "
                   f"the client (no API call)")
        else:
            try:
                from case_engine.llm import create_llm_client
                create_llm_client(args.provider)
                _check("provider", "ok",
                       f"{args.provider} client constructed (no API call made)")
            except Exception as exc:
                _check("provider", "fail",
                       f"{args.provider} client construction failed: "
                       f"{type(exc).__name__}")

    healthy = all(c["status"] != "fail" for c in checks)
    log.debug("doctor finished", extra={"healthy": healthy, "checks": len(checks)})
    if args.json:
        print(json.dumps({"healthy": healthy, "checks": checks},
                         indent=2, ensure_ascii=False))
    else:
        for c in checks:
            print(f"[{c['status'].upper():4}] {c['check']}: {c['detail']}")
        print("doctor: HEALTHY" if healthy else "doctor: UNHEALTHY")
    return _EXIT_OK if healthy else EXIT_FAIL


_INIT_TEMPLATE: dict[str, object] = {
    # Schema-pure on purpose: EngineConfig uses extra="forbid", so every
    # key here MUST be a real field (validated by the round-trip below).
    "provider": "mock",
    "llm_kwargs": {},
    "ledger_backend": "jsonl",
    "ledger_path": None,
    "log_level": "INFO",
    "log_json": False,
    "tee_attested": False,
    "redact_pii_in_audit": False,
    "ledger_encryption": False,
    "result_store_backend": "none",
    "result_store_path": None,
    "escalation_sink_kind": "none",
    "escalation_sink_path": None,
}


def _cmd_init(args: argparse.Namespace) -> int:
    """Implement ``case-engine init`` (G17).

    Writes a JSON configuration template matching the
    :class:`~case_engine.config.EngineConfig` schema to ``--path`` so
    operators can start from a valid document instead of guessing key
    names (``extra="forbid"`` rejects typos at load time).  Refuses to
    overwrite an existing file unless ``--force`` is given.

    Side effects: creates the target file; DEBUG-only log records.

    Args:
        args: Parsed arguments (``path``, ``force``).

    Returns:
        Process exit code: ``0`` when the file was written, ``1`` when
        the target exists (no ``--force``) or cannot be written.
    """
    log.debug("init command invoked", extra={"path": args.path, "force": args.force})
    target = Path(args.path)
    if target.exists() and not args.force:
        print(f"ERROR: {target} already exists (use --force to overwrite)",
              file=sys.stderr)
        return EXIT_FAIL
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(_INIT_TEMPLATE, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        log.error("cannot write config template",
                  extra={"path": str(target)}, exc_info=True)
        print(f"ERROR: cannot write {target}: {exc}", file=sys.stderr)
        return EXIT_FAIL
    # Prove the template round-trips through the strict loader.
    try:
        EngineConfig.from_file(target)
    except (CASEError, ValidationError) as exc:
        print(f"ERROR: template failed validation: {exc}", file=sys.stderr)
        return EXIT_FAIL
    log.debug("config template written", extra={"path": str(target)})
    print(f"Wrote {target} (validated against EngineConfig).")
    print("Next steps:")
    print("  1. edit values (every key is a real EngineConfig field —")
    print("     unknown keys are rejected at load time)")
    print("  2. point CASE_ENGINE_CONFIG at the file, or pass it to")
    print("     EngineConfig.from_file() in code")
    return _EXIT_OK


_CSV_COLUMNS = (
    "session_id", "started_at", "finished_at", "domain_classification",
    "selected_model_ids", "retry_count", "all_valid", "escalated",
    "total_tokens",
)


def _cmd_export(args: argparse.Namespace) -> int:
    """Implement ``case-engine export`` (G17).

    Streams session results out of a G12 SQLite result store into JSON
    (full documents) or CSV (one row per session, projected columns)
    to ``--out`` (``-`` = stdout).  Complements ``history`` (human
    listing) with a machine-consumable dump for spreadsheets/SIEMs.

    Side effects: DEBUG-only log records; output written to stdout or
    the ``--out`` file.

    Args:
        args: Parsed arguments (``store``, ``format``, ``since``,
            ``limit``, ``out``).

    Returns:
        Process exit code: ``0`` when rows were exported, ``1`` when
        the store is empty or cannot be opened.
    """
    from case_engine.storage import SQLiteResultStore, StorageError

    log.debug("export command invoked",
              extra={"store": args.store, "format": args.format,
                     "since": args.since, "limit": args.limit, "out": args.out})
    try:
        store = SQLiteResultStore(args.store)
    except StorageError as exc:
        print(f"ERROR: cannot open result store: {exc}", file=sys.stderr)
        return EXIT_FAIL
    try:
        rows = list(store.iter_since(since_iso=args.since, limit=args.limit))
    finally:
        store.close()
    if not rows:
        print("(no stored sessions to export)", file=sys.stderr)
        return EXIT_FAIL

    out = sys.stdout if args.out == "-" else None
    try:
        if out is None:
            out_file = Path(args.out)
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out = out_file.open("w", encoding="utf-8", newline="")
        if args.format == "json":
            json.dump([r.model_dump() for r in rows], out,
                      indent=2, ensure_ascii=False, default=str)
            out.write("\n")
        else:
            import csv
            writer = csv.DictWriter(out, fieldnames=list(_CSV_COLUMNS))
            writer.writeheader()
            for r in rows:
                writer.writerow({
                    "session_id": r.session_id,
                    "started_at": r.started_at,
                    "finished_at": r.finished_at,
                    "domain_classification": r.domain_classification,
                    "selected_model_ids": ",".join(r.selected_model_ids),
                    "retry_count": r.retry_count,
                    "all_valid": int(r.all_valid),
                    "escalated": int(r.escalated),
                    "total_tokens": r.token_usage.total_tokens,
                })
    except OSError as exc:
        log.error("export write failed", extra={"out": args.out}, exc_info=True)
        print(f"ERROR: cannot write export: {exc}", file=sys.stderr)
        return EXIT_FAIL
    finally:
        if out is not None and out is not sys.stdout:
            out.close()
    log.debug("export finished", extra={"rows": len(rows), "format": args.format})
    return _EXIT_OK


_COMMANDS = {
    "run": _cmd_run,
    "demo": _cmd_demo,
    "models": _cmd_models,
    "show": _cmd_show,
    "verify": _cmd_verify,
    "config": _cmd_config,
    "history": _cmd_history,
    "doctor": _cmd_doctor,
    "init": _cmd_init,
    "export": _cmd_export,
}


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point.

    Installs engine logging at ``WARNING`` (``ERROR`` with ``--quiet``)
    on **stderr** — deliberately above INFO, and deliberately never on
    stdout, so the JSON a ``--json`` consumer parses from stdout stays
    byte-pure regardless of any engine diagnostics — then dispatches to
    the selected command handler.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code: ``0`` success, ``1`` reasoning/verification
        failure or caught :class:`CASEError`, ``2`` usage error (raised
        by argparse itself as ``SystemExit``).

    Raises:
        SystemExit: Propagated from argparse for malformed arguments
            (exit code 2).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    configure_logging(
        "ERROR" if getattr(args, "quiet", False) else "WARNING",
        stream=sys.stderr,
    )
    log.debug("cli dispatch", extra={"command": args.command,
                                     "quiet": getattr(args, "quiet", False)})
    try:
        return _COMMANDS[args.command](args)
    except CASEError as exc:
        log.debug("command failed with CASEError", extra={"error": str(exc)[:200]})
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except KeyboardInterrupt:
        log.debug("cli interrupted by user")
        print("Interrupted.", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
