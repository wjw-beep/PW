from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from .config import load_config
from .engine import ProofWatch
from .memory import SQLiteMemory
from .metrics import evaluate
from .sources import iso


def main() -> None:
    parser = argparse.ArgumentParser(prog="proofwatch")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--config", required=True)
    run = commands.add_parser("run")
    run.add_argument("--config", required=True)
    run.add_argument("--state", required=True)
    run.add_argument("--at", required=True)
    run.add_argument("--budget", type=float)
    simulate = commands.add_parser("simulate")
    simulate.add_argument("--config", required=True)
    simulate.add_argument("--state", required=True)
    simulate.add_argument("--checkpoints", required=True)
    simulate.add_argument("--output")
    show = commands.add_parser("show")
    show.add_argument("--state", required=True)
    args = parser.parse_args()
    if args.command == "show":
        result = SQLiteMemory(args.state).load()
    else:
        config = load_config(args.config)
        if args.command == "validate":
            result = {"valid": True, "sources": len(config["sources"]), "propositions": len(config["propositions"]), "goals": len(config["goals"])}
        else:
            engine = ProofWatch(config, SQLiteMemory(args.state))
            if args.command == "run":
                result = engine.run_cycle(args.at, args.budget)
            else:
                with Path(args.checkpoints).open("r", encoding="utf-8") as stream:
                    payload = yaml.safe_load(stream)
                checkpoints = payload.get("checkpoints", [])
                results = [engine.run_cycle(value) for value in checkpoints]
                result = {"checkpoints": results}
                if "reference_ledger" in payload:
                    reference = {iso(key): value for key, value in payload["reference_ledger"].items()}
                    result["evaluation"] = evaluate(results, reference, config)
                if args.output:
                    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
