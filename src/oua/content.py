"""Content selection: candidate pools, home-page builder, message planner and honest copy templates."""
from __future__ import annotations

import re
from collections import Counter
from datetime import timedelta

from .goals import unfinished_series, recent_releases
from .timeutil import next_send_slot, ist_hour, ist_day, in_quiet_hours

ROW_TYPES = ("next_episode", "because_you_watched", "popular_in_dialect", "new_releases", "genre")
MESSAGE_GOALS = ("G04", "G07", "G01")
CHANNEL_ORDER = {"G04": ["whatsapp", "push", "in_app_message"],
                 "G07": ["push", "whatsapp", "in_app_message"],
                 "G01": ["push", "whatsapp", "in_app_message"]}


def pool_for(pack, env, dialect, lang, at):
    """slug -> one available Title in `dialect` (language variant matching the viewer if there is one)."""
    pool = {}
    for t in pack.by_dialect.get(dialect, []):
        if not env.available(t, at):
            continue
        cur = pool.get(t.slug)
        if cur is None or (t.language == lang and cur.language != lang):
            pool[t.slug] = t
    return pool


def first_minutes(pack, slug):
    eps = pack.episodes.get(slug)
    return eps[0][3] if eps else 999


def build_page(pack, env, facts, goal, at, cfg):
    """Return (page, notes) or (None, reason). The verifier re-checks everything independently."""
    dialect, lang, now = facts["dialect"], facts["language"], facts.now
    pool = pool_for(pack, env, dialect, lang, at)
    if not pool:
        return None, "no available title in dialect %s" % dialect
    pop = pack.popularity
    watched = facts["played_slugs"]
    gid = goal["goal_id"]
    aff = facts["genre_affinity"]
    seen, notes = set(), []

    def rank(slugs, short_first=False):
        if short_first:
            return sorted(slugs, key=lambda s: (first_minutes(pack, s) > 10, -pop.get(s, 0), s))
        return sorted(slugs, key=lambda s: (-pop.get(s, 0), s))

    unfinished_all = []
    for slug, s in facts["series"].items():
        eps = pack.episodes.get(slug) or []
        if s["type"] in ("show", "micro_drama") and s["completed_pos"] + 1 < len(eps) and slug in pool:
            unfinished_all.append((s["last_play"], slug))
    unfinished_all.sort(reverse=True)
    target = goal.get("target", {}).get("slug")

    # ---- hero ----
    hero = None
    dl = facts.get("deeplink_title")
    if gid == "G07" and target in pool:
        hero = pool[target]
    elif gid == "G08":
        rel = [t for t in recent_releases(pack, dialect, now, cfg.new_release_days) if t.slug in pool]
        rel.sort(key=lambda t: (t.release_date, -pop.get(t.slug, 0)), reverse=True)
        hero = pool.get(rel[0].slug) if rel else None
    if hero is None and dl:
        t = pack.titles.get(dl)
        if t and t.dialect == dialect and t.slug in pool:
            hero = pool[t.slug]
            notes.append("hero from campaign deeplink title %s (viewer intent evidence)" % dl)
    if hero is None:
        cand = [s for s in pool if s not in watched]
        if gid in ("G01", "G02"):
            cand = rank(cand, short_first=True)
        elif gid in ("G04", "G05", "G06") and aff:
            pre = [s for s in cand if pool[s].premium and aff[0] in pool[s].genres]
            cand = rank(pre) + [s for s in rank(cand) if s not in pre]
        else:
            cand = rank(cand)
        hero = pool[cand[0]] if cand else None
    if hero is not None:
        seen.add(hero.slug)

    def take(slugs):
        out = []
        for s in slugs:
            if s in seen or s not in pool:
                continue
            out.append(pool[s].title_id)
            if len(out) >= cfg.max_titles_per_row:
                break
        return out

    short = gid in ("G01", "G02")
    unwatched = [s for s in pool if s not in watched]
    releases = [t.slug for t in sorted(recent_releases(pack, dialect, now, cfg.new_release_days),
                                       key=lambda t: t.release_date, reverse=True)]
    builders = {
        "next_episode": lambda: ("next_episode", take([s for _, s in unfinished_all])),
        "because_you_watched": lambda: ("because_you_watched", take(rank([s for s in unwatched if aff and set(pool[s].genres) & set(aff[:2])]))),
        "popular_in_dialect": lambda: ("popular_in_dialect", take(rank(unwatched, short_first=short))),
        "new_releases": lambda: ("new_releases", take([s for s in releases if s in unwatched or gid == "G08"])),
        "genre": None,
    }
    order = ["next_episode", "because_you_watched", "popular_in_dialect", "new_releases", "genre", "genre"]
    if gid == "G07":
        order = ["next_episode", "because_you_watched", "popular_in_dialect", "new_releases", "genre", "genre"]
    elif gid == "G08":
        order = ["new_releases", "because_you_watched", "popular_in_dialect", "next_episode", "genre", "genre"]
    elif gid in ("G01", "G02"):
        order = ["popular_in_dialect", "because_you_watched", "new_releases", "genre", "genre", "next_episode"]
    elif gid in ("G04", "G05", "G06"):
        order = ["because_you_watched", "next_episode", "popular_in_dialect", "new_releases", "genre", "genre"]
    genre_pool = list(aff[:3]) or [g for g, _ in Counter(g for s in pool for g in pool[s].genres).most_common(3)]
    genre_iter = iter(genre_pool[1:] if aff else genre_pool)
    if hero is not None:
        # reserve hero slug so no row repeats it
        seen.add(hero.slug)
    rows = []
    for rt in order:
        if len(rows) >= cfg.max_rows:
            break
        if rt == "genre":
            g = next(genre_iter, None)
            if g is None:
                continue
            ids = take(rank([s for s in unwatched if g in pool[s].genres], short_first=short))
            row = ("genre", ids)
        else:
            row = builders[rt]()
        if len(row[1]) >= cfg.min_titles_per_row:
            rows.append({"row_type": row[0], "title_ids": row[1]})
            for tid in row[1]:
                seen.add(pack.titles[tid].slug)
    if not rows:
        return None, "fewer than %d valid titles for every row type" % cfg.min_titles_per_row
    return {"type": "home_page", "dialect": dialect, "hero_title_id": hero.title_id if hero else None, "rows": rows}, notes


# ----------------------------------------------------------------------------------------------------------------
# messages
# ----------------------------------------------------------------------------------------------------------------
def preferred_hour(events, default):
    hrs = Counter(ist_hour(e["_eff"]) for e in events if e["event"] == "app_open" and 9 <= ist_hour(e["_eff"]) <= 21)
    return hrs.most_common(1)[0][0] if hrs else default


def trial_price(pack):
    plans = {p["plan_id"]: p for p in pack.pricing["plans"]}
    trial = plans.get("P_TRIAL")
    nxt = plans.get(trial["renews_to"]) if trial else None
    return nxt


def push_copy(kind, lang, name, episode=None, minutes=None, price=None, days=None):
    hin = lang == "hin"
    if kind == "resume":
        title = name if len(name) <= 40 else ("Aapka agla episode" if hin else "Your next episode")
        body = ("Episode %s aapka intezaar kar raha hai. Abhi dekhiye." % episode) if hin else ("Episode %s is ready when you are. Watch now." % episode)
    elif kind == "first_play":
        title = ("Aapke liye ek naya title" if hin else "A title picked for you")
        body = ("%s ka pehla episode %s minute ka hai. Abhi dekhiye." % (name, minutes)) if hin else ("%s: episode 1 is %s min long. Watch now." % (name, minutes))
    else:  # trial
        title = "Aapka trial jald khatam hoga" if hin else "Your trial ends soon"
        body = ("Trial ke baad %s mein dekhte rahiye (%s din)." % (price, days)) if hin else ("Keep watching after your trial for %s (%s days)." % (price, days))
    return title[:40], body[:120]


def plan_message(pack, env, facts, goal, at, cfg, events, ledger_days, page):
    """Return (message, reason). A message is proportionate only where the goal has a concrete honest hook."""
    gid = goal["goal_id"]
    if gid not in MESSAGE_GOALS:
        return None, "goal %s is served by the home page alone" % gid
    if goal.get("messages_planned", 0) >= cfg.max_messages_per_goal:
        return None, "one nudge per goal already used"
    lang, dialect = facts["language"], facts["dialect"]
    plans = trial_price(pack)
    hook = {}
    if gid == "G07":
        t = pack.titles.get(goal["target"]["title_id"])
        if not t or not env.available(t, at):
            return None, "target series not available"
        tt = next((x for x in pack.by_slug[t.slug] if x.dialect == dialect and env.available(x, at) and x.language == lang), None) \
            or next((x for x in pack.by_slug[t.slug] if x.dialect == dialect and env.available(x, at)), None)
        if tt is None:
            return None, "no available variant in the viewer's dialect"
        hook = {"kind": "resume", "title": tt, "episode": goal["target"]["next_episode"]}
    elif gid == "G01":
        hid = page and page.get("hero_title_id")
        tt = pack.titles.get(hid) if hid else None
        if tt is None:
            return None, "no hero title to recommend"
        hook = {"kind": "first_play", "title": tt, "minutes": first_minutes(pack, tt.slug)}
    else:
        prof = facts["_profile"]
        ends = None
        if prof and prof.get("plan_ends_at"):
            from .timeutil import parse
            ends = parse(prof["plan_ends_at"])
        if plans is None or ends is None or not (timedelta(0) < ends - facts.now <= timedelta(hours=72)):
            return None, "trial end date unknown or not within 72 h: cannot say the trial is ending"
        hook = {"kind": "trial", "price": "₹%d" % plans["price_inr"], "days": plans["days"], "price_inr": plans["price_inr"]}

    ph = preferred_hour(events, cfg.default_send_hour_ist)
    earliest = at + timedelta(hours=cfg.message_min_delay_h)
    for ch in CHANNEL_ORDER[gid]:
        if ch == "push" and not facts["consent_push"]:
            continue
        if ch == "whatsapp" and not facts["consent_whatsapp"]:
            continue
        if ch in facts["unreachable_channels"]:
            continue
        if ch == "whatsapp" and lang != "hin":
            continue                       # approved templates exist only in Hindi
        if ch == "in_app_message":
            send_at = at
        else:
            send_at = next_send_slot(earliest, ph)
            for _ in range(4):
                if ledger_days.get(ist_day(send_at), 0) == 0:
                    break
                send_at = next_send_slot(send_at + timedelta(days=1), ph)
            else:
                continue
        msg = {"type": ch, "send_at": send_at, "goal_id": gid}
        if hook["kind"] == "trial":
            if ch == "whatsapp":
                msg.update(template_id="wa_trial_ending_v1", variables={"plan_price": hook["price"]}, title_id=None)
            else:
                t_, b_ = push_copy("trial", lang, "", price=hook["price"], days=hook["days"])
                msg.update(title=t_, body=b_, title_id=None, deeplink=None)
        else:
            tt = hook["title"]
            msg["title_id"] = tt.title_id
            if ch == "whatsapp":
                if hook["kind"] == "resume":
                    msg.update(template_id="wa_recommend_title_v1", variables={"title_name": tt.name, "dialect_name": dialect})
                else:
                    msg.update(template_id="wa_recommend_title_v1", variables={"title_name": tt.name, "dialect_name": dialect})
            else:
                t_, b_ = push_copy(hook["kind"], lang, tt.name, episode=hook.get("episode"), minutes=hook.get("minutes"))
                msg.update(title=t_, body=b_, deeplink="ott://title/%s" % tt.title_id)
        return msg, None
    return None, "no consented, reachable channel with a free IST day"
