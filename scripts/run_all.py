"""
Phase 3 - Run the whole pipeline in order  (Week 12)
====================================================

One command that runs every step from raw extracts to answered business
questions, in the only order that works:

    ingest.py     PSA OpenSTAT API  ->  data/raw/            (optional)
    transform.py  data/raw/         ->  data/processed/
    load_db.py    data/processed/   ->  underemployment.db
    run_sql.py    sql/              ->  output/sql_results.md

WHY AN ORCHESTRATOR AND NOT JUST A LIST IN THE README
-----------------------------------------------------
The README lists the same four commands, and a reviewer can run them by hand.
The difference is that here the ORDER IS EXECUTABLE. A written list can drift
from reality - a step gets added, renamed, or reordered and the prose quietly
stops matching. This file cannot drift, because it is what actually runs.

It also fails fast. Running the steps by hand, a failed transform still leaves
the previous database and results sitting there looking current; a reviewer who
missed the error reads stale output as fresh. Here, step two failing means step
three never runs.

WHY INGEST IS OFF BY DEFAULT
----------------------------
data/raw/ is committed on purpose - the Milestone 2 deliverable requires it, and
it is what makes the rest of the pipeline runnable without network access. So the
default run reproduces everything FROM the committed extracts, which is what a
reviewer wants: same inputs, same outputs, no dependency on PSA's API being up.

Pass --with-ingest to re-pull from the API. That overwrites data/raw/ with a
fresh pull under today's date, which is a real change to the inputs, so it is
opt-in rather than something that happens because someone ran the default.

ETL, NOT ELT
------------
The data is transformed BEFORE it is loaded into the database: transform.py
reshapes and validates, and only the clean result reaches SQLite. The alternative
(ELT) would load the raw extracts and reshape in SQL. ETL is right here because
the three raw layouts - one long, two wide, one with paired-year columns - are
far easier to reconcile in pandas than in SQL, and because validating before
loading means the database never holds a row that failed a check.

Usage
-----
    python scripts/run_all.py                 # transform -> load -> query
    python scripts/run_all.py --with-ingest    # re-pull from the PSA API first
    python scripts/run_all.py --check          # also verify reproducibility
    python scripts/run_all.py --list           # show the steps, run nothing
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = PROJECT_ROOT / "scripts"


@dataclass(frozen=True)
class Step:
    name: str
    script: str
    flow: str
    args: tuple[str, ...] = ()
    optional: bool = False

    @property
    def path(self) -> Path:
        return SCRIPTS / self.script


STEPS: list[Step] = [
    Step("ingest", "ingest.py", "PSA OpenSTAT API -> data/raw/", optional=True),
    Step("transform", "transform.py", "data/raw/ -> data/processed/"),
    Step("load_db", "load_db.py", "data/processed/ -> underemployment.db"),
    Step("run_sql", "run_sql.py", "sql/ -> output/sql_results.md"),
]

CHECK_STEP = Step("verify", "transform.py", "rebuild elsewhere and compare",
                  args=("--check-reproducible",))


def run_step(step: Step) -> float:
    """Run one step, streaming its output. Raises on a non-zero exit."""
    started = time.monotonic()
    print(f"\n{'=' * 78}\n  {step.name}   {step.flow}\n{'=' * 78}")

    # sys.executable, not "python": this guarantees the step runs under the same
    # interpreter as the orchestrator, so a reviewer who forgot to activate the
    # virtualenv gets a consistent environment rather than a confusing
    # ModuleNotFoundError from a different Python.
    completed = subprocess.run([sys.executable, str(step.path), *step.args],
                               cwd=PROJECT_ROOT)
    elapsed = time.monotonic() - started

    if completed.returncode != 0:
        raise SystemExit(
            f"\n[FAILED] {step.script} exited {completed.returncode} after "
            f"{elapsed:.1f}s.\n"
            f"    Later steps were NOT run - they would have produced output from "
            f"stale inputs.\n"
            f"    Fix the error above, then re-run 'python scripts/run_all.py'."
        )
    return elapsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the full pipeline: raw -> processed -> database -> answers.")
    parser.add_argument("--with-ingest", action="store_true",
                        help="re-pull from the PSA API first (network; overwrites data/raw/)")
    parser.add_argument("--check", action="store_true",
                        help="after building, verify the transform is reproducible")
    parser.add_argument("--list", action="store_true",
                        help="show the steps in order and exit")
    args = parser.parse_args(argv)

    planned = [s for s in STEPS if not s.optional or args.with_ingest]
    if args.check:
        planned = [*planned, CHECK_STEP]

    if args.list:
        print("pipeline order:\n")
        for number, step in enumerate(planned, start=1):
            print(f"  {number}. {step.name:<10} {step.script:<15} {step.flow}")
        skipped = [s for s in STEPS if s not in planned]
        if skipped:
            print(f"\n  skipped (pass --with-ingest to include): "
                  f"{', '.join(s.name for s in skipped)}")
        return 0

    missing = [step.script for step in planned if not step.path.exists()]
    if missing:
        raise SystemExit(f"Missing script(s) in scripts/: {missing}")

    if not args.with_ingest:
        print("using the committed extracts in data/raw/ "
              "(pass --with-ingest to re-pull from the PSA API)")

    timings = [(step.name, run_step(step)) for step in planned]

    print(f"\n{'=' * 78}")
    print("  pipeline complete")
    print(f"{'=' * 78}")
    for name, elapsed in timings:
        print(f"  {name:<12} {elapsed:6.1f}s")
    print(f"  {'total':<12} {sum(t for _, t in timings):6.1f}s")
    print("\n  processed dataset  data/processed/*.csv")
    print("  answered questions output/sql_results.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
