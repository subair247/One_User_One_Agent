"""Runtime configuration. Every budget is configurable (CLI: --set key=value, or a JSON file via --config)."""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, fields


@dataclass
class Config:
    # --- budgets required by the brief ---
    max_model_calls: int = 8
    max_tool_calls: int = 20
    latency_target_s: float = 60.0      # publish within 60 s of the trigger
    latency_fail_s: float = 300.0       # > 5 minutes counts as a failure

    # --- trigger handling ---
    stale_trigger_s: float = 120.0      # offline-buffered opens older than this are not live sessions
    page_ttl_min: int = 120

    # --- model provider (mock by default) ---
    model_mode: str = "mock"            # only "mock" ships; the provider interface allows a real one
    model_timeout_s: float = 5.0
    model_retry_on_malformed: int = 1
    model_latency_s: float = 0.35       # virtual latency charged per mock model call
    breaker_threshold: int = 3          # consecutive model failures before the circuit opens
    breaker_open_s: float = 60.0

    # --- data store ---
    store_read_timeout_s: float = 1.5
    store_normal_latency_s: float = 0.01
    tool_latency_s: float = 0.005       # virtual latency per tool call (catalogue lookup etc.)

    # --- throughput / load shedding ---
    workers: int = 8
    shed_model_when_wait_s: float = 2.0  # skip the optional model call when the queue backs up

    # --- agent policy knobs ---
    new_release_days: int = 30
    engaged_days_14d: int = 5           # users playing on >= this many IST days in 14 d need no nudge
    message_min_delay_h: float = 3.0    # a message is never sent within the same sitting
    default_send_hour_ist: int = 19
    max_messages_per_goal: int = 1
    max_rows: int = 6
    min_titles_per_row: int = 3
    max_titles_per_row: int = 10
    max_catalog_age_check: int = 0      # reserved (kept 0)

    # --- measurement ---
    observation_days: int = 14          # days of simulated behaviour after the replay day
    seed: str = "oua-practice"

    @classmethod
    def load(cls, path=None, overrides=None):
        cfg = cls()
        names = {f.name: f for f in fields(cls)}
        data = {}
        if path:
            with open(path, encoding="utf-8") as f:
                data.update(json.load(f))
        data.update(overrides or {})
        for k, v in data.items():
            if k not in names:
                raise KeyError("unknown config key: %s" % k)
            cur = getattr(cfg, k)
            if isinstance(cur, bool):
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes")
            elif isinstance(cur, int):
                v = int(v)
            elif isinstance(cur, float):
                v = float(v)
            setattr(cfg, k, v)
        return cfg

    def as_dict(self):
        return asdict(self)
