"""Typed automation requests: dataclasses checked when built, whose
``to_dict()`` is the JSON core's automation app decodes, unset fields left out.

Example::

    req = CreateAutomationRequest(
        name="nightly-sync",
        trigger_type="periodic",
        trigger_config=TriggerConfig(periodic=PeriodicTrigger("0 3 * * *")),
        dag_config=DAGConfig("nightly-sync", "nightly-sync", nodes=[
            http_node("fetch", "any_node", HTTPSettings(app=AppCall("weather", "/api/sync"), method="POST")),
        ]),
    )
    LocalitasClient.default().automation().create(req)
"""

import re
from dataclasses import dataclass
from typing import Any, Optional, Union

from .client import _check_app_name, _check_app_path

EXECUTION_STRATEGIES = ("raft_leader", "any_node", "all_nodes", "local_only")
TRIGGER_RULES = ("all_success", "all_done", "one_success", "one_failed", "none_failed")
SENSOR_WAITS = ("event", "file", "time", "pubsub")
CATCHUPS = ("latest", "all")
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
CONTENT_TYPES = ("json", "form", "raw")

_DURATION = re.compile(r"^(\d+(\.\d+)?(ns|us|µs|ms|s|m|h))+$")


def _one_of(name: str, value: Optional[str], allowed: tuple) -> None:
    if value is not None and value not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(allowed)}, got {value!r}")


def _duration(name: str, value: Optional[str]) -> None:
    if value is not None and (not isinstance(value, str) or not _DURATION.match(value)):
        raise ValueError(f'{name} {value!r} is not a duration such as "30s", "5m" or "1h30m"')


def _int(name: str, value: Optional[int], low: Optional[int] = None, high: Optional[int] = None) -> None:
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an int, got {value!r}")
    if (low is not None and value < low) or (high is not None and value > high):
        bounds = f"at least {low}" if high is None else f"between {low} and {high}"
        raise ValueError(f"{name} must be {bounds}, got {value}")


def _bool(name: str, value: Optional[bool]) -> None:
    if value is not None and not isinstance(value, bool):
        raise TypeError(f"{name} must be a bool, got {value!r}")


def _text(name: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")


def _strings(name: str, value: Optional[list], required: bool = False) -> None:
    if value is None and not required:
        return
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value) or (required and not value):
        raise ValueError(f"{name} must be a {'non-empty ' if required else ''}list of non-empty strings, got {value!r}")


def _set(out: dict, **fields: Any) -> dict:
    out.update({k: v for k, v in fields.items() if v is not None})
    return out


@dataclass
class AppCall:
    """An app's endpoint by name, called through core."""
    app: str
    path: str
    protocol: str = "http"

    def __post_init__(self):
        _check_app_name(self.app)
        _check_app_path(self.path)
        _text("protocol", self.protocol)

    def to_dict(self) -> dict:
        return {"app": self.app, "path": self.path, "protocol": self.protocol}


@dataclass
class HTTPSettings:
    """An http-api task's call: to ``url`` or to ``app``, exactly one."""
    url: Optional[str] = None
    app: Optional[AppCall] = None
    method: Optional[str] = None
    content_type: Optional[str] = None
    headers: Optional[dict[str, str]] = None
    body: Any = None
    timeout_ms: Optional[int] = None
    max_retries: Optional[int] = None
    backoff_ms: Optional[int] = None
    backoff_multiplier: Optional[float] = None
    max_backoff_ms: Optional[int] = None
    expected_status: Optional[int] = None
    async_timeout_ms: Optional[int] = None
    deferrable: Optional[bool] = None
    vault_credential_id: Optional[str] = None
    vault_auth_format: Optional[str] = None
    vault_header_name: Optional[str] = None
    vault_header_prefix: Optional[str] = None

    def __post_init__(self):
        if bool(self.url) == (self.app is not None):
            raise ValueError("an http-api task calls exactly one of url or app")
        if self.app is not None:
            if not isinstance(self.app, AppCall):
                raise TypeError(f"app must be an AppCall, got {self.app!r}")
            if self.app.protocol != "http":
                raise ValueError(f"an http-api task calls an app over http, not {self.app.protocol!r}")
        if self.method is not None and self.method.upper() not in HTTP_METHODS:
            raise ValueError(f"method {self.method!r} is not an HTTP method")
        if self.content_type is not None and self.content_type.lower() not in CONTENT_TYPES:
            raise ValueError(f"content_type must be one of {', '.join(CONTENT_TYPES)}, got {self.content_type!r}")
        _int("timeout_ms", self.timeout_ms, low=0)
        _int("max_retries", self.max_retries, low=0)
        _int("backoff_ms", self.backoff_ms, low=0)
        _int("max_backoff_ms", self.max_backoff_ms, low=0)
        _int("expected_status", self.expected_status, low=0)
        _int("async_timeout_ms", self.async_timeout_ms, low=0)
        _bool("deferrable", self.deferrable)

    def to_dict(self) -> dict:
        return _set(
            {}, url=self.url or None, app=self.app.to_dict() if self.app else None, method=self.method,
            content_type=self.content_type, headers=self.headers, body=self.body, timeout_ms=self.timeout_ms,
            max_retries=self.max_retries, backoff_ms=self.backoff_ms, backoff_multiplier=self.backoff_multiplier,
            max_backoff_ms=self.max_backoff_ms, expected_status=self.expected_status,
            async_timeout_ms=self.async_timeout_ms, deferrable=self.deferrable,
            vault_credential_id=self.vault_credential_id, vault_auth_format=self.vault_auth_format,
            vault_header_name=self.vault_header_name, vault_header_prefix=self.vault_header_prefix,
        )


@dataclass
class Sensor:
    """What a sensor task waits for. Build one with event_sensor, time_sensor,
    file_sensor or pubsub_sensor."""
    wait_for: str
    event_type: Optional[str] = None
    paths: Optional[list[str]] = None
    delay: Optional[str] = None
    channel: Optional[str] = None
    cache: Optional[str] = None
    match: Optional[dict[str, Any]] = None
    timeout: Optional[str] = None

    def __post_init__(self):
        _one_of("wait_for", self.wait_for, SENSOR_WAITS)
        if self.wait_for == "event":
            _text("an event sensor's event_type", self.event_type)
        elif self.wait_for == "file":
            _strings("a file sensor's paths", self.paths, required=True)
        elif self.wait_for == "time":
            _text("a time sensor's delay", self.delay)
            _duration("delay", self.delay)
        else:
            _text("a pubsub sensor's channel", self.channel)
        if self.match is not None and (not isinstance(self.match, dict) or not all(isinstance(k, str) and k for k in self.match)):
            raise ValueError(f"match must map field names to JSON values, got {self.match!r}")
        _duration("timeout", self.timeout)

    def to_dict(self) -> dict:
        return _set(
            {"wait_for": self.wait_for}, event_type=self.event_type, paths=self.paths, delay=self.delay,
            channel=self.channel, cache=self.cache, match=self.match, timeout=self.timeout,
        )


def event_sensor(event_type: str, timeout: Optional[str] = None) -> Sensor:
    """Wait for an event of ``event_type`` published to the automation app."""
    return Sensor("event", event_type=event_type, timeout=timeout)


def time_sensor(delay: str, timeout: Optional[str] = None) -> Sensor:
    """Wait until ``delay`` after the run's logical time."""
    return Sensor("time", delay=delay, timeout=timeout)


def file_sensor(paths: list[str], timeout: Optional[str] = None) -> Sensor:
    """Wait for a change under one of ``paths``."""
    return Sensor("file", paths=paths, timeout=timeout)


def pubsub_sensor(channel: str, match: Optional[dict[str, Any]] = None, cache: Optional[str] = None,
                  timeout: Optional[str] = None) -> Sensor:
    """Wait for a message on ``channel`` (of ``cache``, if given) whose fields
    equal ``match``; a dotted field name reaches into nested objects."""
    return Sensor("pubsub", channel=channel, cache=cache, match=match, timeout=timeout)


@dataclass
class DAGNode:
    """One task of a DAG. ``metadata`` is the task's settings: HTTPSettings
    for http-api, Sensor for sensor, or a dict for other node types."""
    node_id: str
    node_type: str
    execution_strategy: Optional[str] = None
    dependencies: Optional[list[str]] = None
    retries: Optional[int] = None
    retry_delay: Optional[str] = None
    pool: Optional[str] = None
    pool_slots: Optional[int] = None
    priority_weight: Optional[int] = None
    depends_on_past: bool = False
    trigger_rule: Optional[str] = None
    metadata: Union[HTTPSettings, Sensor, dict, None] = None

    def __post_init__(self):
        _text("node_id", self.node_id)
        _text("node_type", self.node_type)
        _one_of("execution_strategy", self.execution_strategy, EXECUTION_STRATEGIES)
        _strings("dependencies", self.dependencies)
        _int("retries", self.retries, low=0, high=10)
        _duration("retry_delay", self.retry_delay)
        _int("pool_slots", self.pool_slots, low=1)
        _int("priority_weight", self.priority_weight)
        _bool("depends_on_past", self.depends_on_past)
        _one_of("trigger_rule", self.trigger_rule, TRIGGER_RULES)

    def to_dict(self) -> dict:
        metadata = self.metadata.to_dict() if isinstance(self.metadata, (HTTPSettings, Sensor)) else self.metadata
        return _set(
            {"node_id": self.node_id, "node_type": self.node_type},
            execution_strategy=self.execution_strategy, dependencies=self.dependencies, retries=self.retries,
            retry_delay=self.retry_delay, pool=self.pool, pool_slots=self.pool_slots,
            priority_weight=self.priority_weight, depends_on_past=self.depends_on_past or None,
            trigger_rule=self.trigger_rule, metadata=metadata,
        )


def http_node(node_id: str, execution_strategy: str, settings: HTTPSettings, **task: Any) -> DAGNode:
    """An http-api task; ``task`` takes DAGNode's other fields."""
    if not isinstance(settings, HTTPSettings):
        raise TypeError(f"settings must be HTTPSettings, got {settings!r}")
    return DAGNode(node_id, "http-api", execution_strategy, metadata=settings, **task)


def sensor_node(node_id: str, execution_strategy: str, sensor: Sensor, **task: Any) -> DAGNode:
    """A sensor task; ``task`` takes DAGNode's other fields."""
    if not isinstance(sensor, Sensor):
        raise TypeError(f"sensor must be a Sensor, got {sensor!r}")
    return DAGNode(node_id, "sensor", execution_strategy, metadata=sensor, **task)


@dataclass
class DAGConfig:
    dag_id: str
    name: str
    nodes: list[DAGNode]
    description: str = ""

    def __post_init__(self):
        _text("dag_id", self.dag_id)
        if not all(isinstance(n, DAGNode) for n in self.nodes):
            raise TypeError("nodes must be DAGNodes")

    def to_dict(self) -> dict:
        return _set({"dag_id": self.dag_id, "name": self.name}, description=self.description or None,
                    nodes=[n.to_dict() for n in self.nodes])


@dataclass
class PeriodicTrigger:
    schedule: str
    timezone: Optional[str] = None
    timeout_minutes: Optional[int] = None
    catchup: Optional[str] = None
    catchup_window: Optional[str] = None

    def __post_init__(self):
        _text("schedule", self.schedule)
        _int("timeout_minutes", self.timeout_minutes, low=0)
        _one_of("catchup", self.catchup, CATCHUPS)
        _duration("catchup_window", self.catchup_window)

    def to_dict(self) -> dict:
        return _set({"schedule": self.schedule}, timezone=self.timezone, timeout_minutes=self.timeout_minutes,
                    catchup=self.catchup, catchup_window=self.catchup_window)


@dataclass
class EventTrigger:
    event_type: str
    event_filter: Optional[dict[str, str]] = None
    debounce_ms: Optional[int] = None
    timeout_minutes: Optional[int] = None
    paths: Optional[list[str]] = None

    def __post_init__(self):
        _text("event_type", self.event_type)
        _int("debounce_ms", self.debounce_ms, low=0)
        _int("timeout_minutes", self.timeout_minutes, low=0)
        _strings("paths", self.paths)

    def to_dict(self) -> dict:
        return _set({"event_type": self.event_type}, event_filter=self.event_filter, debounce_ms=self.debounce_ms,
                    timeout_minutes=self.timeout_minutes, paths=self.paths)


@dataclass
class TriggerConfig:
    periodic: Optional[PeriodicTrigger] = None
    event: Optional[EventTrigger] = None
    retries: Optional[int] = None
    retry_delay: Optional[str] = None
    max_active_runs: Optional[int] = None
    sla: Optional[str] = None

    def __post_init__(self):
        _int("retries", self.retries, low=0)
        _duration("retry_delay", self.retry_delay)
        _int("max_active_runs", self.max_active_runs, low=0)
        _duration("sla", self.sla)

    def to_dict(self) -> dict:
        return _set(
            {}, periodic=self.periodic.to_dict() if self.periodic else None,
            event=self.event.to_dict() if self.event else None, retries=self.retries,
            retry_delay=self.retry_delay, max_active_runs=self.max_active_runs, sla=self.sla,
        )


@dataclass
class CreateAutomationRequest:
    """The body of POST /apps/automation/api/automations. ``node_id`` names
    the node a local_only job runs on."""
    name: str
    dag_config: DAGConfig
    trigger_type: str
    trigger_config: Optional[TriggerConfig] = None
    description: str = ""
    is_enabled: bool = True
    node_id: Optional[str] = None

    def __post_init__(self):
        _text("name", self.name)
        _text("trigger_type", self.trigger_type)
        _bool("is_enabled", self.is_enabled)

    def to_dict(self) -> dict:
        return _set(
            {"name": self.name, "description": self.description, "trigger_type": self.trigger_type,
             "is_enabled": self.is_enabled},
            trigger_config=self.trigger_config.to_dict() if self.trigger_config else None,
            dag_config=self.dag_config.to_dict(), node_id=self.node_id or None,
        )
