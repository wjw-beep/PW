from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

from .logic import evaluate_predicate, minimal_counterwitnesses, status_name, warrant
from .memory import SQLiteMemory
from .probability import RiskModel
from .sources import SourceRouter, iso, timestamp, delta_days


class ProofWatch:
    def __init__(self, config: dict, memory: SQLiteMemory):
        self.config = config
        self.settings = config["engine"]
        self.memory = memory
        self.state = memory.load()
        self.router = SourceRouter(config)
        self.sources = {source["id"]: source for source in config["sources"]}
        self.propositions = {item["id"]: item for item in config["propositions"]}
        self.goals = {goal["id"]: goal for goal in config["goals"]}
        self.source_propositions = {key: [p for p in self.propositions.values() if p["source"] == key] for key in self.sources}
        self.dependencies = {key: {goal["id"] for goal in config["goals"] if any(key in path for path in goal["support_paths"])} for key in self.propositions}
        specification = {"sources": config["sources"], "propositions": config["propositions"], "goals": config["goals"]}
        signature = hashlib.sha256(json.dumps(specification, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        stored = self.state.get("configuration_signature")
        if stored and stored != signature:
            raise ValueError("State is bound to a different source/predicate/goal configuration; use a new state database")
        self.state["configuration_signature"] = signature
        self.state.setdefault("commitments", {})
        for goal_id, goal in self.goals.items():
            self.state["commitments"].setdefault(goal_id, {}).update({"support_paths": goal["support_paths"], "weight": float(goal.get("weight", 1)), "dependencies": sorted({key for path in goal["support_paths"] for key in path})})

    def _related_propositions(self, source_id: str) -> set[str]:
        related = {item["id"] for item in self.source_propositions[source_id]}
        shared = {(self.sources[source_id].get("provenance_group", source_id), item.get("canonical_fact")) for item in self.source_propositions[source_id] if item.get("canonical_fact")}
        for proposition in self.propositions.values():
            source = self.sources[proposition["source"]]
            if proposition.get("canonical_fact") and (source.get("provenance_group", proposition["source"]), proposition["canonical_fact"]) in shared:
                related.add(proposition["id"])
        return related

    def _truth_values(self, now: str) -> dict[str, bool | None]:
        result = {}
        for key, proposition in self.propositions.items():
            evidence = self.state["evidence"].get(key)
            stats = self.state["source_stats"].get(proposition["source"], {})
            if not evidence or evidence["outcome"] is None:
                result[key] = None
            elif stats.get("unresolved_since") and timestamp(stats["unresolved_since"]) >= timestamp(evidence["observed_at"]):
                result[key] = None
            elif float(evidence["reliability"]) < float(self.settings["min_reliability"]):
                result[key] = None
            elif delta_days(evidence["observed_at"], now) > float(self.settings["evidence_max_age_days"]):
                result[key] = None
            else:
                result[key] = bool(evidence["outcome"])
        groups = {}
        for key, proposition in self.propositions.items():
            fact = proposition.get("canonical_fact")
            if fact:
                source_id = proposition["source"]
                group = (self.sources[source_id].get("provenance_group", source_id), fact)
                groups.setdefault(group, []).append(key)
        for keys in groups.values():
            if True in [result[key] for key in keys] and False in [result[key] for key in keys]:
                for key in keys:
                    result[key] = None
        return result

    def _revise(self, now: str, affected: set[str] | None = None) -> list[dict]:
        values = self._truth_values(now)
        changed = []
        for goal_id in sorted(affected if affected is not None else self.goals):
            outcome = warrant(self.goals[goal_id]["support_paths"], values)
            current = {"status": status_name(outcome), "decision": int(outcome) if outcome is not None else None, "updated_at": now}
            previous = self.state["decisions"].get(goal_id)
            if previous is None or previous["status"] != current["status"] or previous["decision"] != current["decision"]:
                self.state["decisions"][goal_id] = current
                changed.append({"goal": goal_id, "previous": previous["status"] if previous else None, "current": current["status"], "decision": current["decision"]})
            elif previous:
                current["updated_at"] = previous["updated_at"]
                self.state["decisions"][goal_id] = current
        return changed

    def _compile(self, now: str) -> None:
        truth = self._truth_values(now)
        by_source: dict[str, dict[str, Any]] = {}
        for goal in self.config["goals"]:
            witnesses = minimal_counterwitnesses(goal["support_paths"], truth, int(self.settings["max_counterwitness_predicates"]))
            self.state["commitments"][goal["id"]]["counterwitnesses"] = witnesses
            for group in witnesses:
                for key in group:
                    source_id = self.propositions[key]["source"]
                    record = by_source.setdefault(source_id, {"goals": set(), "witnesses": set()})
                    record["goals"].add(goal["id"])
                    record["witnesses"].add(tuple(group))
        for source_id, record in sorted(by_source.items()):
            existing = self.state["obligations"].get(source_id)
            if existing is None:
                existing = {
                    "source": source_id,
                    "phase": "resolve",
                    "target": self.sources[source_id]["resource"],
                    "due": now,
                    "last_attempt": None,
                    "blocked_until": None,
                    "completed_at": None,
                    "redirect_hops": 0,
                }
                self.state["obligations"][source_id] = existing
            existing["goals"] = sorted(record["goals"])
            existing["counterwitnesses"] = [list(item) for item in sorted(record["witnesses"])]
            if existing["phase"] == "complete" and timestamp(now) >= timestamp(existing["due"]):
                existing.update({"phase": "resolve", "target": self.sources[source_id]["resource"], "blocked_until": None, "redirect_hops": 0})

    def _cost(self, source_id: str, phase: str) -> float:
        source = self.sources[source_id]
        return float(source.get("action_costs", {}).get(phase, self.settings["action_costs"][phase]))

    def _select(self, now: str, remaining: float) -> tuple[str, float, float] | None:
        predictor = RiskModel(self.config, self.state, now)
        candidates = []
        for source_id, obligation in self.state["obligations"].items():
            phase = obligation["phase"]
            if phase not in ("resolve", "fetch") or timestamp(obligation["due"]) > timestamp(now):
                continue
            if obligation.get("blocked_until") and timestamp(obligation["blocked_until"]) > timestamp(now):
                continue
            if obligation.get("last_attempt") == now and obligation.get("last_phase") == phase:
                continue
            cost = self._cost(source_id, phase)
            if cost <= remaining + 1e-9:
                score = predictor.score(source_id, phase, cost)
                if self.settings["scheduler"]["mode"] == "fill_budget" or score > float(self.settings["scheduler"]["minimum_score"]):
                    candidates.append((score, source_id, cost))
        if not candidates:
            return None
        candidates.sort(key=lambda row: (-row[0], row[1]))
        return candidates[0][1], candidates[0][2], candidates[0][0]

    def _stats(self, source_id: str) -> dict:
        return self.state["source_stats"].setdefault(source_id, {"resolve_attempts": 0, "resolve_successes": 0, "fetch_attempts": 0, "fetch_successes": 0, "changes": 0, "exposure_days": 0.0, "triggers": 0, "last_success": None, "last_digest": None, "unresolved_since": None})

    def _failure(self, source_id: str, phase: str, now: str, resource: str, status: str) -> None:
        stats = self._stats(source_id)
        stats["unresolved_since"] = now
        self.state["coverage"].append({"query": [item["id"] for item in self.source_propositions[source_id]], "sources": [resource], "interval": [now, now], "access_assumptions": {"action": phase, "scope": "inspected_resources_only"}, "result": status})
        obligation = self.state["obligations"][source_id]
        obligation["blocked_until"] = iso(timestamp(now) + timedelta(days=float(self.settings["retry_delay_days"])))
        obligation["due"] = obligation["blocked_until"]
        self._revise(now, {goal for key in self._related_propositions(source_id) for goal in self.dependencies[key]})

    def _successful_fetch(self, source_id: str, now: str, resource: str, observation) -> list[dict]:
        stats = self._stats(source_id)
        digest = hashlib.sha256(json.dumps(observation.data, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
        old_digest = stats.get("last_digest")
        previous_success = stats.get("last_success")
        changed_source = previous_success is not None and digest != old_digest
        if previous_success is not None:
            stats["exposure_days"] += delta_days(previous_success, now)
        if changed_source:
            stats["changes"] += 1
        stats["last_digest"] = digest
        stats["last_success"] = now
        stats["unresolved_since"] = None
        changed_props = set()
        for proposition in self.source_propositions[source_id]:
            key = proposition["id"]
            outcome = evaluate_predicate(observation.data, proposition)
            item = {"source": source_id, "resource": resource, "observed_at": now, "proposition": key, "outcome": outcome, "reliability": observation.reliability, "version_digest": digest, "provenance_group": self.sources[source_id].get("provenance_group", source_id)}
            old = self.state["evidence"].get(key)
            self.state["evidence"][key] = item
            self.state["evidence_history"].append(item)
            if old is None or old["outcome"] != outcome or old["version_digest"] != digest:
                changed_props.add(key)
        affected = set().union(*(self.dependencies[key] for key in changed_props)) if changed_props else set()
        affected.update(goal for key in self._related_propositions(source_id) for goal in self.dependencies[key])
        revisions = self._revise(now, affected)
        if changed_source and revisions:
            stats["triggers"] += 1
        self.state["coverage"].append({"query": [item["id"] for item in self.source_propositions[source_id]], "sources": [resource], "interval": [now, now], "access_assumptions": {"action": "fetch", "scope": "inspected_resources_only"}, "result": "inspected"})
        obligation = self.state["obligations"][source_id]
        obligation.update({"phase": "complete", "completed_at": now, "due": iso(timestamp(now) + timedelta(days=float(self.settings["poll_interval_days"]))), "blocked_until": None, "redirect_hops": 0})
        return revisions

    def _action(self, source_id: str, now: str, score: float, cost: float) -> dict:
        obligation = self.state["obligations"][source_id]
        phase = obligation["phase"]
        resource = obligation["target"]
        observation = self.router.action(source_id, phase, resource, now)
        stats = self._stats(source_id)
        stats[f"{phase}_attempts"] += 1
        obligation["last_attempt"] = now
        obligation["last_phase"] = phase
        trace = {"at": now, "source": source_id, "action": phase, "resource": resource, "status": observation.status, "cost": cost, "score": round(score, 9), "revisions": []}
        if phase == "resolve":
            if observation.status == "resolved" and observation.resolved_resource:
                stats["resolve_successes"] += 1
                obligation["phase"] = "fetch"
                obligation["target"] = observation.resolved_resource
            else:
                self._failure(source_id, phase, now, resource, observation.status)
        elif observation.status == "ok" and observation.data is not None:
            stats["fetch_successes"] += 1
            trace["revisions"] = self._successful_fetch(source_id, now, observation.resolved_resource or resource, observation)
        elif observation.status == "redirect" and observation.resolved_resource:
            hops = int(obligation.get("redirect_hops", 0)) + 1
            if hops > int(self.settings["http"].get("max_redirects", 5)):
                self._failure(source_id, phase, now, resource, "redirect_limit")
            else:
                obligation["redirect_hops"] = hops
                obligation["target"] = observation.resolved_resource
                obligation["last_phase"] = "redirect"
        else:
            self._failure(source_id, phase, now, resource, observation.status)
        self.state["trace"].append(trace)
        return trace

    def run_cycle(self, at: str, budget: float | None = None) -> dict:
        now = iso(at)
        if self.state["last_cycle"] and timestamp(now) < timestamp(self.state["last_cycle"]):
            raise ValueError("Maintenance cycles must be chronological")
        remaining = float(self.settings["action_budget"] if budget is None else budget)
        if remaining < 0:
            raise ValueError("Budget must be nonnegative")
        decisions_before = self._revise(now)
        self._compile(now)
        actions = []
        while True:
            selected = self._select(now, remaining)
            if selected is None:
                break
            source_id, cost, score = selected
            trace = self._action(source_id, now, score, cost)
            actions.append(trace)
            remaining -= cost
            self._compile(now)
        self.state["last_cycle"] = now
        tokens = self.memory.save(self.state, self.settings)
        return {"at": now, "actions": actions, "remaining_budget": round(remaining, 9), "decisions": {key: {"status": value["status"], "decision": value["decision"]} for key, value in sorted(self.state["decisions"].items())}, "pending_obligations": sorted(key for key, value in self.state["obligations"].items() if value["phase"] != "complete"), "memory_tokens_estimate": tokens, "initial_revisions": decisions_before}

    def inspect(self) -> dict:
        return self.state
