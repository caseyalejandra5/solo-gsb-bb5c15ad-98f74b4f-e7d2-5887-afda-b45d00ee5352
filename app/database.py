"""SQLite 存储层（标准库 sqlite3，按请求建连接，线程安全）。"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", "coldchain.db")

# 设备首个规则版本（未指定 effective_at 时）的生效时间：
# 取 UTC 起点，保证早于任何真实业务时间，即首个版本对全部历史读数生效，
# 与版本机制上线前“更新规则立即对历史读数生效”的旧行为保持一致。
BASELINE_EFFECTIVE_AT = datetime(1, 1, 1, tzinfo=timezone.utc).isoformat()

SCHEMA = """
CREATE TABLE IF NOT EXISTS device_rule_versions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id           TEXT NOT NULL,
    version             INTEGER NOT NULL,
    upper_limit         REAL NOT NULL,
    lower_limit         REAL NOT NULL,
    tolerance_seconds   INTEGER NOT NULL DEFAULT 300,
    max_gap_seconds     INTEGER NOT NULL DEFAULT 1800,
    effective_at        TEXT NOT NULL,          -- ISO 8601, 统一存 UTC
    created_at          TEXT NOT NULL,
    UNIQUE (device_id, version),
    UNIQUE (device_id, effective_at)
);

CREATE INDEX IF NOT EXISTS idx_rule_versions_device
    ON device_rule_versions (device_id, version);

CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL,
    ts          TEXT NOT NULL,          -- ISO 8601, 统一存 UTC
    temperature REAL NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE (device_id, ts)              -- 同一时间重复上报按最新覆盖，保证幂等
);

CREATE INDEX IF NOT EXISTS idx_readings_device_ts ON readings (device_id, ts);

CREATE TABLE IF NOT EXISTS device_outage_windows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL,
    start_ts    TEXT NOT NULL,          -- ISO 8601, 统一存 UTC，半开区间 [start_ts, end_ts)
    end_ts      TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outage_windows_device_ts
    ON device_outage_windows (device_id, start_ts, end_ts);
"""


class RuleOrderingError(ValueError):
    """新规则版本的生效时间早于该设备最新版本的生效时间（倒序写入）。"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    if DB_PATH != ":memory:":
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(SCHEMA)
        _migrate_legacy_rules(conn)


def _migrate_legacy_rules(conn: sqlite3.Connection) -> None:
    """把版本机制上线前的单行 device_rules 表迁移为各设备的首个版本。"""
    cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(device_rules)").fetchall()
    }
    if not cols:
        return
    now = utcnow_iso()
    legacy = conn.execute("SELECT * FROM device_rules").fetchall()
    for row in legacy:
        exists = conn.execute(
            "SELECT 1 FROM device_rule_versions WHERE device_id = ?",
            (row["device_id"],),
        ).fetchone()
        if exists:
            continue
        version = _next_version(conn, row["device_id"])
        conn.execute(
            """
            INSERT INTO device_rule_versions
                (device_id, version, upper_limit, lower_limit,
                 tolerance_seconds, max_gap_seconds, effective_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["device_id"], version, row["upper_limit"], row["lower_limit"],
                row["tolerance_seconds"], row["max_gap_seconds"],
                BASELINE_EFFECTIVE_AT, now,
            ),
        )


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_version(conn: sqlite3.Connection, device_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 AS next_version "
        "FROM device_rule_versions WHERE device_id = ?",
        (device_id,),
    ).fetchone()
    return int(row["next_version"])


# ---------- 设备规则版本 ----------

def add_rule_version(device_id: str, upper: float, lower: float,
                     tolerance: int, max_gap: int,
                     effective_at: datetime, *,
                     enforce_order: bool = True) -> dict:
    """为设备追加一个规则版本。

    effective_at 必须是 UTC datetime。enforce_order=True 时，生效时间若
    不晚于该设备最新版本的生效时间，抛出 RuleOrderingError（拒绝倒序写入）。
    enforce_order=False 用于“立即生效”的旧式更新：始终写入，
    若同一时刻已有版本则向后顺延 1 微秒以避开唯一约束。
    """
    effective_dt = effective_at.astimezone(timezone.utc)
    now = utcnow_iso()
    with _connect() as conn:
        latest = conn.execute(
            """
            SELECT version, effective_at FROM device_rule_versions
            WHERE device_id = ?
            ORDER BY effective_at DESC, version DESC
            LIMIT 1
            """,
            (device_id,),
        ).fetchone()
        latest_iso = latest["effective_at"] if latest is not None else None
        effective_iso = effective_dt.isoformat()
        if enforce_order and latest_iso is not None and effective_iso <= latest_iso:
            raise RuleOrderingError(
                f"设备 {device_id!r} 最新规则版本自 {latest_iso} 起生效，"
                f"不允许写入生效时间不晚于它（{effective_iso}）的版本"
            )
        if not enforce_order:
            # 立即生效不做倒序拦截，保证旧的更新方式始终可用；
            # 仅在同一时刻已有版本（同一微秒内连续更新）时顺延 1 微秒
            while _effective_at_exists(conn, device_id, effective_iso):
                effective_dt = effective_dt + timedelta(microseconds=1)
                effective_iso = effective_dt.isoformat()
        version = _next_version(conn, device_id)
        conn.execute(
            """
            INSERT INTO device_rule_versions
                (device_id, version, upper_limit, lower_limit,
                 tolerance_seconds, max_gap_seconds, effective_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (device_id, version, upper, lower, tolerance, max_gap,
             effective_iso, now),
        )
    return get_rule(device_id)


def _effective_at_exists(conn: sqlite3.Connection, device_id: str,
                         effective_iso: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM device_rule_versions "
        "WHERE device_id = ? AND effective_at = ?",
        (device_id, effective_iso),
    ).fetchone() is not None


def _row_to_rule(row: sqlite3.Row) -> dict:
    d = dict(row)
    written_at = d.pop("written_at")
    # 版本一经写入不再变更：created_at / updated_at 都取写入时间
    d["created_at"] = written_at
    d["updated_at"] = written_at
    return d


_LATEST_SELECT = """
    SELECT device_id, version, upper_limit, lower_limit, tolerance_seconds,
           max_gap_seconds, effective_at, created_at AS written_at
    FROM device_rule_versions
"""


def get_rule(device_id: str) -> dict | None:
    """返回设备当前最新的规则版本（版本号最大者）。"""
    with _connect() as conn:
        row = conn.execute(
            _LATEST_SELECT
            + " WHERE device_id = ? ORDER BY version DESC LIMIT 1",
            (device_id,),
        ).fetchone()
    return _row_to_rule(row) if row else None


def list_rule_versions(device_id: str) -> list[dict]:
    """按生效时间升序列出设备的全部规则版本。"""
    with _connect() as conn:
        rows = conn.execute(
            _LATEST_SELECT
            + " WHERE device_id = ? ORDER BY effective_at ASC, version ASC",
            (device_id,),
        ).fetchall()
    return [_row_to_rule(r) for r in rows]


def list_rules() -> list[dict]:
    """列出全部设备当前最新的规则版本。"""
    with _connect() as conn:
        rows = conn.execute(
            _LATEST_SELECT
            + """
            WHERE version = (
                SELECT MAX(version) FROM device_rule_versions AS v2
                WHERE v2.device_id = device_rule_versions.device_id
            )
            ORDER BY device_id
            """
        ).fetchall()
    return [_row_to_rule(r) for r in rows]


def delete_device(device_id: str) -> bool:
    """删除设备的全部规则版本、停运窗口及其全部读数，返回设备是否存在过。"""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM device_rule_versions WHERE device_id = ?", (device_id,)
        )
        conn.execute("DELETE FROM readings WHERE device_id = ?", (device_id,))
        conn.execute(
            "DELETE FROM device_outage_windows WHERE device_id = ?", (device_id,)
        )
        return cur.rowcount > 0


# ---------- 温度读数 ----------

def insert_readings(readings: list[tuple[str, str, float]]) -> int:
    """readings: [(device_id, ts_iso, temperature), ...]，返回写入条数。"""
    now = utcnow_iso()
    with _connect() as conn:
        conn.executemany(
            """
            INSERT INTO readings (device_id, ts, temperature, received_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(device_id, ts) DO UPDATE SET
                temperature=excluded.temperature,
                received_at=excluded.received_at
            """,
            [(d, ts, t, now) for d, ts, t in readings],
        )
    return len(readings)


def fetch_readings(device_id: str, end_iso: str | None = None) -> list[dict]:
    """取某设备截至 end（含）的全部读数，按时间升序。"""
    sql = "SELECT device_id, ts, temperature FROM readings WHERE device_id = ?"
    params: list = [device_id]
    if end_iso is not None:
        sql += " AND ts <= ?"
        params.append(end_iso)
    sql += " ORDER BY ts"
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def fetch_readings_window(device_id: str, start_iso: str | None,
                          end_iso: str | None, limit: int) -> list[dict]:
    sql = "SELECT device_id, ts, temperature, received_at FROM readings WHERE device_id = ?"
    params: list = [device_id]
    if start_iso is not None:
        sql += " AND ts >= ?"
        params.append(start_iso)
    if end_iso is not None:
        sql += " AND ts <= ?"
        params.append(end_iso)
    sql += " ORDER BY ts LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def list_device_ids_with_readings() -> list[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT DISTINCT device_id FROM readings").fetchall()
    return [r["device_id"] for r in rows]


# ---------- 计划停运窗口 ----------

def _merge_window_rows(rows: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """合并 (start_iso, end_iso, note) 列表中重叠或首尾相接的区间。

    半开区间 [a_start, a_end) 与 [b_start, b_end)（a_start <= b_start）
    在 b_start <= a_end 时合并；合并行的备注去重后用 "; " 连接。
    """
    ordered = sorted(rows, key=lambda x: x[0])
    merged: list[tuple[str, str, str]] = []
    for start_iso, end_iso, note in ordered:
        if merged and start_iso <= merged[-1][1]:
            prev_start, prev_end, prev_note = merged[-1]
            end_iso = max(prev_end, end_iso)
            notes = [n for n in prev_note.split("; ") if n]
            if note and note not in notes:
                notes.append(note)
            merged[-1] = (prev_start, end_iso, "; ".join(notes))
        else:
            merged.append((start_iso, end_iso, note))
    return merged


def add_outage_windows(
    windows: list[tuple[str, datetime, datetime, str]],
) -> list[dict]:
    """批量创建设备计划停运窗口，按设备持久化为合并后的区间。

    windows: [(device_id, start, end, note), ...]，start/end 为 UTC datetime，
    窗口使用半开区间 [start, end)。同一设备重叠或首尾相接的窗口（包括与库中
    既有窗口）合并为一个区间；不同设备的窗口互不影响。
    返回受影响设备最终持久化的全部窗口（按设备、开始时间升序）。
    """
    by_device: dict[str, list[tuple[str, str, str]]] = {}
    for device_id, start, end, note in windows:
        start_iso = start.astimezone(timezone.utc).isoformat()
        end_iso = end.astimezone(timezone.utc).isoformat()
        by_device.setdefault(device_id, []).append((start_iso, end_iso, note))

    affected = sorted(by_device)
    now = utcnow_iso()
    with _connect() as conn:
        for device_id in affected:
            new_rows = _merge_window_rows(by_device[device_id])
            lo, hi = new_rows[0][0], new_rows[-1][1]
            # 定点扩展：合并既有窗口后可能继续触碰到更外侧的既有窗口，
            # 反复扩大选择范围直到合并结果不再变化
            merged = new_rows
            while True:
                existing = conn.execute(
                    """
                    SELECT start_ts, end_ts, note FROM device_outage_windows
                    WHERE device_id = ? AND start_ts <= ? AND end_ts >= ?
                    ORDER BY start_ts
                    """,
                    (device_id, hi, lo),
                ).fetchall()
                candidate = _merge_window_rows(
                    new_rows
                    + [(r["start_ts"], r["end_ts"], r["note"]) for r in existing]
                )
                next_lo, next_hi = candidate[0][0], candidate[-1][1]
                if (next_lo, next_hi) == (lo, hi):
                    merged = candidate
                    break
                lo, hi = next_lo, next_hi
            conn.execute(
                """
                DELETE FROM device_outage_windows
                WHERE device_id = ? AND start_ts <= ? AND end_ts >= ?
                """,
                (device_id, hi, lo),
            )
            conn.executemany(
                """
                INSERT INTO device_outage_windows
                    (device_id, start_ts, end_ts, note, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [(device_id, s, e, n, now) for s, e, n in merged],
            )
        return _fetch_outage_windows(conn, affected)


def _fetch_outage_windows(
    conn: sqlite3.Connection, device_ids: list[str]
) -> list[dict]:
    if not device_ids:
        return []
    placeholders = ", ".join("?" for _ in device_ids)
    rows = conn.execute(
        f"""
        SELECT id, device_id, start_ts AS start, end_ts AS end, note, created_at
        FROM device_outage_windows
        WHERE device_id IN ({placeholders})
        ORDER BY device_id, start_ts
        """,
        device_ids,
    ).fetchall()
    return [dict(r) for r in rows]


def list_outage_windows(
    device_id: str | None = None,
    start_iso: str | None = None,
    end_iso: str | None = None,
) -> list[dict]:
    """查询停运窗口（可按设备、时间范围过滤），按设备、开始时间升序。

    时间范围按半开重叠匹配：窗口与查询区间 [start, end) 有交集即返回
    （窗口 start < 查询 end 且 窗口 end > 查询 start）。
    """
    sql = (
        "SELECT id, device_id, start_ts AS start, end_ts AS end, note, created_at "
        "FROM device_outage_windows WHERE 1=1"
    )
    params: list = []
    if device_id is not None:
        sql += " AND device_id = ?"
        params.append(device_id)
    if start_iso is not None:
        sql += " AND end_ts > ?"
        params.append(start_iso)
    if end_iso is not None:
        sql += " AND start_ts < ?"
        params.append(end_iso)
    sql += " ORDER BY device_id, start_ts"
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def fetch_outage_windows(device_id: str) -> list[tuple[str, str]]:
    """取某设备的全部停运窗口 [(start_iso, end_iso), ...]，按开始时间升序。"""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT start_ts, end_ts FROM device_outage_windows
            WHERE device_id = ? ORDER BY start_ts
            """,
            (device_id,),
        ).fetchall()
    return [(r["start_ts"], r["end_ts"]) for r in rows]
