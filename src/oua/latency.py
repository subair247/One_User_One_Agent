"""Latency report: trigger -> decision time, budgets and burst behaviour."""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import timedelta

from .timeutil import parse


def pct(vals, p):
    if not vals:
        return None
    vals = sorted(vals)
    k = min(len(vals) - 1, int(round((p / 100.0) * (len(vals) - 1))))
    return vals[k]


def busiest_window(triggers, minutes=10):
    times = sorted(t["_deliver"] for t in triggers)
    best, bi, j = 0, None, 0
    for i, t in enumerate(times):
        while times[j] < t - timedelta(minutes=minutes):
            j += 1
        if i - j + 1 > best:
            best, bi = i - j + 1, t
    return best, bi


def build_report(decisions, pack, cfg, stats):
    trig = [d for d in decisions if d["decision_type"] == "TRIGGER"]
    lat = [d["decided_in_ms"] / 1000.0 for d in trig]
    comp = [d["compute_ms"] for d in trig]
    n_win, end = busiest_window(pack.triggers)
    per_hour = Counter(t["_deliver"].strftime("%Y-%m-%dT%H") for t in pack.triggers)
    baseline = sorted(per_hour.values())[len(per_hour) // 2] if per_hour else 0
    window_decisions = [d for d in trig if end and end - timedelta(minutes=10) <= parse(d["trigger"]["deliver_at"]) <= end]
    wl = [d["decided_in_ms"] / 1000.0 for d in window_decisions]
    fb = Counter(f for d in decisions for f in d.get("fallback_used", []))
    rep = {
        "budget": {"target_s": cfg.latency_target_s, "fail_s": cfg.latency_fail_s, "max_model_calls": cfg.max_model_calls,
                   "max_tool_calls": cfg.max_tool_calls, "workers": cfg.workers},
        "method": "published_at = deliver_at + virtual queue wait + virtual dependency latency (model/store/tool cost model) + measured compute time",
        "decisions_timed": len(trig),
        "latency_s": {"p50": pct(lat, 50), "p95": pct(lat, 95), "p99": pct(lat, 99), "max": max(lat) if lat else None},
        "measured_compute_ms": {"p50": pct(comp, 50), "p95": pct(comp, 95), "max": max(comp) if comp else None},
        "over_60s": sum(1 for x in lat if x > cfg.latency_target_s),
        "over_300s_failures": sum(1 for x in lat if x > cfg.latency_fail_s),
        "max_model_calls_used": max((d["model_calls"] for d in trig), default=0),
        "max_tool_calls_used": max((d["tool_calls"] for d in trig), default=0),
        "busiest_10_minutes": {"triggers": n_win, "ending_at": end.isoformat() if end else None, "median_hourly_triggers": baseline,
                               "decisions_in_window": len(wl), "p95_latency_s": pct(wl, 95), "max_latency_s": max(wl) if wl else None},
        "fallbacks": dict(fb),
        "state_reads_from_cache": sum(1 for d in trig if d.get("state_source") == "cache"),
        "dependency_windows": [{"dependency": w["dependency"], "mode": w["mode"], "from": w["start"].isoformat(), "until": w["end"].isoformat()}
                               for w in []],
    }
    return rep


def write_report(path, rep):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=2, default=str)
