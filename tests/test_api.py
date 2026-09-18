"""API 集成测试（每个用例使用独立的临时 SQLite 库）。"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import database
from app.main import app

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    with TestClient(app) as c:
        yield c


def _put_rule(client, device_id="truck-01", *, effective_at=None, **overrides):
    payload = {
        "upper_limit": 8.0,
        "lower_limit": 2.0,
        "tolerance_seconds": 300,
        "max_gap_seconds": 1800,
    }
    if effective_at is not None:
        payload["effective_at"] = effective_at
    payload.update(overrides)
    return client.put(f"/api/v1/devices/{device_id}/rule", json=payload)


def _send_readings(client, device_id, points):
    """points: [(分钟偏移, 温度), ...]，基准时间 2026-01-01T00:00:00Z"""
    readings = [
        {
            "device_id": device_id,
            "timestamp": (BASE + timedelta(minutes=m)).isoformat(),
            "temperature": t,
        }
        for m, t in points
    ]
    return client.post("/api/v1/readings", json={"readings": readings})


class TestRuleApi:
    def test_put_and_get_rule(self, client):
        resp = _put_rule(client, "truck-01")
        assert resp.status_code == 200
        body = resp.json()
        assert body["device_id"] == "truck-01"
        assert body["upper_limit"] == 8.0

        got = client.get("/api/v1/devices/truck-01/rule")
        assert got.status_code == 200
        assert got.json()["tolerance_seconds"] == 300

    def test_rule_validation(self, client):
        resp = _put_rule(client, "bad", upper_limit=1.0, lower_limit=5.0)
        assert resp.status_code == 422

    def test_get_missing_rule_404(self, client):
        assert client.get("/api/v1/devices/ghost/rule").status_code == 404

    def test_list_devices(self, client):
        _put_rule(client, "truck-01")
        _put_rule(client, "truck-02")
        devices = client.get("/api/v1/devices").json()
        ids = {d["device_id"] for d in devices}
        assert ids == {"truck-01", "truck-02"}

    def test_delete_device(self, client):
        _put_rule(client, "truck-01")
        _send_readings(client, "truck-01", [(0, 9.0)])
        assert client.delete("/api/v1/devices/truck-01").status_code == 200
        assert client.get("/api/v1/devices/truck-01/rule").status_code == 404
        assert client.get("/api/v1/devices/truck-01/readings").json() == []


class TestIngestAndEvents:
    def test_full_flow(self, client):
        _put_rule(client, "truck-01")
        resp = _send_readings(client, "truck-01", [
            (0, 5.0),
            (10, 9.0),    # 超限开始
            (20, 10.0),   # 峰值
            (24, 6.0),    # 短暂恢复 4 分钟（< 容忍 5 分钟）
            (28, 9.5),    # 再次超限 → 同一事件
            (40, 5.0),    # 恢复
            (200, 9.0),   # 间隔 160 分钟 > max_gap 30 分钟 → 新事件
            (210, 5.0),
        ])
        assert resp.status_code == 201
        assert resp.json()["inserted"] == 8

        events = client.get("/api/v1/devices/truck-01/events").json()
        assert len(events) == 2

        ev1 = events[0]
        assert ev1["start_time"].startswith("2026-01-01T00:10")
        assert ev1["end_time"].startswith("2026-01-01T00:40")
        assert ev1["event_type"] == "high"
        assert ev1["peak_temp"] == 10.0
        assert ev1["ongoing"] is False
        # 超限区间：10→20、20→24、28→40，共 10+4+12 = 26 分钟
        assert ev1["violation_seconds"] == 26 * 60

        ev2 = events[1]
        assert ev2["start_time"].startswith("2026-01-01T03:20")
        assert ev2["violation_seconds"] == 10 * 60

    def test_time_range_filter(self, client):
        _put_rule(client, "truck-01")
        _send_readings(client, "truck-01", [
            (0, 9.0),
            (10, 5.0),
            (100, 9.0),
            (110, 5.0),
        ])
        # 只查覆盖第二个事件的区间
        events = client.get(
            "/api/v1/devices/truck-01/events",
            params={"start": "2026-01-01T01:00:00Z", "end": "2026-01-01T02:00:00Z"},
        ).json()
        assert len(events) == 1
        assert events[0]["start_time"].startswith("2026-01-01T01:40")

    def test_events_require_rule(self, client):
        _send_readings(client, "no-rule-device", [(0, 9.0)])
        resp = client.get("/api/v1/devices/no-rule-device/events")
        assert resp.status_code == 404

    def test_duplicate_timestamp_is_idempotent(self, client):
        _put_rule(client, "truck-01")
        _send_readings(client, "truck-01", [(0, 9.0), (10, 5.0)])
        _send_readings(client, "truck-01", [(0, 9.0), (10, 5.0)])  # 重复上报
        readings = client.get("/api/v1/devices/truck-01/readings").json()
        assert len(readings) == 2

    def test_naive_timestamp_treated_as_utc(self, client):
        _put_rule(client, "truck-01")
        resp = client.post("/api/v1/readings", json={"readings": [
            {"device_id": "truck-01", "timestamp": "2026-01-01T00:00:00", "temperature": 9.0},
            {"device_id": "truck-01", "timestamp": "2026-01-01T00:10:00+00:00", "temperature": 5.0},
        ]})
        assert resp.status_code == 201
        events = client.get("/api/v1/devices/truck-01/events").json()
        assert len(events) == 1

    def test_all_devices_events(self, client):
        _put_rule(client, "truck-01")
        _put_rule(client, "truck-02")
        _send_readings(client, "truck-01", [(0, 9.0), (10, 5.0)])
        _send_readings(client, "truck-02", [(0, 1.0), (10, 5.0)])
        events = client.get("/api/v1/events").json()
        assert {e["device_id"] for e in events} == {"truck-01", "truck-02"}
        types = {e["device_id"]: e["event_type"] for e in events}
        assert types["truck-01"] == "high"
        assert types["truck-02"] == "low"


class TestRuleVersions:
    def test_immediate_update_creates_newer_version(self, client):
        first = _put_rule(client, "truck-01")
        assert first.status_code == 200
        assert first.json()["version"] == 1
        second = _put_rule(client, "truck-01", upper_limit=9.0)
        assert second.status_code == 200
        assert second.json()["version"] == 2
        assert second.json()["upper_limit"] == 9.0
        # GET rule 返回最新版本
        assert client.get("/api/v1/devices/truck-01/rule").json()["version"] == 2

    def test_scheduled_version_is_listed_in_effective_order(self, client):
        _put_rule(client, "truck-01")
        resp = _put_rule(
            client, "truck-01",
            upper_limit=10.0,
            effective_at=(BASE + timedelta(hours=2)).isoformat(),
        )
        assert resp.status_code == 200
        assert resp.json()["version"] == 2
        versions = client.get(
            "/api/v1/devices/truck-01/rule/versions"
        ).json()
        assert [v["version"] for v in versions] == [1, 2]
        assert versions[1]["effective_at"].startswith("2026-01-01T02:00:00")

    def test_versions_endpoint_404_for_unknown_device(self, client):
        assert client.get(
            "/api/v1/devices/ghost/rule/versions"
        ).status_code == 404

    def test_backdated_write_is_rejected_with_409(self, client):
        _put_rule(client, "truck-01")
        _put_rule(
            client, "truck-01", upper_limit=10.0,
            effective_at=(BASE + timedelta(hours=2)).isoformat(),
        )
        # 最新版本 02:00 生效，倒序写入 01:00 生效的版本 → 409
        resp = _put_rule(
            client, "truck-01", upper_limit=11.0,
            effective_at=(BASE + timedelta(hours=1)).isoformat(),
        )
        assert resp.status_code == 409
        # 拒绝后没有新增版本
        versions = client.get(
            "/api/v1/devices/truck-01/rule/versions"
        ).json()
        assert [v["version"] for v in versions] == [1, 2]

    def test_equal_effective_at_is_rejected(self, client):
        _put_rule(client, "truck-01")
        at = (BASE + timedelta(hours=2)).isoformat()
        assert _put_rule(client, "truck-01", upper_limit=10.0,
                         effective_at=at).status_code == 200
        # 同一生效时间不允许第二个版本
        resp = _put_rule(client, "truck-01", upper_limit=11.0,
                         effective_at=at)
        assert resp.status_code in (409, 422)

    def test_future_versions_do_not_apply_yet(self, client):
        _put_rule(client, "truck-01", upper_limit=8.0)
        # 2 小时后上限放宽到 100℃（即 2 小时后不再有超限）
        _put_rule(
            client, "truck-01", upper_limit=100.0,
            effective_at=(BASE + timedelta(hours=2)).isoformat(),
        )
        _send_readings(client, "truck-01", [
            (0, 9.0),     # v1 超限
            (10, 5.0),    # v1 恢复，事件闭合
            (130, 9.0),   # 已适用 v2：9.0 不超限 → 无新事件
        ])
        events = client.get("/api/v1/devices/truck-01/events").json()
        assert len(events) == 1
        ev = events[0]
        assert ev["rule_version"] == 1
        assert ev["end_time"].startswith("2026-01-01T00:10")

    def test_switch_point_forces_event_split(self, client):
        _put_rule(client, "truck-01", upper_limit=8.0)
        _put_rule(
            client, "truck-01", upper_limit=9.0,
            effective_at=(BASE + timedelta(minutes=20)).isoformat(),
        )
        _send_readings(client, "truck-01", [
            (0, 9.0),     # v1 超限（v1 上限 8）
            (10, 9.5),    # v1 持续超限
            (20, 9.5),    # 切换点：v2（上限 9）下仍超限 → 旧事件必须结束
            (30, 5.0),
        ])
        events = client.get("/api/v1/devices/truck-01/events").json()
        assert len(events) == 2
        assert [e["rule_version"] for e in events] == [1, 2]
        assert events[0]["end_time"].startswith("2026-01-01T00:10")
        assert events[1]["start_time"].startswith("2026-01-01T00:20")
        assert events[1]["end_time"].startswith("2026-01-01T00:30")
        assert events[0]["rule_effective_at"] != events[1]["rule_effective_at"]

    def test_events_within_time_window_use_versions(self, client):
        _put_rule(client, "truck-01", upper_limit=8.0)
        _put_rule(
            client, "truck-01", upper_limit=9.0,
            effective_at=(BASE + timedelta(minutes=20)).isoformat(),
        )
        _send_readings(client, "truck-01", [
            (0, 9.0),
            (10, 5.0),
            (20, 9.5),
            (30, 5.0),
        ])
        # 查询完全位于 v2 生效后的窗口，只返回 v2 事件
        events = client.get(
            "/api/v1/devices/truck-01/events",
            params={"start": (BASE + timedelta(minutes=15)).isoformat(),
                    "end": (BASE + timedelta(minutes=60)).isoformat()},
        ).json()
        assert len(events) == 1
        assert events[0]["rule_version"] == 2

