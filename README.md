# National Underemployment Forecast: Is Philippine Economic Growth Creating Real Jobs?

## Problem Statement

I want to answer: "Can quarter-to-quarter changes in the Philippines' national underemployment rate be anticipated from GDP growth and inflation (CPI)?"

Underemployment — employed people who still want more hours or a better job — is a core measure of job quality, and in the Philippines it stays high even in years of strong economic growth, a pattern often called "jobless growth." Headline GDP and inflation figures dominate economic news, but it is not obvious whether faster growth or rising prices actually move underemployment, or in which direction. Reading how these indicators track underemployment quarter to quarter would give an early signal of labor-market slack before the next survey round is released. By analyzing GDP growth, inflation (CPI), and past underemployment together over time, this project aims to show whether these indicators anticipate movements in underemployment and whether economic growth reliably translates into better work opportunities — insights useful to labor and economic planners such as DOLE and NEDA.

## Audience

This project is for labor policy researchers, national government agencies such as DOLE and economic planning offices, journalists, and the general public who want to understand whether economic growth in the Philippines is translating into better-quality jobs. It is intended for people who make or inform decisions on employment and livelihood programs, as well as those who want to look beyond headline economic growth and unemployment figures to assess the country's labor market health.

## KPI or Key Metric

The main metric I want to track is **national underemployment rate (%)**, including its **one-quarter-ahead forecast**, alongside a derived **"growth-employment gap" indicator**, calculated as the GDP growth rate minus the change in the underemployment rate, to identify periods when economic growth does not correspond with improvements in job quality.

## Possible Final Dashboard

The dashboard should help the audience quickly see whether the current quarter's GDP growth and inflation trends point to rising or falling underemployment next quarter, and flag any period where growth is happening without a corresponding improvement in job quality.

## Data Source Notes

### Primary Source

- Name: PSA OpenSTAT — Labor Force Survey (LFS), Quarterly National Accounts (QNA), and Consumer Price Index (CPI) databases
- URL:
  - Underemployment (target): [https://openstat.psa.gov.ph/PXWeb/pxweb/en/DB/DB__1B__LFS/?tablelist=true](https://openstat.psa.gov.ph/PXWeb/pxweb/en/DB/DB__1B__LFS/?tablelist=true)
  - GDP growth (predictor):    [https://openstat.psa.gov.ph/PXWeb/pxweb/en/DB/DB__2B__NA__QT__1SUM/?tablelist=true](https://openstat.psa.gov.ph/PXWeb/pxweb/en/DB/DB__2B__NA__QT__1SUM/?tablelist=true)
  - CPI / inflation (predictor): [https://openstat.psa.gov.ph/PXWeb/pxweb/en/DB/DB__2M__PI__CPI__2018/?tablelist=true](https://openstat.psa.gov.ph/PXWeb/pxweb/en/DB/DB__2M__PI__CPI__2018/?tablelist=true)
- Format: PX-Web tables — exportable as CSV/XLSX/JSON; a PX-Web API endpoint is also available per table
- Coverage:
  - Underemployment rate: national, **April 2005–May 2026**. Mixed frequency, confirmed from the actual pull: **quarterly rounds (Jan/Apr/Jul/Oct) from 2005 through 2020, monthly from 2021 onward.** Aggregating to quarterly yields ~85 observations.
  - GDP: national, quarterly only, with growth rates and by-expenditure breakdowns
  - CPI: national, monthly, All-Income-Households index (2018=100, January 2018 - December 2025) with inflation rate
- Why it fits the problem: PSA is the official producer of all three series I need (underemployment, GDP, CPI), so the target and both predictors come from one authoritative source with a common national scope and a shared quarterly cadence once monthly series are aggregated.
- Known limitations:
  - GDP is quarterly only, so the whole model is capped at quarterly frequency.
  - The PX-Web **web interface** is JS-heavy; direct table links carry an "rxid" session token that breaks when shared, and table titles/paths drift between site updates. **Resolved:** this does not apply to the PX-Web **REST API**, whose paths carry no session token and are stable and shareable. See "Data Ingestion" below — the pipeline uses the API, and additionally resolves value codes and table ids from live metadata so title/path drift cannot silently corrupt a pull.
  - CSV/XLSX exports have a row/cell cap, so wide multi-variable pulls may need to be split by variable or year. The API enforces this as `maxValues: 1000` cells per query; the ingestion script splits oversized queries automatically.

### Fallback Source

- Name: CEIC Data — independent-platform mirror of the PSA underemployment, GDP, and CPI series
- URL:
  - Underemployment (target): [https://www.ceicdata.com/en/philippines/labour-force-survey-underemployment](https://www.ceicdata.com/en/philippines/labour-force-survey-underemployment)
  - GDP growth (predictor):   [https://www.ceicdata.com/en/indicator/philippines/real-gdp-growth](https://www.ceicdata.com/en/indicator/philippines/real-gdp-growth)
  - CPI / inflation (predictor): [https://www.ceicdata.com/en/philippines/consumer-price-index](https://www.ceicdata.com/en/philippines/consumer-price-index)
- Format: CSV / XLSX export plus a REST API per indicator (subscription tiers)
- Coverage:
  - Underemployment: national, monthly, from 2021
  - Real GDP growth: national, quarterly year-on-year %, history back to ~1999
  - CPI / inflation: national, monthly year-on-year %, long history (1987-present, ~460+ obs)
- Why it could still work:
  - Carries the same three PSA series on infrastructure fully independent of PSA's PX-Web portal.
  - A portal outage, broken rxid link, or export cap on the primary does not block the pipeline.
  - The identical variables stay pullable by API from a different platform.
  - All three are available at the frequencies the quarterly model needs.
- Known limitations:
  - Ultimately republishes PSA/PH-government figures, so it shares methodology-revision risk with the primary.
  - Origin independence is not possible for national statistics — PSA is the sole originator.
  - Access is subscription-gated with a capped free view.
  - The monthly underemployment series only starts in 2021, shrinking the usable sample versus PSA's 2005 history.
  - The GDP figure is quarterly year-on-year growth (not quarter-on-quarter).

## Data Ingestion (Phase 2)

`scripts/ingest.py` pulls all six source tables from the PSA OpenSTAT **PX-Web REST API** and
writes the unmodified responses to `data/raw/`.

```bash
python scripts/ingest.py                          # pull everything
python scripts/ingest.py --list                   # show configured tables, no network
python scripts/ingest.py --only lfs_underemployment
python scripts/ingest.py --preview lfs_underemployment   # read a saved pull back as a table
```

Setup:

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### API base

`https://openstat.psa.gov.ph/PXWeb/api/v1/en/DB`

API paths mirror the UI's flattened folder names — the UI's `DB__2B__NA__QT__1SUM` is the API's
`2B/NA/QT/1SUM` — but carry **no `rxid` token**, so they are stable and shareable.

### Tables pulled

| Name | Path / table | Role |
| --- | --- | --- |
| `lfs_underemployment` | `1B/LFS` `0021B3FKEI2.px` | **Target.** All 4 key employment rates, both sexes, 2005–2026 |
| `qna_gdp_growth` | `2B/NA/QT/1SUM` `0062B5BPRQ2.px` | GDP growth, constant 2018 prices, quarterly |
| `qna_gdp_levels` | `2B/NA/QT/1SUM` `0052B5BPRQ1.px` | GDP levels, so true QoQ can be computed |
| `cpi_index_2018_2025` | `2M/PI/CPI/2018` `0012M4ACP09.px` | CPI index, Jan 2018 – Dec 2025 |
| `cpi_index_backcast_1994_2017` | `2M/PI/CPI/2018` `0012M4ACP15.px` | CPI index backcast, Jan 1994 – Dec 2017 |
| `cpi_yoy_official_validation` | `2M/PI/CPI/2018` `0012M4ACP10.px` | Not a model input — PSA's official YoY inflation, used to validate ours |

### Which parameters control the returned data

Each table is an N-dimensional cube. A `GET` on the table URL returns its variables and legal
values; a `POST` to the same URL returns data for the values you name:

```json
{
  "query": [
    {"code": "Rates", "selection": {"filter": "item", "values": ["3"]}},
    {"code": "Sex",   "selection": {"filter": "item", "values": ["0"]}}
  ],
  "response": {"format": "csv"}
}
```

Two things decide what comes back:

1. **Which variables you list.** A variable you *omit* is **not** filtered out — PX-Web returns all
   of its values. Omitting `Geolocation` on a CPI table returns all 119 areas instead of just
   `PHILIPPINES`. The script always lists every variable explicitly.
2. **Which values you list.** These are **positional** codes (`"0"`, `"1"`, …), not identifiers. When
   PSA appends a new year the codes shift and a hardcoded query silently returns the *wrong year*.
   The script therefore asks for values by human label (`"Underemployment Rate"`) and resolves the
   code from live metadata on every run, failing loudly — and printing the available labels — if a
   label ever disappears.

### Limits and how they are handled

Read live from `/api/v1/en/?config`: `{"maxValues": 1000, "maxCalls": 10, "timeWindow": 10}`.

- **1000 cells per query.** The LFS pull is 4 rates × 1 sex × 22 years × 13 months = **1144 cells**,
  so it is split along the time variable into 988 + 156 and saved as `_part1` / `_part2`.
- **10 calls per 10 seconds.** A sliding-window rate limiter throttles ahead of the cap rather than
  reacting to a 429.
- Transport errors, `429` and `5xx` are retried up to 5 times with exponential backoff and jitter,
  honouring `Retry-After`. Other `4xx` are **not** retried — they fail identically every time.
- Every request has `timeout=30`.

### Gotchas found while building this — read before writing `transform.py`

1. **`json-stat2` is broken on this server.** It returns HTTP 200 and a well-formed document with
   correct dimensions, but the `value` array has length 1 regardless of query size
   (`"size": [19,13,4,1], "value": [1.0]`). The `csv` format on the identical query is complete and
   correct. This is why raw files are `.csv`. Do not "fix" it back to JSON without re-verifying.
2. **`Annual` / `Ave` rows are aggregates, not periods.** Every LFS year carries an `Annual` entry
   alongside its months, and CPI carries `Ave`. Averaging these into the series double-counts.
3. **The CSV layout differs per table.** PX-Web decides which variables become rows and which become
   columns. LFS keeps `Year`/`Month` as rows; GDP and CPI pivot periods into 100+ columns with a
   single data row. The transform must handle both.
4. **GDP growth is year-on-year, not quarter-on-quarter.** Its `Year` values are pairs
   (`2000-2001` … `2025-2026`), i.e. each quarter compared with the same quarter a year earlier.
   The problem statement above says "quarter-to-quarter" — `qna_gdp_levels` is pulled so true QoQ
   can be derived if that is what is wanted. **This needs an explicit decision in Phase 3.**
5. **Missing values arrive as `.`** — meaningful (the survey did not run), preserved verbatim.
6. **The usable modelling window ends 2025Q4**, bounded by CPI (ends Dec 2025), not by LFS (May 2026).

### Output and provenance

Raw landing files are named `<source>_<dataset>_<psa_table_id>_<pull_date>.csv`, so the source and
the pull date are readable from the filename alone:

```text
data/
  data_dictionary.md                          fields, types and ERD for everything below
  raw/
    psa_openstat_<dataset>_<table_id>_<YYYY-MM-DD>.csv        raw response, byte-for-byte
    psa_openstat_<dataset>_<table_id>_<YYYY-MM-DD>_part1.csv  when a query had to be split
    psa_openstat_<dataset>_<table_id>_<YYYY-MM-DD>_meta.json  table metadata as of that pull
    _manifest.json
```

Example: `psa_openstat_lfs_underemployment_0021B3FKEI2_2026-08-02_part1.csv`

**See [`data/data_dictionary.md`](data/data_dictionary.md)** for every field, its type and unit, the
entity-relationship diagram, and the conventions (missing-value markers, wide vs long layouts,
aggregate rows) that `transform.py` will need to respect.

`_manifest.json` records, per pull: table id, resolved title, **full source URL**, the **exact POST
body sent**, UTC **retrieval timestamp**, HTTP status, cell count, byte size and SHA-256. That is
the source-and-access-date record for every file, and it makes any pull reproducible and diffable
against a later one.

Raw extracts are committed on purpose — `.gitignore` deliberately does **not** exclude `data/raw/`.
