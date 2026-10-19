# Final report

## Outcome in one paragraph

The agent handled 2679 triggers after deduplication (201 of them stale offline-buffered opens). It published 887 personalised pages and released 403 messages; it chose to do nothing on 467 agent-arm triggers and recorded a reason for each. The independent audit found no policy violations. Decisions were published in p95 0.41 s (max 1.90 s; 0 over 60 s). **Measured against the randomised holdout the effect is +1.3 pp (95% CI -2.2 pp to +4.7 pp)** in the simulator; the interval includes zero, so we do not claim an uplift.

## Decision mix (agent arm and holdout)

| Status | Count |
|---|---|
| HOLDOUT_NO_ACTION | 1325 |
| NO_ACTION | 467 |
| PUBLISHED | 887 |

No-action reasons: stale_trigger=201, live_page_active=163, dialect_unverifiable=91, no_eligible_measurable_goal=12.

Goals behind published pages: G07=463, G02=210, G01=59, G12=54, G05=44, G08=32, G09=15, G06=8, G03=1, G04=1.

Goals never chosen on purpose: **G10** (no success event is defined) and **G11** (its success event `tv_app_open` never occurs in the event stream). Unclear rules, written down as assumptions in every decision: **G03** (later IST day; success rule corrected mid-replay) and **G09** ("rarely" = plays on <= 1 IST day in 14 days).

## Data issues found and how each was handled

| Issue | Count | Handling |
|---|---|---|
| Duplicate event_ids in history | 2176 | dedupe by event_id at load; trigger idempotency key = event_id |
| Duplicate event_ids in triggers / live | 90 / 7 | dropped at load and again at runtime (seen_triggers) |
| Offline-buffered triggers (stale) | 443 | age = sent_at - occurred_at; skipped as a live session, still ingested as history |
| Wrong phone clocks (|received - sent| > 10 min) | 4319 | received_at orders and ages events; phone-clock difference used only within one clock |
| Profile users with two rows | 30 | row agreeing with event-derived status, then latest updated_at; conflict logged |
| Users with events but no profile row | 50 | facts from signup_completed and events; no profile = no consent |
| Triggers whose user has no profile row | 194 | same; home page only, never a message |
| Trigger app_dialect missing / NA / d7 (absent from catalogue) | 178 | no verifiable page: recorded no-action `dialect_unverifiable` |
| Campaign deeplink dialect differs from app setting | 152 | app setting wins; deeplink title used as hero only if it is in the app dialect and available |
| Malformed deeplinks | 3 | ignored (data, not instruction) |
| Language variants sharing a content_slug | 514 | one title per slug per page, variant matching viewer language |
| Titles with an availability end | 103 | availability checked at publish time and again at send time |
| Catalogue description with instructions to the agent | 1 | flagged in security_log.jsonl; text never used or obeyed |

## Replay surprises and what the system did

| Surprise | Response | Evidence |
|---|---|---|
| Offline-buffered burst | stale triggers skipped, ingested as history | 201 decisions `stale_trigger` |
| Purchase 1-2 min after a decision | goal closed, pending messages cancelled, new goal planned | 1 REPLAN decisions, 0 messages cancelled |
| App / profile / campaign dialect conflict | app setting used, conflicts listed per decision | 417 decisions list a dialect conflict |
| Goal rule corrected mid-replay (G03) | new rule version applied to open G03 goals only | 0 open goals re-evaluated, 412 viewers untouched |
| Model claims eligibility without evidence | verifier rejected, rules used | 1 unsupported claims recorded |
| Model returns broken JSON / times out | safe parse, one retry, then rules | fallbacks: model_http_503=12, model_skipped_circuit_open=6, model_malformed_output_retry=1 |
| Catalogue text says message everyone | logged, ignored | 1 security_log entries; 0 policy violations in audit |
| Title withdrawn mid-day | stopped using it, live pages replaced, pending messages cancelled | {'live_pages_affected': 19, 'pages_replaced': 8, 'pending_messages_cancelled': 0} |
| Model provider down; data store degraded | circuit breaker and rules; cached state with home-page-only | 3 store-cache fallbacks, 6 circuit-open skips |
| Traffic burst | worker pool + load shedding | busiest 10 min: 239 triggers, p95 0.00 s |

### Injected model faults (`replay/model_faults.jsonl`): what happened to each

| Fault | Viewer arm | Disposition |
|---|---|---|
| unsupported_claim | agent | model reached; fallbacks ['rules_after_unsupported_claim']; unsupported claims rejected: 1; status PUBLISHED |
| unsupported_claim | agent | stale offline-buffered trigger: skipped before any model call |
| unsupported_claim | agent | stale offline-buffered trigger: skipped before any model call |
| malformed_output | agent | model reached; fallbacks ['model_malformed_output_retry']; unsupported claims rejected: 0; status PUBLISHED |
| timeout | holdout | holdout viewer: no processing at all |

Faults that land on holdout or stale triggers are correctly never acted on; every fault type is also forced in `tests/test_core.py`.

## Safety audit (independent of the verifier)

```json
{
 "actions_audited": 1298,
 "violations": {},
 "holdout_actions": 0,
 "decisions_over_60s": 0,
 "decisions_missing_fields": 0,
 "passed": true
}
```

Simulator-side policy problems: 0. Counts: {"actions_total": 1298, "home_page_accepted": 895, "home_page_published_but_not_shown": 14, "home_page_shown_personalised": 821, "in_app_message_queued": 82, "in_app_message_requested": 82, "organic_purchases_scripted": 36, "push_clicked": 3, "push_delivered": 268, "push_requested": 309, "push_undeliverable_token": 41, "whatsapp_delivered": 12, "whatsapp_requested": 12}

## Honest reading

- The simulator, not real users, produced these outcomes; treat the effect size as a property of that simulator.
- A one-day replay supports weak conclusions. The pilot's own randomised data also shows no uplift (impact_report.md section 4).
- Remaining gaps are listed in KNOWN_LIMITATIONS.md.
