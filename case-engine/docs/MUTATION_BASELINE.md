# Mutation testing baseline (G28)

Rule logic is legal-ish code; example-based tests cannot prove a
surviving ``<=`` → ``<`` flip can't slip through. This baseline is
regenerated **once per release** (not per PR) with:

```bash
python tools/mutation_run.py
```

| field | value |
| --- | --- |
| targets | `case_engine/validation/rules.py`, `case_engine/enforcement/gate.py` |
| tests | `tests/unit/test_validation_rules.py`, `tests/unit/test_gate_and_factory.py` |
| mutation sites | 100 |
| killed | 98 |
| survived | 2 |
| stillborn (syntax-invalid) | 0 |
| timeout (counted killed) | 0 |
| **mutation score** | **0.98** |

## Justified equivalent mutants (no action required)

- `case_engine/validation/rules.py:643:52 '>'->'>='` — VR008 numbered-list heuristic: when numbered==1 the segment count always dominates max(len(segments), ...), so the flip cannot change any verdict or message (segments >= 1 whenever numbered >= 1).
- `case_engine/enforcement/gate.py:181:48 'False'->'True'` — exc.errors(include_url=False) — the flattened error strings only read loc/msg, so include_url has no observable effect (pydantic URL key discarded).

No *unexplained* surviving mutants. Any new survivor in a
future run is a release blocker: either strengthen the
rules tests or justify it in ``JUSTIFIED_EQUIVALENTS``.
