# Known limitations (left out on purpose)

Each item is a deliberate scope choice, with the reason and what would change it.

| # | Limitation | Why it was left | What would close it |
|---|---|---|---|
| 1 | **Mock model only.** The provider interface exists (`model.py`) but no hosted model is wired in. The mock proposes the top rule goal, so the model adds no information in this run. | The pack is personal data and the policy forbids sending it to services that retain it. | A local or no-retention model plus its own evaluation against the rule baseline. |
| 2 | **Latency is partly modelled.** Dependency latency (model, store, tools) comes from a cost model in `config.py`; only compute time is measured. The "cache" is always perfectly consistent in the simulation. | An offline replay has no real network or store. | End-to-end measurement against real services. |
| 3 | **Goal choice is rule-based.** The funnel priority order is a judgement, not an optimised policy. G08 is suppressed for already-engaged viewers (5 or more play days in 14). | No evidence in the pack to learn a policy from. | A policy learned and tested against the holdout. |
| 4 | **Unclear rules are interpretations**: G03, G09 (and G07, G12). | The pack does not define them. | Written into each decision's `assumptions`; confirm with the goal owner. |
| 5 | **"Never played / never completed" rests on a 30-day window.** | Older history is not in the pack. | Longer history; until then confidence is reduced and the assumption is stated. |
| 6 | **`wa_new_episode_v2` is never used.** `dialect_name` for WhatsApp is the dialect code. WhatsApp goes only to Hindi-language viewers. | "A new episode is out" would be dishonest without episode release dates; the pack has no dialect display names; all approved templates are Hindi. | Episode release dates and display names in the catalogue. |
| 7 | **Impact is one replay day on one simulator.** By-goal cells are small, Wald intervals are approximate, and results change with the seed. | Scope of the take-home. | Several seeds and replay days; exact or bootstrap intervals. |
| 8 | **Open G03 goals are rare in this replay** (new viewers start with G01), so the corrected-rule path is exercised mainly by tests and the judge. | Priority follows funnel stage. | A replay with more day-two viewers; `control_log.json` shows exactly how many were affected. |
| 9 | **Frequency cap counts push and WhatsApp only**, as the policy says. In-app messages are separately capped at one per IST day. | Matches `interventions.json`. | None needed. |
| 10 | **No UI and no live service.** A CLI replay harness. | The brief says a working decide, act, measure loop matters more than a UI. | A thin read-only viewer over `sample_run/`. |
| 11 | **Pilot selection bias is argued from the comparison's design, not measured covariates.** | Events start 2026-08-31 and the pilot began 2026-09-01, so prior activity is not available. | Pre-pilot activity data. |
