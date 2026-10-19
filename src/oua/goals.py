"""Goal catalogue: eligibility from evidence, measurability, rule versions and success judging.

Interpretations of unclear rules (also written into every decision's `assumptions`):
  G03  "signed up in the last 2 days"; success is judged by the rule version in force for that goal (v1 app_open, v2
       play_started after the mid-replay correction), on a later IST calendar day than the goal start.
  G09  "rarely watches" = plays on at most 1 distinct IST day in the last 14 days (by estimated event time).
  G07  "unfinished series" = a show/micro_drama the viewer played, with episodes after the last completed one, not played
       for >= 3 days, and currently available in the dialect the viewer sees.
  G12  needs a prior relationship (signup older than 14 days), so brand-new viewers are handled by G01/G03.
Unmeasurable: G10 (no success event) and any goal whose success event never occurs in the pack's event vocabulary (G11).
"""
from __future__ import annotations

from datetime import timedelta

from .timeutil import iso, ist_day, parse

# Funnel-stage priority: time-critical conversion first, then activation, then habit. Documented in ARCHITECTURE.md.
STATUS_GOALS = ("G04", "G05", "G06", "G08", "G09")   # eligibility depends on subscription/engagement state
PRIORITY = ["G04", "G01", "G03", "G12", "G02", "G07", "G06", "G05", "G09", "G08"]


class RuleBook:
    """Versioned success rules. A correction creates a new version; it is applied only to goals still open."""

    def __init__(self, goals_json):
        self.goals = {g["goal_id"]: g for g in goals_json["goals"]}
        self.versions = {gid: [{"version": 1, "effective_at": None, "success": g["success"]}] for gid, g in self.goals.items()}

    def current_version(self, gid):
        return self.versions[gid][-1]["version"]

    def success_rule(self, gid, version):
        for v in self.versions[gid]:
            if v["version"] == version:
                return v["success"]
        raise KeyError((gid, version))

    def correct(self, gid, at, new_success):
        nv = self.versions[gid][-1]["version"] + 1
        self.versions[gid].append({"version": nv, "effective_at": iso(at), "success": new_success})
        return nv


def measurable(goal, vocabulary):
    ev = goal["success"].get("event")
    if ev is None:
        return False, "no success event is defined for this goal"
    if ev not in vocabulary:
        return False, "success event '%s' never occurs in the event stream" % ev
    return True, None


def unfinished_series(pack, facts, now, dialect):
    """Series the viewer left unfinished, newest first. Each item carries the next episode and an available title."""
    out = []
    for slug, s in facts["series"].items():
        if s["type"] not in ("show", "micro_drama"):
            continue
        eps = pack.episodes.get(slug) or []
        pos = s["completed_pos"]
        if pos + 1 >= len(eps):          # nothing left to watch
            continue
        if (now - s["last_play"]) < timedelta(days=3):
            continue
        variants = [t for t in pack.by_slug.get(slug, []) if t.dialect == dialect]
        if not variants:
            continue
        nxt = eps[pos + 1] if pos >= 0 else eps[0]
        out.append({"slug": slug, "title_id": s["title_id"], "last_play": s["last_play"], "next_episode": nxt[1],
                    "next_episode_id": nxt[2], "season": nxt[0], "evidence": "event:%s" % s["event"]})
    out.sort(key=lambda x: x["last_play"], reverse=True)
    return out


def recent_releases(pack, dialect, now, days):
    lo = now - timedelta(days=days)
    seen, out = set(), []
    for t in pack.by_dialect.get(dialect, []):
        if t.release_date and lo <= t.release_date <= now and t.slug not in seen:
            seen.add(t.slug)
            out.append(t)
    return out


def evaluate_goal(gid, pack, facts, now, cfg, vocabulary):
    """Return {goal_id, eligible, measurable, reason, evidence, assumptions}. Pure function of facts."""
    goal = pack.goals_by_id[gid]
    m_ok, m_reason = measurable(goal, vocabulary)
    res = {"goal_id": gid, "eligible": False, "measurable": m_ok, "reason": None, "evidence": [], "assumptions": []}
    if not m_ok:
        res["reason"] = "unmeasurable: " + m_reason
        return res
    status = facts["subscription_status"]
    sub_ref = facts.prov["subscription_status"]["source"]
    signup = facts["signup_at"]
    signup_ref = facts.prov["signup_at"]["source"]
    dialect = facts["dialect"]
    hc = facts["history_complete"]
    ev, why = False, None

    if gid == "G01":
        if signup is None:
            why = "no signup evidence"
        elif now - signup > timedelta(days=7):
            why = "signed up more than 7 days ago"
        elif facts["n_plays"] > 0:
            why = "has already started a play"
        else:
            ev = True
            res["evidence"] = [signup_ref]
    elif gid == "G02":
        if facts["n_completes"] > 0:
            why = "has completed an episode"
        elif signup is None and facts["n_known_events"] == 0:
            why = "no evidence about this viewer (no profile, signup or prior events)"
        else:
            ev = True
            res["evidence"] = [signup_ref] if signup else ["events:no_episode_completed_in_known_window(n=%d)" % facts["n_known_events"]]
            if not hc:
                res["assumptions"].append("no completion in the 30-day window; earlier history is not available")
    elif gid == "G03":
        if signup is None:
            why = "no signup evidence"
        elif now - signup > timedelta(days=2):
            why = "signed up more than 2 days ago"
        else:
            ev = True
            res["evidence"] = [signup_ref]
    elif gid == "G04":
        if status == "trial_active":
            ev, res["evidence"] = True, [sub_ref]
        else:
            why = "subscription_status is %s, not trial_active" % status
    elif gid == "G05":
        if status == "never":
            ev, res["evidence"] = True, [sub_ref]
            if not hc:
                res["assumptions"].append("'never paid' rests on the profile status and the 30-day event window")
        else:
            why = "subscription_status is %s, not never" % status
    elif gid == "G06":
        if status == "lapsed":
            ev, res["evidence"] = True, [sub_ref]
        else:
            why = "subscription_status is %s, not lapsed" % status
    elif gid == "G07":
        if dialect is None:
            why = "dialect unknown"
        else:
            cand = unfinished_series(pack, facts, now, dialect)
            if cand:
                ev, res["evidence"] = True, [cand[0]["evidence"]]
                res["assumptions"].append("series %s has episodes left after the last completed one" % cand[0]["slug"])
            else:
                why = "no unfinished series available in the viewer's dialect"
    elif gid == "G08":
        if status != "active":
            why = "subscription_status is %s, not active" % status
        elif dialect is None:
            why = "dialect unknown"
        elif len(facts["played_days_14d"]) >= cfg.engaged_days_14d:
            why = "already engaged (%d play days in 14 d): no incremental need" % len(facts["played_days_14d"])
        elif len(recent_releases(pack, dialect, now, cfg.new_release_days)) < 1:
            why = "no release in the last %d days in this dialect" % cfg.new_release_days
        else:
            ev, res["evidence"] = True, [sub_ref]
    elif gid == "G09":
        if status != "active":
            why = "subscription_status is %s, not active" % status
        elif len(facts["played_days_14d"]) > 1:
            why = "plays on %d days in the last 14 (>1)" % len(facts["played_days_14d"])
        else:
            ev, res["evidence"] = True, [sub_ref]
            res["assumptions"].append("'rarely' = plays on at most 1 IST day in the last 14 days")
    elif gid == "G12":
        if signup is None or now - signup <= timedelta(days=14):
            why = "no prior relationship older than 14 days (new viewers use G01/G03)"
        elif facts["last_open_at"] is not None and now - facts["last_open_at"] < timedelta(days=14):
            why = "opened the app within the last 14 days"
        else:
            ev, res["evidence"] = True, [signup_ref]
            res["assumptions"].append("no app_open in the 14 days before the trigger, in the 30-day event window")
    else:
        why = "goal not implemented"
    res["eligible"], res["reason"] = ev, why
    return res


def eligible_goals(pack, facts, now, cfg, vocabulary):
    out = []
    for gid in PRIORITY:
        r = evaluate_goal(gid, pack, facts, now, cfg, vocabulary)
        out.append(r)
    return out


def rules_choice(evals):
    for r in evals:
        if r["eligible"] and r["measurable"]:
            return r
    return None


def success_signal_text(rule):
    return "%s: %s" % (rule.get("event"), rule.get("condition"))


# ---------------------------------------------------------------------------------------------------------------
# judging
# ---------------------------------------------------------------------------------------------------------------
def judge_goal(goal, rulebook, pack, events, observation_end):
    """Judge one goal record from attributable events only (received_at in (start, deadline]).

    Returns {status: succeeded|failed|inconclusive, reason, signal_event_id}.
    """
    gid = goal["goal_id"]
    start, deadline = parse(goal["started_at"]), parse(goal["deadline"])
    rule = rulebook.success_rule(gid, goal["rule_version"])
    ok, why = measurable(rulebook.goals[gid], pack.event_vocabulary)
    if not ok:
        return {"status": "inconclusive", "reason": why, "signal_event_id": None}
    window = [e for e in events if start < e["_recv"] <= deadline]
    need = rule["event"]
    hit = None
    if gid == "G03":
        start_day = ist_day(start)
        hit = next((e for e in window if e["event"] == need and ist_day(e["_recv"]) > start_day), None)
    elif gid in ("G04", "G05"):
        hit = next((e for e in window if e["event"] == need and e["properties"].get("is_trial") is False), None)
    elif gid == "G06":
        hit = next((e for e in window if e["event"] == need and e["properties"].get("is_trial") is not True), None)
    elif gid == "G07":
        slug = goal["target"].get("slug")
        hit = next((e for e in window if e["event"] == need and getattr(pack.titles.get(e["properties"].get("title_id")), "slug", None) == slug), None)
    elif gid == "G08":
        lo = start - timedelta(days=30)
        for e in window:
            t = pack.titles.get(e["properties"].get("title_id"))
            if e["event"] == need and t and t.release_date and lo <= t.release_date <= start:
                hit = e
                break
    elif gid == "G09":
        days, last = set(), None
        for e in window:
            if e["event"] == need:
                days.add(ist_day(e["_recv"]))
                if len(days) >= 3:
                    last = e
                    break
        hit = last
    else:
        hit = next((e for e in window if e["event"] == need), None)
    if hit is not None:
        return {"status": "succeeded", "reason": "success event observed in window", "signal_event_id": hit["event_id"]}
    if goal.get("status") == "superseded":
        return {"status": "inconclusive", "reason": "goal replaced before its deadline (%s)" % goal.get("closed_reason"), "signal_event_id": None}
    if deadline > observation_end:
        return {"status": "inconclusive", "reason": "window extends beyond the observed horizon", "signal_event_id": None}
    return {"status": "failed", "reason": "no success event inside the window", "signal_event_id": None}
