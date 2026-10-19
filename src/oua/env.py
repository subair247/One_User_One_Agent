"""Runtime environment shared by planner, verifier and harness: catalogue availability incl. mid-day withdrawals."""
from __future__ import annotations


class Env:
    def __init__(self, pack):
        self.pack = pack
        self.withdrawn = {}          # title_id -> datetime of withdrawal (from control events)

    def withdraw(self, title_id, at):
        self.withdrawn[title_id] = at

    def available(self, title, at):
        if title is None:
            return False
        if title.available_from is None or at < title.available_from:
            return False
        if title.available_until is not None and at >= title.available_until:
            return False
        w = self.withdrawn.get(title.title_id)
        if w is not None and at >= w:
            return False
        return True


class Dependencies:
    """Dependency health windows taken from replay/control_events.jsonl (nothing is hard-coded to this replay)."""

    def __init__(self, control_events):
        from .timeutil import parse
        self.windows = []
        for c in control_events:
            if c["type"] in ("dependency_unavailable", "dependency_degraded"):
                self.windows.append({"dependency": c["dependency"], "mode": c["type"], "start": parse(c["at"]),
                                     "end": parse(c["until"]), "detail": c.get("detail")})

    def active(self, dependency, at):
        for w in self.windows:
            if w["dependency"] == dependency and w["start"] <= at < w["end"]:
                return w
        return None

    def model_down(self, at):
        return self.active("model_provider", at) is not None

    def store_degraded(self, at):
        return self.active("user_data_store", at) is not None
