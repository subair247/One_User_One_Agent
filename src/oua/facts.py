"""Fact resolver: turns raw sources into facts with provenance and explicit precedence rules.

Precedence (per fact) -- the largest, newest or most convenient source never wins by default:

  subscription_status  latest subscription event (purchase/renewal/lapse, by received_at) beats the profile row only when
                       the event is newer than the row's updated_at; a profile "never" is falsified by any purchase event.
                       Otherwise the profile wins and the disagreement is kept visible.
  signup_at            profile.signup_at (server-side) if a row exists, else the earliest signup_completed event.
  dialect_used         the app setting on the trigger (it is what the app will request). Profile, last
                       app_dialect_changed and campaign deeplink are listed as conflicts, never used.
  consent              profile opt-in AND no notification_opt_out event for that channel at any time (conservative).
  duplicate profile    the row that agrees with the event-derived subscription, then the latest updated_at.
  timing of behaviour  received_at - offline buffer delay (sent_at - occurred_at on the same phone clock).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from .loader import DEEPLINK_RX
from .timeutil import iso, ist_day

SUB_EVENTS = ("purchase_completed", "subscription_renewed", "subscription_lapsed")


class Facts:
    def __init__(self, user_id, now):
        self.user_id = user_id
        self.now = now
        self.v = {}
        self.prov = {}
        self.conflicts = []
        self.assumptions = []
        self.evidence = []

    def put(self, name, value, source, as_of=None, rule=None):
        self.v[name] = value
        self.prov[name] = {
            "source": source,
            "as_of": iso(as_of) if as_of else None,
            "freshness_s": round((self.now - as_of).total_seconds(), 1) if as_of else None,
            "rule": rule,
        }
        if source and source not in self.evidence:
            self.evidence.append(source)

    def __getitem__(self, k):
        return self.v[k]

    def get(self, k, default=None):
        return self.v.get(k, default)

    def snapshot(self):
        def js(x):
            if hasattr(x, "isoformat"):
                return x.isoformat()
            if isinstance(x, (set, tuple)):
                return sorted(str(i) for i in x)
            return x
        out = {k: {"value": js(self.v[k]), **p} for k, p in self.prov.items()}
        out["dialect_conflicts"] = {"value": list(self.v.get("dialect_conflicts", []))}
        out["unreachable_channels"] = {"value": list(self.v.get("unreachable_channels", []))}
        return out


def event_derived_subscription(events, now):
    status, ev = None, None
    for e in events:
        if e["_recv"] > now:
            break
        if e["event"] == "purchase_completed":
            status, ev = ("trial_active" if e["properties"].get("is_trial") else "active"), e
        elif e["event"] == "subscription_renewed":
            status, ev = "active", e
        elif e["event"] == "subscription_lapsed":
            status, ev = "lapsed", e
    return status, ev


def pick_profile(rows, ev_status):
    """Choose one row when a user appears twice. Returns (row, note)."""
    if not rows:
        return None, None
    if len(rows) == 1:
        return rows[0], None
    cand = list(rows)
    if ev_status:
        agree = [r for r in cand if r["subscription_status"] == ev_status]
        if agree and len(agree) < len(cand):
            cand = agree
    best = max(cand, key=lambda r: r["_updated"])
    other = [r for r in rows if r is not best][0]
    note = "profile_duplicate_rows: kept updated_at=%s (status=%s, dialect=%s), dropped updated_at=%s (status=%s, dialect=%s)" % (
        iso(best["_updated"]), best["subscription_status"], best["preferred_dialect"],
        iso(other["_updated"]), other["subscription_status"], other["preferred_dialect"])
    return best, note


def resolve_facts(pack, user_id, events, trigger, now, learnings=None):
    """Build the facts for `user_id` as known at `now` (server time). `events` must be sorted by received_at."""
    F = Facts(user_id, now)
    learnings = learnings or {}
    known = [e for e in events if e["_recv"] <= now and (trigger is None or e["event_id"] != trigger["event_id"])]

    ev_status, ev_sub = event_derived_subscription(known, now)
    prof, note = pick_profile(pack.profiles.get(user_id), ev_status)
    if note:
        F.conflicts.append(note)
    F.v["_profile"] = prof

    # ---- subscription ----
    if prof is None and ev_status is None:
        F.put("subscription_status", "unknown", "none", None, "no profile row and no subscription event")
    elif prof is None:
        F.put("subscription_status", ev_status, "event:%s" % ev_sub["event_id"], ev_sub["_recv"], "no profile row; event-derived")
    else:
        ps, pa = prof["subscription_status"], prof["_updated"]
        src = "profile:%s@%s" % (user_id, iso(prof["_snapshot"]))
        if ev_status is None or ev_status == ps:
            F.put("subscription_status", ps, src, pa, "profile (no contradicting event)")
        elif ev_sub["_recv"] > pa:
            F.put("subscription_status", ev_status, "event:%s" % ev_sub["event_id"], ev_sub["_recv"],
                  "newer subscription event beats stale profile")
            F.conflicts.append("subscription: profile:%s vs event:%s -> event wins (newer)" % (ps, ev_status))
        elif ps == "never":
            F.put("subscription_status", ev_status, "event:%s" % ev_sub["event_id"], ev_sub["_recv"],
                  "a purchase event falsifies profile 'never'")
            F.conflicts.append("subscription: profile:never vs event:%s -> event wins (never paid is falsifiable)" % ev_status)
        else:
            F.put("subscription_status", ps, src, pa, "profile row newer than the last subscription event")
            F.conflicts.append("subscription: event:%s older than profile:%s -> profile wins" % (ev_status, ps))

    # ---- signup ----
    sign_ev = next((e for e in known if e["event"] == "signup_completed"), None)
    if prof is not None and prof.get("signup_at"):
        from .timeutil import parse
        sa = parse(prof["signup_at"])
        F.put("signup_at", sa, "profile:%s" % user_id, prof["_updated"], "server-side profile value")
        if sign_ev is not None and abs((sign_ev["_eff"] - sa).total_seconds()) > 86400:
            F.conflicts.append("signup_at: profile:%s vs event:%s differ by > 1 day -> profile wins" % (iso(sa), sign_ev["event_id"]))
    elif sign_ev is not None:
        F.put("signup_at", sign_ev["_eff"], "event:%s" % sign_ev["event_id"], sign_ev["_recv"], "no profile row; built from signup_completed")
    else:
        F.put("signup_at", None, "none", None, "no profile row and no signup event")

    # ---- dialect ----
    valid = set(pack.by_dialect)
    if trigger:
        app_dialect = trigger.get("app_dialect")
    else:   # no live trigger (e.g. at message send time): the latest valid app setting we have seen
        app_dialect = next((e["app_dialect"] for e in reversed(known) if e.get("app_dialect") in valid), None)
    conflicts = []
    if prof is not None and prof.get("preferred_dialect") and prof["preferred_dialect"] != app_dialect:
        conflicts.append("profile:%s" % prof["preferred_dialect"])
    changes = [e for e in known if e["event"] == "app_dialect_changed"]
    if changes:
        last_to = changes[-1]["properties"].get("to")
        if last_to and last_to != app_dialect:
            conflicts.append("app_dialect_changed:%s" % last_to)
    dl = (trigger or {}).get("properties", {}).get("deeplink")
    dl_dialect = None
    dl_title = None
    if dl:
        m = DEEPLINK_RX.match(dl)
        if m:
            dl_dialect, dl_title = m.group(2), m.group(3)
            if dl_dialect != app_dialect:
                conflicts.append("campaign_deeplink:%s" % dl_dialect)
    if app_dialect in valid and trigger:
        F.put("dialect", app_dialect, "app_setting:%s" % trigger["event_id"], trigger["_recv"],
              "app setting decides what the app requests")
    elif app_dialect in valid:
        F.put("dialect", app_dialect, "app_setting:last_known", now, "latest app setting seen (no live trigger)")
    else:
        F.put("dialect", None, "app_setting:%s" % (trigger["event_id"] if trigger else "none"), trigger["_recv"] if trigger else None,
              "app_dialect %r is missing or not in the catalogue; page cannot be verified" % (app_dialect,))
    F.v["dialect_conflicts"] = conflicts
    if conflicts:
        F.conflicts.append("dialect: used %s (app_setting) vs %s" % (app_dialect, ", ".join(conflicts)))
    F.v["deeplink_title"] = dl_title if dl_dialect == app_dialect else None

    # ---- language ----
    lang = (trigger or {}).get("app_language") or (prof or {}).get("preferred_language") or "hin"
    F.put("language", lang, ("app_setting:%s" % trigger["event_id"]) if (trigger or {}).get("app_language") else "profile:%s" % user_id, now, "app language, else profile")

    # ---- consent ----
    opt_out = set()
    for e in known:
        if e["event"] == "notification_opt_out":
            ch = e["properties"].get("channel")
            opt_out.update([ch] if ch else ["push", "whatsapp"])
    push_in = bool(prof) and prof["notifications_opt_in"] == "true"
    wa_in = bool(prof) and prof["whatsapp_opt_in"] == "true"
    unreachable = set(learnings.get("unreachable_channels", []))
    F.put("consent_push", push_in and "push" not in opt_out, "profile:%s" % user_id if prof else "none",
          prof["_updated"] if prof else None, "opt-in in profile and no opt-out event (no profile row = no consent)")
    F.put("consent_whatsapp", wa_in and "whatsapp" not in opt_out, "profile:%s" % user_id if prof else "none",
          prof["_updated"] if prof else None, "opt-in in profile and no opt-out event (no profile row = no consent)")
    if opt_out:
        F.conflicts.append("consent: notification_opt_out events for %s override profile" % sorted(opt_out))
    F.v["unreachable_channels"] = sorted(unreachable)

    # ---- behaviour history ----
    plays, completes, opens, sessions = [], [], [], set()
    series = {}
    sent_today = defaultdict(int)
    trig_session = trigger.get("session_id") if trigger else None
    for e in known:
        ev = e["event"]
        if ev == "play_started":
            plays.append(e)
        elif ev == "episode_completed":
            completes.append(e)
        elif ev == "app_open" and e.get("session_id") != trig_session:
            opens.append(e)
        elif ev == "message_delivered":
            sent_today[ist_day(e["_eff"])] += 1
    F.v["sent_per_ist_day"] = dict(sent_today)
    F.v["n_known_events"] = len(known)
    F.v["n_plays"] = len(plays)
    F.v["n_completes"] = len(completes)
    F.v["last_play_at"] = max((e["_eff"] for e in plays), default=None)
    F.v["last_open_at"] = max((e["_eff"] for e in opens), default=None)
    window14 = now - timedelta(days=14)
    F.v["played_days_14d"] = sorted({ist_day(e["_eff"]) for e in plays if e["_eff"] >= window14})
    F.v["opens_14d"] = sum(1 for e in opens if e["_eff"] >= window14)
    F.v["played_slugs"] = set()
    genres = defaultdict(int)
    for e in plays + completes:
        t = pack.titles.get(e["properties"].get("title_id"))
        if not t:
            continue
        F.v["played_slugs"].add(t.slug)
        if e["event"] == "play_started":
            for g in t.genres:
                genres[g] += 1
            s = series.setdefault(t.slug, {"slug": t.slug, "type": t.content_type, "last_play": e["_eff"],
                                           "completed_pos": -1, "title_id": t.title_id, "event": e["event_id"]})
            if e["_eff"] >= s["last_play"]:
                s.update(last_play=e["_eff"], title_id=t.title_id, event=e["event_id"])
    for e in completes:
        eid = e["properties"].get("episode_id")
        loc = pack.episode_index.get(eid)
        t = pack.titles.get(e["properties"].get("title_id"))
        if loc and t:
            s = series.setdefault(loc[0], {"slug": loc[0], "type": t.content_type, "last_play": e["_eff"],
                                           "completed_pos": -1, "title_id": t.title_id, "event": e["event_id"]})
            s["completed_pos"] = max(s["completed_pos"], loc[1])
    F.v["series"] = series
    F.v["genre_affinity"] = sorted(genres, key=lambda g: (-genres[g], g))
    signup = F.v["signup_at"]
    hist_start = pack.history_start
    F.v["history_complete"] = bool(signup and hist_start and signup >= hist_start)
    return F
