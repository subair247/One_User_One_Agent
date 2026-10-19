"""Model provider abstraction. Mock/replay mode is the default (no API key, no data leaves the machine).

Output of any provider is a *claim*, never evidence: it goes through the verifier before anything is acted on.
"""
from __future__ import annotations

import json
from hashlib import sha256


class ModelError(Exception):
    def __init__(self, kind, latency_s, detail=""):
        super().__init__("%s: %s" % (kind, detail))
        self.kind, self.latency_s, self.detail = kind, latency_s, detail


class ModelResponse:
    def __init__(self, text, latency_s):
        self.text, self.latency_s = text, latency_s


class ModelProvider:
    """Interface. A real provider only needs `complete`; it must raise ModelError on failure."""

    def complete(self, request, trigger_id, attempt, now):   # pragma: no cover - interface
        raise NotImplementedError


class MockModel(ModelProvider):
    """Deterministic stand-in. It proposes the highest-priority eligible goal, and reproduces the faults listed in
    replay/model_faults.jsonl (on the first attempt only) and the model-provider outage from control_events."""

    def __init__(self, cfg, faults, deps):
        self.cfg, self.faults, self.deps = cfg, faults, deps
        self.calls = 0

    def complete(self, request, trigger_id, attempt, now):
        self.calls += 1
        if self.deps.model_down(now):
            raise ModelError("http_503", 0.15, "model provider unavailable")
        f = self.faults.get(trigger_id)
        if f and attempt == 0:
            if f["fault"] == "timeout":
                raise ModelError("timeout", float(f.get("seconds", 30)), "no response")
            out = f["model_output"]
            return ModelResponse(out if isinstance(out, str) else json.dumps(out), self.cfg.model_latency_s)
        elig = request["eligible_goal_ids"]
        if not elig:
            return ModelResponse(json.dumps({"goal_id": None, "eligible": False, "confidence": 0.9,
                                             "reason": "no eligible goal in the evidence"}), self.cfg.model_latency_s)
        gid = elig[0]
        conf = round(min(0.85, 0.55 + 0.05 * request.get("n_evidence", 0)), 2)
        return ModelResponse(json.dumps({"goal_id": gid, "eligible": True, "confidence": conf,
                                         "reason": "highest-priority goal whose eligibility is backed by %d evidence ref(s)" % request.get("n_evidence", 0)}),
                             self.cfg.model_latency_s)


def parse_model_output(text):
    """Parse safely. Raises ValueError with a reason on anything that is not the expected JSON object."""
    try:
        obj = json.loads(text)
    except Exception as exc:
        raise ValueError("malformed_json: %s" % exc)
    if not isinstance(obj, dict):
        raise ValueError("not_an_object")
    if "goal_id" not in obj or "eligible" not in obj:
        raise ValueError("missing_keys")
    if obj["goal_id"] is not None and not isinstance(obj["goal_id"], str):
        raise ValueError("bad_goal_id")
    conf = obj.get("confidence", 0.0)
    if not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
        raise ValueError("bad_confidence")
    return obj


class Breaker:
    """Circuit breaker: after N consecutive failures stop calling the model for a while (saves latency budget)."""

    def __init__(self, threshold, open_s):
        self.threshold, self.open_s = threshold, open_s
        self.fails, self.open_until = 0, None

    def allow(self, now):
        return self.open_until is None or now >= self.open_until

    def record(self, ok, now):
        from datetime import timedelta
        if ok:
            self.fails, self.open_until = 0, None
            return
        self.fails += 1
        if self.fails >= self.threshold:
            self.open_until = now + timedelta(seconds=self.open_s)
            self.fails = 0
