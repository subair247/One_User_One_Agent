"""Post-hoc audit of actions.jsonl, written separately from the verifier: re-checks every published action against the raw pack."""
from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict

from .timeutil import parse, ist_day, in_quiet_hours


def audit(pack, out_dir):
    acts = [json.loads(l) for l in open(os.path.join(out_dir, "actions.jsonl"), encoding="utf-8") if l.strip()]
    decisions = [json.loads(l) for l in open(os.path.join(out_dir, "decisions.jsonl"), encoding="utf-8") if l.strip()]
    withdrawn = {}
    for c in pack.control_events:
        if c["type"] == "title_withdrawn":
            withdrawn[c["title_id"]] = parse(c["at"])
    forb = pack.interventions["push"]["copy"]["forbidden_phrases"]
    prices = {p["price_inr"] for p in pack.pricing["plans"]}
    templates = {t["template_id"]: t for t in pack.interventions["whatsapp"]["approved_templates"]}

    def avail(t, at):
        return (t is not None and t.available_from <= at and (t.available_until is None or at < t.available_until)
                and not (t.title_id in withdrawn and at >= withdrawn[t.title_id]))

    def opted_out(uid, channel, at):
        return any(e["event"] == "notification_opt_out" and e["properties"].get("channel") in (channel, None) and e["_recv"] <= at
                   for e in pack.events_by_user.get(uid, []))

    v = Counter()
    problems = []
    per_day = defaultdict(int)
    for u, evs in pack.events_by_user.items():
        for e in evs:
            if e["event"] == "message_delivered":
                per_day[(u, ist_day(e["_eff"]))] += 1
    profiles = {}
    for u, rows in pack.profiles.items():
        profiles[u] = max(rows, key=lambda r: r["_updated"])

    def bad(kind, a, detail=""):
        v[kind] += 1
        if len(problems) < 50:
            problems.append({"problem": kind, "action_id": a["action_id"], "user_id": a["user_id"], "detail": detail})

    for a in sorted(acts, key=lambda a: a.get("send_at") or a["published_at"]):
        u, t = a["user_id"], a["type"]
        v["actions"] += 1
        if pack.arms.get(u) != "agent":
            bad("action_on_non_agent_viewer", a)
        if t == "home_page":
            at = parse(a["published_at"])
            ids = ([a["hero_title_id"]] if a.get("hero_title_id") else []) + [x for r in a["rows"] for x in r["title_ids"]]
            slugs = [pack.titles[x].slug for x in ids]
            if len(slugs) != len(set(slugs)):
                bad("duplicate_content_on_page", a)
            if len(a["rows"]) > 6 or any(not 3 <= len(r["title_ids"]) <= 10 for r in a["rows"]):
                bad("row_limits", a)
            for x in ids:
                ti = pack.titles[x]
                if ti.dialect != a["dialect"]:
                    bad("page_title_wrong_dialect", a, x)
                if not avail(ti, at):
                    bad("page_title_unavailable", a, x)
        else:
            at = parse(a["send_at"])
            if t in ("push", "whatsapp"):
                prof = profiles.get(u)
                flag = "notifications_opt_in" if t == "push" else "whatsapp_opt_in"
                if not prof or prof[flag] != "true":
                    bad("message_without_consent", a)
                if opted_out(u, t, at):
                    bad("message_after_opt_out", a)
                if in_quiet_hours(at):
                    bad("message_in_quiet_hours", a)
                per_day[(u, ist_day(at))] += 1
                if per_day[(u, ist_day(at))] > 1:
                    bad("frequency_cap", a)
            if a.get("title_id"):
                ti = pack.titles.get(a["title_id"])
                if not avail(ti, at):
                    bad("message_title_unavailable", a, a["title_id"])
            txt = ((a.get("title") or "") + " " + (a.get("body") or "")).lower()
            if any(p in txt for p in forb):
                bad("forbidden_phrase", a)
            if len(a.get("title") or "") > 40 or len(a.get("body") or "") > 120:
                bad("copy_too_long", a)
            if any(int(x) not in prices for x in re.findall(r"₹\s*(\d+)", (a.get("title") or "") + (a.get("body") or ""))):
                bad("invalid_price", a)
            if t == "whatsapp" and a.get("template_id") not in templates:
                bad("unapproved_template", a)
            if a.get("deeplink") and not re.match(r"^ott://title/T\d+$", a["deeplink"]):
                bad("bad_deeplink", a)
    trig = [d for d in decisions if d["decision_type"] == "TRIGGER"]
    late = [d for d in trig if d["decided_in_ms"] > 60000]
    rep = {"actions_audited": v["actions"], "violations": {k: n for k, n in v.items() if k != "actions"},
           "violation_examples": problems, "holdout_actions": sum(1 for a in acts if pack.arms.get(a["user_id"]) == "holdout"),
           "decisions_over_60s": len(late), "decisions_total": len(decisions),
           "decisions_missing_fields": sum(1 for d in decisions if not (d.get("goal") or d.get("no_action_reason")) or d.get("confidence") is None
                                           or not d.get("confidence_reason")),
           "passed": not any(k != "actions" for k in v) and not late}
    with open(os.path.join(out_dir, "audit_report.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=2)
    return rep
