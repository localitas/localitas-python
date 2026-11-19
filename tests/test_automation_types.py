"""Typed automation requests build exactly core's wire JSON.

fixtures/sdk_automation_request.json must stay identical to core's
core/go/internal/apps/automation/testdata/sdk_automation_request.json, which
core's strict decoder round-trips."""

import json
import unittest
from pathlib import Path

from localitas_client import LocalitasClient
from localitas_client.automation import (
    AppCall, CreateAutomationRequest, DAGConfig, DAGNode, EventTrigger, HTTPSettings, PeriodicTrigger, Sensor,
    TriggerConfig, event_sensor, file_sensor, http_node, pubsub_sensor, sensor_node, time_sensor,
)
from tests.local_core import LocalCore

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sdk_automation_request.json"


def nightly_sync() -> CreateAutomationRequest:
    description = "Sync, wait for its follow-ups, report"
    return CreateAutomationRequest(
        name="nightly-sync",
        description=description,
        trigger_type="periodic",
        is_enabled=True,
        trigger_config=TriggerConfig(
            periodic=PeriodicTrigger("0 3 * * *", timezone="Europe/Paris", catchup="all", catchup_window="48h"),
            retries=1, retry_delay="5m", max_active_runs=2, sla="2h",
        ),
        dag_config=DAGConfig("nightly-sync", "nightly-sync", description=description, nodes=[
            http_node("fetch", "any_node",
                      HTTPSettings(app=AppCall("weather", "/api/sync"), method="POST", deferrable=False),
                      retries=2, retry_delay="30s", pool="net", pool_slots=2, trigger_rule="all_success"),
            sensor_node("wait_event", "any_node", event_sensor("invoice.paid", timeout="1h"),
                        dependencies=["fetch"], depends_on_past=True, trigger_rule="all_done"),
            sensor_node("wait_time", "any_node", time_sensor("15m"), dependencies=["fetch"]),
            sensor_node("wait_file", "all_nodes", file_sensor(["/Documents/inbox"]), dependencies=["fetch"]),
            http_node("report", "any_node", HTTPSettings(url="https://example.com/hook", body={"ok": True}),
                      dependencies=["wait_event", "wait_time", "wait_file"], trigger_rule="one_success"),
        ]),
    )


class TestWireShape(unittest.TestCase):
    def test_builders_produce_the_golden_fixture(self):
        self.assertEqual(nightly_sync().to_dict(), json.loads(FIXTURE.read_text()))

    def test_create_posts_the_request(self):
        core = LocalCore({"POST /apps/automation/api/automations": (201, {"id": "a1", "name": "nightly-sync"})})
        self.addCleanup(core.close)
        created = LocalitasClient(core.url, "lt_app").automation().create(nightly_sync())
        self.assertEqual(created, {"id": "a1", "name": "nightly-sync"})
        call = core.next_call()
        self.assertEqual(call.body, json.loads(FIXTURE.read_text()))
        self.assertEqual(call.headers["authorization"], "Bearer lt_app")

    def test_priority_weight_is_sent_only_when_set(self):
        settings = HTTPSettings(url="/apps/automation/api/events")
        self.assertEqual(http_node("a", "raft_leader", settings, priority_weight=5).to_dict()["priority_weight"], 5)
        self.assertEqual(http_node("a", "raft_leader", settings).to_dict(), {
            "node_id": "a", "node_type": "http-api", "execution_strategy": "raft_leader",
            "metadata": {"url": "/apps/automation/api/events"},
        })

    def test_pubsub_sensor_shape(self):
        node = sensor_node("paid", "any_node", pubsub_sensor(
            "orders", match={"status": "paid", "order.total": 5}, cache="shop", timeout="10m"))
        self.assertEqual(node.to_dict()["metadata"], {
            "wait_for": "pubsub", "channel": "orders", "cache": "shop",
            "match": {"status": "paid", "order.total": 5}, "timeout": "10m",
        })
        self.assertEqual(pubsub_sensor("orders").to_dict(), {"wait_for": "pubsub", "channel": "orders"})

    def test_event_trigger_and_node_placement(self):
        req = CreateAutomationRequest(
            name="scan", trigger_type="event", node_id="node-2",
            trigger_config=TriggerConfig(event=EventTrigger("file.created", paths=["/Photos"], debounce_ms=500)),
            dag_config=DAGConfig("scan", "scan", nodes=[
                http_node("scan", "local_only", HTTPSettings(app=AppCall("antivirus", "/api/scan")))]),
        )
        body = req.to_dict()
        self.assertEqual(body["node_id"], "node-2")
        self.assertEqual(body["trigger_config"], {"event": {"event_type": "file.created", "debounce_ms": 500, "paths": ["/Photos"]}})
        self.assertEqual((body["description"], body["is_enabled"]), ("", True))


class TestValidation(unittest.TestCase):
    def test_refuses_bad_values_at_build_time(self):
        url = HTTPSettings(url="/x")
        cases = {
            "strategy": lambda: http_node("a", "everywhere", url),
            "trigger rule": lambda: http_node("a", "any_node", url, trigger_rule="always"),
            "retries above 10": lambda: http_node("a", "any_node", url, retries=11),
            "negative retries": lambda: http_node("a", "any_node", url, retries=-1),
            "retry delay": lambda: http_node("a", "any_node", url, retry_delay="30 sec"),
            "pool slots": lambda: http_node("a", "any_node", url, pool_slots=0),
            "priority weight": lambda: http_node("a", "any_node", url, priority_weight="high"),
            "empty node id": lambda: DAGNode("", "http-api"),
            "wait_for": lambda: Sensor("webhook"),
            "time sensor delay": lambda: time_sensor("soon"),
            "sensor timeout": lambda: event_sensor("x", timeout="1 hour"),
            "event sensor type": lambda: event_sensor(""),
            "file sensor paths": lambda: file_sensor([]),
            "pubsub channel": lambda: pubsub_sensor(""),
            "pubsub match": lambda: pubsub_sensor("c", match=["status"]),
            "catchup": lambda: PeriodicTrigger("0 3 * * *", catchup="some"),
            "catchup window": lambda: PeriodicTrigger("0 3 * * *", catchup_window="2 days"),
            "sla": lambda: TriggerConfig(sla="fast"),
            "trigger retry delay": lambda: TriggerConfig(retry_delay="1d"),
            "neither url nor app": lambda: HTTPSettings(),
            "both url and app": lambda: HTTPSettings(url="/x", app=AppCall("weather", "/api")),
            "app name": lambda: AppCall("Weather", "/api"),
            "app path": lambda: AppCall("weather", "api"),
            "app protocol": lambda: HTTPSettings(app=AppCall("weather", "/api", protocol="grpc")),
        }
        for name, build in cases.items():
            with self.subTest(name), self.assertRaises((ValueError, TypeError)):
                build()

    def test_accepts_go_durations(self):
        for d in ("30s", "5m", "1h30m", "1.5h", "300ms", "10us", "10µs", "2ns"):
            with self.subTest(d):
                time_sensor(d)


if __name__ == "__main__":
    unittest.main()
