"""Independent verifier. It never sees the planner's reasoning; it re-derives facts from raw state and can reject anything.

Checks: eligibility, measurable, dialect, availability, consent, quiet_hours, frequency_cap, copy_policy, template,
page_structure, duplicates, holdout, injection, deeplink, staleness.
"""
from __future__ import annotations

import re

from .goals import evaluate_goal
from .sanitize import scan_text
from .timeutil import in_quiet_hours, ist_day

ROW_TYPES = {"next_episode", "because_you_watched", "popular_in_dialect", "new_releases", "genre"}
DEEPLINK_OUT = re.compile(r"^ott://title/T\d+$")


class Checker:
    def __init__(self):
        self.passed, self.failed = [], []

    def check(self, name, ok, reason=""):
        if ok:
            if name not in self.passed:
                self.passed.append(name)
        else:
            self.failed.append({"check": name, "reason": reason})
        return ok

    def merge(self, other):
        for p in other.passed:
            if p not in self.passed and not any(f["check"] == p for f in self.failed):
                self.passed.append(p)
        self.failed.extend(other.failed)


def verify_goal(pack, goal_id, facts, now, cfg, vocabulary, claimed_by_model=False, continued=False):
    """`continued=True` re-checks a goal committed earlier: event-based goals stay valid until their own success or
    deadline (the viewer's own return must not make the goal that brought them back ineligible)."""
    from .goals import STATUS_GOALS, measurable
    c = Checker()
    if goal_id is None:
        return c
    if continued and goal_id not in STATUS_GOALS:
        ok, why = measurable(pack.goals_by_id[goal_id], vocabulary)
        c.check("measurable", ok, why or "")
        c.check("eligibility", True)
        return c
    ev = evaluate_goal(goal_id, pack, facts, now, cfg, vocabulary)
    c.check("measurable", ev["measurable"], ev["reason"] or "")
    ok = ev["eligible"]
    reason = ev["reason"] or "no supporting event or profile fact"
    c.check("eligibility", ok, reason)
    if claimed_by_model:
        c.check("evidence_supported", ok and bool(ev["evidence"]), "model claim has no supporting event/profile fact: %s" % reason)
    return c


def verify_page(page, facts, at, pack, env, cfg, arm):
    c = Checker()
    c.check("holdout", arm != "holdout", "viewer is in the holdout arm")
    d = page["dialect"]
    c.check("dialect", d == facts["dialect"] and d is not None, "page dialect %s != app dialect %s" % (d, facts["dialect"]))
    ids = ([page["hero_title_id"]] if page.get("hero_title_id") else []) + [t for r in page["rows"] for t in r["title_ids"]]
    bad_d = [t for t in ids if getattr(pack.titles.get(t), "dialect", None) != d]
    c.check("dialect", not bad_d, "titles not in page dialect: %s" % bad_d[:3])
    bad_a = [t for t in ids if not env.available(pack.titles.get(t), at)]
    c.check("availability", not bad_a, "titles unavailable at publish time: %s" % bad_a[:3])
    slugs = [pack.titles[t].slug for t in ids if t in pack.titles]
    c.check("duplicates", len(slugs) == len(set(slugs)), "same content_slug appears twice on the page")
    struct = (len(page["rows"]) <= cfg.max_rows and all(r["row_type"] in ROW_TYPES for r in page["rows"]) and
              all(cfg.min_titles_per_row <= len(r["title_ids"]) <= cfg.max_titles_per_row for r in page["rows"]))
    c.check("page_structure", struct and len(page["rows"]) >= 1, "row count/type/size limits violated")
    return c


def _allowed_prices(pack):
    return {p["price_inr"] for p in pack.pricing["plans"]}


def verify_message(msg, facts, at, pack, env, cfg, sent_days, arm):
    """`at` is the send time. `sent_days` maps IST day -> messages already sent/scheduled (excluding this one)."""
    c = Checker()
    ch = msg["type"]
    c.check("holdout", arm != "holdout", "viewer is in the holdout arm")
    if ch == "push":
        c.check("consent", bool(facts["consent_push"]), "no push consent")
    elif ch == "whatsapp":
        c.check("consent", bool(facts["consent_whatsapp"]), "no WhatsApp consent")
    if ch in ("push", "whatsapp"):
        c.check("quiet_hours", not in_quiet_hours(at), "send time is inside 22:00-08:00 IST")
        c.check("frequency_cap", sent_days.get(ist_day(at), 0) == 0, "another push/WhatsApp already on this IST day")
    tid = msg.get("title_id")
    if tid:
        t = pack.titles.get(tid)
        c.check("availability", env.available(t, at), "title %s unavailable at send time" % tid)
        c.check("dialect", t is not None and t.dialect == facts["dialect"], "title not in the dialect the viewer sees")
        c.check("injection", not (t and scan_text(t.name)), "title text contains instructions")
    if ch == "push" or ch == "in_app_message":
        title, body = msg.get("title") or "", msg.get("body") or ""
        forb = [p for p in pack.interventions["push"]["copy"]["forbidden_phrases"] if p in (title + " " + body).lower()]
        c.check("copy_policy", len(title) <= pack.interventions["push"]["copy"]["title_max_chars"] and
                len(body) <= pack.interventions["push"]["copy"]["body_max_chars"] and not forb,
                "length or forbidden phrase (%s)" % ", ".join(forb))
        prices = [int(x) for x in re.findall(r"₹\s*(\d+)", title + " " + body)]
        c.check("copy_policy", all(p in _allowed_prices(pack) for p in prices), "price not in pricing.json")
        c.check("injection", not scan_text(title + " " + body), "copy contains instruction-like text")
        if msg.get("deeplink"):
            c.check("deeplink", bool(DEEPLINK_OUT.match(msg["deeplink"])) and msg["deeplink"].endswith(tid or "?"), "bad deeplink")
    if ch == "whatsapp":
        tmpl = {t["template_id"]: t for t in pack.interventions["whatsapp"]["approved_templates"]}.get(msg.get("template_id"))
        c.check("template", tmpl is not None, "template not approved")
        if tmpl is not None:
            vars_ = msg.get("variables") or {}
            c.check("template", set(vars_) == set(tmpl["variables"]), "variables differ from the template")
            ok = True
            if "plan_price" in vars_:
                ok = ok and any(vars_["plan_price"] == "₹%d" % p for p in _allowed_prices(pack))
            if "title_name" in vars_:
                ok = ok and tid is not None and vars_["title_name"] == pack.titles[tid].name
            if "dialect_name" in vars_:
                ok = ok and vars_["dialect_name"] in pack.by_dialect
            c.check("template", ok, "variable not sourced from catalogue/pricing")
            c.check("template", tmpl["language"] == facts["language"], "template language %s != viewer language" % tmpl["language"])
    return c
