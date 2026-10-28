from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


def timestamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def iso(value: str | datetime) -> str:
    return timestamp(value).isoformat()


def delta_days(early: str, late: str) -> float:
    return max(0.0, (timestamp(late) - timestamp(early)).total_seconds() / 86400.0)


@dataclass
class Observation:
    status: str
    resource: str
    at: str
    data: Any = None
    resolved_resource: str | None = None
    reliability: float = 1.0
    error: str | None = None


class FixtureSource:
    def __init__(self, location: str | Path):
        with Path(location).open("r", encoding="utf-8") as stream:
            payload = yaml.safe_load(stream)
        if not isinstance(payload, dict) or not isinstance(payload.get("resources"), dict):
            raise ValueError("Fixture YAML requires a resources mapping")
        self.resources = payload["resources"]

    def _snapshot(self, resource: str, at: str) -> dict | None:
        snapshots = self.resources.get(resource, [])
        eligible = [item for item in snapshots if timestamp(item["at"]) <= timestamp(at)]
        return max(eligible, key=lambda item: timestamp(item["at"])) if eligible else None

    def resolve(self, resource: str, at: str) -> Observation:
        snapshot = self._snapshot(resource, at)
        if snapshot is None:
            return Observation("unavailable", resource, at)
        status = snapshot.get("status", "ok")
        if status == "redirect":
            if not snapshot.get("target"):
                return Observation("unavailable", resource, at)
            return Observation("resolved", resource, at, resolved_resource=snapshot["target"])
        if status != "ok":
            return Observation(status, resource, at)
        return Observation("resolved", resource, at, resolved_resource=resource)

    def fetch(self, resource: str, at: str) -> Observation:
        snapshot = self._snapshot(resource, at)
        if snapshot is None:
            return Observation("unavailable", resource, at)
        status = snapshot.get("status", "ok")
        if status == "redirect":
            return Observation("redirect", resource, at, resolved_resource=snapshot.get("target"))
        return Observation(status, resource, at, data=snapshot.get("data"), reliability=float(snapshot.get("reliability", 1.0)))


class HTTPSource:
    def __init__(self, settings: dict):
        self.settings = settings

    def _request(self, resource: str, method: str):
        parts = urlparse(resource)
        if parts.scheme not in self.settings["allowed_schemes"] or not parts.hostname:
            raise ValueError("Disallowed or invalid remote URL")
        headers = dict(self.settings.get("headers", {}))
        headers["User-Agent"] = self.settings["user_agent"]
        request = urllib.request.Request(resource, method=method, headers=headers)
        return urllib.request.urlopen(request, timeout=float(self.settings["timeout_seconds"]))

    def resolve(self, resource: str, at: str) -> Observation:
        try:
            with self._request(resource, "HEAD") as response:
                return Observation("resolved", resource, at, resolved_resource=response.geturl())
        except urllib.error.HTTPError as error:
            if error.code in (405, 501):
                try:
                    with self._request(resource, "GET") as response:
                        return Observation("resolved", resource, at, resolved_resource=response.geturl())
                except (urllib.error.URLError, ValueError, OSError) as inner:
                    return Observation("unavailable", resource, at, error=str(inner))
            return Observation("not_found" if error.code == 404 else "unavailable", resource, at, error=str(error))
        except (urllib.error.URLError, ValueError, OSError) as error:
            return Observation("unavailable", resource, at, error=str(error))

    def fetch(self, resource: str, at: str) -> Observation:
        try:
            with self._request(resource, "GET") as response:
                maximum = int(self.settings["max_response_bytes"])
                payload = response.read(maximum + 1)
                if len(payload) > maximum:
                    return Observation("unavailable", resource, at, error="response size limit exceeded")
                content_type = response.headers.get("Content-Type", "")
                encoding = response.headers.get_content_charset() or "utf-8"
                body = payload.decode(encoding, errors="replace")
                data = json.loads(body) if "json" in content_type else {"body": body}
                return Observation("ok", resource, at, data=data, resolved_resource=response.geturl())
        except urllib.error.HTTPError as error:
            return Observation("not_found" if error.code == 404 else "unavailable", resource, at, error=str(error))
        except (urllib.error.URLError, ValueError, OSError, json.JSONDecodeError) as error:
            return Observation("unavailable", resource, at, error=str(error))


class SourceRouter:
    def __init__(self, config: dict):
        self.sources = {item["id"]: item for item in config["sources"]}
        self.fixture = FixtureSource(config["fixture_file"]) if any(item["kind"] == "fixture" for item in config["sources"]) else None
        self.http = HTTPSource(config["engine"]["http"])

    def action(self, source_id: str, kind: str, resource: str, at: str) -> Observation:
        source = self.sources[source_id]
        adapter = self.fixture if source["kind"] == "fixture" else self.http
        return getattr(adapter, kind)(resource, at)
