"""Command line interface.

  python -m oua all      --pack pack --out sample_run          # replay + simulator + judging + impact (one command)
  python -m oua replay   --pack pack --out sample_run
  python -m oua simulate --pack pack --out sample_run --seed S
  python -m oua measure  --pack pack --out sample_run
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from .config import Config


def _cfg(a):
    ov = {}
    for kv in a.set or []:
        k, _, v = kv.partition("=")
        ov[k] = v
    if getattr(a, "seed", None):
        ov["seed"] = a.seed
    return Config.load(a.config, ov)


def cmd_replay(a):
    from .loader import load_pack
    from .harness import Harness
    from .latency import build_report, write_report
    cfg = _cfg(a)
    pack = load_pack(a.pack)
    h = Harness(pack, cfg, a.out, resume=a.resume)
    summary = h.run()
    rep = build_report(h.decisions, pack, cfg, h.stats)
    rep["dependency_windows"] = [{"dependency": w["dependency"], "mode": w["mode"], "from": w["start"].isoformat(),
                                  "until": w["end"].isoformat()} for w in h.deps.windows]
    write_report(os.path.join(a.out, "latency_report.json"), rep)
    with open(os.path.join(a.out, "run_config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg.as_dict(), f, indent=2)
    print("replay: %d decisions, %d actions, %d cancelled messages -> %s" % (summary["decisions"], summary["actions"], summary["cancelled"], a.out), file=sys.stderr)
    print("latency p95=%.3fs max=%.3fs over_60s=%d" % (rep["latency_s"]["p95"] or 0, rep["latency_s"]["max"] or 0, rep["over_60s"]), file=sys.stderr)
    return h


def cmd_simulate(a):
    cfg = _cfg(a)
    sim = os.path.join(a.pack, "simulator", "simulate_outcomes.py")
    out = os.path.join(a.out, "sim")
    r = subprocess.run([sys.executable, sim, "--pack", a.pack, "--actions", os.path.join(a.out, "actions.jsonl"),
                        "--out", out, "--seed", cfg.seed], capture_output=True, text=True)
    sys.stderr.write(r.stderr)
    if r.returncode != 0:
        raise SystemExit("simulator failed: %s" % r.stderr[-400:])


def cmd_measure(a):
    from .loader import load_pack
    from .judge import judge_all
    from .impact import write_impact
    cfg = _cfg(a)
    pack = load_pack(a.pack)
    res = judge_all(pack, cfg, a.out)
    write_impact(pack, cfg, a.out, res)
    print("measure: judged %d goals -> outcomes.jsonl, impact_report.md" % res["n_judged"], file=sys.stderr)


def cmd_all(a):
    cmd_replay(a)
    cmd_simulate(a)
    cmd_measure(a)
    from .report import write_final_report
    write_final_report(a.pack, a.out)
    print("done. See %s/final_report.md and %s/impact_report.md" % (a.out, a.out), file=sys.stderr)


def main(argv=None):
    p = argparse.ArgumentParser(prog="oua", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("replay", cmd_replay), ("simulate", cmd_simulate), ("measure", cmd_measure), ("all", cmd_all)):
        s = sub.add_parser(name)
        s.add_argument("--pack", default="pack", help="root of the data pack (data/, config/, replay/, pilot/, simulator/)")
        s.add_argument("--out", default="sample_run", help="output directory")
        s.add_argument("--seed", default=None, help="simulator seed (default from config)")
        s.add_argument("--config", default=None, help="JSON file with config overrides")
        s.add_argument("--set", action="append", help="override a config value, e.g. --set max_model_calls=4")
        s.add_argument("--resume", action="store_true", help="continue from existing viewer_state (carries learnings forward)")
        s.set_defaults(fn=fn)
    a = p.parse_args(argv)
    a.fn(a)
    return 0
