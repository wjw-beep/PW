from __future__ import annotations


def evaluate(checkpoints: list[dict], ledger: dict, config: dict) -> dict:
    weights = {goal["id"]: float(goal.get("weight", 1.0)) for goal in config["goals"]}
    penalty = float(config["engine"]["risk"]["suspension_penalty"])
    total_weight = 0.0
    loss = 0.0
    answered = 0
    incorrect = 0
    evaluated = 0
    for cycle in checkpoints:
        reference = ledger.get(cycle["at"], {})
        for goal_id, truth in reference.items():
            if truth not in (0, 1, False, True):
                continue
            weight = weights[goal_id]
            decision = cycle["decisions"][goal_id]["decision"]
            evaluated += 1
            total_weight += weight
            if decision is None:
                loss += weight * penalty
            else:
                answered += 1
                if decision != int(truth):
                    incorrect += 1
                    loss += weight
    if not evaluated:
        return {"evaluated": 0, "cel_percent": None, "coverage_percent": None, "selective_error_percent": None}
    return {"evaluated": evaluated, "cel_percent": round(100 * loss / total_weight, 4), "coverage_percent": round(100 * answered / evaluated, 4), "selective_error_percent": round(100 * incorrect / answered, 4) if answered else None}
