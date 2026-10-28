from __future__ import annotations

from copy import deepcopy
import re
from pathlib import Path
from typing import Any

import yaml


class ConfigurationError(ValueError):
    pass


def load_config(path: str | Path) -> dict[str, Any]:
    location = Path(path).expanduser().resolve()
    with location.open("r", encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    if not isinstance(config, dict):
        raise ConfigurationError("Configuration must be a YAML mapping")
    with Path(__file__).with_name("defaults.yaml").open("r", encoding="utf-8") as stream:
        defaults = yaml.safe_load(stream)
    config = deep_merge(defaults, config)
    config["_config_path"] = str(location)
    if "fixture_file" in config and config["fixture_file"]:
        config["fixture_file"] = str((location.parent / config["fixture_file"]).resolve())
    validate_config(config)
    return config


def deep_merge(original: dict, override: dict) -> dict:
    result = deepcopy(original)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def validate_config(config: dict) -> None:
    if config.get("version") != 1:
        raise ConfigurationError("Unsupported configuration version")
    for name in ("sources", "propositions", "goals"):
        if not isinstance(config.get(name), list) or not config[name]:
            raise ConfigurationError(f"{name} must be a nonempty list")
        ids = [item.get("id") for item in config[name]]
        if any(not isinstance(item, str) or not item for item in ids) or len(ids) != len(set(ids)):
            raise ConfigurationError(f"{name} must have unique nonempty ids")
    sources = {item["id"] for item in config["sources"]}
    propositions = {item["id"] for item in config["propositions"]}
    for source in config["sources"]:
        if source.get("kind") not in ("fixture", "http"):
            raise ConfigurationError(f"Unsupported source kind: {source.get('kind')}")
        if not source.get("resource") or not source.get("category"):
            raise ConfigurationError("Each source requires resource and category")
    if any(source["kind"] == "fixture" for source in config["sources"]):
        if not config.get("fixture_file") or not Path(config["fixture_file"]).exists():
            raise ConfigurationError("fixture_file must exist when using fixture sources")
    for proposition in config["propositions"]:
        if proposition.get("source") not in sources:
            raise ConfigurationError(f"Unknown proposition source: {proposition['id']}")
        if not isinstance(proposition.get("path"), str):
            raise ConfigurationError(f"Proposition {proposition['id']} requires path")
        if proposition.get("operator") not in ("eq", "ne", "ge", "gt", "le", "lt", "contains", "exists"):
            raise ConfigurationError(f"Unknown operator: {proposition['id']}")
        if proposition.get("operator") != "exists" and "threshold" not in proposition:
            raise ConfigurationError(f"Proposition {proposition['id']} requires threshold")
        if "pattern" in proposition:
            try:
                re.compile(proposition["pattern"])
            except re.error as error:
                raise ConfigurationError(f"Invalid regular expression for {proposition['id']}: {error}") from error
            if proposition.get("cast", "str") not in ("str", "int", "float", "bool"):
                raise ConfigurationError(f"Unsupported cast for {proposition['id']}")
    for goal in config["goals"]:
        paths = goal.get("support_paths")
        if not isinstance(paths, list) or not paths or any(not isinstance(path, list) or not path for path in paths):
            raise ConfigurationError(f"Goal {goal['id']} needs nonempty support_paths")
        if any(p not in propositions for path in paths for p in path):
            raise ConfigurationError(f"Goal {goal['id']} uses unregistered proposition")
        if float(goal.get("weight", 1)) <= 0:
            raise ConfigurationError("Goal weights must be positive")
    engine = config["engine"]
    if engine["scheduler"]["mode"] not in ("positive_only", "fill_budget"):
        raise ConfigurationError("scheduler.mode must be positive_only or fill_budget")
    if float(engine["action_budget"]) <= 0 or int(engine["memory_tokens"]) <= 0:
        raise ConfigurationError("Action and memory budgets must be positive")
    if float(engine["hazard"]["kappa_days"]) < 0:
        raise ConfigurationError("Hazard shrinkage must be nonnegative")
    if not (0 <= float(engine["risk"]["suspension_penalty"]) <= 1):
        raise ConfigurationError("Suspension penalty must be in [0, 1]")
