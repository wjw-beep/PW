from __future__ import annotations

from itertools import combinations
import re
from typing import Any


def conjunction(values: list[bool | None]) -> bool | None:
    if False in values:
        return False
    if None in values:
        return None
    return True


def disjunction(values: list[bool | None]) -> bool | None:
    if True in values:
        return True
    if None in values:
        return None
    return False


def warrant(paths: list[list[str]], values: dict[str, bool | None]) -> bool | None:
    return disjunction([conjunction([values.get(key) for key in path]) for path in paths])


def status_name(value: bool | None) -> str:
    if value is True:
        return "supported"
    if value is False:
        return "contradicted"
    return "unknown"


def minimal_counterwitnesses(paths: list[list[str]], values: dict[str, bool | None], max_exact_variables: int = 14) -> list[list[str]]:
    unique = sorted(set().union(*map(set, paths)))
    if len(unique) > max_exact_variables:
        return [[item] for item in unique]
    baseline = warrant(paths, values)
    candidates: list[tuple[str, ...]] = []
    for count in range(1, len(unique) + 1):
        for subset in combinations(unique, count):
            chosen = set(subset)
            if any(set(previous).issubset(chosen) for previous in candidates):
                continue
            flipped = dict(values)
            for key in subset:
                flipped[key] = None if baseline is False else False
            if warrant(paths, flipped) != baseline:
                candidates.append(subset)
    if candidates:
        return [list(item) for item in candidates]
    return [[item] for item in unique]


def field_value(data: Any, path: str) -> Any:
    current = data
    if not path:
        return current
    for segment in path.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        elif isinstance(current, list) and segment.isdecimal() and int(segment) < len(current):
            current = current[int(segment)]
        else:
            raise KeyError(path)
    return current


def evaluate_predicate(data: Any, predicate: dict) -> bool | None:
    operator = predicate["operator"]
    try:
        actual = field_value(data, predicate["path"])
    except (KeyError, TypeError, IndexError):
        return False if operator == "exists" else None
    if operator == "exists":
        return actual is not None
    if "pattern" in predicate:
        match = re.search(predicate["pattern"], str(actual))
        if match is None:
            return None
        group = predicate.get("capture_group", 1)
        try:
            actual = match.group(group)
            cast = predicate.get("cast", "str")
            if cast == "int":
                actual = int(actual)
            elif cast == "float":
                actual = float(actual)
            elif cast == "bool":
                actual = actual.strip().lower() in ("true", "yes", "1")
        except (IndexError, ValueError, TypeError):
            return None
    threshold = predicate["threshold"]
    try:
        if operator == "eq":
            return actual == threshold
        if operator == "ne":
            return actual != threshold
        if operator == "ge":
            return actual >= threshold
        if operator == "gt":
            return actual > threshold
        if operator == "le":
            return actual <= threshold
        if operator == "lt":
            return actual < threshold
        if operator == "contains":
            return threshold in actual
    except (TypeError, ValueError):
        return None
    return None
