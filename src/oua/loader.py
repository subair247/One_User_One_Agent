"""Data loader: reads the pack, deduplicates, normalises timestamps and records every data issue it finds."""
from __future__ import annotations

import csv
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from .sanitize import scan_text
from .timeutil import parse, iso

DEEPLINK_RX = re.compile(r"^ott://c/(cmp\d+)/(d\d+)/title/(T\d+)$")


@dataclass
class Title:
    title_id: str
    slug: str
    name: str
    dialect: str
    language: str
    content_type: str
    genres: list
    release_date: object
    available_from: object
    available_until: object
    premium: bool
    flagged: list = field(default_factory=list)   # injection patterns found in the text


@dataclass
class Pack:
    root: str
    goals: dict
    interventions: dict
    pricing: dict
    titles: dict                 # title_id -> Title
    by_slug: dict                # slug -> [Title]
    by_dialect: dict             # dialect -> [Title]
    episodes: dict               # slug -> [(season, episode, episode_id, minutes)]
    episode_index: dict          # episode_id -> (slug, position)
    profiles: dict               # user_id -> [row]
    arms: dict
    events_by_user: dict         # user_id -> [event] sorted by received_at
    triggers: list
    live_events: list
    control_events: list
    faults: dict                 # trigger_event_id -> fault
    event_vocabulary: set
    history_start: object
    history_end: object
    popularity: dict             # slug -> number of plays in the history
    issues: dict                 # data-issue counters + examples
    security_log: list
    goals_by_id: dict = field(default_factory=dict)

    def dialects(self):
        return set(self.by_dialect)


def _read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _read_csv(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def normalise_event(e):
    """Adds _recv (trustworthy server time), _eff (best estimate of when it really happened) and _delay_s.

    received_at is server time and is trusted. occurred_at/sent_at are phone clocks and may be wrong, but their
    *difference* is measured on one clock, so it is a valid estimate of how long the event sat in the offline
    buffer. _eff = received_at - buffer delay.
    """
    recv = parse(e["received_at"])
    occ = parse(e["occurred_at"])
    sent = parse(e["sent_at"])
    delay = max(0.0, (sent - occ).total_seconds())
    e["_recv"] = recv
    e["_delay_s"] = delay
    e["_eff"] = recv - timedelta(seconds=delay)
    e["_clock_suspect"] = abs((recv - sent).total_seconds()) > 600
    return e


def event_age_s(e):
    return e["_delay_s"]


def load_pack(root):
    cfgdir = os.path.join(root, "config")
    datadir = os.path.join(root, "data")
    repdir = os.path.join(root, "replay")
    issues = {"counters": Counter(), "examples": defaultdict(list)}
    security = []

    def issue(name, example=None, n=1):
        issues["counters"][name] += n
        if example is not None and len(issues["examples"][name]) < 3:
            issues["examples"][name].append(example)

    with open(os.path.join(cfgdir, "goals.json"), encoding="utf-8") as f:
        goals = json.load(f)
    with open(os.path.join(cfgdir, "interventions.json"), encoding="utf-8") as f:
        interventions = json.load(f)
    with open(os.path.join(cfgdir, "pricing.json"), encoding="utf-8") as f:
        pricing = json.load(f)

    # --- catalogue ---
    titles, by_slug, by_dialect = {}, defaultdict(list), defaultdict(list)
    for r in _read_csv(os.path.join(datadir, "catalogue.csv")):
        flags = scan_text(r["description"]) + scan_text(r["title"])
        t = Title(
            title_id=r["title_id"], slug=r["content_slug"], name=r["title"], dialect=r["dialect"],
            language=r["language"], content_type=r["content_type"], genres=r["genres"].split("|") if r["genres"] else [],
            release_date=parse(r["release_date"] + "T00:00:00Z") if r["release_date"] else None,
            available_from=parse(r["available_from"]), available_until=parse(r["available_until"]),
            premium=r["is_premium"] == "true", flagged=flags,
        )
        titles[t.title_id] = t
        by_slug[t.slug].append(t)
        by_dialect[t.dialect].append(t)
        if flags:
            issue("catalogue_description_with_instructions", t.title_id)
            security.append({"source": "catalogue", "id": t.title_id, "patterns": flags,
                             "excerpt": r["description"][:160], "handling": "treated as content, never as a command; text never used in copy"})
    multi = sum(1 for v in by_slug.values() if len(v) > 1)
    issue("catalogue_slugs_with_language_variants", n=multi)
    issue("catalogue_titles_with_end_of_availability", n=sum(1 for t in titles.values() if t.available_until))

    # --- episodes ---
    episodes = defaultdict(list)
    for r in _read_csv(os.path.join(datadir, "episodes.csv")):
        episodes[r["content_slug"]].append((int(r["season"]), int(r["episode"]), r["episode_id"], int(r["minutes"])))
    episode_index = {}
    for slug, lst in episodes.items():
        lst.sort()
        for pos, (_, _, eid, _) in enumerate(lst):
            episode_index[eid] = (slug, pos)

    # --- profiles (kept raw; resolution happens in facts.py) ---
    profiles = defaultdict(list)
    for r in _read_csv(os.path.join(datadir, "profiles_snapshot.csv")):
        r["_updated"] = parse(r["updated_at"])
        r["_snapshot"] = parse(r["snapshot_at"])
        profiles[r["user_id"]].append(r)
    issue("profile_users_with_two_rows", n=sum(1 for v in profiles.values() if len(v) > 1),
          example=next((k for k, v in profiles.items() if len(v) > 1), None))

    arms = {r["user_id"]: r["arm"] for r in _read_csv(os.path.join(repdir, "arms.csv"))}

    # --- history events ---
    vocab = set()
    seen, events_by_user = {}, defaultdict(list)
    dup = 0
    lo = hi = None
    for e in _read_jsonl(os.path.join(datadir, "events.jsonl")):
        vocab.add(e["event"])
        if e["event_id"] in seen:
            dup += 1
            continue
        normalise_event(e)
        seen[e["event_id"]] = 1
        events_by_user[e["user_id"]].append(e)
        lo = e["_recv"] if lo is None or e["_recv"] < lo else lo
        hi = e["_recv"] if hi is None or e["_recv"] > hi else hi
        if e["_delay_s"] > 120:
            issue("history_offline_buffered_events")
        if e["_clock_suspect"]:
            issue("history_wrong_phone_clock_events")
    issue("history_duplicate_event_ids", n=dup)
    for u in events_by_user:
        events_by_user[u].sort(key=lambda x: (x["_recv"], x["event_id"]))
    del seen

    popularity = Counter()
    for evs in events_by_user.values():
        for e in evs:
            if e["event"] == "play_started":
                t = titles.get(e["properties"].get("title_id"))
                if t:
                    popularity[t.slug] += 1

    for evs in events_by_user.values():
        for e in evs:
            if e["event"] == "search":
                flags = scan_text(str(e["properties"].get("query", "")))
                if flags:
                    security.append({"source": "search_query", "id": e["event_id"], "patterns": flags,
                                     "excerpt": str(e["properties"].get("query"))[:160], "handling": "content only"})
            if e.get("app_dialect") not in (None, "NA") and e["app_dialect"] not in by_dialect:
                issue("events_with_dialect_absent_from_catalogue", example=e["app_dialect"])
            if e.get("app_dialect") in (None, "NA"):
                issue("events_with_missing_app_dialect")

    # --- replay ---
    def load_stream(name):
        out, ids, dups = [], set(), 0
        for e in _read_jsonl(os.path.join(repdir, name)):
            vocab.add(e["event"])
            if e["event_id"] in ids:
                dups += 1
                continue
            ids.add(e["event_id"])
            normalise_event(e)
            e["_deliver"] = parse(e["deliver_at"])
            out.append(e)
        out.sort(key=lambda x: (x["_deliver"], x["event_id"]))
        return out, dups

    triggers, tdup = load_stream("triggers.jsonl")
    live, ldup = load_stream("live_events.jsonl")
    issue("trigger_duplicate_event_ids", n=tdup)
    issue("live_duplicate_event_ids", n=ldup)
    for e in triggers:
        if e["_delay_s"] > 120:
            issue("trigger_offline_buffered_stale")
        if e.get("app_dialect") in (None, "NA") or e.get("app_dialect") not in by_dialect:
            issue("trigger_app_dialect_unusable", example=e.get("app_dialect"))
        dl = e["properties"].get("deeplink")
        if dl:
            m = DEEPLINK_RX.match(dl)
            if not m:
                issue("trigger_malformed_deeplink_ignored", example=dl[:80])
            elif m.group(2) != e.get("app_dialect"):
                issue("trigger_deeplink_dialect_differs_from_app_setting", example=dl)
        if e["user_id"] not in profiles:
            issue("trigger_user_without_profile_row")
    control = sorted(_read_jsonl(os.path.join(repdir, "control_events.jsonl")), key=lambda c: c["at"])
    faults = {f["trigger_event_id"]: f for f in _read_jsonl(os.path.join(repdir, "model_faults.jsonl"))}

    users_hist = set(events_by_user)
    issue("history_users_without_profile_row", n=len(users_hist - set(profiles)))

    return Pack(root=root, goals=goals, interventions=interventions, pricing=pricing, titles=titles,
                by_slug=dict(by_slug), by_dialect=dict(by_dialect), episodes=dict(episodes),
                episode_index=episode_index, profiles=dict(profiles), arms=arms,
                events_by_user=events_by_user, triggers=triggers, live_events=live, control_events=control,
                faults=faults, event_vocabulary=vocab, history_start=lo, history_end=hi, popularity=popularity,
                issues={"counters": dict(issues["counters"]), "examples": dict(issues["examples"])},
                security_log=security, goals_by_id={g["goal_id"]: g for g in goals["goals"]})
