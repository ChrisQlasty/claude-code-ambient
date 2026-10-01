"""The daemon's REST API, against a recording player instead of real devices."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ambient import hooks_install, log
from ambient.config import DeviceConfig, load_raw
from ambient.daemon import server
from ambient.daemon.queue import DeviceQueues
from ambient.drivers.base import DiscoveredDevice
from ambient.drivers.nanoleaf import NanoleafDriver
from ambient.effects import AnyEffect

CONFIG = """\
version: 1
devices:
  desk: {driver: console}  # keep me
events:
  Stop:
    - device: desk
      effect: {type: flash, color: '#00ff60', times: 2}
"""


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, DeviceConfig, list[AnyEffect]]] = []

    def __call__(self, device_id: str, config: DeviceConfig, effects: list[AnyEffect]) -> bool:
        self.calls.append((device_id, config, effects))
        return True


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG)
    return path


@pytest.fixture
def player() -> Recorder:
    return Recorder()


@pytest.fixture
def queues(player: Recorder) -> Iterator[DeviceQueues]:
    queues = DeviceQueues(player)
    yield queues
    queues.close(timeout=5)


@pytest.fixture
def client(queues: DeviceQueues, config_path: Path, tmp_path: Path) -> TestClient:
    app = server.create_app(
        queues, config_path, settings_path=tmp_path / "settings.json", web_dir=tmp_path / "web"
    )
    return TestClient(app, base_url="http://127.0.0.1:8765")


def _drain(queues: DeviceQueues) -> None:
    queues.close(timeout=5)


def test_status_reports_hooks(client: TestClient, tmp_path: Path) -> None:
    command = hooks_install.fire_command(Path("/bin/ambient"))
    hooks_install.apply(hooks_install.plan_install(tmp_path / "settings.json", command))
    body = client.get("/api/status").json()
    assert body["hooks"]["installed"] == ["UserPromptSubmit", "Stop"]
    assert body["hooks"]["handled"] == ["UserPromptSubmit", "Stop"]


def test_status_with_broken_settings(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text("{nope")
    assert client.get("/api/status").json()["hooks"]["installed"] is None


def test_drivers(client: TestClient) -> None:
    drivers = {d["name"]: d for d in client.get("/api/drivers").json()}
    assert drivers["nanoleaf"]["pairable"] is True
    assert drivers["console"]["pairable"] is False
    assert "color" in drivers["console"]["capabilities"]


def test_devices_with_status(client: TestClient, queues: DeviceQueues) -> None:
    assert client.get("/api/devices").json() == [
        {"name": "desk", "driver": "console", "status": None}
    ]
    client.post("/api/devices/desk/test", json={"effect": {"type": "solid", "color": "#f00"}})
    _drain(queues)
    [device] = client.get("/api/devices").json()
    assert device["status"]["last_result"] == "ok"


def test_preview_uses_unsaved_devices(
    client: TestClient, queues: DeviceQueues, player: Recorder
) -> None:
    response = client.post(
        "/api/preview",
        json={
            "device": ["desk", "draft"],
            "effect": {"type": "pulse", "color": "#123456", "times": 2},
            "devices": {"draft": {"driver": "console", "baseline": {"color": "#fff"}}},
        },
    )
    assert response.status_code == 202
    _drain(queues)
    assert sorted(device for device, _, _ in player.calls) == ["desk", "draft"]
    draft = next(config for device, config, _ in player.calls if device == "draft")
    assert draft.baseline is not None


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"device": ["nope"], "effect": {"type": "solid", "color": "#f00"}}, "unknown device"),
        (
            {
                "device": ["x"],
                "effect": {"type": "solid", "color": "#f00"},
                "devices": {"x": {"driver": "nope"}},
            },
            "unknown driver",
        ),
    ],
)
def test_preview_errors(client: TestClient, body: dict[str, Any], detail: str) -> None:
    response = client.post("/api/preview", json=body)
    assert response.status_code == 422
    assert detail in response.json()["detail"]


def test_preview_validates_effects(client: TestClient) -> None:
    response = client.post(
        "/api/preview", json={"device": ["desk"], "effect": {"type": "flash", "color": "red"}}
    )
    assert response.status_code == 422


def test_simulate(client: TestClient, queues: DeviceQueues, player: Recorder) -> None:
    assert client.post("/api/events/Stop/simulate", json={}).json() == {"queued": ["desk"]}
    assert client.post("/api/events/Notification/simulate", json={}).json() == {"queued": []}
    assert client.post("/api/events/Nope/simulate", json={}).status_code == 422
    _drain(queues)
    [(_, _, effects)] = player.calls
    assert effects[0].type == "flash"


def test_get_config_as_written(client: TestClient, config_path: Path) -> None:
    body = client.get("/api/config").json()
    assert body["error"] is None
    assert body["config"]["events"]["Stop"][0]["device"] == "desk"
    config_path.write_text("devices: [")
    body = client.get("/api/config").json()
    assert body["config"] is None
    assert "invalid YAML" in body["error"]


def test_invalid_config_blocks_device_actions(client: TestClient, config_path: Path) -> None:
    config_path.write_text("devices: {a: {}}")
    assert client.get("/api/devices").status_code == 409


def test_put_config_saves_and_keeps_comments(client: TestClient, config_path: Path) -> None:
    config = load_raw(config_path)
    config["events"]["Stop"][0]["effect"]["color"] = "#ff0000"
    response = client.put("/api/config", json={"config": config})
    assert response.status_code == 200
    assert response.json()["backup"].endswith("config.yaml.bak")
    assert "# keep me" in config_path.read_text()
    assert load_raw(config_path)["events"]["Stop"][0]["effect"]["color"] == "#ff0000"


@pytest.mark.parametrize(
    ("config", "detail"),
    [
        ({"devices": {"a": {"driver": "nope"}}}, "unknown driver"),
        ({"events": {"Stop": [{"device": "x", "effect": {"color": "#f00"}}]}}, "Stop"),
        ({"version": 2}, "version"),
    ],
)
def test_put_config_rejects_invalid(
    client: TestClient, config_path: Path, config: dict[str, Any], detail: str
) -> None:
    response = client.put("/api/config", json={"config": config})
    assert response.status_code == 422
    assert detail in json.dumps(response.json())
    assert config_path.read_text() == CONFIG


def test_schema(client: TestClient) -> None:
    assert "devices" in client.get("/api/schema").json()["properties"]


def test_logs(client: TestClient) -> None:
    log.setup().info("hello from the test")
    assert "hello from the test" in client.get("/api/logs?lines=5").json()["text"]


def test_discover(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    found = [DiscoveredDevice("nanoleaf", "Lines", "10.0.0.2", 16021, {"id": "x"})]
    monkeypatch.setattr(NanoleafDriver, "discover", classmethod(lambda cls, timeout: found))
    response = client.post("/api/discover", json={"driver": "nanoleaf"})
    assert response.json() == [
        {"driver": "nanoleaf", "name": "Lines", "host": "10.0.0.2", "port": 16021, "id": "x"}
    ]
    assert client.post("/api/discover", json={"driver": "nope"}).status_code == 422


def test_pair(client: TestClient, config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(NanoleafDriver, "pair", lambda self, timeout: {"token": "secret"})
    response = client.post(
        "/api/devices/lines/pair", json={"driver": "nanoleaf", "host": "10.0.0.2"}
    )
    assert response.status_code == 200
    assert load_raw(config_path)["devices"]["lines"] == {"driver": "nanoleaf", "token": "secret"}
    response = client.post("/api/devices/desk/pair", json={})
    assert response.status_code == 400
    assert "doesn't need pairing" in response.json()["detail"]


@pytest.mark.parametrize(
    "headers",
    [{"Host": "evil.example"}, {"Origin": "http://evil.example"}, {"Host": "127.0.0.1.evil.com"}],
)
def test_rejects_non_local_requests(client: TestClient, headers: dict[str, str]) -> None:
    assert client.get("/api/status", headers=headers).status_code == 403


@pytest.mark.parametrize("host", ["localhost:8765", "127.0.0.1", "[::1]:8765"])
def test_accepts_loopback_hosts(client: TestClient, host: str) -> None:
    response = client.get("/api/status", headers={"Host": host, "Origin": f"http://{host}"})
    assert response.status_code == 200


def test_writes_must_be_json(client: TestClient) -> None:
    # A cross-site <form> can only send form encodings, never application/json.
    response = client.post(
        "/api/events/Stop/simulate",
        content="a=b",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 415


def test_serves_the_ui_when_built(queues: DeviceQueues, config_path: Path, tmp_path: Path) -> None:
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<h1>ambient</h1>")
    app = server.create_app(queues, config_path, web_dir=web)
    client = TestClient(app, base_url="http://127.0.0.1")
    assert client.get("/").text == "<h1>ambient</h1>"


def test_explains_a_missing_ui(client: TestClient) -> None:
    assert "isn't built" in client.get("/").text


def test_handle_event(config_path: Path, queues: DeviceQueues, player: Recorder) -> None:
    server.handle_event(config_path, queues, "Stop")
    config_path.write_text("devices: [")
    server.handle_event(config_path, queues, "Stop")  # logged, not raised
    server.handle_event(config_path.with_name("missing.yaml"), queues, "Stop")
    _drain(queues)
    assert [device for device, _, _ in player.calls] == ["desk"]
