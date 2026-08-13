"""
Phase 3 - Data Transformation  (first version, Week 8)
======================================================

Reshapes the six raw PSA OpenSTAT extracts in data/raw/ into the processed layer
designed in data/data_dictionary.md:

    dim_quarter.csv              one row = one calendar quarter
    dim_indicator.csv            one row = one indicator
    fact_indicator_quarter.csv   one row = one indicator's value for one quarter
    analysis_quarterly.csv       one row = one quarter, indicators as columns

WHY THIS VERSION USES ONLY THE STANDARD LIBRARY
-----------------------------------------------
Week 8 is the SQL week and Week 9 is the pandas week. Nothing here needs a
dataframe: the reshaping is an unpivot and some lagged arithmetic, both of which
the csv module handles in a few lines. Keeping this version dependency-free means
the SQL layer can be built and re-run by a reviewer with a bare Python install.
Week 9 ports this to pandas and adds profiling - that port is the exercise, not
busywork, and the diff is the record of it.

THE THREE RESHAPING PROBLEMS
----------------------------
1. LFS is LONG   - Year/Month down the rows. Quarterly rounds through 2020,
                   monthly from 2021, plus an "Annual" aggregate row every year.
2. GDP/CPI are WIDE - one data row with 100+ period columns. They have to be
                   unpivoted before anything can be joined to them.
3. Nothing shares a key - the extracts only meet once each is reshaped onto a
                   common quarter_id. That is what fact_indicator_quarter is.

THE RULES THIS SCRIPT ENCODES  (reasoning in data/data_dictionary.md)
---------------------------------------------------------------------
  * A quarter's LFS value is its ROUND-MONTH value: Jan/Apr/Jul/Oct -> Q1..Q4.
    One estimator for all 83 quarters. Averaging the three months where they
    exist from 2021 would change the estimator mid-series and manufacture a 2021
    level shift that is an artefact of cleaning, not of the labour market.
  * "Annual" (LFS) and "Ave" (CPI) rows are ANNUAL AGGREGATES, not periods.
    Including them double-counts. They are dropped explicitly, not by accident.
  * GDP growth is YEAR-ON-YEAR and its Year values are PAIRS: "2025-2026 Q1" is
    Q1 of 2026. The SECOND year is the observation year. An off-by-one here is
    silent and fatal, so there is a canary assert on a known published figure.
  * Missing values arrive as "." or ".." and mean the survey did not run or the
    figure is unpublished. They are NOT zeros and are never imputed.
  * The window is 2005Q2-2025Q4: 83 quarters, no gaps. Bounded below by the
    start of the LFS (April 2005) and above by the end of CPI coverage.

Usage
-----
    python scripts/transform.py
    python scripts/transform.py --quiet
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

# Reuse the raw-reading helpers rather than reimplementing them. read_pxweb_csv
# already knows about PXWeb's UTF-8 BOM, and MISSING_MARKERS already lists every
# missing-value spelling this source uses - including "..", which the GDP levels
# table uses for unpublished quarters where LFS uses ".".
from ingest import MISSING_MARKERS, RAW_DATA_DIR, TABLES, is_missing, read_pxweb_csv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
MANIFEST_PATH = RAW_DATA_DIR / "_manifest.json"
REPORT_PATH = PROCESSED_DIR / "_transform_report.json"

# The modelling window. Both ends are data facts, not preferences:
#   2005Q2 - the LFS began in April 2005, so there is no 2005Q1 round.
#   2025Q4 - CPI coverage ends December 2025. LFS runs to May 2026 and GDP to
#            2026Q1, but a quarter is only usable when all three exist.
WINDOW_START = "2005Q2"
WINDOW_END = "2025Q4"

# Tolerance for our computed inflation against PSA's published figure. Not zero:
# we compute year-on-year change on the quarterly MEAN index, while PSA publishes
# monthly year-on-year rates that we average. Those are close but not identical.
INFLATION_TOLERANCE_PP = 0.20

# PSA's published GDP growth for 2026 Q1, used as a canary that the year-pair
# columns are being read with the correct year. Documented in the data dictionary.
GDP_CANARY_QUARTER = "2026Q1"
GDP_CANARY_VALUE = 2.8

ROUND_MONTH_TO_QUARTER = {"January": 1, "April": 2, "July": 3, "October": 4}
QUARTER_TO_ROUND_MONTH = {q: m for m, q in ROUND_MONTH_TO_QUARTER.items()}
MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class TransformError(Exception):
    """A data problem the user can act on. main() prints it without a traceback."""


# --------------------------------------------------------------------------- #
# Quarter arithmetic
# --------------------------------------------------------------------------- #
#
# Quarters are held as a single integer index so that "one quarter earlier" and
# "four quarters earlier" are plain subtraction. Doing this with (year, quarter)
# tuples invites off-by-one errors at every year boundary.


def qindex(year: int, quarter: int) -> int:
    return year * 4 + (quarter - 1)


def qid(year: int, quarter: int) -> str:
    return f"{year}Q{quarter}"


def qid_from_index(index: int) -> str:
    return qid(index // 4, index % 4 + 1)


def index_from_qid(quarter_id: str) -> int:
    year, quarter = quarter_id.split("Q")
    return qindex(int(year), int(quarter))


def quarter_bounds(index: int) -> tuple[date, date]:
    year, quarter = index // 4, index % 4 + 1
    start_month = 3 * (quarter - 1) + 1
    end_month = start_month + 2
    last_day = 31 if end_month in (3, 12) else 30
    return date(year, start_month, 1), date(year, end_month, last_day)


# --------------------------------------------------------------------------- #
# Locating raw files
# --------------------------------------------------------------------------- #


def resolve_latest_raw_files() -> dict[str, list[Path]]:
    """Map each dataset slug to its most recent pull's file(s).

    Read from _manifest.json rather than globbing for a hardcoded date. Raw
    filenames carry the pull date, so hardcoding one means this script silently
    keeps reading a stale extract the next time ingest.py runs. The manifest is
    the record of which pull is current.
    """
    if not MANIFEST_PATH.exists():
        raise TransformError(
            f"No manifest at {MANIFEST_PATH}. Run 'python scripts/ingest.py' first."
        )

    records = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    latest_date: dict[str, str] = {}
    for record in records:
        name, pulled = record["name"], record["retrieved_at_utc"]
        if name not in latest_date or pulled > latest_date[name]:
            latest_date[name] = pulled

    # Keep only the records belonging to each dataset's newest pull, in chunk
    # order so multi-part extracts concatenate as contiguous blocks of periods.
    files: dict[str, list[Path]] = {}
    for record in sorted(records, key=lambda r: r["chunk"]):
        name = record["name"]
        if record["retrieved_at_utc"] != latest_date[name]:
            continue
        path = RAW_DATA_DIR / record["output_file"]
        if not path.exists():
            raise TransformError(
                f"Manifest lists '{path.name}' for dataset '{name}' but the file is "
                f"missing. Re-run 'python scripts/ingest.py'."
            )
        files.setdefault(name, []).append(path)

    missing = [spec.name for spec in TABLES if spec.name not in files]
    if missing:
        raise TransformError(
            f"The manifest has no pull for: {missing}.\n"
            f"    Run 'python scripts/ingest.py' to fetch the missing table(s)."
        )
    return files


def to_number(cell: str) -> float | None:
    """Parse a PXWeb cell. Missing markers become None, never 0.0."""
    if is_missing(cell):
        return None
    try:
        return float(cell.replace(",", ""))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Readers - one per raw layout
# --------------------------------------------------------------------------- #


def read_lfs(paths: list[Path]) -> dict[str, dict[int, float]]:
    """LFS is LONG: Year/Month down the rows, one column per rate.

    Returns {indicator_code: {quarter_index: value}} using ONLY the round months.
    Rows for the other eight months (which exist from 2021) and for the "Annual"
    aggregate are dropped here, which is the single place that rule lives.
    """
    wanted = {
        "Underemployment Rate Both sexes": "underemployment_rate",
        "Unemployment Rate Both sexes": "unemployment_rate",
        "Employment Rate Both sexes": "employment_rate",
        "Labor Force Participation Rate Both sexes": "lfpr",
    }

    series: dict[str, dict[int, float]] = {code: {} for code in wanted.values()}
    seen_annual = 0

    for path in paths:
        header, rows = read_pxweb_csv(path)
        missing_cols = [col for col in wanted if col not in header]
        if missing_cols:
            raise TransformError(
                f"{path.name} is missing expected column(s) {missing_cols}.\n"
                f"    Columns present: {header}"
            )
        position = {col: header.index(col) for col in wanted}
        year_at, month_at = header.index("Year"), header.index("Month")

        for row in rows:
            month = row[month_at]
            if month == "Annual":
                seen_annual += 1        # counted so validation can prove it was dropped
                continue
            if month not in ROUND_MONTH_TO_QUARTER:
                continue                # a non-round month, 2021 onward
            index = qindex(int(row[year_at]), ROUND_MONTH_TO_QUARTER[month])
            for column, code in wanted.items():
                value = to_number(row[position[column]])
                if value is not None:
                    series[code][index] = value

    if not seen_annual:
        raise TransformError(
            "No 'Annual' rows were found in the LFS extract. Every year carries one, "
            "so either the extract is malformed or the aggregate-row filter is no "
            "longer matching - which would mean aggregates are leaking into the data."
        )
    return series


def read_wide(path: Path, pattern: re.Pattern[str]) -> list[tuple[re.Match[str], float | None]]:
    """GDP and CPI are WIDE: one data row, 100+ period columns.

    Returns the regex match for each period column paired with its value, leaving
    the caller to decide what the captured groups mean - the tables disagree about
    that (year pairs vs single years vs year+month).
    """
    header, rows = read_pxweb_csv(path)
    if len(rows) != 1:
        raise TransformError(
            f"{path.name}: expected exactly 1 data row in a wide extract, found "
            f"{len(rows)}. The selection in ingest.py may have widened."
        )

    row = rows[0]
    matched = [(m, to_number(row[i]))
               for i, column in enumerate(header)
               if (m := pattern.search(column))]
    if not matched:
        raise TransformError(
            f"{path.name}: no column matched /{pattern.pattern}/. PXWeb may have "
            f"changed its column labels.\n    First few columns: {header[:4]}"
        )
    return matched


def read_gdp_growth(path: Path) -> dict[int, float]:
    """Year-on-year growth. Columns read '... 2025-2026 Q1'.

    The SECOND year of the pair is the observation year: that column is Q1 2026
    compared with Q1 2025. Reading the first year instead shifts the entire
    predictor a year against the target and nothing about the output looks wrong.
    """
    pattern = re.compile(r"(\d{4})-(\d{4})\s+Q(\d)\s*$")
    series: dict[int, float] = {}
    for match, value in read_wide(path, pattern):
        earlier, later, quarter = int(match[1]), int(match[2]), int(match[3])
        if later != earlier + 1:
            raise TransformError(
                f"{path.name}: year pair '{match[1]}-{match[2]}' is not consecutive. "
                f"The column format has changed and the year-on-year reading is no "
                f"longer safe."
            )
        if value is not None:
            series[qindex(later, quarter)] = value
    return series


def read_gdp_levels(path: Path) -> dict[int, float]:
    """Levels at constant 2018 prices. Columns read '... 2000 Q1'."""
    pattern = re.compile(r"(\d{4})\s+Q(\d)\s*$")
    return {qindex(int(m[1]), int(m[2])): v
            for m, v in read_wide(path, pattern) if v is not None}


def read_cpi_monthly(paths: list[Path]) -> dict[tuple[int, int], float]:
    """Stitch the CPI index legs into one monthly series keyed (year, month).

    Columns read '2018 Jan' ... '2018 Dec' plus '2018 Ave'. "Ave" is the annual
    average sitting alongside the months - an aggregate, not a period - so it is
    excluded by the month lookup rather than by a name check.
    """
    pattern = re.compile(r"(\d{4})\s+([A-Za-z]{3})\s*$")
    month_number = {abbr: i + 1 for i, abbr in enumerate(MONTH_ABBR)}

    monthly: dict[tuple[int, int], float] = {}
    for path in paths:
        for match, value in read_wide(path, pattern):
            month = month_number.get(match[2])
            if month is None or value is None:      # "Ave" lands here and is skipped
                continue
            key = (int(match[1]), month)
            if key in monthly:
                raise TransformError(
                    f"CPI month {key} appears in more than one extract. The backcast "
                    f"and current legs are supposed to abut at 2017-12 / 2018-01, not "
                    f"overlap - stitching them would double-count."
                )
            monthly[key] = value
    return monthly


def quarterly_mean(monthly: dict[tuple[int, int], float]) -> dict[int, float]:
    """Collapse a monthly series to quarters, requiring all three months.

    A quarter built from one or two months is not comparable with one built from
    three, so a partial quarter is dropped rather than silently averaged.
    """
    buckets: dict[int, list[float]] = {}
    for (year, month), value in monthly.items():
        buckets.setdefault(qindex(year, (month - 1) // 3 + 1), []).append(value)
    return {index: sum(values) / 3 for index, values in buckets.items() if len(values) == 3}


# --------------------------------------------------------------------------- #
# Derived series
# --------------------------------------------------------------------------- #


def diff(series: dict[int, float], lag: int) -> dict[int, float]:
    """Difference against `lag` quarters earlier. Absent history yields no row."""
    return {i: v - series[i - lag] for i, v in series.items() if i - lag in series}


def pct_change(series: dict[int, float], lag: int) -> dict[int, float]:
    """Percent change against `lag` quarters earlier."""
    return {i: (v / series[i - lag] - 1) * 100
            for i, v in series.items()
            if i - lag in series and series[i - lag]}


# --------------------------------------------------------------------------- #
# Indicator catalogue - the source of dim_indicator
# --------------------------------------------------------------------------- #

TABLE_IDS = {spec.name: spec.table_id for spec in TABLES}

INDICATORS: list[dict] = [
    # code, label, unit, source dataset, frequency, aggregation, model input?
    ("underemployment_change_qoq", "Underemployment rate, quarter-on-quarter change",
     "pp", "lfs_underemployment", "derived", "computed", False),
    ("underemployment_rate", "Underemployment rate",
     "percent", "lfs_underemployment", "quarterly", "round_month", False),
    ("unemployment_rate", "Unemployment rate",
     "percent", "lfs_underemployment", "quarterly", "round_month", False),
    ("employment_rate", "Employment rate",
     "percent", "lfs_underemployment", "quarterly", "round_month", False),
    ("lfpr", "Labor force participation rate",
     "percent", "lfs_underemployment", "quarterly", "round_month", False),
    ("underemployment_change_yoy", "Underemployment rate, year-on-year change",
     "pp", "lfs_underemployment", "derived", "computed", False),
    ("gdp_growth_yoy", "GDP growth, year-on-year",
     "percent", "qna_gdp_growth", "quarterly", "as_published", True),
    ("gdp_growth_yoy_accel", "GDP growth acceleration, quarter-on-quarter",
     "pp", "qna_gdp_growth", "derived", "computed", True),
    ("gdp_level", "GDP at constant 2018 prices",
     "million_php_const2018", "qna_gdp_levels", "quarterly", "as_published", False),
    ("gdp_growth_qoq", "GDP growth, quarter-on-quarter (NOT seasonally adjusted)",
     "percent", "qna_gdp_levels", "derived", "computed", False),
    ("cpi_index", "Consumer price index",
     "index_2018=100", "cpi_index_2018_2025", "monthly", "quarter_mean", False),
    ("inflation_yoy", "Inflation, year-on-year",
     "percent", "cpi_index_2018_2025", "derived", "computed", True),
    ("inflation_yoy_accel", "Inflation acceleration, quarter-on-quarter",
     "pp", "cpi_index_2018_2025", "derived", "computed", True),
    ("growth_employment_gap", "Growth-employment gap",
     "pp", "qna_gdp_growth", "derived", "computed", False),
]

# Which indicators are published as-is versus computed here. fact.value_status
# carries this so a reader can tell a PSA figure from one of ours.
OBSERVED = {"underemployment_rate", "unemployment_rate", "employment_rate", "lfpr",
            "gdp_growth_yoy", "gdp_level"}


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #


def build_series(files: dict[str, list[Path]]) -> tuple[dict[str, dict[int, float]], dict]:
    """Reshape every extract onto quarter indices and derive the rest."""
    lfs = read_lfs(files["lfs_underemployment"])
    gdp_growth = read_gdp_growth(files["qna_gdp_growth"][0])
    gdp_levels = read_gdp_levels(files["qna_gdp_levels"][0])
    cpi_monthly = read_cpi_monthly(
        files["cpi_index_backcast_1994_2017"] + files["cpi_index_2018_2025"]
    )
    cpi_index = quarterly_mean(cpi_monthly)

    # The year-pair canary. Checked on the full series BEFORE the window clip,
    # because the canary quarter sits outside the window.
    canary = gdp_growth.get(index_from_qid(GDP_CANARY_QUARTER))
    if canary is None or abs(canary - GDP_CANARY_VALUE) > 0.05:
        raise TransformError(
            f"GDP year-pair canary failed: {GDP_CANARY_QUARTER} reads {canary}, "
            f"expected {GDP_CANARY_VALUE} (PSA's published figure).\n"
            f"    The paired-year columns are probably being read with the wrong "
            f"year, which shifts the predictor against the target invisibly."
        )

    inflation = pct_change(cpi_index, 4)

    series = {
        "underemployment_rate": lfs["underemployment_rate"],
        "unemployment_rate": lfs["unemployment_rate"],
        "employment_rate": lfs["employment_rate"],
        "lfpr": lfs["lfpr"],
        "underemployment_change_qoq": diff(lfs["underemployment_rate"], 1),
        "underemployment_change_yoy": diff(lfs["underemployment_rate"], 4),
        "gdp_growth_yoy": gdp_growth,
        "gdp_growth_yoy_accel": diff(gdp_growth, 1),
        "gdp_level": gdp_levels,
        "gdp_growth_qoq": pct_change(gdp_levels, 1),
        "cpi_index": cpi_index,
        "inflation_yoy": inflation,
        "inflation_yoy_accel": diff(inflation, 1),
    }

    # The headline KPI: growth minus the IMPROVEMENT in job quality, both
    # year-on-year so the two terms share a window.
    #
    # Note the plus sign. An improvement in underemployment is a FALL, so the
    # change term is negative when things get better - which means growth MINUS
    # the change would grow largest exactly when job quality improved most,
    # ranking the best quarters as the worst. Adding it makes a large gap mean
    # what the name says:
    #     7% growth, underemployment +2 pp  ->  9.0   growth without jobs
    #     7% growth, underemployment -3 pp  ->  4.0   growth reaching workers
    series["growth_employment_gap"] = {
        i: v + series["underemployment_change_yoy"][i]
        for i, v in gdp_growth.items()
        if i in series["underemployment_change_yoy"]
    }

    diagnostics = check_inflation_against_psa(inflation, files)
    diagnostics.update(check_lfs_estimator(files))
    return series, diagnostics


def check_inflation_against_psa(inflation: dict[int, float],
                                files: dict[str, list[Path]]) -> dict:
    """Compare our computed inflation with PSA's published year-on-year rates.

    This is the check that the CPI stitch and the quarterly aggregation are both
    right. Nothing else would catch a mis-joined backcast leg - the numbers would
    simply be wrong and plausible.
    """
    pattern = re.compile(r"(\d{4})\s+([A-Za-z]{3})\s*$")
    month_number = {abbr: i + 1 for i, abbr in enumerate(MONTH_ABBR)}

    buckets: dict[int, list[float]] = {}
    for match, value in read_wide(files["cpi_yoy_official_validation"][0], pattern):
        month = month_number.get(match[2])
        if month is None or value is None:
            continue
        buckets.setdefault(qindex(int(match[1]), (month - 1) // 3 + 1), []).append(value)
    official = {i: sum(v) / 3 for i, v in buckets.items() if len(v) == 3}

    overlap = sorted(set(official) & set(inflation))
    if not overlap:
        raise TransformError("No overlap between computed and published inflation.")

    worst_index = max(overlap, key=lambda i: abs(inflation[i] - official[i]))
    worst = abs(inflation[worst_index] - official[worst_index])
    if worst > INFLATION_TOLERANCE_PP:
        detail = "\n".join(
            f"      {qid_from_index(i)}  ours {inflation[i]:6.2f}  PSA {official[i]:6.2f}"
            f"  diff {inflation[i] - official[i]:+.2f}"
            for i in overlap if abs(inflation[i] - official[i]) > INFLATION_TOLERANCE_PP
        )
        raise TransformError(
            f"Computed inflation diverges from PSA's published figure by up to "
            f"{worst:.2f} pp, over the {INFLATION_TOLERANCE_PP} pp tolerance.\n"
            f"    Quarters over tolerance:\n{detail}"
        )
    return {
        "inflation_vs_psa_quarters_compared": len(overlap),
        "inflation_vs_psa_max_divergence_pp": round(worst, 4),
        "inflation_vs_psa_worst_quarter": qid_from_index(worst_index),
    }


def check_lfs_estimator(files: dict[str, list[Path]]) -> dict:
    """Quantify what the round-month rule costs versus averaging all 3 months.

    Only possible from 2021, when the LFS went monthly. This is the evidence for
    the round-month decision rather than an assertion of it - if the divergence
    were large the decision would deserve revisiting, and this is what would say so.
    """
    column = "Underemployment Rate Both sexes"
    monthly: dict[tuple[int, int], float] = {}
    for path in files["lfs_underemployment"]:
        header, rows = read_pxweb_csv(path)
        at = header.index(column)
        year_at, month_at = header.index("Year"), header.index("Month")
        for row in rows:
            if row[month_at] == "Annual":
                continue
            value = to_number(row[at])
            if value is None:
                continue
            month = next((i + 1 for i, name in enumerate(
                ["January", "February", "March", "April", "May", "June", "July",
                 "August", "September", "October", "November", "December"])
                if name == row[month_at]), None)
            if month:
                monthly[(int(row[year_at]), month)] = value

    buckets: dict[int, list[float]] = {}
    for (year, month), value in monthly.items():
        buckets.setdefault(qindex(year, (month - 1) // 3 + 1), []).append(value)
    full = {i: v for i, v in buckets.items() if len(v) == 3}

    round_month_number = {1: 1, 2: 4, 3: 7, 4: 10}
    gaps: list[tuple[int, float]] = []
    for index, values in full.items():
        year, quarter = index // 4, index % 4 + 1
        round_value = monthly.get((year, round_month_number[quarter]))
        if round_value is not None:
            gaps.append((index, round_value - sum(values) / 3))

    if not gaps:
        return {"lfs_estimator_quarters_compared": 0}

    # The MEAN signed gap is the number that matters, not the max. It is
    # consistently positive - the round month reads about a point above the
    # quarter's mean - which is exactly why the two estimators must not be mixed:
    # switching to 3-month means from 2021 would step the series down by roughly
    # that amount at the join, and the step would be an artefact of cleaning
    # rather than anything the labour market did.
    signed = [gap for _, gap in gaps]
    worst_index, worst = max(gaps, key=lambda pair: abs(pair[1]))
    return {
        "lfs_estimator_quarters_compared": len(gaps),
        "lfs_estimator_mean_bias_pp": round(sum(signed) / len(signed), 4),
        "lfs_estimator_quarters_round_month_higher": sum(1 for g in signed if g > 0),
        "lfs_estimator_max_divergence_pp": round(abs(worst), 4),
        "lfs_estimator_worst_quarter": qid_from_index(worst_index),
    }


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def validate(quarters: list[int], series: dict[str, dict[int, float]],
             fact: list[dict], diagnostics: dict) -> list[str]:
    """Checks matched to this project's real risks, run every time.

    In the script, not in a notebook cell - a check that is easy to skip is a
    check that eventually gets skipped.
    """
    checks: list[str] = []

    expected = list(range(index_from_qid(WINDOW_START), index_from_qid(WINDOW_END) + 1))
    if quarters != expected:
        raise TransformError(
            f"Quarter grid is not contiguous {WINDOW_START}..{WINDOW_END}: expected "
            f"{len(expected)} quarters, built {len(quarters)}. A gap would corrupt "
            f"every lag-derived column at once."
        )
    checks.append(f"quarter grid contiguous: {len(quarters)} quarters "
                  f"{WINDOW_START}..{WINDOW_END}")

    keys = {(row["quarter_id"], row["indicator_code"]) for row in fact}
    if len(keys) != len(fact):
        raise TransformError("fact_indicator_quarter has duplicate (quarter_id, "
                             "indicator_code) pairs - the primary key is not unique.")
    checks.append(f"fact composite key unique: {len(fact)} rows")

    codes = {spec[0] for spec in INDICATORS}
    orphans = {row["indicator_code"] for row in fact} - codes
    if orphans:
        raise TransformError(f"fact references unknown indicator(s): {sorted(orphans)}")
    checks.append(f"fact -> dim_indicator integrity: {len(codes)} indicators")

    # Range checks. Deliberately wide enough to admit 2020Q2: the COVID round's
    # unemployment spike is real data, and a bound quietly tightened until an
    # outlier disappears is worse than no bound at all.
    bounds = {
        "underemployment_rate": (5.0, 40.0),
        "unemployment_rate": (0.0, 25.0),
        "employment_rate": (60.0, 100.0),
        "lfpr": (50.0, 80.0),
        "inflation_yoy": (-5.0, 25.0),
    }
    for code, (low, high) in bounds.items():
        values = [v for i, v in series[code].items() if i in set(quarters)]
        if not values:
            continue
        if min(values) < low or max(values) > high:
            raise TransformError(
                f"'{code}' has values outside [{low}, {high}]: observed range "
                f"[{min(values):.2f}, {max(values):.2f}]."
            )
        checks.append(f"{code} within [{low}, {high}]: observed "
                      f"[{min(values):.2f}, {max(values):.2f}]")

    for row in fact:
        if row["value"] is not None and not isinstance(row["value"], float):
            raise TransformError(f"Non-numeric value survived into fact: {row!r}")
        if isinstance(row["value"], str) and row["value"].strip() in MISSING_MARKERS:
            raise TransformError(f"Missing marker survived into fact: {row!r}")
    checks.append("no missing markers survived into numeric columns")

    checks.append(
        f"computed inflation vs PSA: max divergence "
        f"{diagnostics['inflation_vs_psa_max_divergence_pp']} pp at "
        f"{diagnostics['inflation_vs_psa_worst_quarter']} "
        f"(tolerance {INFLATION_TOLERANCE_PP}) over "
        f"{diagnostics['inflation_vs_psa_quarters_compared']} quarters"
    )
    if diagnostics.get("lfs_estimator_quarters_compared"):
        compared = diagnostics["lfs_estimator_quarters_compared"]
        checks.append(
            f"round-month vs 3-month mean over {compared} quarters: mean bias "
            f"{diagnostics['lfs_estimator_mean_bias_pp']:+} pp, round month higher in "
            f"{diagnostics['lfs_estimator_quarters_round_month_higher']}/{compared} "
            f"(max {diagnostics['lfs_estimator_max_divergence_pp']} pp at "
            f"{diagnostics['lfs_estimator_worst_quarter']}) - the two estimators are "
            f"offset, so they must not be mixed mid-series"
        )
    checks.append(f"GDP year-pair canary: {GDP_CANARY_QUARTER} = {GDP_CANARY_VALUE}")
    return checks


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def fmt(value: float | None) -> str:
    """Fixed precision so a rerun is byte-identical. None becomes empty, not 0."""
    return "" if value is None else f"{round(value, 4):g}"


def write_csv(path: Path, header: list[str], rows: list[list]) -> str:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reshape data/raw into data/processed.")
    parser.add_argument("--quiet", action="store_true", help="suppress the check log")
    args = parser.parse_args(argv)

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    files = resolve_latest_raw_files()
    series, diagnostics = build_series(files)

    quarters = list(range(index_from_qid(WINDOW_START), index_from_qid(WINDOW_END) + 1))
    codes = [spec[0] for spec in INDICATORS]

    # ---- fact: one row per (quarter, indicator), including explicit absences ----
    # Each indicator traces back to the raw file it came from, so a value in the
    # fact can be walked back to a specific pull without consulting the manifest.
    raw_file_of = {spec[0]: files[spec[3]][0].name for spec in INDICATORS}
    fact = [
        {
            "quarter_id": qid_from_index(index),
            "indicator_code": code,
            "value": series[code].get(index),
            "value_status": ("missing" if series[code].get(index) is None
                             else "observed" if code in OBSERVED else "derived"),
            "source_file": raw_file_of[code],
        }
        for index in quarters
        for code in codes
    ]

    checks = validate(quarters, series, fact, diagnostics)

    # ---- dim_quarter ----
    dim_quarter_rows = []
    for index in quarters:
        start, end = quarter_bounds(index)
        quarter = index % 4 + 1
        dim_quarter_rows.append([qid_from_index(index), index // 4, quarter,
                                 start.isoformat(), end.isoformat(),
                                 QUARTER_TO_ROUND_MONTH[quarter]])

    # ---- dim_indicator ----
    dim_indicator_rows = [
        [code, label, unit, dataset, TABLE_IDS[dataset], frequency, aggregation,
         "true" if model_input else "false"]
        for code, label, unit, dataset, frequency, aggregation, model_input
        in sorted(INDICATORS)
    ]

    # ---- analysis_quarterly: the fact pivoted, plus lags ----
    lagged = ["gdp_growth_yoy", "gdp_growth_yoy_accel", "inflation_yoy",
              "inflation_yoy_accel"]
    analysis_header = (
        ["quarter_id", "year", "quarter_num", "quarter_start_date"]
        + codes + [f"{code}_lag1" for code in lagged] + ["naive_forecast_change_pp"]
    )
    analysis_rows = []
    for index in quarters:
        start, _ = quarter_bounds(index)
        row = [qid_from_index(index), index // 4, index % 4 + 1, start.isoformat()]
        row += [fmt(series[code].get(index)) for code in codes]
        row += [fmt(series[code].get(index - 1)) for code in lagged]
        row.append("0")            # the no-change baseline the model must beat
        analysis_rows.append(row)

    outputs = {
        "dim_quarter.csv": write_csv(
            PROCESSED_DIR / "dim_quarter.csv",
            ["quarter_id", "year", "quarter_num", "quarter_start_date",
             "quarter_end_date", "lfs_round_month"],
            dim_quarter_rows),
        "dim_indicator.csv": write_csv(
            PROCESSED_DIR / "dim_indicator.csv",
            ["indicator_code", "indicator_label", "unit", "source_dataset",
             "source_table_id", "native_frequency", "aggregation_method",
             "is_model_input"],
            dim_indicator_rows),
        "fact_indicator_quarter.csv": write_csv(
            PROCESSED_DIR / "fact_indicator_quarter.csv",
            ["quarter_id", "indicator_code", "value", "value_status", "source_file"],
            [[r["quarter_id"], r["indicator_code"], fmt(r["value"]),
              r["value_status"], r["source_file"]] for r in fact]),
        "analysis_quarterly.csv": write_csv(
            PROCESSED_DIR / "analysis_quarterly.csv", analysis_header, analysis_rows),
    }

    # The run timestamp lives here and nowhere else, so the four CSVs stay
    # byte-identical across reruns and a rerun shows up as a clean git diff.
    REPORT_PATH.write_text(json.dumps({
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window": {"start": WINDOW_START, "end": WINDOW_END, "quarters": len(quarters)},
        "raw_inputs": {name: [p.name for p in paths] for name, paths in sorted(files.items())},
        "outputs": [{"file": name, "sha256": digest} for name, digest in outputs.items()],
        "row_counts": {"dim_quarter": len(dim_quarter_rows),
                       "dim_indicator": len(dim_indicator_rows),
                       "fact_indicator_quarter": len(fact),
                       "analysis_quarterly": len(analysis_rows)},
        "missing_by_indicator": {
            code: sum(1 for r in fact
                      if r["indicator_code"] == code and r["value"] is None)
            for code in sorted(codes)},
        "diagnostics": diagnostics,
        "checks_passed": checks,
    }, indent=2), encoding="utf-8")

    if not args.quiet:
        print(f"transform -> {PROCESSED_DIR}")
        for check in checks:
            print(f"  [ok] {check}")
        print()
        for name, digest in outputs.items():
            print(f"  {name:<30} {digest[:12]}")
    print(f"\n{len(quarters)} quarters x {len(codes)} indicators -> "
          f"{len(fact)} fact rows. Report: {REPORT_PATH.name}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TransformError as exc:
        print(f"\n[FAILED] {exc}", file=sys.stderr)
        sys.exit(1)
