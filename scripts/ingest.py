"""
Phase 2 - Data Ingestion
========================

Pulls the three input series for the National Underemployment Forecast from the
PSA OpenSTAT PXWeb REST API and saves the raw responses to data/raw/.

    Underemployment rate  <- Labor Force Survey (LFS)
    GDP growth + levels   <- Quarterly National Accounts (QNA)
    CPI index             <- Consumer Price Index (2018=100)


WHY THE REST API AND NOT THE WEB UI
-----------------------------------
The README notes that the OpenSTAT *web interface* is JavaScript-heavy and that
its deep links carry an "rxid" session token which 404s for anyone else. That is
true of the UI, but it does NOT apply to the REST API used here. API paths are
plain, stable and shareable:

    UI   .../pxweb/en/DB/DB__2B__NA__QT__1SUM/       (needs an rxid to be useful)
    API  .../api/v1/en/DB/2B/NA/QT/1SUM/             (no token, works anywhere)

So the "resolve by navigation, not a hard-coded URL" requirement is satisfied by
hitting documented API paths, not by driving a browser.


HOW A PXWEB QUERY WORKS  (this is the "which parameters control the data" answer)
--------------------------------------------------------------------------------
Every table is an N-dimensional cube. For the LFS table the dimensions are
Year x Month x Rates x Sex. Reading data is a two-step conversation:

1. GET the table URL  -> metadata: the list of variables and their legal values.
2. POST to the same URL -> data: a JSON body naming, for each variable, which
   values you want.

The POST body looks like this:

    {
      "query": [
        {"code": "Rates", "selection": {"filter": "item", "values": ["3"]}},
        {"code": "Sex",   "selection": {"filter": "item", "values": ["0"]}}
      ],
      "response": {"format": "csv"}
    }

Two things about that body decide what comes back:

  * Which variables you list. A variable you OMIT is not filtered out - PXWeb
    returns ALL of its values. Omitting "Geolocation" on the CPI table means all
    119 provinces instead of just PHILIPPINES, which is how a 104-cell query
    becomes a 12,000-cell one. Every variable is therefore always listed
    explicitly below.
  * Which values you list for each. These are POSITIONAL codes ("0", "1", "2"),
    not labels - see the next section.


THE ONE RULE THAT MATTERS: RESOLVE BY METADATA, NEVER HARDCODE
--------------------------------------------------------------
PXWeb value codes are positions, not identities. On the LFS table Year is coded
"0".."21" for 2005..2026. When PSA appends 2027, every code still parses fine but
now points one year off, and a hardcoded query silently returns the WRONG DATA
with no error.

So this script never hardcodes a code. It asks for values by their human label
("Underemployment Rate", "PHILIPPINES") and looks the code up in the metadata at
runtime. If a label ever disappears the script fails loudly and prints the labels
that ARE available, instead of quietly pulling the wrong column.

The same idea protects the table id: TableSpec.title_pattern re-finds the table
by title if its id changes.


API LIMITS (read live from /api/v1/en/?config)
----------------------------------------------
    {"maxValues": 1000, "maxCalls": 10, "timeWindow": 10, "CORS": true}

  * maxValues 1000 - a single query may return at most 1000 cells. The LFS pull
    is 4 rates x 1 sex x 22 years x 13 months = 1144 cells, so it genuinely has
    to be split across calls. chunk_by_time() does that.
  * maxCalls 10 per timeWindow 10s - roughly one call per second. RateLimiter
    enforces this before we get a 429 rather than after.


WHY CSV AND NOT JSON-STAT2  (a real server bug - do not "fix" this back)
------------------------------------------------------------------------
The obvious choice for a JSON API is response format "json-stat2", and it looks
like it works: HTTP 200, a well-formed document with the right dimensions and
the right labels. It is silently wrong. PSA's json-stat2 emitter returns a
"value" array of length 1 no matter how much data you asked for:

    {"id": ["Year","Month","Rates","Sex"], "size": [19,13,4,1], "value": [1.0]}

That is 988 cells of metadata wrapped around a single bogus number. Verified on
2026-08-01 against several query sizes. The "csv" format on the identical query
returns the full, correct series, so the bug is in the emitter, not the query:

    "Year","Month","Underemployment Rate Both sexes"
    "2005","January",.          <- LFS began in April 2005, so this really is missing
    "2005","April",26.135

So we ask for CSV and save the bytes verbatim. Missing observations arrive as
"." - PXWeb's missing-value marker - which is meaningful data and is preserved
as-is in the raw file rather than being converted to an empty cell.

This is the reason the raw files are .csv. If a future PSA release fixes
json-stat2, switching back is a one-line change in build_query_body().

Usage
-----
    python scripts/ingest.py                  # pull everything into data/raw/
    python scripts/ingest.py --list           # show the configured tables, no network
    python scripts/ingest.py --only lfs_underemployment
    python scripts/ingest.py --preview lfs_underemployment
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BASE_URL = "https://openstat.psa.gov.ph/PXWeb/api/v1/en/DB"

# Goes at the front of every raw filename so the SOURCE is readable from the
# filename alone, without opening the manifest. Raw landing files are named
# <source>_<dataset>_<table_id>_<pull_date>.csv
SOURCE_SLUG = "psa_openstat"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
MANIFEST_PATH = RAW_DATA_DIR / "_manifest.json"

# Server-declared limits. Confirmed live at /api/v1/en/?config on 2026-08-01.
MAX_VALUES_PER_QUERY = 1000
MAX_CALLS = 10
TIME_WINDOW_SECONDS = 10

REQUEST_TIMEOUT = 30          # seconds; never make a request without one
MAX_ATTEMPTS = 5              # total tries per request, including the first
RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class TableSpec:
    """One table to pull.

    select maps a PXWeb variable code to the value LABELS we want.
    A value of None means "every value of this variable" - still sent
    explicitly in the query so the cell count stays predictable.
    """

    name: str                 # short slug, used in output filenames
    path: str                 # API folder path, e.g. "1B/LFS"
    table_id: str             # e.g. "0021B3FKEI2.px"
    title_pattern: str        # regex fallback used if table_id 404s
    select: dict[str, list[str] | None] = field(default_factory=dict)
    note: str = ""

    @property
    def url(self) -> str:
        return f"{BASE_URL}/{self.path}/{self.table_id}"

    @property
    def folder_url(self) -> str:
        return f"{BASE_URL}/{self.path}/"


TABLES: list[TableSpec] = [
    # ---- TARGET VARIABLE ---------------------------------------------------
    TableSpec(
        name="lfs_underemployment",
        path="1B/LFS",
        table_id="0021B3FKEI2.px",
        title_pattern=r"Rates\s+Key\s+Employment\s+Indicators",
        select={
            # All four rates: the underemployment rate is the target, and the
            # other three are cheap context for sanity-checking the pull.
            "Rates": None,
            "Sex": ["Both sexes"],       # national headline, not sex-disaggregated
            "Year": None,                # 2005..2026
            "Month": None,               # Jan..Dec + "Annual"
        },
        note="Target variable. 4 x 1 x 22 x 13 = 1144 cells -> exceeds maxValues, gets chunked.",
    ),
    # ---- PREDICTOR: GDP ----------------------------------------------------
    TableSpec(
        name="qna_gdp_growth",
        path="2B/NA/QT/1SUM",
        table_id="0062B5BPRQ2.px",
        title_pattern=r"Gross Domestic Product by Industry, Growth Rates",
        select={
            "Industry": ["Gross Domestic Product"],
            "Type of Valuation": ["At Constant 2018 Prices"],
            "Year": None,
            "Period": None,              # Q1..Q4
        },
        note=(
            "PSA's published growth rate. NOTE its Year values are pairs "
            "('2000-2001'), i.e. these are YEAR-ON-YEAR growth rates per quarter, "
            "not quarter-on-quarter. Hence the levels table below."
        ),
    ),
    TableSpec(
        name="qna_gdp_levels",
        path="2B/NA/QT/1SUM",
        table_id="0052B5BPRQ1.px",
        title_pattern=r"Gross Domestic Product by Industry$",
        select={
            "Industry": ["Gross Domestic Product"],
            "Type of Valuation": ["At Constant 2018 Prices"],
            "Year": None,
            "Period": None,
        },
        note="Levels at constant 2018 prices, so transform can compute true QoQ growth.",
    ),
    # ---- PREDICTOR: CPI ----------------------------------------------------
    # The pre-computed year-on-year inflation table only starts Jan 2019 (~28
    # quarters), which is far too short to forecast on with two predictors. So we
    # pull the INDEX LEVELS from two tables that join into one continuous
    # 1994-2025 monthly series and compute inflation ourselves in transform.
    TableSpec(
        name="cpi_index_2018_2025",
        path="2M/PI/CPI/2018",
        table_id="0012M4ACP09.px",
        title_pattern=r"Consumer Price Index for All Income Households by Commodity Group \(2018=100\): January 2018",
        select={
            "Geolocation": ["PHILIPPINES"],     # national only; 119 areas available
            "Commodity Description": ["ALL ITEMS"],  # headline; 354 groups available
            "Year": None,
            "Period": None,                     # Jan..Dec + "Ave"
        },
        note="Current CPI index, Jan 2018 - Dec 2025.",
    ),
    TableSpec(
        name="cpi_index_backcast_1994_2017",
        path="2M/PI/CPI/2018",
        table_id="0012M4ACP15.px",
        title_pattern=r"Consumer Price Index for All Income Households by Commodity Group \(2018=100\): January 1994",
        select={
            "Geolocation": ["PHILIPPINES"],
            "Commodity Description": ["ALL ITEMS"],
            "Year": None,
            "Period": None,
        },
        note="Backcasted CPI index, Jan 1994 - Dec 2017. Same 2018=100 base, so it "
             "joins cleanly onto the table above.",
    ),
    TableSpec(
        name="cpi_yoy_official_validation",
        path="2M/PI/CPI/2018",
        table_id="0012M4ACP10.px",
        title_pattern=r"Year-on-Year Changes of the Consumer Price Index in Percent by Commodity Group",
        select={
            "Geolocation": ["PHILIPPINES"],
            "Commodity Description": ["ALL ITEMS"],
            "Year": None,
            "Period": None,
        },
        note="NOT a model input. PSA's official YoY inflation, 2019-2025, kept only "
             "to check our computed inflation against on the overlap.",
    ),
]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class IngestError(Exception):
    """Something went wrong in a way the user can act on.

    main() catches this and prints the message without a traceback - a bad label
    or a moved table is a data problem, not a crash.
    """


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #


class RateLimiter:
    """Keeps us under maxCalls per timeWindow using a sliding window.

    We stay a couple of calls below the published ceiling so a retry never tips
    us over.
    """

    def __init__(self, max_calls: int = MAX_CALLS - 2, window: float = TIME_WINDOW_SECONDS):
        self.max_calls = max(1, max_calls)
        self.window = window
        self._calls: list[float] = []

    def acquire(self) -> None:
        now = time.monotonic()
        # Drop timestamps that have aged out of the window.
        self._calls = [t for t in self._calls if now - t < self.window]
        if len(self._calls) >= self.max_calls:
            sleep_for = self.window - (now - self._calls[0]) + 0.05
            if sleep_for > 0:
                print(f"    [rate-limit] pausing {sleep_for:.1f}s to stay under "
                      f"{self.max_calls} calls / {self.window:.0f}s")
                time.sleep(sleep_for)
            now = time.monotonic()
            self._calls = [t for t in self._calls if now - t < self.window]
        self._calls.append(time.monotonic())


# --------------------------------------------------------------------------- #
# HTTP with retries
# --------------------------------------------------------------------------- #


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    limiter: RateLimiter,
    *,
    json_body: dict | None = None,
) -> requests.Response:
    """Issue one request, retrying only on failures that are worth retrying.

    Retries 429 and 5xx with exponential backoff plus jitter. Does NOT retry
    other 4xx: a 404 table or a malformed query will fail identically every
    time, so retrying just wastes the rate-limit budget.
    """
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        limiter.acquire()
        try:
            response = session.request(
                method,
                url,
                json=json_body,
                timeout=REQUEST_TIMEOUT,   # never omit: without it a hung server hangs us forever
                headers={"Accept": "application/json"},
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            # Transport-level failure - no response at all. Worth retrying.
            last_error = exc
            if attempt == MAX_ATTEMPTS:
                break
            backoff = min(2 ** (attempt - 1), 16) + random.uniform(0, 0.5)
            print(f"    [retry {attempt}/{MAX_ATTEMPTS}] {type(exc).__name__}; "
                  f"waiting {backoff:.1f}s")
            time.sleep(backoff)
            continue

        if response.status_code in RETRY_STATUSES:
            if attempt == MAX_ATTEMPTS:
                raise IngestError(
                    f"{method} {url} still failing with HTTP {response.status_code} "
                    f"after {MAX_ATTEMPTS} attempts."
                )
            # Honour Retry-After when the server tells us how long to wait.
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                backoff = float(retry_after)
            else:
                backoff = min(2 ** (attempt - 1), 16) + random.uniform(0, 0.5)
            print(f"    [retry {attempt}/{MAX_ATTEMPTS}] HTTP {response.status_code}; "
                  f"waiting {backoff:.1f}s")
            time.sleep(backoff)
            continue

        if response.status_code in (400, 403):
            # PXWeb uses these when a query is rejected - most often because it
            # asks for more than maxValues cells. Surface the body; it explains why.
            raise IngestError(
                f"{method} {url} rejected with HTTP {response.status_code}. "
                f"Usually this means the query exceeded maxValues={MAX_VALUES_PER_QUERY} "
                f"cells or named a variable that does not exist.\n"
                f"    Server said: {response.text[:400]}"
            )

        if response.status_code == 404:
            raise IngestError(
                f"{method} {url} returned HTTP 404 - the table or folder does not "
                f"exist at that path. PSA may have moved or renamed it."
            )

        response.raise_for_status()
        return response

    raise IngestError(f"{method} {url} failed after {MAX_ATTEMPTS} attempts: {last_error}")


# --------------------------------------------------------------------------- #
# Metadata resolution
# --------------------------------------------------------------------------- #


def _normalise(label: str) -> str:
    """Collapse case and whitespace so label matching survives cosmetic edits."""
    return re.sub(r"\s+", " ", label).strip().casefold()


# PXWeb decorates value labels with presentation junk that encodes hierarchy:
#   "..Gross Domestic Product"   <- leading dots are the indent depth
#   "0 - ALL ITEMS"              <- leading COICOP classification code
# Both are cosmetic and both move when PSA re-indents or re-codes a hierarchy,
# so we strip them before matching rather than hardcoding them into TABLES.
_LABEL_DECORATION = re.compile(r"^[\s.]*(?:[0-9][0-9.]*\s*-\s*)?")


def _loose(label: str) -> str:
    """Normalise a label with its hierarchy prefix stripped."""
    return _normalise(_LABEL_DECORATION.sub("", label))


def get_metadata(session: requests.Session, spec: TableSpec, limiter: RateLimiter) -> dict:
    """Fetch a table's variable list. Falls back to a title search if the id moved."""
    try:
        response = request_with_retry(session, "GET", spec.url, limiter)
        return response.json()
    except IngestError as exc:
        if "404" not in str(exc):
            raise
        print(f"    [!] {spec.table_id} not found; searching '{spec.path}' by title...")
        resolved = resolve_table_id_by_title(session, spec, limiter)
        print(f"    [!] resolved to {resolved} - update TABLES to make this permanent.")
        moved = f"{BASE_URL}/{spec.path}/{resolved}"
        return request_with_retry(session, "GET", moved, limiter).json()


def resolve_table_id_by_title(
    session: requests.Session, spec: TableSpec, limiter: RateLimiter
) -> str:
    """Re-find a table by matching its title, for when PSA changes an id."""
    listing = request_with_retry(session, "GET", spec.folder_url, limiter).json()
    pattern = re.compile(spec.title_pattern, re.IGNORECASE)
    matches = [e for e in listing if e.get("type") == "t" and pattern.search(e.get("text", ""))]
    if not matches:
        available = "\n      ".join(f"{e.get('id')}  {e.get('text')}" for e in listing)
        raise IngestError(
            f"No table in '{spec.path}' matches /{spec.title_pattern}/.\n"
            f"    Tables available there:\n      {available}"
        )
    return matches[0]["id"]


def resolve_selection(
    meta: dict, spec: TableSpec
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Turn the human labels in spec.select into the positional codes PXWeb wants.

    Returns (selection, time_variable_info) where selection is a list of
    {"code", "values", "labels"} - one entry per variable.
    """
    variables = {v["code"]: v for v in meta.get("variables", [])}

    unknown = [code for code in spec.select if code not in variables]
    if unknown:
        raise IngestError(
            f"Table '{spec.name}' has no variable(s) {unknown}.\n"
            f"    Variables this table actually has: {list(variables)}"
        )

    selection: list[dict[str, Any]] = []
    for code, wanted_labels in spec.select.items():
        var = variables[code]
        all_codes: list[str] = var["values"]
        all_labels: list[str] = var["valueTexts"]

        if wanted_labels is None:
            chosen_codes, chosen_labels = list(all_codes), list(all_labels)
        else:
            exact = {_normalise(lbl): (c, lbl) for c, lbl in zip(all_codes, all_labels)}
            chosen_codes, chosen_labels = [], []
            for wanted in wanted_labels:
                hit = exact.get(_normalise(wanted))
                if hit is None:
                    # Fall back to matching with the hierarchy decoration stripped,
                    # but only accept it if exactly one value matches - an ambiguous
                    # match is a silent wrong-column bug waiting to happen.
                    target = _loose(wanted)
                    loose_hits = [(c, lbl) for c, lbl in zip(all_codes, all_labels)
                                  if _loose(lbl) == target]
                    if len(loose_hits) == 1:
                        hit = loose_hits[0]
                        print(f"    matched '{wanted}' -> '{hit[1]}' (code {hit[0]})")
                    elif len(loose_hits) > 1:
                        raise IngestError(
                            f"Table '{spec.name}', variable '{code}': '{wanted}' is "
                            f"ambiguous - it matches {[lbl for _, lbl in loose_hits]}.\n"
                            f"    Use the exact label in TABLES."
                        )
                if hit is None:
                    raise IngestError(
                        f"Table '{spec.name}', variable '{code}': no value labelled "
                        f"'{wanted}'.\n"
                        f"    Available labels: {all_labels[:40]}"
                        f"{' ...' if len(all_labels) > 40 else ''}"
                    )
                chosen_codes.append(hit[0])
                chosen_labels.append(hit[1])

        selection.append({"code": code, "values": chosen_codes, "labels": chosen_labels})

    # Which variable is the time axis? We chunk along it when a query is too big.
    time_code = next(
        (v["code"] for v in meta.get("variables", []) if v.get("time")),
        None,
    )
    return selection, {"time_code": time_code or ""}


def count_cells(selection: list[dict[str, Any]]) -> int:
    """Cells a query will return = the product of the selected value counts."""
    total = 1
    for entry in selection:
        total *= len(entry["values"])
    return total


def chunk_by_time(
    selection: list[dict[str, Any]], time_code: str, limit: int = MAX_VALUES_PER_QUERY
) -> list[list[dict[str, Any]]]:
    """Split one oversized query into several that each stay under `limit` cells.

    This is the pagination step. We slice along the time variable because that
    keeps every chunk a contiguous, self-describing block of periods - easy to
    reason about and easy to concatenate later.
    """
    total = count_cells(selection)
    if total <= limit:
        return [selection]

    time_entry = next((e for e in selection if e["code"] == time_code), None)
    if time_entry is None:
        raise IngestError(
            f"Query needs {total} cells (limit {limit}) but has no time variable "
            f"to split on. Narrow the selection in TABLES instead."
        )

    # Cells contributed by every OTHER variable; that fixes the max periods per chunk.
    other = total // len(time_entry["values"])

    # If even ONE period busts the limit, splitting on time cannot rescue this
    # query - the fix has to be a narrower selection. Say so instead of quietly
    # emitting chunks that are still too big and failing later at the server.
    if other > limit:
        raise IngestError(
            f"A single '{time_code}' period already needs {other} cells, over the "
            f"{limit}-cell limit, so splitting on time cannot help.\n"
            f"    Narrow the non-time selection in TABLES (pick specific values "
            f"instead of None) for: "
            f"{[e['code'] for e in selection if e['code'] != time_code and len(e['values']) > 1]}"
        )

    per_chunk = max(1, limit // other)

    chunks: list[list[dict[str, Any]]] = []
    codes, labels = time_entry["values"], time_entry["labels"]
    for start in range(0, len(codes), per_chunk):
        sliced = []
        for entry in selection:
            if entry["code"] == time_code:
                sliced.append({
                    "code": entry["code"],
                    "values": codes[start:start + per_chunk],
                    "labels": labels[start:start + per_chunk],
                })
            else:
                sliced.append(dict(entry))
        chunks.append(sliced)
    return chunks


def build_query_body(selection: list[dict[str, Any]]) -> dict:
    """Assemble the POST body. Every variable is listed explicitly on purpose:
    a variable left out is returned in full, not dropped."""
    return {
        "query": [
            {
                "code": entry["code"],
                "selection": {"filter": "item", "values": entry["values"]},
            }
            for entry in selection
        ],
        # CSV, not json-stat2 - see the module docstring. PSA's json-stat2
        # emitter returns a single bogus value for any query size.
        "response": {"format": "csv"},
    }


# --------------------------------------------------------------------------- #
# Fetch + save
# --------------------------------------------------------------------------- #


def _stem(spec: TableSpec, today: str) -> str:
    """Filename stem: source, dataset, PSA table id, and pull date.

    Every raw landing file names where it came from and when it was pulled, so a
    file is traceable on its own even if it is copied out of this repo.
    """
    return f"{SOURCE_SLUG}_{spec.name}_{spec.table_id.replace('.px', '')}_{today}"


def fetch_table(
    session: requests.Session, spec: TableSpec, limiter: RateLimiter, today: str
) -> list[dict]:
    """Pull one table and write its raw response(s) plus metadata to data/raw/."""
    print(f"\n  {spec.name}")
    print(f"    GET  {spec.url}")

    meta = get_metadata(session, spec, limiter)
    title = meta.get("title", "(no title)")
    print(f"    title: {title}")

    selection, time_info = resolve_selection(meta, spec)
    total_cells = count_cells(selection)
    shape = " x ".join(f"{e['code']}({len(e['values'])})" for e in selection)
    print(f"    shape: {shape} = {total_cells} cells")

    chunks = chunk_by_time(selection, time_info["time_code"])
    if len(chunks) > 1:
        print(f"    over the {MAX_VALUES_PER_QUERY}-cell limit -> split into "
              f"{len(chunks)} calls on '{time_info['time_code']}'")

    stem = _stem(spec, today)

    # Save the metadata alongside the data: it is what makes the positional
    # codes in the raw file interpretable months from now.
    meta_path = RAW_DATA_DIR / f"{stem}_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    records: list[dict] = []
    for index, chunk in enumerate(chunks, start=1):
        body = build_query_body(chunk)
        cells = count_cells(chunk)
        suffix = "" if len(chunks) == 1 else f"_part{index}"
        out_path = RAW_DATA_DIR / f"{stem}{suffix}.csv"

        print(f"    POST chunk {index}/{len(chunks)} ({cells} cells) -> {out_path.name}")
        response = request_with_retry(session, "POST", spec.url, limiter, json_body=body)

        # Write the response bytes EXACTLY as received - byte-for-byte, including
        # PXWeb's UTF-8 BOM and its "." missing-value markers. A raw extract you
        # have already re-encoded or cleaned is not a raw extract.
        out_path.write_bytes(response.content)

        records.append({
            "name": spec.name,
            "chunk": index,
            "chunks_total": len(chunks),
            "table_id": spec.table_id,
            "title": title,
            "source_url": spec.url,
            "query_body": body,          # exact parameters sent - reproducible
            "retrieved_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "http_status": response.status_code,
            "cells_requested": cells,
            "output_file": out_path.name,
            "metadata_file": meta_path.name,
            "bytes": len(response.content),
            "sha256": hashlib.sha256(response.content).hexdigest(),
            "note": spec.note,
        })

    return records


# --------------------------------------------------------------------------- #
# Reading the raw CSV back  (Week 5 topic: response -> tabular structure)
# --------------------------------------------------------------------------- #

# PXWeb writes missing observations as "." (and sometimes ".." or blank). These
# are NOT zeros - they mean the survey did not run that period.
MISSING_MARKERS = {".", "..", "...", "-", ""}


def is_missing(cell: str) -> bool:
    return cell.strip() in MISSING_MARKERS


def read_pxweb_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    """Read one raw PXWeb CSV. utf-8-sig strips the BOM PXWeb prepends."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        return [], []
    return rows[0], [r for r in rows[1:] if r]


def preview(name: str, today: str, limit: int = 12) -> int:
    """Print a saved raw pull as a table, plus a coverage profile per period.

    The profile is the point: PXWeb hands back a dense rectangle of Year x Month
    whether or not the survey actually ran in a given month, so the only way to
    know your real observation frequency is to count the non-missing cells.
    """
    spec = next((s for s in TABLES if s.name == name), None)
    if spec is None:
        print(f"Unknown table '{name}'. Options: {[s.name for s in TABLES]}")
        return 1

    stem = _stem(spec, today)
    paths = sorted(RAW_DATA_DIR.glob(f"{stem}*.csv"))
    if not paths:
        print(f"No raw files matching '{stem}*.csv'. Run the ingest first.")
        return 1

    header: list[str] = []
    all_rows: list[list[str]] = []
    for path in paths:
        head, rows = read_pxweb_csv(path)
        if head:
            header = head
        all_rows.extend(rows)

    # Dimension columns keep their variable name as the header; PXWeb folds every
    # other selected variable into the data column names. Use the saved metadata
    # to tell the two apart instead of guessing from the values.
    meta_path = RAW_DATA_DIR / f"{stem}_meta.json"
    dim_names: set[str] = set()
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        dim_names = {v.get("text", "") for v in meta.get("variables", [])}
    dim_idx = [i for i, h in enumerate(header) if h in dim_names]
    data_idx = [i for i, h in enumerate(header) if i not in dim_idx]
    if not data_idx:                       # metadata missing: fall back to "last column"
        dim_idx, data_idx = list(range(len(header) - 1)), [len(header) - 1]

    # PXWeb decides per table which variables become rows ("stub") and which
    # become columns ("heading"). LFS keeps Year/Month as rows; the GDP and CPI
    # tables pivot them into 100+ columns and leave a single row. Both layouts
    # are legitimate raw output, so print whichever one is readable.
    wide = len(all_rows) <= 3 and len(data_idx) > 12
    rule = "-" * 100

    print(f"\n{spec.name}: {len(all_rows)} row(s) x {len(data_idx)} data column(s) "
          f"across {len(paths)} file(s)")

    if wide:
        print(f"layout: WIDE - periods are columns, not rows")
        print(rule)
        for row in all_rows:
            label = " / ".join(row[i] for i in dim_idx) or "(series)"
            present = [i for i in data_idx if not is_missing(row[i])]
            print(f"  {label}")
            print(f"    {len(present)} of {len(data_idx)} periods present")
            if present:
                print(f"    first: {header[present[0]]} = {row[present[0]]}")
                print(f"    last:  {header[present[-1]]} = {row[present[-1]]}")
            missing = [header[i] for i in data_idx if is_missing(row[i])]
            if missing:
                shown_missing = ", ".join(missing[:6])
                more = f" (+{len(missing) - 6} more)" if len(missing) > 6 else ""
                print(f"    missing: {shown_missing}{more}")
        print(rule)
        return 0

    widths = [max(10, min(26, len(h) + 2)) for h in header]
    print(rule)
    print(" | ".join(f"{header[i][:widths[i]]:>{widths[i]}}" for i in range(len(header))))
    print(rule)
    shown = [r for r in all_rows if any(not is_missing(r[i]) for i in data_idx)][:limit]
    for row in shown:
        print(" | ".join(f"{row[i][:widths[i]]:>{widths[i]}}" for i in range(len(header))))
    if not shown:
        print("(every observation in this pull is missing)")

    print(rule)
    for i in data_idx:
        present = sum(1 for r in all_rows if not is_missing(r[i]))
        if all_rows:
            print(f"  {header[i]}: {present} of {len(all_rows)} present "
                  f"({present / len(all_rows):.0%})")

    # ---- coverage profile: which periods actually carry an observation? ----
    year_i = header.index("Year") if "Year" in header else None
    period_i = next((i for i in dim_idx if header[i] in ("Month", "Period", "Quarter")), None)
    if year_i is None or not data_idx:
        return 0

    # Profile the underemployment column when present, else the first data column.
    target = next((i for i in data_idx if "underemploy" in header[i].lower()), data_idx[0])
    print(f"\ncoverage of '{header[target]}' by year:")
    counts: Counter[str] = Counter()
    periods: dict[str, list[str]] = {}
    for row in all_rows:
        if is_missing(row[target]):
            continue
        year = row[year_i]
        counts[year] += 1
        if period_i is not None:
            periods.setdefault(year, []).append(row[period_i])
    for year in sorted(counts):
        marks = " ".join(p[:3] for p in periods.get(year, []))
        print(f"  {year}: {counts[year]:>3}  {marks}")

    per_year = [c for y, c in counts.items()]
    if per_year:
        print(f"\n  min {min(per_year)} / max {max(per_year)} observations per year "
              f"across {len(counts)} years")
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pull PSA OpenSTAT tables into data/raw/.")
    parser.add_argument("--only", metavar="NAME", help="pull just one table by name")
    parser.add_argument("--preview", metavar="NAME", help="print a saved pull as rows; no network")
    parser.add_argument("--list", action="store_true", help="list configured tables; no network")
    parser.add_argument("--date", metavar="YYYY-MM-DD", help="override the date stamp in filenames")
    args = parser.parse_args(argv)

    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    today = args.date or datetime.now().strftime("%Y-%m-%d")

    if args.list:
        for spec in TABLES:
            print(f"{spec.name:<32} {spec.table_id:<16} {spec.path}")
            print(f"{'':<32} {spec.note}")
        return 0

    if args.preview:
        return preview(args.preview, today)

    specs = TABLES
    if args.only:
        specs = [s for s in TABLES if s.name == args.only]
        if not specs:
            print(f"Unknown table '{args.only}'. Options: {[s.name for s in TABLES]}")
            return 1

    print(f"PSA OpenSTAT ingest -> {RAW_DATA_DIR}")
    print(f"limits: {MAX_VALUES_PER_QUERY} cells/query, {MAX_CALLS} calls/{TIME_WINDOW_SECONDS}s")

    limiter = RateLimiter()
    manifest: list[dict] = []
    failures: list[tuple[str, str]] = []

    with requests.Session() as session:
        for spec in specs:
            try:
                manifest.extend(fetch_table(session, spec, limiter, today))
            except IngestError as exc:
                # One bad table should not lose the pulls that already succeeded.
                print(f"    [FAILED] {exc}")
                failures.append((spec.name, str(exc)))

    if manifest:
        existing: list[dict] = []
        if MANIFEST_PATH.exists():
            try:
                existing = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = []
        # Re-running the same day replaces that day's rows rather than duplicating.
        fresh = {(r["name"], r["chunk"], r["retrieved_at_utc"][:10]) for r in manifest}
        kept = [r for r in existing
                if (r.get("name"), r.get("chunk"), r.get("retrieved_at_utc", "")[:10]) not in fresh]
        MANIFEST_PATH.write_text(
            json.dumps(kept + manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    print("\n" + "=" * 78)
    print(f"saved {len(manifest)} raw file(s) to {RAW_DATA_DIR}")
    print(f"manifest: {MANIFEST_PATH.name} (source URL, exact query and timestamp per pull)")
    if failures:
        print(f"\n{len(failures)} table(s) failed:")
        for name, message in failures:
            print(f"  - {name}: {message.splitlines()[0]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
