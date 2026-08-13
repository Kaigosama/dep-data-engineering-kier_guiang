-- =====================================================================
-- Business questions - National Underemployment Forecast
-- Phase 3, Week 8
--
-- Run against data/processed/underemployment.db, built by:
--     python scripts/transform.py
--     python scripts/load_db.py
--
-- Execute all of these and write the answers to output/sql_results.md with:
--     python scripts/run_sql.py
--
-- Every query below answers a question from the README problem statement.
-- Each is preceded by that question in plain language, and by what the
-- result would have to look like to count as evidence either way.
-- =====================================================================


-- @question: Is economic growth creating better jobs?
-- The project's core question, at annual resolution. For each year, average
-- GDP growth against the average underemployment rate, and net up the four
-- quarter-on-quarter moves to see which direction job quality actually went.
-- HAVING restricts this to years the economy actually grew - the "jobless
-- growth" claim is only interesting where there was growth to begin with.
-- A year with positive growth AND a positive net change is a year where the
-- economy expanded while job quality got worse.
SELECT
    q.year,
    ROUND(AVG(CASE WHEN f.indicator_code = 'gdp_growth_yoy'
                   THEN f.value END), 2)                    AS avg_gdp_growth_pct,
    ROUND(AVG(CASE WHEN f.indicator_code = 'underemployment_rate'
                   THEN f.value END), 2)                    AS avg_underemployment_pct,
    ROUND(SUM(CASE WHEN f.indicator_code = 'underemployment_change_qoq'
                   THEN f.value END), 2)                    AS net_change_pp,
    CASE WHEN SUM(CASE WHEN f.indicator_code = 'underemployment_change_qoq'
                       THEN f.value END) > 0
         THEN 'growth without job-quality gains'
         ELSE 'job quality improved'
    END                                                     AS verdict
FROM fact_indicator_quarter f
INNER JOIN dim_quarter   q ON q.quarter_id     = f.quarter_id
INNER JOIN dim_indicator d ON d.indicator_code = f.indicator_code
WHERE f.indicator_code IN ('gdp_growth_yoy',
                           'underemployment_rate',
                           'underemployment_change_qoq')
  AND f.value IS NOT NULL
GROUP BY q.year
HAVING avg_gdp_growth_pct > 0
ORDER BY q.year;


-- @question: Which quarters had the widest growth-employment gap?
-- The dashboard's headline KPI: GDP growth minus the year-on-year change in
-- underemployment, both on the same window. A large positive gap is the
-- "jobless growth" signature - the economy grew and job quality did not
-- follow. Ranking them shows whether the pattern clusters in particular
-- periods or is spread evenly across twenty years.
SELECT
    a.quarter_id,
    q.lfs_round_month                        AS survey_round,
    ROUND(a.gdp_growth_yoy, 2)               AS gdp_growth_yoy_pct,
    ROUND(a.underemployment_change_yoy, 2)   AS underemployment_change_yoy_pp,
    ROUND(a.growth_employment_gap, 2)        AS growth_employment_gap
FROM analysis_quarterly a
INNER JOIN dim_quarter q ON q.quarter_id = a.quarter_id
WHERE a.growth_employment_gap IS NOT NULL
ORDER BY a.growth_employment_gap DESC
LIMIT 10;


-- @question: Does inflation coincide with worsening underemployment?
-- CPI is the project's second predictor, so this asks whether it carries any
-- signal at all before a model is fitted. Quarters are bucketed by inflation
-- band and the average quarter-on-quarter move in underemployment is taken
-- within each. If the averages rise monotonically with the band, inflation
-- plausibly tracks job quality. If they are flat or unordered, that is an
-- early warning that this predictor may be weak - which the project treats
-- as a publishable finding, not a failure.
SELECT
    CASE
        WHEN a.inflation_yoy <  2 THEN '1. under 2%'
        WHEN a.inflation_yoy <  4 THEN '2. 2 to 4%'
        WHEN a.inflation_yoy <  6 THEN '3. 4 to 6%'
        ELSE                           '4. over 6%'
    END                                                AS inflation_band,
    COUNT(*)                                           AS quarters,
    ROUND(AVG(a.inflation_yoy), 2)                     AS avg_inflation_pct,
    ROUND(AVG(a.underemployment_rate), 2)              AS avg_underemployment_pct,
    ROUND(AVG(a.underemployment_change_qoq), 3)        AS avg_qoq_change_pp
FROM analysis_quarterly a
WHERE a.inflation_yoy IS NOT NULL
  AND a.underemployment_change_qoq IS NOT NULL
GROUP BY inflation_band
ORDER BY inflation_band;


-- @question: Where is the dataset incomplete, and why?
-- Not a business question but a data-quality one, kept alongside the others
-- so a reviewer can see the gaps rather than discover them. LEFT JOIN from
-- the dimensions means indicators with no rows at all would still appear -
-- an INNER JOIN would hide exactly the failure worth catching.
-- Every gap here should be explainable by differencing at the window start:
-- the quarter-on-quarter target cannot exist in 2005Q2, and the year-on-year
-- columns cannot exist before 2006Q2.
SELECT
    d.indicator_code,
    d.unit,
    d.is_model_input,
    COUNT(f.value)                                     AS quarters_present,
    SUM(CASE WHEN f.value IS NULL THEN 1 ELSE 0 END)   AS quarters_missing,
    MIN(CASE WHEN f.value IS NOT NULL THEN f.quarter_id END) AS first_quarter
FROM dim_indicator d
LEFT JOIN fact_indicator_quarter f ON f.indicator_code = d.indicator_code
GROUP BY d.indicator_code, d.unit, d.is_model_input
HAVING quarters_missing > 0
ORDER BY quarters_missing DESC, d.indicator_code;
