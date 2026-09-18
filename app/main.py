"""冷链温度事件分析 API。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query

from . import database as db
from .events import Reading, Rule, RuleVersion, detect_versioned_events
from .schemas import (
    DeviceRule,
    DeviceRuleIn,
    DeviceRuleVersionOut,
    EventOut,
    IngestResult,
    ReadingBatchIn,
    ReadingOut,
    to_utc,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="Cold Chain Temperature Event API",
    version="1.1.0",
    description=(
        "接收设备按时间上报的温度读数，按每个设备配置的上下限与容忍时长"
        "识别连续超温事件，返回事件起止时间、峰值与累计超限时长。"
        "规则支持多版本：更新时可指定 effective_at，查询事件时对每条读数"
        "应用当时已生效的规则版本，版本切换点强制结束旧事件。"
    ),
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ---------- 设备规则配置 ----------

@app.put("/api/v1/devices/{device_id}/rule", response_model=DeviceRule)
def put_device_rule(device_id: str, rule: DeviceRuleIn):
    """创建或更新设备的温度规则（上下限、容忍时长、最大间隔）。

    请求体携带 effective_at 时创建一个在该时间生效的新版本；不传则立即生效。
    显式指定的生效时间早于该设备最新版本的生效时间时返回 409。
    """
    if rule.effective_at is not None:
        try:
            return db.add_rule_version(
                device_id,
                rule.upper_limit,
                rule.lower_limit,
                rule.tolerance_seconds,
                rule.max_gap_seconds,
                rule.effective_at,
                enforce_order=True,
            )
        except db.RuleOrderingError as exc:
            raise HTTPException(409, str(exc)) from exc

    # 未传 effective_at：立即生效，不做倒序拦截，保持旧的更新方式始终可用
    try:
        return db.add_rule_version(
            device_id,
            rule.upper_limit,
            rule.lower_limit,
            rule.tolerance_seconds,
            rule.max_gap_seconds,
            _immediate_effective_at(device_id),
            enforce_order=False,
        )
    except db.RuleOrderingError as exc:  # 立即生效路径不拦截，理论上不会到达
        raise HTTPException(409, str(exc)) from exc


def _immediate_effective_at(device_id: str) -> datetime:
    """立即生效更新的生效时间。

    设备首个版本使用 UTC 起点作为生效时间，使首个版本对全部历史读数生效
    （兼容版本机制上线前的行为）；后续版本使用当前时间立即生效。
    """
    if db.list_rule_versions(device_id):
        return datetime.now(timezone.utc)
    return datetime(1, 1, 1, tzinfo=timezone.utc)


@app.get("/api/v1/devices", response_model=list[DeviceRule])
def list_devices():
    """列出全部已配置规则的设备（含当前最新版本信息）。"""
    return db.list_rules()


@app.get("/api/v1/devices/{device_id}/rule", response_model=DeviceRule)
def get_device_rule(device_id: str):
    """查询单个设备当前最新的规则版本。"""
    rule = db.get_rule(device_id)
    if rule is None:
        raise HTTPException(404, f"设备 {device_id!r} 尚未配置规则")
    return rule


@app.get(
    "/api/v1/devices/{device_id}/rule/versions",
    response_model=list[DeviceRuleVersionOut],
)
def list_device_rule_versions(device_id: str):
    """按生效时间升序列出设备的全部规则版本。"""
    versions = db.list_rule_versions(device_id)
    if not versions:
        raise HTTPException(404, f"设备 {device_id!r} 尚未配置规则")
    return versions


@app.delete("/api/v1/devices/{device_id}")
def delete_device(device_id: str):
    """删除设备的全部规则版本及其全部温度读数。"""
    if not db.delete_device(device_id):
        raise HTTPException(404, f"设备 {device_id!r} 不存在")
    return {"deleted": device_id}


# ---------- 温度读数上报 ----------

@app.post("/api/v1/readings", response_model=IngestResult, status_code=201)
def ingest_readings(batch: ReadingBatchIn):
    """批量上报温度读数（同一 device_id + timestamp 重复上报会覆盖旧值）。"""
    rows = [
        (r.device_id, r.timestamp.isoformat(), r.temperature)
        for r in batch.readings
    ]
    return IngestResult(inserted=db.insert_readings(rows))


@app.get("/api/v1/devices/{device_id}/readings", response_model=list[ReadingOut])
def list_readings(
    device_id: str,
    start: datetime | None = Query(None, description="起始时间（含），ISO 8601"),
    end: datetime | None = Query(None, description="结束时间（含），ISO 8601"),
    limit: int = Query(1000, ge=1, le=100000),
):
    """按时间范围查询原始读数。"""
    return db.fetch_readings_window(
        device_id,
        to_utc(start).isoformat() if start else None,
        to_utc(end).isoformat() if end else None,
        limit,
    )


# ---------- 事件查询 ----------

def _events_for_device(device_id: str,
                       start: datetime | None,
                       end: datetime | None) -> list[EventOut]:
    version_rows = db.list_rule_versions(device_id)
    if not version_rows:
        raise HTTPException(404, f"设备 {device_id!r} 尚未配置规则，无法分析事件")
    versions = [
        RuleVersion(
            version=row["version"],
            effective_at=to_utc(datetime.fromisoformat(row["effective_at"])),
            rule=Rule(
                upper_limit=row["upper_limit"],
                lower_limit=row["lower_limit"],
                tolerance_seconds=row["tolerance_seconds"],
                max_gap_seconds=row["max_gap_seconds"],
            ),
        )
        for row in version_rows
    ]
    end_iso = to_utc(end).isoformat() if end else None
    rows = db.fetch_readings(device_id, end_iso)
    readings = [
        Reading(ts=to_utc(datetime.fromisoformat(r["ts"])), temperature=r["temperature"])
        for r in rows
    ]
    events = detect_versioned_events(readings, versions)

    start_utc = to_utc(start) if start else None
    end_utc = to_utc(end) if end else None
    result = []
    for ev in events:
        # 只返回与查询区间 [start, end] 有交集的事件
        if start_utc is not None and ev["end_time"] < start_utc:
            continue
        if end_utc is not None and ev["start_time"] > end_utc:
            continue
        result.append(EventOut(device_id=device_id, **ev))
    return result


@app.get("/api/v1/devices/{device_id}/events", response_model=list[EventOut])
def device_events(
    device_id: str,
    start: datetime | None = Query(None, description="起始时间（含），ISO 8601"),
    end: datetime | None = Query(None, description="结束时间（含），ISO 8601"),
):
    """查询单个设备在指定时间范围内的超温事件。"""
    return _events_for_device(device_id, start, end)


@app.get("/api/v1/events", response_model=list[EventOut])
def all_events(
    start: datetime | None = Query(None, description="起始时间（含），ISO 8601"),
    end: datetime | None = Query(None, description="结束时间（含），ISO 8601"),
):
    """查询全部已配置设备在指定时间范围内的超温事件。"""
    result: list[EventOut] = []
    for rule in db.list_rules():
        result.extend(_events_for_device(rule["device_id"], start, end))
    result.sort(key=lambda e: (e.start_time, e.device_id))
    return result
