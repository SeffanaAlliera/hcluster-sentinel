import argparse
import json
import logging
import sys

from . import __version__
from .agent import Agent
from .config import ConfigError, load_config
from .discover import discover

DEFAULT_CONFIG = "/etc/hcluster-agent/config.yaml"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hcluster-agent", description="hCluster Sentinel node agent")
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run", help="run the agent (default)")
    sub.add_parser("check-config", help="validate the config file and exit")
    o = sub.add_parser("once", help="collect one report; print it (add --send to deliver it)")
    o.add_argument("--send", action="store_true")
    d = sub.add_parser("discover", help="probe Prometheus, listening ports and well-known services")
    d.add_argument("--prometheus-url", action="append", help="repeatable; default tries :9091 then :9090 on localhost")
    args = ap.parse_args(argv)
    cmd = args.cmd or "run"

    if cmd == "discover":
        return discover(args.prometheus_url or ["http://127.0.0.1:9091", "http://127.0.0.1:9090"])

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if cmd == "check-config":
        print(f"OK: node {cfg.node_id}, {len(cfg.services)} service check(s), "
              f"prometheus {'enabled' if cfg.prometheus.enabled else 'disabled'} ({len(cfg.prometheus.queries)} queries), api {cfg.api_url}")
        return 0
    agent = Agent(cfg)
    if cmd == "once":
        agent.local.collect()                       # prime CPU / network deltas
        import time
        time.sleep(1.0)
        rep = agent.step(force=True)
        print(json.dumps(rep, indent=2))
        if args.send:
            agent.reporter.flush()
            print(f"queued after send: {len(agent.reporter.queue)} (0 = delivered)", file=sys.stderr)
            return 0 if not agent.reporter.queue else 1
        agent.reporter.queue.clear()
        agent.reporter._persist()
        return 0
    agent.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
