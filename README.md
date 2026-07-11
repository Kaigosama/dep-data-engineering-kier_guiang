# National Underemployment Forecast: Is Philippine Economic Growth Creating Real Jobs?

## Problem Statement

I want to answer: "Can national GDP growth and CPI inflation predict whether the Philippines' underemployment rate will rise or fall?"

## Audience

This project is for labor policy researchers, national government agencies such as DOLE and economic planning offices, journalists, and the general public who want to understand whether economic growth in the Philippines is translating into better-quality jobs. It is intended for people who make or inform decisions on employment and livelihood programs, as well as those who want to look beyond headline economic growth and unemployment figures to assess the country's labor market health.

## KPI or Key Metric

The main metric I want to track is **national underemployment rate (%)**, including its **one-quarter-ahead forecast**, alongside a derived **"growth-employment gap" indicator**, calculated as the GDP growth rate minus the change in the underemployment rate, to identify periods when economic growth does not correspond with improvements in job quality.

## Likely Data Source

I will explore:

- **PSA OpenSTAT — Labor Force Survey (Labor and Employment category) for national underemployment rate**: [openstat.psa.gov.ph/Database/Labor-and-Employment](https://openstat.psa.gov.ph/Database/Labor-and-Employment)
- **PSA OpenSTAT — National Accounts of the Philippines (GDP)**: [openstat.psa.gov.ph/Database/Economic-Accounts/National-Accounts-of-the-Philippines/Annual-National-Accounts](https://openstat.psa.gov.ph/Database/Economic-Accounts/National-Accounts-of-the-Philippines/Annual-National-Accounts)
- **PSA OpenSTAT — Consumer Price Index for All Income Households**: [openstat.psa.gov.ph/PXWeb/pxweb/en/DB/DB__2M__PI__CPI__2018/?tablelist=true](https://openstat.psa.gov.ph/PXWeb/pxweb/en/DB/DB__2M__PI__CPI__2018/?tablelist=true)

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
  - Underemployment rate: national, monthly and quarterly, January 2005–present
  - GDP: national, quarterly only, with growth rates and by-expenditure breakdowns
  - CPI: national, monthly, All-Income-Households index (2018=100, January 2018 - December 2025) with inflation rate
- Why it fits the problem: PSA is the official producer of all three series I need (underemployment, GDP, CPI), so the target and both predictors come from one authoritative source with a common national scope and a shared quarterly cadence once monthly series are aggregated.
- Known limitations:
  - GDP is quarterly only, so the whole model is capped at quarterly frequency.
  - The PX-Web portal is JS-heavy; direct table links carry an "rxid" session token that breaks when shared, and table titles/paths drift between site updates, so the pull step must resolve the table by navigation, not a hard-coded rxid URL.
  - CSV/XLSX exports have a row/cell cap, so wide multi-variable pulls may need to be split by variable or year.

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
  