"""C.A.S.E. adversarial test pack (G23).

This sub-package is the curated prompt-injection corpus and its
regression harness: a corpus of known-bad prompts
(:mod:`tests.adversarial.corpus`) plus a standalone report runner
(:mod:`tests.adversarial.report`) that replays every payload through a
fully wired offline engine and prints the verdict table.

The pack exists because the constitution is a defence *claim*; running
~30 injection payloads against node_4 → node_5 on every test run turns
that claim into a regression-tested invariant.  It is NOT packaged
inside the runtime ``case_engine`` module — adversarial content never
ships inside the library itself.

Cross-references: ``docs/GAP_ANALYSIS.md`` §G23,
``docs/CONTEXT_ENGINEERING.md`` (CE-11 adversarial corpus).
"""
