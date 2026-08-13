"""
Phase 3 - Data Transformation  (Week 9: pandas)
===============================================

Reshapes the six raw PSA OpenSTAT extracts in data/raw/ into the processed layer
designed in data/data_dictionary.md:

    dim_quarter.csv              one row = one calendar quarter
    dim_indicator.csv            one row = one indicator
    fact_indicator_quarter.csv   one row = one indicator's value for one quarter
    analysis_quarterly.csv       one row = one quarter, indicators as columns

This is the pandas version of the Week 8 standard-library script. It produces
BYTE-IDENTICAL output to it - the four CSVs were already committed, so running
this and getting a clean `git diff data/processed/*.csv` is what proves the port
is faithful rather than merely plausible. (An automated re-run check lands in
Week 11; today the check is the diff.)

WHAT PANDAS ACTUALLY BOUGHT
---------------------------
Not brevity for its own sake - four specific things the hand-rolled version had
to do by hand and could have got wrong:

  * na_values= turns PXWeb's "." and ".." into NaN at read time, so a missing
    marker can never be mistaken for a number further downstream.
  * melt() unpivots the wide GDP and CPI tables (100+ period columns, one data
    row) declaratively instead of scanning column names in a loop.
  * A PeriodIndex with freq="Q" makes "one quarter earlier" and "four quarters
    earlier" real operations - .shift(1), .shift(4) - instead of integer
    arithmetic on a hand-built quarter index.
  * Automatic index alignment. growth_employment_gap adds two series that do not
    cover the same quarters; pandas aligns them on the index and yields NaN
    where either side is absent, which is exactly the wanted behaviour.

THE ONE PANDAS TRAP THIS CODE GUARDS AGAINST
--------------------------------------------
.shift() and .diff() are POSITIONAL, not label-aware. On a series whose index is
missing 2009Q3, .diff(1) at 2009Q4 silently differences against 2009Q2 - a
six-month change presented as a three-month one, with nothing to show for it.
So every series is reindexed onto a complete quarterly PeriodIndex BEFORE any
differencing, and assert_contiguous() enforces that. The Week 8 version was
immune to this by accident, because it did index arithmetic explicitly.

THE THREE RESHAPING PROBLEMS
----------------------------
1. LFS is LONG   - Year/Month down the rows. Quarterly rounds through 2020,
                   monthly from 2021, plus an "Annual" aggregate row every year.
2. GDP/CPI are WIDE - one data row with 100+ period columns.
3. Nothing shares a key - the extracts only meet once each is reshaped onto a
                   common quarter_id. That is what fact_indicator_quarter is.

THE RULES THIS SCRIPT ENCODES  (reasoning in data/data_dictionary.md)
---------------------------------------------------------------------
  * A quarter's LFS value is its ROUND-MONTH value: Jan/Apr/Jul/Oct -> Q1..Q4.
    One estimator for all 83 quarters. The round month reads about 1.2 pp above
    the 3-month mean (measured, 17 of 21 quarters), so averaging the months
    where they exist from 2021 would step the series down at the join - an
    artefact of cleaning, not of the labour market.
  * "Annual" (LFS) and "Ave" (CPI) rows are ANNUAL AGGREGATES, not periods.
  * GDP growth is YEAR-ON-YEAR and its Year values are PAIRS: "2025-2026 Q1" is
    Q1 of 2026. The SECOND year is the observation year.
  * Missing values arrive as "." or ".." and mean the survey did not run or the
    figure is unpublished. They are NOT zeros and are never imputed.
  * The window is 2005Q2-2025Q4: 83 quarters, no gaps.

Usage
-----
    python scripts/transform.py
    python scripts/transform.py --profile     # head/info/describe/value_counts
    python scripts/transform.py --quiet
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# Reuse the ingestion constants rather than restating them. MISSING_MARKERS
# already lists every missing-value spelling this source uses - including "..",
# which the GDP levels table uses where LFS uses ".".
from ingest import MISSING_MARKERS, RAW_DATA_DIR, TABLES

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

# Every series is reindexed onto this before differencing, so that .shift() -
# which counts rows, not quarters - always steps by a real quarter. Wide enough
# to cover the earliest source (CPI, 1994) and the latest (GDP, 2026).
FULL_SPAN = pd.period_range("1994Q1", "2026Q4", freq="Q")
WINDOW = pd.period_range(WINDOW_START, WINDOW_END, freq="Q")

# Tolerance for our computed inflation against PSA's published figure. Not zero:
# we compute year-on-year change on the quarterly MEAN index, while PSA publishes
# monthly year-on-year rates that we average. Close, but not identical.
#
# Started at 0.20 as a placeholder before the first run. The observed worst case
# across the 28-quarter overlap is 0.042 pp, so the bound is now set just above
# that. A tolerance far looser than the data warrants is a check that cannot
# fail, which is the same as no check at all.
INFLATION_TOLERANCE_PP = 0.10

# PSA's published GDP growth for 2026 Q1, a canary that the paired-year columns
# are read with the correct year. Documented in the data dictionary.
GDP_CANARY_QUARTER = "2026Q1"
GDP_CANARY_VALUE = 2.8

ROUND_MONTH_TO_QUARTER = {"January": 1, "April": 2, "July": 3, "October": 4}
QUARTER_TO_ROUND_MONTH = {q: m for m, q in ROUND_MONTH_TO_QUARTER.items()}
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]
MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Column patterns for the three wide layouts.
GDP_GROWTH_COLUMNS = re.compile(r"(\d{4})-(\d{4})\s+Q(\d)\s*$")
GDP_LEVEL_COLUMNS = re.compile(r"(\d{4})\s+Q(\d)\s*$")
CPI_COLUMNS = re.compile(r"(\d{4})\s+([A-Za-z]{3})\s*$")


class TransformError(Exception):
    """A data problem the user can act on. main() prints it without a traceback."""


# --------------------------------------------------------------------------- #
# Locating raw files
# --------------------------------------------------------------------------- #


def resolve_latest_raw_files() -> dict[str, list[Path]]:
    """Map each dataset slug to its most recent pull's file(s).

    Read from _manifest.json rather than globbing for a hardcoded date. Raw
    filenames carry the pull date, so hardcoding one means this script silently
    keeps reading a stale extract the next time ingest.py runs.
    """
    if not MANIFEST_PATH.exists():
        raise TransformError(
            f"No manifest at {MANIFEST_PATH}. Run 'python scripts/ingest.py' first."
        )

    records = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    latest = pd.DataFrame(records).groupby("name")["retrieved_at_utc"].max()

    files: dict[str, list[Path]] = {}
    for record in sorted(records, key=lambda r: r["chunk"]):
        name = record["name"]
        if record["retrieved_at_utc"] != latest[name]:
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


def read_raw(path: Path) -> pd.DataFrame:
    """Read one raw PXWeb CSV with its missing markers already resolved.

    utf-8-sig strips the BOM PXWeb prepends. na_values + keep_default_na=False
    means EXACTLY the markers ingest.py documented become NaN and nothing else -
    a value pandas would otherwise guess at stays a plain string and shows up as
    a dtype surprise rather than a silent null.
    """
    return pd.read_csv(path, encoding="utf-8-sig",
                       na_values=sorted(MISSING_MARKERS), keep_default_na=False)


def assert_contiguous(series: pd.Series, label: str) -> pd.Series:
    """Reindex onto FULL_SPAN so .shift() steps by quarters, not by rows.

    This is the guard for the trap in the module docstring: shifting a series
    with a hole in its index differences against the wrong period and looks fine.
    """
    reindexed = series.reindex(FULL_SPAN)
    if not reindexed.index.is_monotonic_increasing:
        raise TransformError(f"{label}: index is not sorted after reindexing.")
    return reindexed


# --------------------------------------------------------------------------- #
# Readers - one per raw layout
# --------------------------------------------------------------------------- #


def read_lfs(paths: list[Path]) -> dict[str, pd.Series]:
    """LFS is LONG: Year/Month down the rows, one column per rate.

    Drops the "Annual" aggregate rows and the eight non-round months, which is
    the single place the round-month rule lives.
    """
    wanted = {
        "Underemployment Rate Both sexes": "underemployment_rate",
        "Unemployment Rate Both sexes": "unemployment_rate",
        "Employment Rate Both sexes": "employment_rate",
        "Labor Force Participation Rate Both sexes": "lfpr",
    }

    frame = pd.concat([read_raw(path) for path in paths], ignore_index=True)
    absent = [column for column in wanted if column not in frame.columns]
    if absent:
        raise TransformError(
            f"LFS extract is missing expected column(s) {absent}.\n"
            f"    Columns present: {list(frame.columns)}"
        )

    annual = int((frame["Month"] == "Annual").sum())
    if not annual:
        raise TransformError(
            "No 'Annual' rows found in the LFS extract. Every year carries one, so "
            "either the extract is malformed or the aggregate-row filter no longer "
            "matches - which would mean aggregates are leaking into the data."
        )

    # The LFS arrives as two files because the query exceeded the API's cell cap
    # and was split along the time axis. The split is supposed to produce
    # disjoint blocks of periods; if the chunk boundaries ever overlapped, the
    # concat above would duplicate whole rows and every downstream aggregate
    # would be quietly wrong. Cheap to check, expensive to miss.
    duplicated = frame.duplicated(subset=["Year", "Month"])
    if duplicated.any():
        clash = frame.loc[duplicated, ["Year", "Month"]].head(5).to_dict("records")
        raise TransformError(
            f"Duplicate (Year, Month) rows after concatenating the LFS parts: "
            f"{clash}.\n    The ingest chunks are meant to be disjoint - check "
            f"chunk_by_time() in scripts/ingest.py."
        )

    rounds = frame[frame["Month"].isin(ROUND_MONTH_TO_QUARTER)].copy()
    rounds["quarter"] = rounds["Month"].map(ROUND_MONTH_TO_QUARTER)
    index = pd.PeriodIndex(
        rounds["Year"].astype(str) + "Q" + rounds["quarter"].astype(str), freq="Q"
    )

    series = {}
    for column, code in wanted.items():
        values = pd.to_numeric(rounds[column], errors="coerce")
        values.index = index
        series[code] = assert_contiguous(values.dropna().sort_index(), code)
    return series


def melt_periods(path: Path, pattern: re.Pattern[str]) -> pd.DataFrame:
    """Unpivot a wide extract: 100+ period columns, one data row, into long form.

    Returns the regex capture groups alongside the value and leaves the caller to
    say what they mean - the three wide tables disagree about that (year pairs vs
    single years vs year and month).
    """
    frame = read_raw(path)
    period_columns = [column for column in frame.columns if pattern.search(column)]
    if not period_columns:
        raise TransformError(
            f"{path.name}: no column matched /{pattern.pattern}/. PXWeb may have "
            f"changed its column labels.\n    First few columns: "
            f"{list(frame.columns[:4])}"
        )
    if len(frame) != 1:
        raise TransformError(
            f"{path.name}: expected exactly 1 data row in a wide extract, found "
            f"{len(frame)}. The selection in ingest.py may have widened."
        )

    long = frame.melt(value_vars=period_columns, var_name="label", value_name="value")
    long["value"] = pd.to_numeric(long["value"], errors="coerce")
    return long.join(long["label"].str.extract(pattern))


def read_gdp_growth(path: Path) -> pd.Series:
    """Year-on-year growth. Columns read '... 2025-2026 Q1'.

    The SECOND year of the pair is the observation year: that column is Q1 2026
    against Q1 2025. Reading the first instead shifts the whole predictor a year
    against the target, and nothing about the output looks wrong.
    """
    long = melt_periods(path, GDP_GROWTH_COLUMNS)
    earlier, later = long[0].astype(int), long[1].astype(int)
    if not (later == earlier + 1).all():
        raise TransformError(
            f"{path.name}: a year pair is not consecutive. The column format has "
            f"changed and the year-on-year reading is no longer safe."
        )
    index = pd.PeriodIndex(long[1] + "Q" + long[2], freq="Q")
    series = pd.Series(long["value"].values, index=index, name="gdp_growth_yoy")
    return assert_contiguous(series.dropna().sort_index(), "gdp_growth_yoy")


def read_gdp_levels(path: Path) -> pd.Series:
    """Levels at constant 2018 prices. Columns read '... 2000 Q1'."""
    long = melt_periods(path, GDP_LEVEL_COLUMNS)
    index = pd.PeriodIndex(long[0] + "Q" + long[1], freq="Q")
    series = pd.Series(long["value"].values, index=index, name="gdp_level")
    return assert_contiguous(series.dropna().sort_index(), "gdp_level")


def read_cpi_quarterly(paths: list[Path]) -> pd.Series:
    """Stitch the CPI index legs and collapse months to quarters.

    Columns read '2018 Jan' ... '2018 Dec' plus '2018 Ave'. "Ave" is the annual
    average sitting alongside the months - an aggregate, not a period - and is
    dropped by the month lookup rather than by a name check.

    A quarter needs all three of its months: one built from two is not comparable
    with one built from three, so a partial quarter is dropped, not averaged.
    """
    parts = [melt_periods(path, CPI_COLUMNS) for path in paths]
    long = pd.concat(parts, ignore_index=True)
    long["month"] = long[1].map({abbr: i + 1 for i, abbr in enumerate(MONTH_ABBR)})
    long = long.dropna(subset=["month", "value"])

    duplicated = long.duplicated(subset=[0, "month"])
    if duplicated.any():
        clash = long.loc[duplicated, [0, 1]].head(3).to_dict("records")
        raise TransformError(
            f"CPI month(s) {clash} appear in more than one extract. The backcast and "
            f"current legs abut at 2017-12 / 2018-01; an overlap would double-count."
        )

    quarter = pd.PeriodIndex(
        long[0] + "Q" + ((long["month"].astype(int) - 1) // 3 + 1).astype(str), freq="Q"
    )
    grouped = long.groupby(quarter)["value"]
    complete = grouped.mean().where(grouped.count() == 3).dropna()
    return assert_contiguous(complete.sort_index(), "cpi_index")


# --------------------------------------------------------------------------- #
# Indicator catalogue - the source of dim_indicator
# --------------------------------------------------------------------------- #

TABLE_IDS = {spec.name: spec.table_id for spec in TABLES}

INDICATORS: list[tuple] = [
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

CODES = [spec[0] for spec in INDICATORS]
LAGGED = ["gdp_growth_yoy", "gdp_growth_yoy_accel", "inflation_yoy",
          "inflation_yoy_accel"]


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #


def build_frame(files: dict[str, list[Path]]) -> tuple[pd.DataFrame, dict]:
    """Reshape every extract onto a quarterly index and derive the rest.

    Every series here is on FULL_SPAN, so .shift() and .diff() step by exactly
    one quarter and pandas aligns the arithmetic on the index for free.
    """
    lfs = read_lfs(files["lfs_underemployment"])
    gdp_growth = read_gdp_growth(files["qna_gdp_growth"][0])
    gdp_levels = read_gdp_levels(files["qna_gdp_levels"][0])
    cpi_index = read_cpi_quarterly(
        files["cpi_index_backcast_1994_2017"] + files["cpi_index_2018_2025"]
    )

    # The year-pair canary, checked on the full series BEFORE the window clip -
    # the canary quarter sits outside the window on purpose.
    canary = gdp_growth.get(pd.Period(GDP_CANARY_QUARTER, freq="Q"))
    if pd.isna(canary) or abs(canary - GDP_CANARY_VALUE) > 0.05:
        raise TransformError(
            f"GDP year-pair canary failed: {GDP_CANARY_QUARTER} reads {canary}, "
            f"expected {GDP_CANARY_VALUE} (PSA's published figure).\n"
            f"    The paired-year columns are probably being read with the wrong "
            f"year, which shifts the predictor against the target invisibly."
        )

    # Explicit ratio rather than .pct_change(), whose fill_method behaviour has
    # changed across pandas versions. This spelling means the same thing in every
    # version and matches the formula written in the data dictionary.
    inflation = (cpi_index / cpi_index.shift(4) - 1) * 100
    underemployment = lfs["underemployment_rate"]

    frame = pd.DataFrame({
        "underemployment_rate": underemployment,
        "unemployment_rate": lfs["unemployment_rate"],
        "employment_rate": lfs["employment_rate"],
        "lfpr": lfs["lfpr"],
        "underemployment_change_qoq": underemployment.diff(1),
        "underemployment_change_yoy": underemployment.diff(4),
        "gdp_growth_yoy": gdp_growth,
        "gdp_growth_yoy_accel": gdp_growth.diff(1),
        "gdp_level": gdp_levels,
        "gdp_growth_qoq": (gdp_levels / gdp_levels.shift(1) - 1) * 100,
        "cpi_index": cpi_index,
        "inflation_yoy": inflation,
        "inflation_yoy_accel": inflation.diff(1),
    })

    # The headline KPI: growth minus the IMPROVEMENT in job quality, both
    # year-on-year so the two terms share a window.
    #
    # Note the plus sign. An improvement in underemployment is a FALL, so the
    # change term is negative when things get better - which means growth MINUS
    # the change would be largest exactly when job quality improved most,
    # ranking the best quarters as the worst. Adding it makes a large gap mean
    # what the name says:
    #     7% growth, underemployment +2 pp  ->  9.0   growth without jobs
    #     7% growth, underemployment -3 pp  ->  4.0   growth reaching workers
    #
    # pandas aligns the two series on the index, so quarters present in only one
    # of them come out NaN rather than silently pairing the wrong periods.
    frame["growth_employment_gap"] = (
        frame["gdp_growth_yoy"] + frame["underemployment_change_yoy"]
    )

    diagnostics = check_inflation_against_psa(inflation, files)
    diagnostics.update(check_lfs_estimator(files))

    # Returned on FULL_SPAN, NOT clipped to the window. Derive first, clip last:
    # GDP and CPI both have real 2005Q1 values - the window starts at 2005Q2
    # because of the LFS, not because of them - so clipping before taking lags
    # would leave the first modelling quarter with no features and silently throw
    # away an observation that exists.
    return frame[CODES], diagnostics


def check_inflation_against_psa(inflation: pd.Series,
                                files: dict[str, list[Path]]) -> dict:
    """Compare computed inflation with PSA's published year-on-year rates.

    This is the check that the CPI stitch and the quarterly aggregation are both
    right. Nothing else would catch a mis-joined backcast leg - the numbers would
    simply be wrong and entirely plausible.
    """
    long = melt_periods(files["cpi_yoy_official_validation"][0], CPI_COLUMNS)
    long["month"] = long[1].map({abbr: i + 1 for i, abbr in enumerate(MONTH_ABBR)})
    long = long.dropna(subset=["month", "value"])

    quarter = pd.PeriodIndex(
        long[0] + "Q" + ((long["month"].astype(int) - 1) // 3 + 1).astype(str), freq="Q"
    )
    grouped = long.groupby(quarter)["value"]
    official = grouped.mean().where(grouped.count() == 3).dropna()

    comparison = pd.DataFrame({"ours": inflation, "psa": official}).dropna()
    if comparison.empty:
        raise TransformError("No overlap between computed and published inflation.")

    comparison["diff"] = comparison["ours"] - comparison["psa"]
    worst = comparison["diff"].abs().idxmax()
    worst_value = abs(comparison.loc[worst, "diff"])

    if worst_value > INFLATION_TOLERANCE_PP:
        over = comparison[comparison["diff"].abs() > INFLATION_TOLERANCE_PP]
        raise TransformError(
            f"Computed inflation diverges from PSA's published figure by up to "
            f"{worst_value:.2f} pp, over the {INFLATION_TOLERANCE_PP} pp tolerance.\n"
            f"    Quarters over tolerance:\n{over.round(3).to_string()}"
        )
    return {
        "inflation_vs_psa_quarters_compared": int(len(comparison)),
        "inflation_vs_psa_max_divergence_pp": round(float(worst_value), 4),
        "inflation_vs_psa_worst_quarter": str(worst),
    }


def check_lfs_estimator(files: dict[str, list[Path]]) -> dict:
    """Quantify what the round-month rule costs versus averaging all 3 months.

    Only possible from 2021, when the LFS went monthly. This is the evidence for
    the round-month decision rather than an assertion of it.
    """
    frame = pd.concat([read_raw(path) for path in files["lfs_underemployment"]],
                      ignore_index=True)
    frame = frame[frame["Month"].isin(MONTH_NAMES)].copy()
    frame["month"] = frame["Month"].map({name: i + 1 for i, name in enumerate(MONTH_NAMES)})
    frame["value"] = pd.to_numeric(frame["Underemployment Rate Both sexes"],
                                   errors="coerce")
    frame = frame.dropna(subset=["value"])
    frame["quarter"] = pd.PeriodIndex(
        frame["Year"].astype(str) + "Q" + ((frame["month"] - 1) // 3 + 1).astype(str),
        freq="Q")

    grouped = frame.groupby("quarter")["value"]
    full = grouped.mean().where(grouped.count() == 3).dropna()

    is_round = frame["month"].isin([1, 4, 7, 10])
    round_value = frame[is_round].set_index("quarter")["value"]

    gaps = (round_value.reindex(full.index) - full).dropna()
    if gaps.empty:
        return {"lfs_estimator_quarters_compared": 0}

    # The MEAN SIGNED gap is the number that matters, not the max. It is
    # consistently positive - the round month reads about a point above the
    # quarter's mean - which is exactly why the two estimators must not be mixed:
    # switching to 3-month means from 2021 would step the series down by roughly
    # that amount at the join, and the step would be an artefact of cleaning
    # rather than anything the labour market did.
    return {
        "lfs_estimator_quarters_compared": int(len(gaps)),
        "lfs_estimator_mean_bias_pp": round(float(gaps.mean()), 4),
        "lfs_estimator_quarters_round_month_higher": int((gaps > 0).sum()),
        "lfs_estimator_max_divergence_pp": round(float(gaps.abs().max()), 4),
        "lfs_estimator_worst_quarter": str(gaps.abs().idxmax()),
    }


# --------------------------------------------------------------------------- #
# Profiling  (Week 9: head / info / describe / value_counts)
# --------------------------------------------------------------------------- #


def profile(files: dict[str, list[Path]], frame: pd.DataFrame) -> None:
    """Inspect the raw extracts and the assembled table.

    Kept in the script rather than a notebook so profiling is reproducible and
    reviewable in a diff. The point is not the numbers - it is that the shapes
    and dtypes are what the data dictionary claims they are.
    """
    rule = "=" * 78

    print(f"\n{rule}\nRAW EXTRACTS AS READ\n{rule}")
    for name in sorted(files):
        raw = pd.concat([read_raw(path) for path in files[name]], ignore_index=True)
        print(f"\n--- {name}  {raw.shape[0]} rows x {raw.shape[1]} columns ---")
        print(raw.iloc[:, :6].head(3).to_string())
        if "Month" in raw.columns:
            # The value_counts that matters. Printed in full, and in calendar
            # order rather than by frequency, so the "Annual" row is visible
            # sitting alongside the twelve months instead of buried in a tail.
            # That row is an annual aggregate, not a period, and gets dropped.
            counts = raw["Month"].value_counts()
            ordered = counts.reindex([*MONTH_NAMES, "Annual"]).dropna().astype(int)
            print(f"\nMonth value_counts ({len(counts)} distinct labels; "
                  f"'Annual' is an aggregate, not a period - dropped):")
            print(ordered.to_string())

    print(f"\n{rule}\nASSEMBLED analysis_quarterly\n{rule}\n")
    print(frame.head().to_string())
    print()
    frame.info()
    print("\nDescribe (model inputs and target):")
    columns = ["underemployment_change_qoq", "gdp_growth_yoy", "gdp_growth_yoy_accel",
               "inflation_yoy", "inflation_yoy_accel"]
    print(frame[columns].describe().round(3).to_string())
    print("\nNulls per column (expected only where differencing runs off the start):")
    nulls = frame.isna().sum()
    print(nulls[nulls > 0].to_string() if nulls.any() else "  none")


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def validate(frame: pd.DataFrame, fact: pd.DataFrame, diagnostics: dict) -> list[str]:
    """Checks matched to this project's real risks, run every time.

    In the script, not in a notebook cell - a check that is easy to skip is a
    check that eventually gets skipped.
    """
    checks: list[str] = []

    if len(frame) != len(WINDOW) or not frame.index.equals(WINDOW):
        raise TransformError(
            f"Quarter grid is not contiguous {WINDOW_START}..{WINDOW_END}: expected "
            f"{len(WINDOW)} quarters, built {len(frame)}. A gap would corrupt every "
            f"lag-derived column at once."
        )
    checks.append(f"quarter grid contiguous: {len(frame)} quarters "
                  f"{WINDOW_START}..{WINDOW_END}")

    if fact.duplicated(subset=["quarter_id", "indicator_code"]).any():
        raise TransformError("fact_indicator_quarter has duplicate (quarter_id, "
                             "indicator_code) pairs - the primary key is not unique.")
    checks.append(f"fact composite key unique: {len(fact)} rows")

    orphans = set(fact["indicator_code"]) - set(CODES)
    if orphans:
        raise TransformError(f"fact references unknown indicator(s): {sorted(orphans)}")
    checks.append(f"fact -> dim_indicator integrity: {len(CODES)} indicators")

    if frame.select_dtypes(include="object").columns.any():
        raise TransformError(
            f"Non-numeric columns survived into the analysis frame: "
            f"{list(frame.select_dtypes(include='object').columns)}. A missing marker "
            f"was probably read as a string instead of NaN."
        )
    checks.append("all indicator columns numeric - no missing markers survived")

    infinite = frame.columns[frame.isin([float("inf"), float("-inf")]).any()].tolist()
    if infinite:
        raise TransformError(f"Infinite values in {infinite} - a division by zero.")
    checks.append("no infinities from ratio columns")

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
        column = frame[code].dropna()
        if column.empty:
            continue
        if column.min() < low or column.max() > high:
            raise TransformError(
                f"'{code}' has values outside [{low}, {high}]: observed range "
                f"[{column.min():.2f}, {column.max():.2f}]."
            )
        checks.append(f"{code} within [{low}, {high}]: observed "
                      f"[{column.min():.2f}, {column.max():.2f}]")

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


def fmt(value) -> str:
    """Fixed precision so a rerun is byte-identical. NaN becomes empty, not 0."""
    return "" if pd.isna(value) else f"{round(float(value), 4):g}"


def write_csv(path: Path, header: list[str], rows: list[list]) -> str:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_fact(frame: pd.DataFrame, files: dict[str, list[Path]]) -> pd.DataFrame:
    """Unpivot the analysis frame into the long fact, quarter-major.

    Sorting on an ordered Categorical keeps indicators in catalogue order within
    each quarter, so the file is stable across runs regardless of dict ordering.
    """
    fact = (frame.melt(ignore_index=False, var_name="indicator_code",
                       value_name="value")
            .reset_index(names="quarter"))
    fact["indicator_code"] = pd.Categorical(fact["indicator_code"],
                                            categories=CODES, ordered=True)
    fact = fact.sort_values(["quarter", "indicator_code"], kind="stable")

    fact["quarter_id"] = fact["quarter"].astype(str)
    fact["value_status"] = [
        "missing" if pd.isna(value) else "observed" if code in OBSERVED else "derived"
        for code, value in zip(fact["indicator_code"], fact["value"])
    ]
    # Each indicator traces back to the raw file it came from, so a value in the
    # fact can be walked back to a pull without consulting the manifest.
    raw_file_of = {spec[0]: files[spec[3]][0].name for spec in INDICATORS}
    fact["source_file"] = fact["indicator_code"].map(raw_file_of).astype(str)
    return fact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reshape data/raw into data/processed.")
    parser.add_argument("--profile", action="store_true",
                        help="print head/info/describe/value_counts and exit codes 0")
    parser.add_argument("--quiet", action="store_true", help="suppress the check log")
    args = parser.parse_args(argv)

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    files = resolve_latest_raw_files()

    # full_frame spans 1994Q1-2026Q4; frame is the 83-quarter modelling window.
    # Lags are taken from full_frame so the first window quarter keeps the real
    # predictor values that exist just before it.
    full_frame, diagnostics = build_frame(files)
    frame = full_frame.reindex(WINDOW)
    fact = build_fact(frame, files)
    checks = validate(frame, fact, diagnostics)

    if args.profile:
        profile(files, frame)

    # ---- dim_quarter ----
    dim_quarter_rows = [
        [str(period), period.year, period.quarter,
         period.start_time.date().isoformat(), period.end_time.date().isoformat(),
         QUARTER_TO_ROUND_MONTH[period.quarter]]
        for period in frame.index
    ]

    # ---- dim_indicator ----
    dim_indicator_rows = [
        [code, label, unit, dataset, TABLE_IDS[dataset], frequency, aggregation,
         "true" if model_input else "false"]
        for code, label, unit, dataset, frequency, aggregation, model_input
        in sorted(INDICATORS)
    ]

    # ---- analysis_quarterly: the fact pivoted, plus lags ----
    analysis = frame.copy()
    for code in LAGGED:
        analysis[f"{code}_lag1"] = full_frame[code].shift(1).reindex(WINDOW)
    analysis["naive_forecast_change_pp"] = 0      # the baseline the model must beat

    analysis_header = (["quarter_id", "year", "quarter_num", "quarter_start_date"]
                       + list(analysis.columns))
    analysis_rows = [
        [str(period), period.year, period.quarter,
         period.start_time.date().isoformat()]
        + [fmt(value) for value in row]
        for period, row in zip(analysis.index, analysis.itertuples(index=False))
    ]

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
            [[row.quarter_id, str(row.indicator_code), fmt(row.value),
              row.value_status, row.source_file] for row in fact.itertuples()]),
        "analysis_quarterly.csv": write_csv(
            PROCESSED_DIR / "analysis_quarterly.csv", analysis_header, analysis_rows),
    }

    # The run timestamp lives here and nowhere else, so the four CSVs stay
    # byte-identical across reruns and a rerun shows up as a clean git diff.
    REPORT_PATH.write_text(json.dumps({
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pandas_version": pd.__version__,
        "window": {"start": WINDOW_START, "end": WINDOW_END, "quarters": len(frame)},
        "raw_inputs": {name: [p.name for p in paths] for name, paths in sorted(files.items())},
        "outputs": [{"file": name, "sha256": digest} for name, digest in outputs.items()],
        "row_counts": {"dim_quarter": len(dim_quarter_rows),
                       "dim_indicator": len(dim_indicator_rows),
                       "fact_indicator_quarter": len(fact),
                       "analysis_quarterly": len(analysis_rows)},
        "missing_by_indicator": {code: int(frame[code].isna().sum())
                                 for code in sorted(CODES)},
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
    print(f"\n{len(frame)} quarters x {len(CODES)} indicators -> {len(fact)} fact rows. "
          f"Report: {REPORT_PATH.name}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TransformError as exc:
        print(f"\n[FAILED] {exc}", file=sys.stderr)
        sys.exit(1)
