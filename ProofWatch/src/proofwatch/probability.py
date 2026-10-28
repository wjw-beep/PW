from __future__ import annotations

import math
import random
from itertools import product

from .logic import warrant
from .sources import delta_days


class RiskModel:
    def __init__(self, config: dict, state: dict, now: str):
        self.config = config
        self.state = state
        self.now = now
        self.settings = config["engine"]
        self.sources = {source["id"]: source for source in config["sources"]}
        self.propositions = {item["id"]: item for item in config["propositions"]}
        self.provenance_keys = {}
        for key, proposition in self.propositions.items():
            source_id = proposition["source"]
            fact = proposition.get("canonical_fact")
            self.provenance_keys[key] = (self.sources[source_id].get("provenance_group", source_id), fact) if fact else ("unique", key)

    def hazard_rate(self, source_id: str) -> float:
        source = self.sources[source_id]
        settings = self.settings["hazard"]
        prior = float(settings["category_priors_per_day"].get(source["category"], settings["default_prior_per_day"]))
        stats = self.state["source_stats"].get(source_id, {})
        kappa = float(settings["kappa_days"])
        return (float(stats.get("changes", 0)) + kappa * prior) / (float(stats.get("exposure_days", 0)) + kappa) if float(stats.get("exposure_days", 0)) + kappa > 0 else prior

    def change_probability(self, source_id: str, observed_at: str) -> float:
        return -math.expm1(-self.hazard_rate(source_id) * delta_days(observed_at, self.now))

    def trigger_probability(self, source_id: str) -> float:
        settings = self.settings["hazard"]
        stats = self.state["source_stats"].get(source_id, {})
        alpha = float(settings["trigger_alpha"])
        beta = float(settings["trigger_beta"])
        return (float(stats.get("triggers", 0)) + alpha) / (float(stats.get("changes", 0)) + alpha + beta)

    def access_probability(self, source_id: str, action: str) -> float:
        stats = self.state["source_stats"].get(source_id, {})
        attempts = float(stats.get(f"{action}_attempts", 0))
        if attempts == 0:
            return float(self.settings["hazard"]["cold_start_access_probability"])
        alpha = float(self.settings["hazard"]["access_alpha"])
        beta = float(self.settings["hazard"]["access_beta"])
        return (float(stats.get(f"{action}_successes", 0)) + alpha) / (attempts + alpha + beta)

    def truth_probabilities(self) -> dict[str, float]:
        settings = self.settings["scheduler"]
        result = {}
        for proposition in self.propositions.values():
            key = proposition["id"]
            evidence = self.state["evidence"].get(key)
            source_id = proposition["source"]
            stats = self.state["source_stats"].get(source_id, {})
            if not evidence or evidence["outcome"] is None or stats.get("unresolved_since") or float(evidence["reliability"]) < float(self.settings["min_reliability"]):
                result[key] = float(settings["prior_true"])
                continue
            change_risk = self.change_probability(source_id, evidence["observed_at"]) * self.trigger_probability(source_id)
            confidence = float(evidence["reliability"])
            if evidence["outcome"] is True:
                result[key] = confidence * (1 - change_risk) + (1 - confidence) * 0.5
            else:
                result[key] = confidence * change_risk + (1 - confidence) * 0.5
        return result

    def _goal_probability(self, paths: list[list[str]], probabilities: dict[str, float]) -> float:
        variables = sorted({key for path in paths for key in path})
        groups = {}
        for key in variables:
            groups.setdefault(self.provenance_keys[key], []).append(key)
        grouped = sorted(groups)
        group_probabilities = {group: sum(probabilities[key] for key in members) / len(members) for group, members in groups.items()}
        max_exact = int(self.settings["scheduler"]["max_exact_variables"])
        if len(grouped) <= max_exact:
            total = 0.0
            for assignment in product((False, True), repeat=len(grouped)):
                chosen = dict(zip(grouped, assignment))
                weight = math.prod(group_probabilities[group] if value else 1 - group_probabilities[group] for group, value in chosen.items())
                if warrant(paths, {key: chosen[self.provenance_keys[key]] for key in variables}) is True:
                    total += weight
            return max(0.0, min(1.0, total))
        randomizer = random.Random(int(self.settings["scheduler"]["seed"]))
        count = int(self.settings["scheduler"]["monte_carlo_samples"])
        return sum(warrant(paths, {key: chosen[self.provenance_keys[key]] for key in variables}) is True for chosen in ({group: randomizer.random() < group_probabilities[group] for group in grouped} for _ in range(count))) / count

    def risk(self, probabilities: dict[str, float]) -> float:
        suspension = float(self.settings["risk"]["suspension_penalty"])
        wrong = float(self.settings["risk"]["wrong_decision_loss"])
        risk = 0.0
        for goal in self.config["goals"]:
            chance = self._goal_probability(goal["support_paths"], probabilities)
            risk += float(goal.get("weight", 1.0)) * min(wrong * chance, wrong * (1 - chance), suspension)
        return risk

    def fetch_evsi(self, source_id: str) -> float:
        prior = self.truth_probabilities()
        baseline = self.risk(prior)
        keys = sorted(key for key, prop in self.propositions.items() if prop["source"] == source_id)
        if not keys:
            return 0.0
        groups = sorted({self.provenance_keys[key] for key in keys})
        members = {group: sorted(key for key in prior if self.provenance_keys[key] == group) for group in groups}
        chances = {group: sum(prior[key] for key in members[group]) / len(members[group]) for group in groups}
        limit = int(self.settings["scheduler"]["max_exact_variables"])
        if len(groups) <= limit:
            scenarios = [(dict(zip(groups, assignment)), math.prod(chances[group] if value else 1 - chances[group] for group, value in zip(groups, assignment))) for assignment in product((False, True), repeat=len(groups))]
        else:
            randomizer = random.Random(int(self.settings["scheduler"]["seed"]))
            samples = int(self.settings["scheduler"]["monte_carlo_samples"])
            scenarios = [({group: randomizer.random() < chances[group] for group in groups}, 1 / samples) for _ in range(samples)]
        posterior_risk = 0.0
        for outcomes, likelihood in scenarios:
            posterior = dict(prior)
            for group, outcome in outcomes.items():
                posterior.update({key: float(outcome) for key in members[group]})
            posterior_risk += likelihood * self.risk(posterior)
        return max(0.0, baseline - posterior_risk) * self.access_probability(source_id, "fetch")

    def score(self, source_id: str, phase: str, cost: float) -> float:
        settings = self.settings["scheduler"]
        fetch_value = self.fetch_evsi(source_id)
        if phase == "resolve":
            immediate = 0.0
            continuation = self.access_probability(source_id, "resolve") * fetch_value
        else:
            immediate = fetch_value
            continuation = 0.0
        return (immediate + float(settings["beta"]) * continuation) / (cost + float(settings["epsilon"])) - float(settings["lambda_access"]) * (1 - self.access_probability(source_id, phase)) - float(settings["lambda_cost"]) * cost
