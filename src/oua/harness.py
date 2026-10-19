"""Replay harness + the per-viewer agent loop (understand -> goal -> intervene -> verify -> act -> close the loop)."""
from __future__ import annotations

import heapq
import json
import os
import time
from collections import Counter, defaultdict
from datetime import timedelta

from .config import Config
from .content import build_page, plan_message, MESSAGE_GOALS
from .env import Env, Dependencies
from .facts import resolve_facts
from .goals import RuleBook, eligible_goals, rules_choice, unfinished_series, judge_goal, success_signal_text
from .loader import load_pack
from .model import MockModel, ModelError, Breaker, parse_model_output
from .store import ViewerStore
from .timeutil import parse, iso, ist_day, UTC
from .verifier import Checker, verify_goal, verify_page, verify_message


class BudgetExceeded(Exception):
    pass


class Budget:
    def __init__(self, cfg):
        self.cfg = cfg
        self.model_calls = 0
        self.tool_calls = 0
        self.virtual = 0.0

    def tool(self, n=1):
        self.tool_calls += n
        self.virtual += self.cfg.tool_latency_s * n
        if self.tool_calls > self.cfg.max_tool_calls:
            raise BudgetExceeded("tool call budget (%d) exceeded" % self.cfg.max_tool_calls)

    def model(self):
        if self.model_calls >= self.cfg.max_model_calls:
            raise BudgetExceeded("model call budget (%d) exceeded" % self.cfg.max_model_calls)
        self.model_calls += 1


from .goals import STATUS_GOALS


class Harness:
    def __init__(self, pack, cfg, out_dir, resume=False, model=None):
        self.pack, self.cfg, self.out = pack, cfg, out_dir
        os.makedirs(out_dir, exist_ok=True)
        self.env = Env(pack)
        self.deps = Dependencies(pack.control_events)
        self.rulebook = RuleBook(pack.goals)
        self.model = model or MockModel(cfg, pack.faults, self.deps)
        self.breaker = Breaker(cfg.breaker_threshold, cfg.breaker_open_s)
        self.store = ViewerStore(out_dir, resume)
        self.events = pack.events_by_user
        self.decisions, self.actions, self.cancelled = [], [], []
        self.shadow = {}
        self.control_log = []
        self.pending = []                  # heap of (send_at, seq, action_id)
        self.pending_actions = {}
        self.open_goals = defaultdict(set)
        self.live_by_title = defaultdict(set)
        self.last_trigger = {}
        self.d_n = self.a_n = self.seq = 0
        self.workers = [0.0] * max(1, cfg.workers)
        self.rule_corrections = []
        self.stats = Counter()
        self.vocab = pack.event_vocabulary

    # ------------------------------------------------------------------ ids
    def _did(self):
        self.d_n += 1
        return "D-%05d" % self.d_n

    def _aid(self):
        self.a_n += 1
        return "A-%05d" % self.a_n

    # ------------------------------------------------------------------ timeline
    def run(self):
        items = []
        for c in self.pack.control_events:
            items.append((parse(c["at"]), 0, len(items), "control", c))
        for e in self.pack.live_events:
            items.append((e["_deliver"], 1, len(items), "live", e))
        for e in self.pack.triggers:
            items.append((e["_deliver"], 2, len(items), "trigger", e))
        items.sort(key=lambda x: x[:3])
        if items:
            t0 = items[0][0].timestamp()
            self.workers = [t0] * len(self.workers)
        for at, _, _, kind, obj in items:
            self._release_due(at)
            if kind == "control":
                self._on_control(obj, at)
            elif kind == "live":
                self._on_live(obj)
            else:
                self._on_trigger(obj)
        self._release_due(None)
        self._write_outputs()
        return {"decisions": len(self.decisions), "actions": len(self.actions), "cancelled": len(self.cancelled),
                "stats": dict(self.stats)}

    # ------------------------------------------------------------------ worker pool (virtual time)
    def _acquire(self, deliver):
        free = heapq.heappop(self.workers)
        start = max(free, deliver.timestamp())
        return start

    def _release_worker(self, end_ts):
        heapq.heappush(self.workers, end_ts)

    # ------------------------------------------------------------------ decision skeleton
    def _skeleton(self, kind, uid, arm, trig, wait_s):
        return {
            "decision_id": self._did(), "decision_type": kind, "viewer_id": uid, "arm": arm,
            "trigger": {"event_id": trig["event_id"], "event": trig["event"], "received_at": iso(trig["_recv"]),
                        "deliver_at": iso(trig["_deliver"]), "event_age_s": round(trig["_delay_s"] + wait_s, 3),
                        "buffered_s": round(trig["_delay_s"], 3), "queue_wait_ms": int(wait_s * 1000)} if trig else None,
            "goal": None, "action": "NO_ACTION", "action_types": [], "actions": [], "dialect": None, "content": [],
            "confidence": None, "confidence_reason": None, "evidence": [], "assumptions": [], "conflicts": [],
            "checks": {"passed": [], "failed": []}, "no_action_reason": None, "fallback_used": [],
            "unsupported_claims": [], "model_calls": 0, "tool_calls": 0, "state_source": None,
            "security_flags": [], "status": "NO_ACTION",
        }

    def _no_action(self, dec, status, reason, confidence=0.9, why=None):
        dec["status"] = status
        dec["action"] = "NO_ACTION"
        dec["no_action_reason"] = reason
        dec["confidence"] = confidence
        dec["confidence_reason"] = why or "rule-based abstention: %s" % reason

    # ------------------------------------------------------------------ trigger handling
    def _on_trigger(self, trig):
        t0 = time.perf_counter()
        uid, deliver = trig["user_id"], trig["_deliver"]
        start_ts = self._acquire(deliver)
        wait_s = start_ts - deliver.timestamp()
        start = deliver + timedelta(seconds=wait_s)
        arm = self.pack.arms.get(uid, "unassigned")
        dec = self._skeleton("TRIGGER", uid, arm, trig, wait_s)
        budget = Budget(self.cfg)
        ctx = {"start": start, "t0": t0, "budget": budget, "deliver": deliver}
        try:
            self._decide(trig, dec, ctx, arm, wait_s)
        except BudgetExceeded as exc:
            dec["fallback_used"].append("budget_exceeded")
            self._no_action(dec, "NO_ACTION", "budget_exceeded: " + str(exc), 0.5, "safe fallback after budget exhaustion")
            self.stats["budget_exceeded"] += 1
        except Exception as exc:     # never let one viewer break the loop: degrade to doing nothing
            dec["fallback_used"].append("internal_error")
            self._no_action(dec, "NO_ACTION", "internal_error: %s" % type(exc).__name__, 0.3, "unexpected error; safe fallback")
            self.stats["internal_error"] += 1
        compute_s = time.perf_counter() - t0
        end = start + timedelta(seconds=budget.virtual + compute_s)
        if dec["status"] in ("PUBLISHED",) and dec.get("published_at"):
            end = parse(dec["published_at"])
        dec["published_at"] = iso(end) if dec["status"] == "PUBLISHED" else None
        dec["decided_at"] = iso(end)
        dec["decided_in_ms"] = int((end - deliver).total_seconds() * 1000)
        dec["compute_ms"] = round(compute_s * 1000, 2)
        dec["model_calls"], dec["tool_calls"] = budget.model_calls, budget.tool_calls
        self._release_worker(end.timestamp())
        self.decisions.append(dec)
        self.stats["decisions"] += 1
        self.stats["status_" + dec["status"]] += 1
        if dec["no_action_reason"]:
            self.stats["no_action:" + dec["no_action_reason"].split(":")[0]] += 1
        self.events[uid].append(trig)            # the trigger itself becomes history only after the decision
        self.last_trigger[uid] = trig
        if arm == "agent":
            st = self.store.get(uid, arm)
            st["decisions"].append(dec["decision_id"])
            self.store.touch(uid)

    def _now(self, ctx):
        return ctx["start"] + timedelta(seconds=ctx["budget"].virtual + (time.perf_counter() - ctx["t0"]) + 0.002)

    # ---- the decision
    def _decide(self, trig, dec, ctx, arm, wait_s):
        cfg, pack, budget = self.cfg, self.pack, ctx["budget"]
        uid = trig["user_id"]
        now0 = trig["_recv"]

        if arm != "agent":
            self._no_action(dec, "HOLDOUT_NO_ACTION", "holdout" if arm == "holdout" else "no_arm_assignment",
                            1.0, "viewer is in the %s arm: no agent action of any kind" % arm)
            dec["checks"]["passed"].append("holdout")
            self._shadow_goal(trig)
            return

        state = self.store.get(uid, arm)
        # idempotency
        if trig["event_id"] in state["seen_triggers"]:
            self._no_action(dec, "DUPLICATE_IGNORED", "duplicate_trigger", 1.0, "same event_id already processed")
            dec["checks"]["passed"].append("duplicates")
            return
        state["seen_triggers"].append(trig["event_id"])
        self._shadow_goal(trig)

        if trig["_delay_s"] > cfg.stale_trigger_s:
            self._no_action(dec, "NO_ACTION", "stale_trigger: buffered %.0f s on the phone (> %.0f s); not a live session" %
                            (trig["_delay_s"], cfg.stale_trigger_s), 0.95, "age measured as sent_at - occurred_at on one phone clock")
            dec["checks"]["failed"].append({"check": "staleness", "reason": "offline-buffered trigger"})
            return

        # --- 1. understand the viewer
        st, meta = self.store.read(uid, arm, ctx["start"], self.deps, cfg, trig["event_id"])
        budget.tool()
        budget.virtual += meta["latency_s"]
        dec["state_source"] = meta["source"]
        cached = meta["source"] == "cache"
        if cached:
            dec["fallback_used"].append("store_timeout_cached_state")
            self.stats["store_cache_fallback"] += 1
        budget.tool(2)                                      # events read + profile read
        facts = resolve_facts(pack, uid, self.events[uid], trig, now0, st["learnings"])
        state["facts"] = facts.snapshot()
        state["conflicts"] = list(facts.conflicts)
        dec["evidence"] = ["event:%s" % trig["event_id"]] + [e for e in facts.evidence if e != "none"]
        dec["conflicts"] = list(facts.conflicts)
        dec["assumptions"] = list(facts.assumptions)
        dec["dialect"] = {"used": facts["dialect"], "source": "app_setting", "conflicts": list(facts["dialect_conflicts"])}
        dl_flag = trig["properties"].get("deeplink")
        if dl_flag:
            dec["security_flags"].append("deeplink treated as data only")

        if facts["dialect"] is None:
            self._no_action(dec, "NO_ACTION", "dialect_unverifiable: app_dialect=%r is missing or has no catalogue" % (trig.get("app_dialect"),),
                            0.9, "cannot know which dialect the app will request, so no page can be verified")
            dec["checks"]["failed"].append({"check": "dialect", "reason": "unverifiable app dialect"})
            return

        # --- 2. goal: keep, close, or choose
        pub_now = self._now(ctx)
        goal = self._current_goal(uid, state, facts, pub_now)
        info = {}
        if goal is None:
            goal, info = self._choose_goal(uid, trig, facts, pub_now, ctx, dec, cached)
        else:
            info = {"source": "continued", "confidence": 0.7, "parts": ["goal continued while still valid"]}
        if goal is None:
            self._no_action(dec, "NO_ACTION", "no_eligible_measurable_goal", info.get("confidence", 0.8),
                            info.get("why", "no goal is both eligible on evidence and measurable"))
            dec["assumptions"].extend(info.get("assumptions", []))
            return
        dec["goal"] = self._goal_view(goal)
        dec["assumptions"].extend(a for a in goal.get("assumptions", []) if a not in dec["assumptions"])

        # --- 3. proportionality: is a live page already serving this viewer?
        lp = state["live_page"]
        if lp and parse(lp["published_at"]) + timedelta(minutes=cfg.page_ttl_min) > pub_now and lp["dialect"] == facts["dialect"] \
                and lp["goal_instance"] == goal["instance"] and not lp.get("invalid"):
            dec["status"] = "NO_ACTION"
            dec["no_action_reason"] = "live_page_active: page %s is still live for this goal and dialect" % lp["action_id"]
            dec["confidence"], dec["confidence_reason"] = 0.9, "avoid page churn; platform shows the live page"
            dec["checks"]["passed"].append("duplicates")
            self._finalise_goal_conf(dec, info)
            return
        if state["last_session_id"] == trig["session_id"] and lp and not lp.get("invalid"):
            dec["status"] = "NO_ACTION"
            dec["no_action_reason"] = "session_already_served: second trigger in the same session"
            dec["confidence"], dec["confidence_reason"] = 0.9, "one intervention per session"
            dec["checks"]["passed"].append("duplicates")
            return

        # --- 4. plan the intervention
        budget.tool()                                       # catalogue pool
        page, notes = build_page(pack, self.env, facts, goal, pub_now, cfg)
        budget.tool()
        if page is None:
            dec["goal"] = self._goal_view(goal)
            self._no_action(dec, "NO_ACTION", "insufficient_valid_content: %s" % notes, 0.85,
                            "a page needs at least one row of >= %d valid titles" % cfg.min_titles_per_row)
            self._commit_goal(uid, state, goal, dec, pub_now)
            return

        # --- 5. verify (independent) and act
        pub = self._now(ctx)
        vfacts = resolve_facts(pack, uid, self.events[uid], trig, now0, st["learnings"])     # re-derived, not reused
        budget.tool(2)
        chk = Checker()
        chk.merge(verify_goal(pack, goal["goal_id"], vfacts, pub, cfg, self.vocab, claimed_by_model=False,
                              continued=info.get("source") == "continued"))
        chk.merge(verify_page(page, vfacts, pub, pack, self.env, cfg, arm))
        budget.tool()
        if chk.failed:
            dec["checks"] = {"passed": chk.passed, "failed": chk.failed}
            self._no_action(dec, "NO_ACTION", "verifier_rejected: " + "; ".join(f["check"] for f in chk.failed), 0.9,
                            "independent verifier rejected the page")
            self.stats["verifier_rejected"] += 1
            return
        msg_entry, msg_reason = None, None
        allow_msg = not cached
        if not allow_msg:
            msg_reason = "state read fell back to cache: consent may be stale, so no new message"
        else:
            ledger = self._ledger(state, vfacts)
            msg, msg_reason = plan_message(pack, self.env, vfacts, goal, pub, cfg, self.events[uid], ledger, page)
            budget.tool()
            if msg is not None:
                mchk = verify_message(msg, vfacts, msg["send_at"], pack, self.env, cfg, ledger, arm)
                if msg["type"] == "in_app_message" and any(m["type"] == "in_app_message" and
                        ist_day(parse(m["send_at"])) == ist_day(msg["send_at"]) for m in state["messages"]):
                    mchk.check("frequency_cap", False, "one in-app message per day")
                chk.merge(mchk)
                if mchk.failed:
                    msg_reason = "verifier_rejected message: " + "; ".join(f["check"] + " (" + f["reason"] + ")" for f in mchk.failed)
                    self.stats["message_rejected"] += 1
                    msg = None
                else:
                    msg_entry = msg
        dec["checks"] = {"passed": [c for c in chk.passed if not any(f["check"] == c for f in chk.failed)], "failed": [
            f for f in chk.failed if not f["reason"].startswith("one in-app")] if msg_entry else chk.failed}
        pub = self._now(ctx)
        # final availability pass at the actual publish instant
        chk2 = verify_page(page, vfacts, pub, pack, self.env, cfg, arm)
        if chk2.failed:
            dec["checks"]["failed"].extend(chk2.failed)
            self._no_action(dec, "NO_ACTION", "verifier_rejected: " + "; ".join(f["check"] for f in chk2.failed), 0.9, "title became unavailable at publish time")
            return

        # --- 6. commit goal, publish page, schedule message
        self._commit_goal(uid, state, goal, dec, pub)
        a_page = self._publish_page(uid, state, page, goal, dec, pub, trig)
        dec["actions"].append(a_page["action_id"])
        dec["action_types"].append("HOME_PAGE")
        dec["content"] = ["title:%s" % t for t in self._page_ids(page)]
        dec["evidence"].extend("catalogue:%s" % t for t in self._page_ids(page)[:12])
        dec["evidence"].append("goal_rule:%s@v%d" % (goal["goal_id"], goal["rule_version"]))
        if msg_entry:
            aid = self._schedule_message(uid, state, goal, msg_entry, dec, pub)
            dec["actions"].append(aid)
            dec["action_types"].append(msg_entry["type"].upper())
            if msg_entry.get("title_id") and "title:%s" % msg_entry["title_id"] not in dec["content"]:
                dec["content"].append("title:%s" % msg_entry["title_id"])
        else:
            dec["message_not_sent_reason"] = msg_reason
        dec["action"] = "+".join(dec["action_types"])
        dec["status"] = "PUBLISHED"
        dec["published_at"] = iso(pub)
        dec["goal"] = self._goal_view(goal)
        dec["assumptions"].extend(notes if isinstance(notes, list) else [])
        state["last_session_id"] = trig["session_id"]
        self._finalise_goal_conf(dec, info, cached=cached, has_conflicts=bool(facts.conflicts), facts=facts)
        self.store.touch(uid)

    # ------------------------------------------------------------------ goals
    def _goal_view(self, goal):
        return {"id": goal["goal_id"], "name": goal["name"], "started_at": goal["started_at"], "deadline": goal["deadline"],
                "success_signal": goal["success_signal"], "rule_version": goal["rule_version"], "source": goal.get("source")}

    def _current_goal(self, uid, state, facts, now):
        g = state["goal"]
        if g is None:
            return None
        if parse(g["deadline"]) <= now:
            self._close_goal(uid, state, g, "expired", "deadline passed")
            return None
        # success observed? (close the loop on new events)
        res = judge_goal(g, self.rulebook, self.pack, self.events[uid], now)
        if res["status"] == "succeeded":
            self._close_goal(uid, state, g, "achieved", "success event %s" % res["signal_event_id"])
            return None
        if g["goal_id"] in STATUS_GOALS:
            ev = verify_goal(self.pack, g["goal_id"], facts, now, self.cfg, self.vocab)
            if ev.failed:
                self._close_goal(uid, state, g, "superseded", "no longer eligible: " + ev.failed[0]["reason"])
                return None
        return g

    def _close_goal(self, uid, state, g, status, reason):
        g["status"] = "superseded" if status in ("superseded", "revised") else status
        g["closed_reason"] = reason
        g["closed_at"] = iso(self.last_ts) if getattr(self, "last_ts", None) else None
        state["goals"].append(g)
        state["goal"] = None
        self.open_goals[g["goal_id"]].discard(uid)
        self._cancel_pending(uid, lambda m: m.get("goal_instance") == g["instance"], "goal closed: " + reason)

    def _choose_goal(self, uid, trig, facts, now, ctx, dec, cached):
        cfg, pack, budget = self.cfg, self.pack, ctx["budget"]
        budget.tool()
        evals = eligible_goals(pack, facts, now, cfg, self.vocab)
        for r in evals:
            if not r["eligible"] and r["reason"] and r["reason"].startswith("unmeasurable"):
                if r["goal_id"] not in dec["assumptions"]:
                    pass
        elig = [r for r in evals if r["eligible"] and r["measurable"]]
        rules = elig[0] if elig else None
        info = {"source": "rules", "evals": evals}
        if rules is None:
            info.update(confidence=0.85, why="no goal is both eligible on evidence and measurable (%s)" %
                        "; ".join("%s: %s" % (r["goal_id"], r["reason"]) for r in evals[:3]))
            return None, info

        claim, used_model = None, False
        wait_s = dec["trigger"]["queue_wait_ms"] / 1000.0
        can_model = True
        if wait_s > cfg.shed_model_when_wait_s:
            can_model = False
            dec["fallback_used"].append("model_skipped_load_shedding")
        elif not self.breaker.allow(now):
            can_model = False
            dec["fallback_used"].append("model_skipped_circuit_open")
        if can_model:
            req = {"task": "choose_goal", "viewer_id": uid, "eligible_goal_ids": [r["goal_id"] for r in elig],
                   "n_evidence": len(rules["evidence"])}
            attempts = 1 + cfg.model_retry_on_malformed
            for attempt in range(attempts + (1 if self.deps.model_down(now) else 0)):
                try:
                    budget.model()
                except BudgetExceeded:
                    dec["fallback_used"].append("model_budget_exhausted")
                    break
                try:
                    resp = self.model.complete(req, trig["event_id"], attempt, now)
                    if resp.latency_s > cfg.model_timeout_s:
                        budget.virtual += cfg.model_timeout_s
                        raise ModelError("timeout", cfg.model_timeout_s, "slower than the %.1fs timeout" % cfg.model_timeout_s)
                    budget.virtual += resp.latency_s
                    claim = parse_model_output(resp.text)
                    self.breaker.record(True, now)
                    used_model = True
                    break
                except ModelError as me:
                    budget.virtual += min(me.latency_s, cfg.model_timeout_s)
                    self.breaker.record(False, now)
                    dec["fallback_used"].append("model_%s" % me.kind)
                    self.stats["model_" + me.kind] += 1
                    if me.kind == "timeout":
                        break                           # a timeout burns the budget: go straight to rules
                except ValueError as ve:
                    budget.virtual += cfg.model_latency_s
                    dec["fallback_used"].append("model_malformed_output_retry" if attempt + 1 < attempts else "model_malformed_output")
                    self.stats["model_malformed"] += 1
                    self.breaker.record(False, now)
        chosen, parts = rules, ["rules: first eligible measurable goal in funnel priority"]
        conf = 0.6
        if claim is not None:
            gid = claim.get("goal_id")
            vc = verify_goal(pack, gid, facts, now, cfg, self.vocab, claimed_by_model=True) if gid else Checker()
            if gid and vc.failed:
                dec["unsupported_claims"].append({"goal_id": gid, "model_confidence": claim.get("confidence"),
                                                  "model_reason": str(claim.get("reason"))[:160], "rejected_by": [f["check"] for f in vc.failed],
                                                  "why": vc.failed[0]["reason"]})
                dec["checks"]["failed"].append({"check": "evidence_supported", "reason": "model claimed %s eligible without supporting event/profile fact" % gid})
                dec["fallback_used"].append("rules_after_unsupported_claim")
                self.stats["unsupported_claim_rejected"] += 1
                parts.append("model claim rejected by verifier; rules used")
            elif gid and any(r["goal_id"] == gid for r in elig):
                chosen = next(r for r in elig if r["goal_id"] == gid)
                conf += 0.05
                parts.append("model proposal verified against evidence")
        if cached:
            conf -= 0.15
            parts.append("-0.15 state served from cache")
        if not facts["history_complete"]:
            conf -= 0.1
            parts.append("-0.10 history older than the 30-day window is unknown")
        if facts.conflicts:
            conf -= 0.05
            parts.append("-0.05 conflicting sources were resolved by rule")
        f = facts.prov["subscription_status"]["freshness_s"]
        if f is not None and f < 172800:
            conf += 0.1
            parts.append("+0.10 subscription fact fresher than 2 days")
        gid = chosen["goal_id"]
        gdef = pack.goals_by_id[gid]
        start = now
        window = gdef["success"]["window_hours"]
        target = {}
        if gid == "G07":
            u = unfinished_series(pack, facts, now, facts["dialect"])
            if u:
                target = {"slug": u[0]["slug"], "title_id": u[0]["title_id"], "next_episode": u[0]["next_episode"]}
        goal = {"instance": "%s:%s:%d" % (uid, gid, len(state_goals(self.store.get(uid, "agent")))), "goal_id": gid, "name": gdef["name"],
                "started_at": iso(start), "deadline": iso(start + timedelta(hours=window)),
                "rule_version": self.rulebook.current_version(gid),
                "success_signal": success_signal_text(self.rulebook.success_rule(gid, self.rulebook.current_version(gid))),
                "status": "open", "target": target, "messages_planned": 0, "assumptions": list(chosen["assumptions"]),
                "evidence": list(chosen["evidence"]), "source": "model_verified" if (claim and not dec["unsupported_claims"] and used_model) else "rules"}
        info.update(confidence=max(0.2, min(0.9, round(conf, 2))), parts=parts)
        return goal, info

    def _commit_goal(self, uid, state, goal, dec, at):
        if state["goal"] is None or state["goal"]["instance"] != goal["instance"]:
            goal["started_at"] = iso(at)
            goal["deadline"] = iso(at + timedelta(hours=self.pack.goals_by_id[goal["goal_id"]]["success"]["window_hours"]))
            state["goal"] = goal
            self.open_goals[goal["goal_id"]].add(uid)
            self.store.touch(uid)

    def _finalise_goal_conf(self, dec, info, cached=False, has_conflicts=False, facts=None):
        conf = info.get("confidence", 0.6)
        dec["confidence"] = conf
        dec["confidence_reason"] = "; ".join(info.get("parts", ["rule-based"]))

    # ------------------------------------------------------------------ shadow goals (measurement only, never an action)
    def _shadow_goal(self, trig):
        """Assigned goal at the viewer's FIRST fresh trigger, by the same pure rules for both arms. Used only to define
        who is analysed and which success event is judged; it changes nothing for the viewer."""
        uid = trig["user_id"]
        if uid in self.shadow or trig["_delay_s"] > self.cfg.stale_trigger_s:
            return
        facts = resolve_facts(self.pack, uid, self.events[uid], trig, trig["_recv"], None)
        evals = eligible_goals(self.pack, facts, trig["_recv"], self.cfg, self.vocab)
        ch = rules_choice(evals)
        rec = {"viewer_id": uid, "arm": self.pack.arms.get(uid), "trigger_event_id": trig["event_id"],
               "goal_id": None, "started_at": iso(trig["_recv"])}
        if ch:
            gid = ch["goal_id"]
            gdef = self.pack.goals_by_id[gid]
            target = {}
            if gid == "G07" and facts["dialect"]:
                u = unfinished_series(self.pack, facts, trig["_recv"], facts["dialect"])
                if u:
                    target = {"slug": u[0]["slug"]}
            rec.update(goal_id=gid, deadline=iso(trig["_recv"] + timedelta(hours=gdef["success"]["window_hours"])),
                       rule_version=self.rulebook.current_version(gid), target=target)
        self.shadow[uid] = rec

    # ------------------------------------------------------------------ actions
    def _page_ids(self, page):
        ids = [page["hero_title_id"]] if page.get("hero_title_id") else []
        for r in page["rows"]:
            ids.extend(r["title_ids"])
        return ids

    def _publish_page(self, uid, state, page, goal, dec, pub, trig):
        aid = self._aid()
        act = {"action_id": aid, "user_id": uid, "type": "home_page", "published_at": iso(pub), "dialect": page["dialect"],
               "hero_title_id": page["hero_title_id"], "rows": page["rows"], "ttl_minutes": self.cfg.page_ttl_min,
               "decision_id": dec["decision_id"], "goal_id": goal["goal_id"], "trigger_event_id": trig["event_id"]}
        self.actions.append(act)
        state["actions"].append(aid)
        old = state["live_page"]
        if old:
            for t in old.get("title_ids", []):
                self.live_by_title[t].discard(uid)
        state["live_page"] = {"action_id": aid, "published_at": iso(pub), "dialect": page["dialect"], "goal_instance": goal["instance"],
                              "title_ids": self._page_ids(page)}
        for t in state["live_page"]["title_ids"]:
            self.live_by_title[t].add(uid)
        self.stats["home_pages_published"] += 1
        return act

    def _ledger(self, state, facts):
        days = Counter(facts["sent_per_ist_day"])
        for m in state["messages"]:
            if m["status"] in ("pending", "sent") and m["type"] in ("push", "whatsapp"):
                days[ist_day(parse(m["send_at"]))] += 1
        return days

    def _schedule_message(self, uid, state, goal, msg, dec, pub):
        aid = self._aid()
        act = {"action_id": aid, "user_id": uid, "type": msg["type"], "published_at": iso(pub), "send_at": iso(msg["send_at"]),
               "title_id": msg.get("title_id"), "decision_id": dec["decision_id"], "goal_id": goal["goal_id"]}
        for k in ("title", "body", "deeplink", "template_id", "variables"):
            if msg.get(k) is not None:
                act[k] = msg[k]
        self.pending_actions[aid] = act
        self.seq += 1
        heapq.heappush(self.pending, (msg["send_at"], self.seq, aid))
        state["messages"].append({"action_id": aid, "type": msg["type"], "send_at": iso(msg["send_at"]), "status": "pending",
                                  "goal_instance": goal["instance"], "title_id": msg.get("title_id")})
        state["actions"].append(aid)
        goal["messages_planned"] = goal.get("messages_planned", 0) + 1
        self.stats["messages_scheduled"] += 1
        return aid

    def _cancel_pending(self, uid, pred, reason):
        state = self.store.states.get(uid)
        if not state:
            return
        for m in state["messages"]:
            if m["status"] == "pending" and pred(m):
                m["status"] = "cancelled"
                m["reason"] = reason
                act = self.pending_actions.pop(m["action_id"], None)
                if act:
                    self.cancelled.append({**act, "cancelled_reason": reason, "cancelled_at": iso(self.last_ts) if getattr(self, "last_ts", None) else None})
                    self.stats["messages_cancelled"] += 1
                self.store.touch(uid)

    def _release_due(self, until):
        while self.pending and (until is None or self.pending[0][0] <= until):
            send_at, _, aid = heapq.heappop(self.pending)
            act = self.pending_actions.get(aid)
            if act is None:
                continue                                  # cancelled
            uid = act["user_id"]
            state = self.store.get(uid, "agent")
            m = next(x for x in state["messages"] if x["action_id"] == aid)
            facts = resolve_facts(self.pack, uid, self.events[uid], None, send_at, state["learnings"])
            ledger = self._ledger(state, facts)
            ledger[ist_day(send_at)] -= 1                  # do not count this message against itself
            msg = {"type": act["type"], "title_id": act.get("title_id"), "title": act.get("title"), "body": act.get("body"),
                   "deeplink": act.get("deeplink"), "template_id": act.get("template_id"), "variables": act.get("variables")}
            chk = verify_message(msg, facts, send_at, self.pack, self.env, self.cfg, ledger, "agent")
            g = state["goal"]
            if g is None or g["instance"] != m["goal_instance"]:
                chk.check("goal_active", False, "goal closed before the send time")
            self.pending_actions.pop(aid, None)
            if chk.failed:
                m["status"] = "cancelled"
                m["reason"] = "; ".join(f["check"] + ": " + f["reason"] for f in chk.failed)
                self.cancelled.append({**act, "cancelled_reason": m["reason"], "cancelled_at": iso(send_at)})
                self.stats["messages_cancelled"] += 1
            else:
                m["status"] = "sent"
                self.actions.append(act)
                self.stats["messages_released"] += 1
            self.store.touch(uid)

    # ------------------------------------------------------------------ live events and control events
    def _on_live(self, e):
        uid = e["user_id"]
        self.events[uid].append(e)
        self.last_ts = e["_recv"]
        self.stats["live_events"] += 1
        st = self.store.states.get(uid)
        if not st or st["arm"] != "agent" or st["goal"] is None:
            return
        g = st["goal"]
        res = judge_goal(g, self.rulebook, self.pack, self.events[uid], e["_recv"])
        reason = None
        if res["status"] == "succeeded":
            reason = ("achieved", "success event %s observed after the decision" % res["signal_event_id"])
        elif g["goal_id"] in STATUS_GOALS and e["event"] in ("purchase_completed", "subscription_renewed", "subscription_lapsed"):
            trig = self.last_trigger.get(uid)
            if trig:
                facts = resolve_facts(self.pack, uid, self.events[uid], trig, e["_recv"], st["learnings"])
                v = verify_goal(self.pack, g["goal_id"], facts, e["_recv"], self.cfg, self.vocab)
                if v.failed:
                    reason = ("superseded", "no longer eligible after %s: %s" % (e["event"], v.failed[0]["reason"]))
        if reason is None:
            return
        n_pending = sum(1 for m in st["messages"] if m["status"] == "pending")
        self._close_goal(uid, st, g, reason[0], reason[1])
        dec = self._skeleton("REPLAN", uid, "agent", e, 0.0)
        dec["trigger"]["event"] = e["event"]
        dec["trigger"]["deliver_at"] = iso(e["_deliver"])
        dec["no_action_reason"] = "goal_revised: %s" % reason[1]
        dec["status"] = "NO_ACTION"
        dec["cancelled_pending_messages"] = n_pending
        dec["confidence"], dec["confidence_reason"] = 0.9, "closed from an attributable event; pending messages that no longer make sense were cancelled"
        dec["evidence"] = ["event:%s" % e["event_id"]]
        dec["decided_in_ms"] = int(self.cfg.tool_latency_s * 3000)
        dec["decided_at"] = iso(e["_deliver"])
        trig = self.last_trigger.get(uid)
        if trig is not None:
            facts = resolve_facts(self.pack, uid, self.events[uid], trig, e["_recv"], st["learnings"])
            if facts["dialect"]:
                evals = eligible_goals(self.pack, facts, e["_recv"], self.cfg, self.vocab)
                ch = rules_choice(evals)
                if ch:
                    gdef = self.pack.goals_by_id[ch["goal_id"]]
                    ng = {"instance": "%s:%s:%d" % (uid, ch["goal_id"], len(st["goals"])), "goal_id": ch["goal_id"], "name": gdef["name"],
                          "started_at": iso(e["_recv"]), "deadline": iso(e["_recv"] + timedelta(hours=gdef["success"]["window_hours"])),
                          "rule_version": self.rulebook.current_version(ch["goal_id"]),
                          "success_signal": success_signal_text(self.rulebook.success_rule(ch["goal_id"], self.rulebook.current_version(ch["goal_id"]))),
                          "status": "open", "target": {}, "messages_planned": 0, "assumptions": ch["assumptions"], "evidence": ch["evidence"], "source": "replan"}
                    st["goal"] = ng
                    self.open_goals[ng["goal_id"]].add(uid)
                    dec["goal"] = self._goal_view(ng)
                    dec["no_action_reason"] += "; new goal %s planned, next intervention waits for the next open" % ng["goal_id"]
        self.decisions.append(dec)
        self.stats["replans"] += 1
        self.store.touch(uid)

    def _on_control(self, c, at):
        self.last_ts = at
        self.control_log.append(c)
        t = c["type"]
        if t == "title_withdrawn":
            self._on_withdrawn(c, at)
        elif t == "goal_rule_corrected":
            self._on_rule_correction(c, at)

    def _on_withdrawn(self, c, at):
        title_id = c["title_id"]
        self.env.withdraw(title_id, at)
        affected = sorted(self.live_by_title.get(title_id, set()))
        n_msg = 0
        for uid in list(self.store.states):
            st = self.store.states[uid]
            before = self.stats["messages_cancelled"]
            self._cancel_pending(uid, lambda m: m.get("title_id") == title_id, "title %s withdrawn" % title_id)
            n_msg += self.stats["messages_cancelled"] - before
        remediated = 0
        for uid in affected:
            st = self.store.states[uid]
            lp = st["live_page"]
            if not lp or parse(lp["published_at"]) + timedelta(minutes=self.cfg.page_ttl_min) <= at:
                continue
            lp["invalid"] = True
            trig = self.last_trigger.get(uid)
            goal = st["goal"]
            dec = self._skeleton("PAGE_REMEDIATION", uid, "agent", trig, 0.0) if trig else None
            if dec is None or goal is None:
                continue
            facts = resolve_facts(self.pack, uid, self.events[uid], trig, at, st["learnings"])
            pub = at + timedelta(seconds=0.2)
            facts.v["dialect"] = lp["dialect"]          # the page's own dialect, not whatever the last trigger said
            page, notes = build_page(self.pack, self.env, facts, goal, pub, self.cfg)
            dec["trigger"]["event"] = "title_withdrawn"
            dec["trigger"]["received_at"] = iso(at)
            dec["trigger"]["deliver_at"] = iso(at)
            dec["goal"] = self._goal_view(goal)
            dec["evidence"] = ["control:title_withdrawn:%s" % title_id]
            if page is not None:
                chk = verify_page(page, facts, pub, self.pack, self.env, self.cfg, "agent")
            if page is None or chk.failed:
                dec["no_action_reason"] = "page_remediation_failed: %s" % (notes if page is None else chk.failed[0]["reason"])
                dec["checks"]["failed"] = [] if page is None else chk.failed
                dec["confidence"], dec["confidence_reason"] = 0.6, "live page contains a withdrawn title and no replacement could be verified"
            else:
                a = self._publish_page(uid, st, page, goal, dec, pub, trig)
                lp_new = st["live_page"]
                dec.update(status="PUBLISHED", action="HOME_PAGE", action_types=["HOME_PAGE"], actions=[a["action_id"]],
                           content=["title:%s" % t for t in self._page_ids(page)], published_at=iso(pub))
                dec["checks"] = {"passed": chk.passed, "failed": []}
                dec["confidence"], dec["confidence_reason"] = 0.9, "replacement page built without the withdrawn title and re-verified"
                dec["no_action_reason"] = None
                remediated += 1
            dec["decided_in_ms"] = 200
            dec["decided_at"] = iso(pub)
            self.decisions.append(dec)
            self.store.touch(uid)
        self.live_by_title[title_id] = set()
        self.stats["withdrawal_pages_remediated"] += remediated
        self.control_log[-1] = {**c, "effect": {"live_pages_affected": len(affected), "pages_replaced": remediated, "pending_messages_cancelled": n_msg}}

    def _on_rule_correction(self, c, at):
        gid = c["goal_id"]
        nv = self.rulebook.correct(gid, at, c["new_success"])
        affected = []
        for uid in sorted(self.open_goals.get(gid, set())):
            st = self.store.states[uid]
            g = st["goal"]
            if g and g["goal_id"] == gid and parse(g["deadline"]) > at and g["status"] == "open":
                g.setdefault("rule_history", []).append({"from": g["rule_version"], "to": nv, "at": iso(at)})
                g["rule_version"] = nv
                g["success_signal"] = success_signal_text(c["new_success"])
                affected.append(uid)
                dec = self._skeleton("RULE_CORRECTION", uid, "agent", None, 0.0)
                dec["trigger"] = {"event": "goal_rule_corrected", "received_at": iso(at), "deliver_at": iso(at), "event_age_s": 0.0}
                dec["goal"] = self._goal_view(g)
                dec["no_action_reason"] = "goal rule corrected (%s v%d -> v%d); only this open goal is re-judged under the new rule" % (gid, nv - 1, nv)
                dec["confidence"], dec["confidence_reason"] = 1.0, "control event from the analytics owner"
                dec["evidence"] = ["control:goal_rule_corrected:%s" % gid]
                dec["decided_in_ms"], dec["decided_at"] = 0, iso(at)
                self.decisions.append(dec)
                self.store.touch(uid)
        shadow_hit = 0
        for rec in self.shadow.values():
            if rec.get("goal_id") == gid and parse(rec["deadline"]) > at:
                rec["rule_version"] = nv
                shadow_hit += 1
        total_agent_users = len(self.store.states)
        self.rule_corrections.append({"goal_id": gid, "at": iso(at), "new_version": nv, "open_goals_reevaluated": len(affected),
                                      "viewer_ids": affected, "shadow_goals_updated": shadow_hit,
                                      "viewers_not_touched": total_agent_users - len(affected)})
        self.control_log[-1] = {**c, "effect": self.rule_corrections[-1]}

    # ------------------------------------------------------------------ outputs
    def _write_outputs(self):
        def dump(name, rows):
            with open(os.path.join(self.out, name), "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        acts = sorted(self.actions, key=lambda a: (a.get("send_at") or a["published_at"], a["action_id"]))
        dump("decisions.jsonl", self.decisions)
        dump("actions.jsonl", acts)
        dump("cancelled_actions.jsonl", self.cancelled)
        dump("shadow_goals.jsonl", sorted(self.shadow.values(), key=lambda r: r["viewer_id"]))
        dump("security_log.jsonl", self.pack.security_log)
        with open(os.path.join(self.out, "control_log.json"), "w", encoding="utf-8") as f:
            json.dump({"control_events": self.control_log, "rule_corrections": self.rule_corrections}, f, indent=1, default=str)
        with open(os.path.join(self.out, "data_issues.json"), "w", encoding="utf-8") as f:
            json.dump(self.pack.issues, f, indent=1, default=str)
        self.store.flush()


def state_goals(state):
    return state["goals"]
