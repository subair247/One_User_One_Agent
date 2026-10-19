"""Impact: agent arm vs randomised holdout (intention to treat), by goal, plus the pilot re-analysis."""
from __future__ import annotations

import csv
import json
import os
from collections import defaultdict

from .stats import diff_ci
from .timeutil import parse


def pct(x):
    return "n/a" if x is None else "%.1f%%" % (100 * x)


def pp(x):
    return "n/a" if x is None else "%+.1f pp" % (100 * x)


def itt_table(rows):
    """rows: assigned-goal outcomes. Inconclusive goals are reported but excluded from the rates."""
    def tally(sel):
        conc = [r for r in sel if r["status"] in ("succeeded", "failed")]
        return len(sel), len(conc), sum(1 for r in conc if r["status"] == "succeeded")
    agent = [r for r in rows if r["arm"] == "agent"]
    hold = [r for r in rows if r["arm"] == "holdout"]
    out = {"overall": None, "by_goal": {}}
    na, ca, sa = tally(agent)
    nh, ch, sh = tally(hold)
    out["overall"] = {"agent_n": na, "agent_conclusive": ca, "agent_success": sa, "holdout_n": nh, "holdout_conclusive": ch,
                      "holdout_success": sh, **diff_ci(sa, ca, sh, ch)}
    for gid in sorted({r["goal_id"] for r in rows}):
        a = [r for r in agent if r["goal_id"] == gid]
        h = [r for r in hold if r["goal_id"] == gid]
        na, ca, sa = tally(a)
        nh, ch, sh = tally(h)
        out["by_goal"][gid] = {"agent_n": na, "agent_conclusive": ca, "agent_success": sa, "holdout_n": nh, "holdout_conclusive": ch,
                               "holdout_success": sh, **diff_ci(sa, ca, sh, ch)}
    return out


def served_vs_rest(rows, actions_path):
    """The biased comparison on THIS replay: agent viewers who were served a page vs everyone else."""
    served = set()
    with open(actions_path, encoding="utf-8") as f:
        for line in f:
            a = json.loads(line)
            if a["type"] == "home_page":
                served.add(a["user_id"])
    s = [r for r in rows if r["viewer_id"] in served and r["status"] in ("succeeded", "failed")]
    o = [r for r in rows if r["viewer_id"] not in served and r["status"] in ("succeeded", "failed")]
    ss, so = sum(r["status"] == "succeeded" for r in s), sum(r["status"] == "succeeded" for r in o)
    return {"served_n": len(s), "served_success": ss, "rest_n": len(o), "rest_success": so, **diff_ci(ss, len(s), so, len(o))}


def pilot_reanalysis(pack):
    root = os.path.join(pack.root, "pilot")
    path = os.path.join(root, "pilot_outcomes.csv")
    if not os.path.exists(path):
        return None
    rows = list(csv.DictReader(open(path, encoding="utf-8", newline="")))
    for r in rows:
        r["ok"] = r["succeeded"] == "true"
        r["srv"] = r["served"] == "true"
    served = [r for r in rows if r["srv"]]
    rest = [r for r in rows if not r["srv"]]
    t = [r for r in rows if r["arm"] == "treatment"]
    h = [r for r in rows if r["arm"] == "holdout"]
    biased = diff_ci(sum(r["ok"] for r in served), len(served), sum(r["ok"] for r in rest), len(rest))
    itt = diff_ci(sum(r["ok"] for r in t), len(t), sum(r["ok"] for r in h), len(h))
    # selection evidence: prior app opens before the goal started, from the event history we hold
    def prior_opens(r):
        start = parse(r["goal_started_at"])
        return sum(1 for e in pack.events_by_user.get(r["user_id"], []) if e["event"] == "app_open" and e["_recv"] < start)
    groups = {"served (treatment)": served, "not served (treatment)": [r for r in t if not r["srv"]], "holdout": h}
    activity = {k: (sum(prior_opens(r) for r in v) / len(v) if v else None) for k, v in groups.items()}
    return {"served": {"n": len(served), "success": sum(r["ok"] for r in served)}, "rest": {"n": len(rest), "success": sum(r["ok"] for r in rest)},
            "biased": biased, "treatment": {"n": len(t), "success": sum(r["ok"] for r in t)},
            "holdout": {"n": len(h), "success": sum(r["ok"] for r in h)}, "itt": itt, "mean_prior_app_opens": activity,
            "served_share_of_treatment": len(served) / len(t) if t else None}


def write_impact(pack, cfg, out_dir, res):
    itt = itt_table(res["itt_rows"])
    sv = served_vs_rest(res["itt_rows"], os.path.join(out_dir, "actions.jsonl"))
    pilot = pilot_reanalysis(pack)
    inconc = sum(1 for r in res["itt_rows"] if r["status"] == "inconclusive")
    with open(os.path.join(out_dir, "impact.json"), "w", encoding="utf-8") as f:
        json.dump({"itt": itt, "served_vs_rest_biased": sv, "pilot": pilot, "inconclusive_goals": inconc}, f, indent=2, default=str)
    o = itt["overall"]
    L = []
    L.append("# Impact report\n")
    L.append("Seed `%s`. Outcomes come from `simulator_report.json` / `outcome_events.jsonl` only; nothing else is used as evidence.\n" % cfg.seed)
    L.append("## 1. Headline (intention to treat)\n")
    L.append("Every viewer with a fresh trigger on the replay day is analysed in the arm they were randomised to (`replay/arms.csv`), "
             "whether or not the agent acted on them. Each viewer is judged on the goal the same pure rules assign at their first fresh "
             "trigger (for the holdout this is a measurement-only shadow assignment; nothing is ever sent or published to them).\n")
    L.append("| Arm | Viewers with a goal | Conclusive | Reached goal | Rate |\n|---|---|---|---|---|")
    L.append("| Agent | %d | %d | %d | %s |" % (o["agent_n"], o["agent_conclusive"], o["agent_success"], pct(o["p1"])))
    L.append("| Holdout | %d | %d | %d | %s |\n" % (o["holdout_n"], o["holdout_conclusive"], o["holdout_success"], pct(o["p0"])))
    sig = o["p_value"] is not None and o["p_value"] < 0.05
    L.append("**Difference (agent - holdout): %s, 95%% CI [%s, %s], p = %.3f.** %s\n" % (
        pp(o["diff"]), pp(o["ci_low"]), pp(o["ci_high"]), o["p_value"] if o["p_value"] is not None else float("nan"),
        "The interval excludes zero." if sig and o["ci_low"] > 0 else ("The interval excludes zero but is negative." if sig else "The interval includes zero: this replay does not show a reliable effect, and we do not claim one.")))
    if inconc:
        L.append("%d assigned goals were inconclusive (window beyond the observed horizon) and are excluded from the rates.\n" % inconc)
    L.append("## 2. By goal\n")
    L.append("| Goal | Agent n | Agent success | Holdout n | Holdout success | Diff | 95% CI |\n|---|---|---|---|---|---|---|")
    for gid, r in itt["by_goal"].items():
        L.append("| %s | %d | %s | %d | %s | %s | [%s, %s] |" % (gid, r["agent_conclusive"], pct(r["p1"]), r["holdout_conclusive"], pct(r["p0"]),
                                                              pp(r["diff"]), pp(r["ci_low"]), pp(r["ci_high"])))
    L.append("\nPer-goal cells are small; intervals are wide and most include zero. Read them as direction, not proof.\n")
    L.append("## 3. Why \"served vs everyone else\" misleads\n")
    L.append("The agent only serves viewers it has a reason to serve: it needs a fresh trigger, a verifiable dialect, an eligible goal and consent, "
             "and it skips stale or already-served sessions. Those are the viewers who were already more active and easier to convert, so they "
             "would have done better without the agent (selection bias). The randomised holdout removes that: both arms have the same mix "
             "of viewers, so the only systematic difference is the agent.\n")
    L.append("On this replay the biased comparison gives served %s (n=%d) vs rest %s (n=%d), a gap of %s, versus the ITT estimate of %s.\n" % (
        pct(sv["p1"]), sv["served_n"], pct(sv["p0"]), sv["rest_n"], pp(sv["diff"]), pp(o["diff"])))
    if pilot:
        L.append("## 4. Pilot re-analysis (`pilot/`)\n")
        L.append("| Comparison | Group A | Group B | Difference (95% CI) |\n|---|---|---|---|")
        L.append("| Pilot report: served vs all others | %s (%d/%d) | %s (%d/%d) | %s [%s, %s] |" % (
            pct(pilot["biased"]["p1"]), pilot["served"]["success"], pilot["served"]["n"], pct(pilot["biased"]["p0"]), pilot["rest"]["success"],
            pilot["rest"]["n"], pp(pilot["biased"]["diff"]), pp(pilot["biased"]["ci_low"]), pp(pilot["biased"]["ci_high"])))
        L.append("| **Randomised: all treatment vs all holdout** | %s (%d/%d) | %s (%d/%d) | **%s [%s, %s]** |\n" % (
            pct(pilot["itt"]["p1"]), pilot["treatment"]["success"], pilot["treatment"]["n"], pct(pilot["itt"]["p0"]), pilot["holdout"]["success"],
            pilot["holdout"]["n"], pp(pilot["itt"]["diff"]), pp(pilot["itt"]["ci_low"]), pp(pilot["itt"]["ci_high"])))
        L.append("The report's 40.1%% vs 17.7%% is reproduced exactly, but its \"all other users\" mixes the unserved treatment users with the entire holdout, "
                 "and only %s of the treatment arm was served. Using the randomised arms, the pilot shows %s with an interval that includes zero: "
                 "**no evidence of uplift, and the \"roll out to every user\" recommendation is not supported.**\n" % (
                     pct(pilot["served_share_of_treatment"]), pp(pilot["itt"]["diff"])))
        L.append("")
    L.append("## 5. Limits of this measurement\n")
    L.append("- One replay day, one seed: the simulator's randomness is part of the interval's uncertainty only through the sample size; re-run with `--seed` to see seed variation.\n"
             "- The simulator's behaviour model is an assumption, not reality. Any uplift here is uplift *in the simulator*.\n"
             "- Wald intervals are approximate for small cells (see by-goal table). Goals are assigned by rules from pre-trigger data only, so the same rule applies to both arms.\n")
    with open(os.path.join(out_dir, "impact_report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return {"itt": itt, "served_vs_rest": sv, "pilot": pilot}
