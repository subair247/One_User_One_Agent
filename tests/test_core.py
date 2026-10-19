import json
import unittest

from fixtures import Scenario, decisions_for


def new_viewer(s, uid="U-1", **kw):
    s.user(uid, **kw)
    s.history(uid, "signup_completed", "2026-09-29T10:00:00.000Z")


def resume_viewer(s, uid, **kw):
    """Long-time viewer who left show1 unfinished 11 days ago and opened the app 6 days ago (so G12 does not apply)."""
    s.user(uid, signup="2025-01-01T00:00:00.000Z", **kw)
    s.history(uid, "app_open", "2026-09-25T12:00:00.000Z", session="old")
    s.play_history(uid, ("T001", "show1-d1"), "2026-09-20T12:00:00.000Z", complete=True)


class LateAndDuplicateTriggers(unittest.TestCase):
    def tearDown(self):
        self.s.cleanup()

    def test_duplicate_trigger_makes_no_second_action(self):
        self.s = s = Scenario()
        new_viewer(s)
        t = s.trigger("U-1", "2026-10-01T10:00:00.000Z", session="a")
        s.triggers.append(dict(t))                       # same event_id delivered twice
        h = s.run()
        d = decisions_for(h, "U-1")
        self.assertEqual(len(d), 1)                      # loader dedupes by event_id
        self.assertEqual(sum(1 for a in h.actions if a["type"] == "home_page"), 1)
        # and the runtime idempotency key also holds if a duplicate slips past the loader
        h.pack.triggers.append(h.pack.triggers[0])
        n = len(h.actions)
        h._on_trigger(h.pack.triggers[0])
        self.assertEqual(h.decisions[-1]["status"], "DUPLICATE_IGNORED")
        self.assertEqual(len(h.actions), n)

    def test_stale_offline_buffered_trigger_is_skipped(self):
        self.s = s = Scenario()
        new_viewer(s)
        s.trigger("U-1", "2026-10-01T10:00:00.000Z", delay=86400 * 2, session="old")      # two days in the offline buffer
        h = s.run()
        d = decisions_for(h, "U-1")[0]
        self.assertEqual(d["status"], "NO_ACTION")
        self.assertTrue(d["no_action_reason"].startswith("stale_trigger"))
        self.assertEqual(h.actions, [])

    def test_wrong_phone_clock_uses_received_at(self):
        self.s = s = Scenario()
        new_viewer(s)
        e = s.trigger("U-1", "2026-10-01T10:00:00.000Z", session="a")
        e["sent_at"] = e["occurred_at"] = "2026-10-05T10:00:00.000Z"       # phone clock 4 days ahead, no buffering
        h = s.run()
        d = decisions_for(h, "U-1")[0]
        self.assertEqual(d["status"], "PUBLISHED")                          # not treated as stale or in the future


class ConflictingSources(unittest.TestCase):
    def tearDown(self):
        self.s.cleanup()

    def test_dialect_app_setting_wins_and_conflicts_are_listed(self):
        self.s = s = Scenario()
        new_viewer(s, dialect="d1")
        s.trigger("U-1", "2026-10-01T10:00:00.000Z", dialect="d2", props={"deeplink": "ott://c/cmp1/d1/title/T001"})
        h = s.run()
        d = decisions_for(h, "U-1")[0]
        self.assertEqual(d["dialect"]["used"], "d2")
        self.assertIn("profile:d1", d["dialect"]["conflicts"])
        self.assertIn("campaign_deeplink:d1", d["dialect"]["conflicts"])
        page = [a for a in h.actions if a["type"] == "home_page"][0]
        self.assertEqual(page["dialect"], "d2")
        for r in page["rows"]:
            for t in r["title_ids"]:
                self.assertEqual(h.pack.titles[t].dialect, "d2")

    def test_newer_purchase_event_beats_stale_profile(self):
        self.s = s = Scenario()
        s.user("U-2", status="never", signup="2025-01-01T00:00:00.000Z")
        s.history("U-2", "purchase_completed", "2026-09-30T08:00:00.000Z", {"plan_id": "P_MONTH", "price_inr": 149, "is_trial": False})
        s.trigger("U-2", "2026-10-01T10:00:00.000Z")
        h = s.run()
        d = decisions_for(h, "U-2")[0]
        self.assertTrue(any("subscription" in c for c in d["conflicts"]))
        self.assertNotIn(d["goal"]["id"] if d["goal"] else "", ("G05",))     # not 'first paid': events say they pay

    def test_duplicate_profile_rows_are_resolved(self):
        self.s = s = Scenario()
        s.user("U-3", status="never", updated="2026-09-29T12:00:00.000Z", signup="2025-01-01T00:00:00.000Z")
        s.user("U-3", status="lapsed", updated="2026-09-28T12:00:00.000Z", signup="2025-01-01T00:00:00.000Z")
        s.trigger("U-3", "2026-10-01T10:00:00.000Z")
        h = s.run()
        d = decisions_for(h, "U-3")[0]
        self.assertTrue(any("profile_duplicate_rows" in c for c in d["conflicts"]))


class Failures(unittest.TestCase):
    def tearDown(self):
        self.s.cleanup()

    def test_model_outage_falls_back_to_rules(self):
        self.s = s = Scenario()
        new_viewer(s)
        s.control.append({"at": "2026-10-01T09:00:00.000Z", "type": "dependency_unavailable", "dependency": "model_provider",
                          "until": "2026-10-01T09:30:00.000Z", "detail": "503"})
        s.trigger("U-1", "2026-10-01T09:10:00.000Z")
        h = s.run()
        d = decisions_for(h, "U-1")[0]
        self.assertEqual(d["status"], "PUBLISHED")
        self.assertTrue(any(f.startswith("model_http_503") for f in d["fallback_used"]))
        self.assertLessEqual(d["decided_in_ms"], 60000)

    def test_malformed_and_timeout_faults(self):
        self.s = s = Scenario()
        new_viewer(s, "U-1")
        new_viewer(s, "U-2")
        a = s.trigger("U-1", "2026-10-01T10:00:00.000Z", session="a")
        b = s.trigger("U-2", "2026-10-01T10:01:00.000Z", session="b")
        s.faults += [{"trigger_event_id": a["event_id"], "user_id": "U-1", "fault": "malformed_output", "model_output": "{\"goal_id\": \"G02\", \"eli"},
                     {"trigger_event_id": b["event_id"], "user_id": "U-2", "fault": "timeout", "seconds": 45}]
        h = s.run()
        da, db = decisions_for(h, "U-1")[0], decisions_for(h, "U-2")[0]
        self.assertEqual(da["status"], "PUBLISHED")
        self.assertIn("model_malformed_output_retry", da["fallback_used"])
        self.assertEqual(db["status"], "PUBLISHED")
        self.assertIn("model_timeout", db["fallback_used"])
        self.assertLess(db["decided_in_ms"], 60000)           # the 45 s model hang is capped by the 5 s timeout

    def test_data_store_timeout_uses_cache_and_no_message(self):
        self.s = s = Scenario()
        new_viewer(s, "U-1", push=True)
        s.control.append({"at": "2026-10-01T09:00:00.000Z", "type": "dependency_degraded", "dependency": "user_data_store",
                          "until": "2026-10-01T10:00:00.000Z", "detail": "slow"})
        for i in range(30):
            u = "U-%d" % (i + 10)
            new_viewer(s, u)
            s.trigger(u, "2026-10-01T09:%02d:00.000Z" % (i + 1), session="x%d" % i)
        h = s.run()
        cached = [d for d in h.decisions if d.get("state_source") == "cache"]
        self.assertTrue(cached)                               # some reads timed out
        for d in cached:
            self.assertNotIn("PUSH", d["action"])             # smaller safe action: page only
            self.assertIn("store_timeout_cached_state", d["fallback_used"])

    def test_tool_budget_exceeded_degrades_to_no_action(self):
        self.s = s = Scenario()
        new_viewer(s)
        s.trigger("U-1", "2026-10-01T10:00:00.000Z")
        h = s.run(max_tool_calls=3)
        d = decisions_for(h, "U-1")[0]
        self.assertEqual(d["status"], "NO_ACTION")
        self.assertIn("budget_exceeded", d["no_action_reason"])
        self.assertEqual(h.actions, [])

    def test_unsupported_model_claim_is_rejected(self):
        self.s = s = Scenario()
        new_viewer(s)
        t = s.trigger("U-1", "2026-10-01T10:00:00.000Z")
        s.faults.append({"trigger_event_id": t["event_id"], "user_id": "U-1", "fault": "unsupported_claim",
                         "model_output": {"goal_id": "G04", "eligible": True, "confidence": 0.97, "reason": "trial ends tomorrow (confirmed in billing)"}})
        h = s.run()
        d = decisions_for(h, "U-1")[0]
        self.assertEqual(len(d["unsupported_claims"]), 1)
        self.assertNotEqual(d["goal"]["id"], "G04")
        self.assertIn("rules_after_unsupported_claim", d["fallback_used"])


class CorrectedGoalRule(unittest.TestCase):
    def tearDown(self):
        self.s.cleanup()

    def test_only_open_g03_goals_are_reevaluated(self):
        self.s = s = Scenario()
        # U-1: signed up recently, already played+completed -> G03. U-2: still unplayed -> G01 (not affected).
        new_viewer(s, "U-1")
        s.play_history("U-1", ("T001", "show1-d1"), "2026-09-29T12:00:00.000Z", complete=True)
        new_viewer(s, "U-2")
        s.trigger("U-1", "2026-10-01T06:00:00.000Z", session="a")
        s.trigger("U-2", "2026-10-01T06:01:00.000Z", session="b")
        s.control.append({"at": "2026-10-01T08:30:00.000Z", "type": "goal_rule_corrected", "goal_id": "G03",
                          "old_success": {"event": "app_open"}, "new_success": {"event": "play_started", "condition": "on a later IST calendar day within the window"},
                          "applies_to": "every G03 goal that is still open"})
        h = s.run()
        self.assertEqual(decisions_for(h, "U-1")[0]["goal"]["id"], "G03")
        rc = h.rule_corrections[0]
        self.assertEqual(rc["viewer_ids"], ["U-1"])
        self.assertEqual(h.store.states["U-1"]["goal"]["rule_version"], 2)
        self.assertEqual(h.store.states["U-2"]["goal"]["rule_version"], 1)
        self.assertEqual(len(decisions_for(h, "U-2", "RULE_CORRECTION")), 0)          # nobody else is reprocessed

    def test_judge_uses_the_corrected_rule(self):
        from oua.goals import RuleBook, judge_goal
        from oua.loader import load_pack, normalise_event
        self.s = s = Scenario()
        new_viewer(s)
        s.trigger("U-1", "2026-10-01T06:00:00.000Z")
        pack = load_pack(s.write())
        rb = RuleBook(pack.goals)
        from oua.timeutil import parse
        rb.correct("G03", parse("2026-10-01T08:30:00.000Z"), {"event": "play_started", "condition": "later IST day"})
        opened = {"goal_id": "G03", "started_at": "2026-10-01T06:00:00.000Z", "deadline": "2026-10-03T06:00:00.000Z", "rule_version": 2, "status": "open"}
        old = dict(opened, rule_version=1)
        mk = lambda ev, t: normalise_event({"event_id": ev + t, "user_id": "U-1", "event": ev, "occurred_at": t, "sent_at": t, "received_at": t,
                                            "session_id": "x", "app_dialect": "d1", "properties": {}})
        evs = [mk("app_open", "2026-10-02T10:00:00.000Z")]
        end = parse("2026-10-16T00:00:00.000Z")
        self.assertEqual(judge_goal(old, rb, pack, evs, end)["status"], "succeeded")      # old rule: an app_open counted
        self.assertEqual(judge_goal(opened, rb, pack, evs, end)["status"], "failed")       # corrected rule: it does not


class SafetyRules(unittest.TestCase):
    def tearDown(self):
        self.s.cleanup()

    def test_holdout_gets_nothing(self):
        self.s = s = Scenario()
        new_viewer(s, "U-H", arm="holdout")
        s.trigger("U-H", "2026-10-01T10:00:00.000Z")
        h = s.run()
        self.assertEqual(h.actions, [])
        self.assertEqual(decisions_for(h, "U-H")[0]["status"], "HOLDOUT_NO_ACTION")
        self.assertNotIn("U-H", h.store.states)               # no state, no goal, nothing

    def test_quiet_hours_and_frequency_cap(self):
        from oua.timeutil import in_quiet_hours, ist_day, parse
        self.s = s = Scenario()
        # resume-series viewers with push consent; two triggers different days must not produce two pushes on one IST day
        for u in ("U-1", "U-2"):
            resume_viewer(s, u)
            s.history(u, "message_delivered", "2026-10-01T13:00:00.000Z", {"channel": "push", "campaign_id": "x", "title_id": "T002"})
            s.trigger(u, "2026-10-01T10:00:00.000Z")
        h = s.run()
        pushes = [a for a in h.actions if a["type"] in ("push", "whatsapp")]
        self.assertTrue(pushes)
        per = {}
        for a in pushes:
            at = parse(a["send_at"])
            self.assertFalse(in_quiet_hours(at))
            per[(a["user_id"], ist_day(at))] = per.get((a["user_id"], ist_day(at)), 0) + 1
        self.assertTrue(all(v == 1 for v in per.values()))
        for a in pushes:     # the IST day that already had a delivered message is never used
            self.assertNotEqual(str(ist_day(parse(a["send_at"]))), "2026-10-01")

    def test_no_consent_no_message_and_opt_out_overrides_profile(self):
        self.s = s = Scenario()
        for u, push in (("U-1", False), ("U-2", True)):
            resume_viewer(s, u, push=push)
            s.trigger(u, "2026-10-01T10:00:00.000Z")
        s.history("U-2", "notification_opt_out", "2026-09-30T10:00:00.000Z", {"channel": "push"})
        h = s.run()
        self.assertFalse([a for a in h.actions if a["type"] in ("push", "whatsapp")])

    def test_injected_instruction_is_logged_never_obeyed(self):
        self.s = s = Scenario()
        new_viewer(s, "U-1", push=False)
        new_viewer(s, "U-2", push=False)
        s.trigger("U-1", "2026-10-01T10:00:00.000Z")
        h = s.run()
        self.assertTrue(any(x["id"] == "T100" for x in h.pack.security_log))
        self.assertFalse([a for a in h.actions if a["user_id"] == "U-2"])                 # U-2 was never messaged
        self.assertFalse([a for a in h.actions if a["type"] == "whatsapp"])

    def test_row_with_fewer_than_three_valid_titles_is_dropped(self):
        self.s = s = Scenario()
        new_viewer(s, "U-1", dialect="d2")                    # d2 has only 4 titles: hero + one row of 3
        s.trigger("U-1", "2026-10-01T10:00:00.000Z", dialect="d2")
        h = s.run()
        page = [a for a in h.actions if a["type"] == "home_page"][0]
        self.assertTrue(page["rows"])
        for r in page["rows"]:
            self.assertGreaterEqual(len(r["title_ids"]), 3)

    def test_withdrawn_title_is_not_used_and_live_page_is_replaced(self):
        self.s = s = Scenario()
        new_viewer(s, "U-1")
        s.trigger("U-1", "2026-10-01T09:00:00.000Z", session="a")
        s.trigger("U-1", "2026-10-01T12:00:00.000Z", session="b")
        h0 = s.run()
        first_page = [a for a in h0.actions if a["type"] == "home_page"][0]
        victim = first_page["hero_title_id"]
        s.cleanup()
        self.s = s = Scenario()
        new_viewer(s, "U-1")
        s.trigger("U-1", "2026-10-01T09:00:00.000Z", session="a")
        s.trigger("U-1", "2026-10-01T12:00:00.000Z", session="b")
        s.control.append({"at": "2026-10-01T09:30:00.000Z", "type": "title_withdrawn", "title_id": victim, "reason": "licence"})
        h = s.run()
        pages = [a for a in h.actions if a["type"] == "home_page"]
        self.assertGreaterEqual(len(pages), 2)                              # original + replacement
        for p in pages:
            if p["published_at"] > "2026-10-01T09:30:00.000Z":
                ids = [p["hero_title_id"]] + [t for r in p["rows"] for t in r["title_ids"]]
                self.assertNotIn(victim, ids)

    def test_unmeasurable_goals_are_never_chosen(self):
        self.s = s = Scenario()
        new_viewer(s)
        s.trigger("U-1", "2026-10-01T10:00:00.000Z")
        h = s.run()
        for d in h.decisions:
            if d["goal"]:
                self.assertNotIn(d["goal"]["id"], ("G10", "G11"))
        from oua.goals import measurable
        self.assertFalse(measurable(h.pack.goals_by_id["G10"], h.vocab)[0])
        self.assertFalse(measurable(h.pack.goals_by_id["G11"], h.vocab)[0])


class Purchase(unittest.TestCase):
    def test_purchase_after_decision_replans_and_stops_first_paid_goal(self):
        s = Scenario()
        try:
            s.user("U-1", status="never", signup="2025-01-01T00:00:00.000Z", push=True)
            s.history("U-1", "app_open", "2026-09-25T12:00:00.000Z", session="old")
            s.play_history("U-1", ("T100", "evil-d1"), "2026-09-26T12:00:00.000Z", complete=True)       # single-episode title: nothing unfinished
            s.trigger("U-1", "2026-10-01T10:00:00.000Z")
            s.live_event("U-1", "purchase_completed", "2026-10-01T10:02:00.000Z", {"plan_id": "P_MONTH", "price_inr": 149, "is_trial": False})
            h = s.run()
            first = decisions_for(h, "U-1")[0]
            self.assertEqual(first["goal"]["id"], "G05")
            replan = decisions_for(h, "U-1", "REPLAN")
            self.assertEqual(len(replan), 1)
            self.assertNotEqual((replan[0]["goal"] or {}).get("id"), "G05")
            self.assertEqual(h.store.states["U-1"]["goals"][0]["status"], "achieved")   # the purchase IS the G05 success event
        finally:
            s.cleanup()


if __name__ == "__main__":
    unittest.main()
