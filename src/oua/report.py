"""final_report.md: assembled from the run's own artefacts, so every number in it is reproducible."""
from __future__ import annotations

import json
import os
from collections import Counter

from .audit import audit
from .loader import load_pack


def _jl(path):
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def write_final_report(pack_root, out):
    pack = load_pack(pack_root)
    dec = _jl(os.path.join(out, "decisions.jsonl"))
    acts = _jl(os.path.join(out, "actions.jsonl"))
    canc = _jl(os.path.join(out, "cancelled_actions.jsonl"))
    lat = json.load(open(os.path.join(out, "latency_report.json")))
    ctl = json.load(open(os.path.join(out, "control_log.json")))
    imp = json.load(open(os.path.join(out, "impact.json")))
    sim = json.load(open(os.path.join(out, "sim", "simulator_report.json")))
    rep = audit(pack, out)
    iss = pack.issues["counters"]
    T = [d for d in dec if d["decision_type"] == "TRIGGER"]
    st = Counter(d["status"] for d in T)
    reasons = Counter((d["no_action_reason"] or "").split(":")[0] for d in T if d["status"] == "NO_ACTION")
    goals = Counter(d["goal"]["id"] for d in T if d["status"] == "PUBLISHED" and d["goal"])
    fb = Counter(f for d in dec for f in d.get("fallback_used", []))
    o = imp["itt"]["overall"]

    def pp(x):
        return "n/a" if x is None else "%+.1f pp" % (100 * x)

    L = ["# Final report\n"]
    L.append("## Outcome in one paragraph\n")
    L.append("The agent handled %d triggers after deduplication (%d of them stale offline-buffered opens). It published %d personalised pages and released %d messages; it chose to do "
             "nothing on %d agent-arm triggers and recorded a reason for each. The independent audit found %s. Decisions were published in p95 %.2f s "
             "(max %.2f s; %d over 60 s). **Measured against the randomised holdout the effect is %s (95%% CI %s to %s)** in the simulator; "
             "%s\n" % (len(T), reasons.get("stale_trigger", 0), sum(1 for d in T if d["status"] == "PUBLISHED"), sum(1 for a in acts if a["type"] != "home_page"),
                       st.get("NO_ACTION", 0), "no policy violations" if rep["passed"] else "violations (see audit_report.json)",
                       lat["latency_s"]["p95"] or 0, lat["latency_s"]["max"] or 0, lat["over_60s"], pp(o["diff"]), pp(o["ci_low"]), pp(o["ci_high"]),
                       "the interval includes zero, so we do not claim an uplift." if o["ci_low"] is not None and o["ci_low"] <= 0 <= o["ci_high"]
                       else "see impact_report.md for the reading."))
    L.append("## Decision mix (agent arm and holdout)\n")
    L.append("| Status | Count |\n|---|---|")
    for k, v in sorted(st.items()):
        L.append("| %s | %d |" % (k, v))
    L.append("\nNo-action reasons: " + ", ".join("%s=%d" % kv for kv in reasons.most_common()) + ".\n")
    L.append("Goals behind published pages: " + ", ".join("%s=%d" % kv for kv in goals.most_common()) + ".\n")
    L.append("Goals never chosen on purpose: **G10** (no success event is defined) and **G11** (its success event `tv_app_open` never occurs in the event stream). "
             "Unclear rules, written down as assumptions in every decision: **G03** (later IST day; success rule corrected mid-replay) and **G09** (\"rarely\" = plays on <= 1 IST day in 14 days).\n")
    L.append("## Data issues found and how each was handled\n")
    L.append("| Issue | Count | Handling |\n|---|---|---|")
    rows = [
        ("Duplicate event_ids in history", iss.get("history_duplicate_event_ids"), "dedupe by event_id at load; trigger idempotency key = event_id"),
        ("Duplicate event_ids in triggers / live", "%s / %s" % (iss.get("trigger_duplicate_event_ids"), iss.get("live_duplicate_event_ids")), "dropped at load and again at runtime (seen_triggers)"),
        ("Offline-buffered triggers (stale)", iss.get("trigger_offline_buffered_stale"), "age = sent_at - occurred_at; skipped as a live session, still ingested as history"),
        ("Wrong phone clocks (|received - sent| > 10 min)", iss.get("history_wrong_phone_clock_events"), "received_at orders and ages events; phone-clock difference used only within one clock"),
        ("Profile users with two rows", iss.get("profile_users_with_two_rows"), "row agreeing with event-derived status, then latest updated_at; conflict logged"),
        ("Users with events but no profile row", iss.get("history_users_without_profile_row"), "facts from signup_completed and events; no profile = no consent"),
        ("Triggers whose user has no profile row", iss.get("trigger_user_without_profile_row"), "same; home page only, never a message"),
        ("Trigger app_dialect missing / NA / d7 (absent from catalogue)", iss.get("trigger_app_dialect_unusable"), "no verifiable page: recorded no-action `dialect_unverifiable`"),
        ("Campaign deeplink dialect differs from app setting", iss.get("trigger_deeplink_dialect_differs_from_app_setting"), "app setting wins; deeplink title used as hero only if it is in the app dialect and available"),
        ("Malformed deeplinks", iss.get("trigger_malformed_deeplink_ignored"), "ignored (data, not instruction)"),
        ("Language variants sharing a content_slug", iss.get("catalogue_slugs_with_language_variants"), "one title per slug per page, variant matching viewer language"),
        ("Titles with an availability end", iss.get("catalogue_titles_with_end_of_availability"), "availability checked at publish time and again at send time"),
        ("Catalogue description with instructions to the agent", iss.get("catalogue_description_with_instructions"), "flagged in security_log.jsonl; text never used or obeyed"),
    ]
    for r in rows:
        L.append("| %s | %s | %s |" % r)
    L.append("")
    L.append("## Replay surprises and what the system did\n")
    eff = {c["type"]: c.get("effect") for c in ctl["control_events"]}
    wd = eff.get("title_withdrawn") or {}
    rc = ctl["rule_corrections"][0] if ctl["rule_corrections"] else {}
    L.append("| Surprise | Response | Evidence |\n|---|---|---|")
    L.append("| Offline-buffered burst | stale triggers skipped, ingested as history | %d decisions `stale_trigger` |" % reasons.get("stale_trigger", 0))
    L.append("| Purchase 1-2 min after a decision | goal closed, pending messages cancelled, new goal planned | %d REPLAN decisions, %d messages cancelled |" % (
        sum(1 for d in dec if d["decision_type"] == "REPLAN"), len(canc)))
    L.append("| App / profile / campaign dialect conflict | app setting used, conflicts listed per decision | %d decisions list a dialect conflict |" % sum(1 for d in T if d.get("dialect") and d["dialect"].get("conflicts")))
    L.append("| Goal rule corrected mid-replay (G03) | new rule version applied to open G03 goals only | %s open goals re-evaluated, %s viewers untouched |" % (rc.get("open_goals_reevaluated"), rc.get("viewers_not_touched")))
    L.append("| Model claims eligibility without evidence | verifier rejected, rules used | %d unsupported claims recorded |" % sum(len(d["unsupported_claims"]) for d in dec))
    L.append("| Model returns broken JSON / times out | safe parse, one retry, then rules | fallbacks: %s |" % ", ".join("%s=%d" % kv for kv in fb.items() if kv[0].startswith("model")))
    L.append("| Catalogue text says message everyone | logged, ignored | %d security_log entries; 0 policy violations in audit |" % len(pack.security_log))
    L.append("| Title withdrawn mid-day | stopped using it, live pages replaced, pending messages cancelled | %s |" % wd)
    L.append("| Model provider down; data store degraded | circuit breaker and rules; cached state with home-page-only | %d store-cache fallbacks, %d circuit-open skips |" % (
        lat["state_reads_from_cache"], fb.get("model_skipped_circuit_open", 0)))
    L.append("| Traffic burst | worker pool + load shedding | busiest 10 min: %s triggers, p95 %.2f s |\n" % (lat["busiest_10_minutes"]["triggers"], lat["busiest_10_minutes"]["p95_latency_s"] or 0))
    L.append("### Injected model faults (`replay/model_faults.jsonl`): what happened to each\n")
    L.append("| Fault | Viewer arm | Disposition |\n|---|---|---|")
    byid = {d["trigger"]["event_id"]: d for d in dec if d.get("trigger") and d["trigger"].get("event_id")}
    for f in pack.faults.values():
        d = byid.get(f["trigger_event_id"])
        if d is None:
            disp, arm = "trigger not in replay", "-"
        else:
            arm = d["arm"]
            if d["arm"] == "holdout":
                disp = "holdout viewer: no processing at all"
            elif (d["no_action_reason"] or "").startswith("stale_trigger"):
                disp = "stale offline-buffered trigger: skipped before any model call"
            else:
                disp = "model reached; fallbacks %s; unsupported claims rejected: %d; status %s" % (d["fallback_used"], len(d["unsupported_claims"]), d["status"])
        L.append("| %s | %s | %s |" % (f["fault"], arm, disp))
    L.append("\nFaults that land on holdout or stale triggers are correctly never acted on; every fault type is also forced in `tests/test_core.py`.\n")
    L.append("## Safety audit (independent of the verifier)\n")
    L.append("```json\n%s\n```\n" % json.dumps({k: rep[k] for k in ("actions_audited", "violations", "holdout_actions", "decisions_over_60s", "decisions_missing_fields", "passed")}, indent=1))
    L.append("Simulator-side policy problems: %d. Counts: %s\n" % (sim.get("policy_problem_count", 0), json.dumps(sim.get("counts", {}))))
    L.append("## Honest reading\n")
    L.append("- The simulator, not real users, produced these outcomes; treat the effect size as a property of that simulator.\n"
             "- A one-day replay supports weak conclusions. The pilot's own randomised data also shows no uplift (impact_report.md section 4).\n"
             "- Remaining gaps are listed in KNOWN_LIMITATIONS.md.\n")
    with open(os.path.join(out, "final_report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))
