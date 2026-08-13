"""
Phase 3 - Load the processed CSVs into SQLite  (Week 8)
=======================================================

Builds data/processed/underemployment.db from the four CSVs written by
transform.py, using explicit DDL so the schema designed in
data/data_dictionary.md is ENFORCED rather than merely described:

    declared column types
    PRIMARY KEY on every table, composite on the fact
    FOREIGN KEY from the fact to both dimensions
    PRAGMA foreign_keys = ON

That last line matters: SQLite parses FOREIGN KEY clauses but does not enforce
them unless the pragma is set per connection. Without it the constraints are
decoration.

The database is a BUILD ARTEFACT. It is dropped and rebuilt on every run and is
gitignored - the CSVs are the committed deliverable and the source of truth.

Usage
-----
    python scripts/load_db.py
    python scripts/load_db.py --check     # rebuild, then report row counts
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
DB_PATH = PROCESSED_DIR / "underemployment.db"

# REAL, not INTEGER, for every measure: they are rates and percentages.
# Nullable on purpose where a value can be genuinely absent - a quarter with no
# survey round is not a zero, and NOT NULL here would force us to invent one.
SCHEMA = """
CREATE TABLE dim_quarter (
    quarter_id         TEXT    NOT NULL PRIMARY KEY,
    year               INTEGER NOT NULL,
    quarter_num        INTEGER NOT NULL CHECK (quarter_num BETWEEN 1 AND 4),
    quarter_start_date TEXT    NOT NULL,
    quarter_end_date   TEXT    NOT NULL,
    lfs_round_month    TEXT    NOT NULL
);

CREATE TABLE dim_indicator (
    indicator_code    TEXT NOT NULL PRIMARY KEY,
    indicator_label   TEXT NOT NULL,
    unit              TEXT NOT NULL,
    source_dataset    TEXT NOT NULL,
    source_table_id   TEXT NOT NULL,
    native_frequency  TEXT NOT NULL,
    aggregation_method TEXT NOT NULL,
    is_model_input    TEXT NOT NULL CHECK (is_model_input IN ('true', 'false'))
);

CREATE TABLE fact_indicator_quarter (
    quarter_id     TEXT NOT NULL,
    indicator_code TEXT NOT NULL,
    value          REAL,
    value_status   TEXT NOT NULL CHECK (value_status IN ('observed','derived','missing')),
    source_file    TEXT NOT NULL,
    PRIMARY KEY (quarter_id, indicator_code),
    FOREIGN KEY (quarter_id)     REFERENCES dim_quarter (quarter_id),
    FOREIGN KEY (indicator_code) REFERENCES dim_indicator (indicator_code)
);

CREATE INDEX idx_fact_indicator ON fact_indicator_quarter (indicator_code);
"""

# analysis_quarterly is created from its CSV header rather than hardcoded DDL:
# it is a pivot whose column list grows as indicators are added, and duplicating
# that list here would just be a second place to forget to update.
ANALYSIS_KEY = "quarter_id"
ANALYSIS_TEXT_COLUMNS = {"quarter_id", "quarter_start_date"}
ANALYSIS_INT_COLUMNS = {"year", "quarter_num"}


def read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    if not path.exists():
        raise SystemExit(
            f"Missing {path.name}. Run 'python scripts/transform.py' first."
        )
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    return rows[0], rows[1:]


def to_value(cell: str) -> str | float | None:
    """Empty cell -> SQL NULL. transform.py writes '' for a genuine absence."""
    return None if cell == "" else cell


def load_table(connection: sqlite3.Connection, table: str, path: Path,
               numeric_from: int = 0) -> int:
    """Insert a CSV into an existing table, converting blanks to NULL.

    numeric_from is the first column index whose blanks mean NULL rather than an
    empty string - everything before it is a key or label that is never blank.
    """
    header, rows = read_csv(path)
    placeholders = ", ".join("?" * len(header))
    prepared = [
        [to_value(cell) if i >= numeric_from else cell for i, cell in enumerate(row)]
        for row in rows
    ]
    connection.executemany(
        f"INSERT INTO {table} ({', '.join(header)}) VALUES ({placeholders})", prepared
    )
    return len(prepared)


def create_analysis_table(connection: sqlite3.Connection, header: list[str]) -> None:
    columns = []
    for name in header:
        if name in ANALYSIS_TEXT_COLUMNS:
            declared = "TEXT NOT NULL"
        elif name in ANALYSIS_INT_COLUMNS:
            declared = "INTEGER NOT NULL"
        else:
            declared = "REAL"          # nullable: lags and diffs are absent at the start
        if name == ANALYSIS_KEY:
            declared += " PRIMARY KEY"
        columns.append(f"    {name} {declared}")
    connection.execute(
        "CREATE TABLE analysis_quarterly (\n" + ",\n".join(columns) + "\n)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load data/processed CSVs into SQLite.")
    parser.add_argument("--check", action="store_true",
                        help="after loading, print row counts and key integrity")
    args = parser.parse_args(argv)

    # Rebuild from scratch every run. An incrementally-updated database drifts
    # from the CSVs, and then it is unclear which one a query result came from.
    DB_PATH.unlink(missing_ok=True)

    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA)

        counts = {
            "dim_quarter": load_table(
                connection, "dim_quarter", PROCESSED_DIR / "dim_quarter.csv"),
            "dim_indicator": load_table(
                connection, "dim_indicator", PROCESSED_DIR / "dim_indicator.csv"),
            # value is column 2; blanks there are real absences and become NULL
            "fact_indicator_quarter": load_table(
                connection, "fact_indicator_quarter",
                PROCESSED_DIR / "fact_indicator_quarter.csv", numeric_from=2),
        }

        analysis_path = PROCESSED_DIR / "analysis_quarterly.csv"
        header, _ = read_csv(analysis_path)
        create_analysis_table(connection, header)
        counts["analysis_quarterly"] = load_table(
            connection, "analysis_quarterly", analysis_path, numeric_from=1)

        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise SystemExit(
                f"Foreign key violations after load: {violations[:5]}"
            )

        print(f"built {DB_PATH.relative_to(PROJECT_ROOT)}")
        for table, count in counts.items():
            print(f"  {table:<26} {count:>5} rows")
        print("  foreign key check           clean")

        if args.check:
            print("\nindicator coverage:")
            query = """
                SELECT d.indicator_code,
                       COUNT(f.value)                        AS present,
                       COUNT(*) - COUNT(f.value)             AS missing
                FROM dim_indicator d
                LEFT JOIN fact_indicator_quarter f
                       ON f.indicator_code = d.indicator_code
                GROUP BY d.indicator_code
                ORDER BY missing DESC, d.indicator_code
            """
            for code, present, missing in connection.execute(query):
                print(f"  {code:<30} {present:>3} present  {missing:>3} missing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
