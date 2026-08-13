# Cleaning Log

Every decision that changes what ends up in `data/processed/`, with the reasoning behind it.

`data/data_dictionary.md` says **what** the pipeline does. This file says **why**, and what
evidence the decision rests on. Where a decision could reasonably have gone the other way, the
alternative is stated too — a cleaning decision with no visible alternative is usually a
decision nobody actually made.

Each entry carries a **What would change this** line. These are judgements about a specific
dataset, not laws; recording the condition that would overturn them is what keeps them
honest when the data updates.

---

## 1. Aggregate rows are dropped

**Rule.** LFS `Annual` rows and CPI `Ave` rows are removed before anything else.

**Why.** They sit in the same column as the months and look exactly like periods, but they are
annual aggregates. Averaging them into a quarterly series counts the year twice.

**Evidence.** Every LFS year carries an `Annual` row — 22 of them. The transform *asserts* they
are present and fails loudly if none are found, because their disappearance would more likely
mean the filter stopped matching than that PSA removed them.

**What would change this.** Nothing. This is a correctness issue, not a judgement.

---

## 2. A quarter's LFS value is its round-month value

**Rule.** January, April, July and October map to Q1–Q4. The other eight months, which exist
from 2021 onward, are discarded.

**Why.** The LFS ran quarterly through 2020 and monthly from 2021. Averaging the three months
where they exist would change the estimator halfway through the series.

**Evidence.** Measured across the 21 quarters where both are computable: the round-month value
sits **+1.17 pp above** the 3-month mean on average, higher in **17 of 21** quarters, worst case
4.42 pp at 2021Q3. That is a systematic offset, not noise — so a mixed series would step down by
roughly a point at the 2021 join, and that step would be an artefact of cleaning rather than
anything the labour market did.

**Alternative rejected.** Use 3-month means from 2021 and round months before. More precise for
recent quarters, but it makes the most recent — and most decision-relevant — part of the series
non-comparable with its own history.

**What would change this.** If the analysis were restricted to 2021 onward, the monthly data is
richer and the 3-month mean would be the better estimator. The check that produced the numbers
above runs on every execution, so the bias is re-measured whenever the data updates.

---

## 3. The window is 2005Q2 – 2025Q4

**Rule.** 83 quarters. Anything outside is dropped from the processed output.

**Why.** Both ends are data facts, not preferences. The LFS began in April 2005, so there is no
2005Q1 round to difference from. CPI coverage ends December 2025 — LFS runs to May 2026 and GDP
to 2026Q1, but a quarter is only usable when all three exist.

**What would change this.** A CPI release extending past December 2025 moves the end forward.
The window is derived from constants at the top of `transform.py`, not scattered through it.

---

## 4. Nothing is ever filled

**Rule.** No `fillna`, no forward-fill, no interpolation, anywhere in the pipeline. Missing
values stay missing and are flagged `value_status = 'missing'`.

**Why.** The largest gaps are in the **target**. Imputing them would mean inventing values for
the thing the project is trying to forecast, and any model fitted on top would be partly
learning our filling rule rather than the labour market.

**Consequence, accepted.** Every null in `data/processed/` means "genuinely absent", and that is
the only thing it can mean. Downstream code never has to ask whether a number is real.

**What would change this.** Nothing for the target. If a *predictor* developed sparse gaps, an
explicit imputation could be justified — but it would need its own flag column so a model can
be fitted with and without it.

---

## 5. Partial CPI quarters are dropped

**Rule.** A quarter needs all three monthly index values or it is excluded.

**Why.** A mean of two months is not comparable with a mean of three; mixing them would put a
subtle discontinuity into the predictor.

**What would change this.** Nothing at present — the CPI series is complete across the window.
The rule exists to make sure a future partial quarter fails visibly rather than silently
producing a slightly-wrong average.

---

## 6. CPI is stitched from two tables and validated against PSA

**Rule.** The 1994–2017 backcast and the 2018–2025 current index are concatenated into one
monthly series, then aggregated to quarters and turned into year-on-year inflation.

**Why.** PSA's ready-made inflation table starts January 2019 — about 28 quarters, far too short
to forecast on with two predictors. Both index tables share the 2018=100 base, so they join
continuously.

**Evidence.** Computed inflation is compared against PSA's published year-on-year series across
the 28-quarter overlap on every run. Worst divergence: **0.042 pp**. Nothing else in the pipeline
would catch a mis-joined backcast leg — the numbers would simply be wrong and entirely plausible.

**What would change this.** If PSA published a long-history inflation series directly, the
stitch would become unnecessary.

---

## 7. GDP enters the model year-on-year, not quarter-on-quarter

**Rule.** The predictor is PSA's published year-on-year growth plus its one-quarter
acceleration. `gdp_growth_qoq` is still computed but flagged `is_model_input = false`.

**Why.** This looked like a contradiction with the problem statement, which asks about
"quarter-to-quarter changes". It is not: that phrase describes the **target** — the change in the
underemployment rate — and never constrained the predictors' base period.

**Evidence.** Measured 2005–2025: of the variance in the *underemployment rate's* QoQ change,
**23 %** is seasonal — workable with quarter dummies. For GDP's QoQ change from the unadjusted
levels table it is **94 %**, with seasonal means running from −9.93 % (Q4→Q1) to +13.86 %
(Q3→Q4). Modelling on that would mostly be modelling the calendar.

**Alternative rejected.** Deseasonalise GDP ourselves and use true QoQ. Defensible, but it means
owning a seasonal adjustment instead of pointing at PSA's published figure — and 94 % of the
predictor's variance would be removed by our own choice of method.

**What would change this.** If PSA published a seasonally adjusted quarterly series, true QoQ
would become available without us adjusting anything.

---

## 8. The target keeps the quarter-on-quarter basis

**Rule.** The model target is `underemployment_change_qoq`. The problem statement is unedited.

**Why.** The QoQ change is only 23 % seasonal, and its year-on-year counterpart is actually
*noisier* (sd 2.19 pp vs 2.00 pp) — so switching to YoY would have cost the original framing and
bought nothing.

**What would change this.** Evidence that the seasonal component is larger than measured, or a
decision to forecast annual rather than quarterly movements.

---

## 9. The growth-employment gap is growth **plus** the change

**Rule.** `growth_employment_gap = gdp_growth_yoy + underemployment_change_yoy`.

**Why.** The README originally specified "GDP growth minus the change in the underemployment
rate", which contradicts its own stated purpose. An improvement in underemployment is a *fall*,
so subtracting the change makes the metric largest exactly when job quality improved most.

**Evidence.** Under the original formula the top-ranked "widest gap" quarter was 2022Q3 — growth
7.7 % with underemployment down 7.24 pp, one of the strongest jobs recoveries in the series,
ranked as the worst quarter. Under the corrected formula the top entry is 2012Q3: growth 7.4 %
with underemployment **up** 3.69 pp, which is what jobless growth actually looks like.

**How it surfaced.** Not by reading the code — by writing a query that asked for the worst
quarters and getting back the best ones. A query that merely dumped the column would have passed.

**Known limitation.** Where GDP growth is deeply negative the gap goes very negative regardless
of job quality: 2020Q2 scores −11.31. The metric is saying "job quality held up better than
output did", which is true but reads as "best quarter". The dashboard should only interpret the
gap where growth is positive.

**What would change this.** Renaming the indicator to something where a large value is good
would make the original formula correct. That option was considered and rejected — the dashboard
is a jobless-growth monitor, so the headline number should flag bad quarters.

---

## 10. Lags are derived before the window is clipped

**Rule.** All differencing happens on the full 1994Q1–2026Q4 span; the clip to 83 quarters is the
last step.

**Why.** GDP and CPI have real 2005Q1 values — the window starts at 2005Q2 because of the LFS,
not because of them. Clipping first would leave the first modelling quarter with no predictor
values and silently discard a usable observation.

**Evidence.** This was a genuine bug introduced during the pandas port. It was caught because
the previous output was committed, so the regression showed up as a one-line diff: `2005Q2` lost
`gdp_growth_yoy_lag1 = 5` and `inflation_yoy_lag1 = 7.2419`. Neither reading the code nor
eyeballing the numbers would have found it.

---

## 11. Range bounds are wide enough to admit the 2020 outlier

**Rule.** Unemployment is checked against `[0, 25]`, not a band fitted to the observed data.

**Why.** April 2020's unemployment rate of 17.61 % is real. A range check quietly narrowed until
an inconvenient value disappears is worse than no range check, because it looks like diligence.

**What would change this.** Nothing. Bounds should encode what is *physically possible*, not
what has happened so far.

---

## 12. The inflation tolerance was tightened to match the data

**Rule.** Computed inflation must agree with PSA's published series within **0.10 pp**.

**Why.** The bound started at 0.20 as a placeholder set before the first run. Observed worst case
is 0.042 pp, so at 0.20 the check could not have failed on any plausible regression — which makes
it equivalent to no check.

**What would change this.** The tolerance cannot go to zero: we compute year-on-year change on
the quarterly mean index while PSA publishes monthly rates that we average, so a small
approximation gap is expected and correct.

---

## Reproducibility

`python scripts/transform.py --check-reproducible` rebuilds the entire processed layer into a
scratch directory and compares checksums against `data/processed/`. It writes nothing and exits
non-zero on any difference.

It catches two distinct failures:

- **non-determinism** in the transform — unstable sort order, dict iteration, a timestamp leaking
  into a data file;
- **drift** between the committed dataset and what the current code produces, which is what
  happens when the transform is edited and the output is not regenerated.

`_transform_report.json` is deliberately excluded from the comparison: it records the run
timestamp, so it *should* differ between runs. Reproducibility means the data is stable, not that
every byte on disk is frozen.

The validation checks themselves have been **negative-tested** — a mid-series null, a moved null,
an invalid category, a duplicated fact row, a duplicated join key and an injected drift were each
introduced deliberately to confirm the corresponding check fails. A check that has never been
seen to fail is a check nobody knows works.
