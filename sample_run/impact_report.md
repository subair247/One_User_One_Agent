# Impact report

Seed `oua-practice`. Outcomes come from `simulator_report.json` / `outcome_events.jsonl` only; nothing else is used as evidence.

## 1. Headline (intention to treat)

Every viewer with a fresh trigger on the replay day is analysed in the arm they were randomised to (`replay/arms.csv`), whether or not the agent acted on them. Each viewer is judged on the goal the same pure rules assign at their first fresh trigger (for the holdout this is a measurement-only shadow assignment; nothing is ever sent or published to them).

| Arm | Viewers with a goal | Conclusive | Reached goal | Rate |
|---|---|---|---|---|
| Agent | 733 | 733 | 100 | 13.6% |
| Holdout | 719 | 719 | 89 | 12.4% |

**Difference (agent - holdout): +1.3 pp, 95% CI [-2.2 pp, +4.7 pp], p = 0.474.** The interval includes zero: this replay does not show a reliable effect, and we do not claim one.

## 2. By goal

| Goal | Agent n | Agent success | Holdout n | Holdout success | Diff | 95% CI |
|---|---|---|---|---|---|---|
| G01 | 59 | 37.3% | 51 | 29.4% | +7.9 pp | [-9.7 pp, +25.4 pp] |
| G02 | 182 | 14.3% | 162 | 11.1% | +3.2 pp | [-3.8 pp, +10.2 pp] |
| G03 | 1 | n/a | 0 | n/a | n/a | [n/a, n/a] |
| G04 | 1 | 0.0% | 1 | 0.0% | +0.0 pp | [+0.0 pp, +0.0 pp] |
| G05 | 45 | 4.4% | 49 | 6.1% | -1.7 pp | [-10.7 pp, +7.3 pp] |
| G06 | 15 | 6.7% | 9 | 22.2% | -15.6 pp | [-45.5 pp, +14.4 pp] |
| G07 | 348 | 4.0% | 349 | 3.7% | +0.3 pp | [-2.6 pp, +3.2 pp] |
| G08 | 24 | 29.2% | 29 | 24.1% | +5.0 pp | [-18.9 pp, +29.0 pp] |
| G09 | 12 | 25.0% | 10 | 20.0% | +5.0 pp | [-29.9 pp, +39.9 pp] |
| G12 | 46 | 54.3% | 59 | 49.2% | +5.2 pp | [-14.0 pp, +24.4 pp] |

Per-goal cells are small; intervals are wide and most include zero. Read them as direction, not proof.

## 3. Why "served vs everyone else" misleads

The agent only serves viewers it has a reason to serve: it needs a fresh trigger, a verifiable dialect, an eligible goal and consent, and it skips stale or already-served sessions. Those are the viewers who were already more active and easier to convert, so they would have done better without the agent (selection bias). The randomised holdout removes that: both arms have the same mix of viewers, so the only systematic difference is the agent.

On this replay the biased comparison gives served 14.2% (n=698) vs rest 11.9% (n=754), a gap of +2.2 pp, versus the ITT estimate of +1.3 pp.

## 4. Pilot re-analysis (`pilot/`)

| Comparison | Group A | Group B | Difference (95% CI) |
|---|---|---|---|
| Pilot report: served vs all others | 40.1% (87/217) | 17.7% (649/3674) | +22.4 pp [+15.8 pp, +29.1 pp] |
| **Randomised: all treatment vs all holdout** | 18.3% (349/1912) | 19.6% (387/1979) | **-1.3 pp [-3.8 pp, +1.2 pp]** |

The report's 40.1% vs 17.7% is reproduced exactly, but its "all other users" mixes the unserved treatment users with the entire holdout, and only 11.3% of the treatment arm was served. Using the randomised arms, the pilot shows -1.3 pp with an interval that includes zero: **no evidence of uplift, and the "roll out to every user" recommendation is not supported.**


## 5. Limits of this measurement

- One replay day, one seed: the simulator's randomness is part of the interval's uncertainty only through the sample size; re-run with `--seed` to see seed variation.
- The simulator's behaviour model is an assumption, not reality. Any uplift here is uplift *in the simulator*.
- Wald intervals are approximate for small cells (see by-goal table). Goals are assigned by rules from pre-trigger data only, so the same rule applies to both arms.
