"""Outcome judge: succeeded / failed / inconclusive from attributable events only, honouring versioned goal rules."""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import timedelta

from .goals import RuleBook, judge_goal
from .loader import normalise_event
from .timeutil import parse, iso

REPLAY_START_UTC = parse("2026-09-30T18:30:00.000Z")      # 2026-10-01 00:00 IST (same constant the simulator uses)


def observation_end(cfg):
    return REPLAY_START_UTC + timedelta(days=cfg.observation_days + 1)


def build_rulebook(pack):
    rb = RuleBook(pack.goals)
    for c in pack.control_events:
        if c["type"] == "goal_rule_corrected":
            rb.correct(c["goal_id"], parse(c["at"]), c["new_success"])
    return rb


def load_outcome_events(out_dir):
    path = os.path.join(out_dir, "sim", "outcome_events.jsonl")
    evs = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    evs.append(normalise_event(json.loads(line)))
    return evs


def judge_all(pack, cfg, out_dir):
    rb = build_rulebook(pack)
    end = observation_end(cfg)
    by_user = defaultdict(dict)
    for e in pack.live_events:
        by_user[e["user_id"]][e["event_id"]] = e
    outcomes_events = load_outcome_events(out_dir)
    for e in outcomes_events:
        by_user[e["user_id"]][e["event_id"]] = e
    ev = {u: sorted(d.values(), key=lambda x: (x["_recv"], x["event_id"])) for u, d in by_user.items()}

    rows = []
    # (a) the goals the agent actually committed to (agent arm)
    sd = os.path.join(out_dir, "viewer_state")
    states = {}
    for fn in sorted(os.listdir(sd)):
        with open(os.path.join(sd, fn), encoding="utf-8") as f:
            s = json.load(f)
        states[s["viewer_id"]] = s
    for uid, st in states.items():
        goals = list(st["goals"]) + ([st["goal"]] if st["goal"] else [])
        st["outcomes"] = []
        for g in goals:
            if g["goal_id"] not in rb.goals:
                continue
            r = judge_goal(g, rb, pack, ev.get(uid, []), end)
            row = {"kind": "agent_goal", "viewer_id": uid, "arm": st["arm"], "goal_instance": g["instance"], "goal_id": g["goal_id"],
                   "rule_version": g["rule_version"], "started_at": g["started_at"], "deadline": g["deadline"],
                   "status": r["status"], "reason": r["reason"], "signal_event_id": r["signal_event_id"]}
            rows.append(row)
            st["outcomes"].append(row)
        # learnings from attributable message signals
        notes = st["learnings"]["notes"]
        for e in ev.get(uid, []):
            if e["event"] == "message_failed" and e["properties"].get("reason") == "token_invalid":
                if "push" not in st["learnings"]["unreachable_channels"]:
                    st["learnings"]["unreachable_channels"].append("push")
                    notes.append("push token invalid (message_failed): prefer other channels next time")
            elif e["event"] == "notification_opt_out":
                ch = e["properties"].get("channel")
                if ch and ch not in st["learnings"]["unreachable_channels"]:
                    st["learnings"]["unreachable_channels"].append(ch)
                    notes.append("opted out of %s after a message: stop using it" % ch)
        for o in st["outcomes"]:
            notes.append("goal %s judged %s (%s)" % (o["goal_id"], o["status"], o["reason"]))
        with open(os.path.join(sd, "%s.json" % uid), "w", encoding="utf-8") as f:
            json.dump(st, f, indent=1, ensure_ascii=False)

    # (b) the assigned goal of every analysed viewer, both arms, for the intention-to-treat comparison
    shadow = []
    with open(os.path.join(out_dir, "shadow_goals.jsonl"), encoding="utf-8") as f:
        for line in f:
            if line.strip():
                shadow.append(json.loads(line))
    itt = []
    for g in shadow:
        if not g.get("goal_id"):
            continue
        gg = {"goal_id": g["goal_id"], "started_at": g["started_at"], "deadline": g["deadline"], "rule_version": g["rule_version"],
              "target": g.get("target", {}), "status": "open"}
        r = judge_goal(gg, rb, pack, ev.get(g["viewer_id"], []), end)
        row = {"kind": "assigned_goal_itt", "viewer_id": g["viewer_id"], "arm": g["arm"], "goal_id": g["goal_id"],
               "rule_version": g["rule_version"], "started_at": g["started_at"], "deadline": g["deadline"],
               "status": r["status"], "reason": r["reason"], "signal_event_id": r["signal_event_id"]}
        itt.append(row)
        rows.append(row)
    with open(os.path.join(out_dir, "outcomes.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return {"n_judged": len(rows), "agent_rows": [r for r in rows if r["kind"] == "agent_goal"], "itt_rows": itt,
            "events_by_user": ev, "states": states}
