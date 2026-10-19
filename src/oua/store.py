"""Durable per-viewer state (one JSON file per viewer) with a read path that can time out and fall back to a cache."""
from __future__ import annotations

import json
import os
from hashlib import sha256


def h01(*parts):
    return int(sha256(":".join(map(str, parts)).encode()).hexdigest()[:12], 16) / 16 ** 12


def new_state(user_id, arm):
    return {
        "viewer_id": user_id, "arm": arm, "goal": None, "goals": [], "decisions": [], "actions": [],
        "pending": [], "seen_triggers": [], "last_session_id": None, "last_trigger": None, "live_page": None,
        "messages": [], "learnings": {"unreachable_channels": [], "notes": []}, "outcomes": [],
        "facts": {}, "conflicts": [],
    }


class ViewerStore:
    def __init__(self, out_dir, resume=False):
        self.dir = os.path.join(out_dir, "viewer_state")
        self.states = {}
        self.touched = set()
        os.makedirs(self.dir, exist_ok=True)
        if resume:
            for fn in os.listdir(self.dir):
                if fn.endswith(".json"):
                    with open(os.path.join(self.dir, fn), encoding="utf-8") as f:
                        s = json.load(f)
                    self.states[s["viewer_id"]] = s
        else:
            for fn in os.listdir(self.dir):
                if fn.endswith(".json"):
                    os.remove(os.path.join(self.dir, fn))

    def get(self, user_id, arm):
        s = self.states.get(user_id)
        if s is None:
            s = self.states[user_id] = new_state(user_id, arm)
        s["arm"] = arm
        return s

    def read(self, user_id, arm, at, deps, cfg, key):
        """Returns (state, meta). During a degraded window ~20% of reads time out and the rest are slow; a read slower
        than the timeout is abandoned and the in-memory cached copy is used (meta['source'] == 'cache')."""
        meta = {"source": "store", "latency_s": cfg.store_normal_latency_s}
        if deps.store_degraded(at):
            r1, r2 = h01("read-timeout", user_id, key), h01("read-latency", user_id, key)
            lat = 0.05 + 8.0 * (r2 ** 2.5)
            if r1 < 0.20 or lat > cfg.store_read_timeout_s:
                meta = {"source": "cache", "latency_s": cfg.store_read_timeout_s, "reason": "store read timed out"}
            else:
                meta = {"source": "store", "latency_s": lat}
        return self.get(user_id, arm), meta

    def touch(self, user_id):
        self.touched.add(user_id)

    def flush(self):
        for u in sorted(self.touched):
            with open(os.path.join(self.dir, "%s.json" % u), "w", encoding="utf-8") as f:
                json.dump(self.states[u], f, indent=1, ensure_ascii=False)
