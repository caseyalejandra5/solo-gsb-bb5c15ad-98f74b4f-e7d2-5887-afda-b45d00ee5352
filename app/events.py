"""连续超温事件识别算法。

判定规则（按设备配置）：
- 温度 > upper_limit 或 < lower_limit 视为超限读数；
- 超限后若读数回到正常区间，但恢复正常持续的时长不超过
  tolerance_seconds 就又再次超限，则视为同一次事件（短暂恢复不切断）；
- 恢复正常超过 tolerance_seconds 后再超限，则开启新事件；
- 相邻两条读数（无论是否超限）的时间间隔超过 max_gap_seconds，
  视为数据中断，强制分段；
- 连续的超限读数（中间没有恢复正常）始终属于同一事件。

规则版本机制：
- 每条规则版本带一个生效时间 effective_at；读数只使用当时已生效的最新版本；
- 规则版本切换点必须切断旧事件：切换点两侧的读数分别独立识别，
  禁止跨版本合并（短暂恢复、容忍时长均不能跨越版本边界）。

时长口径（基于离散采样点的区间归属近似）：
- 事件起点 = 首个超限读数时间；
- 事件终点 = 最后一次超限之后首个恢复正常读数的时间；
  若之后没有读数（事件仍在持续），终点 = 最后超限读数时间且 ongoing=True；
- violation_seconds（累计超限时长）= 事件窗口内，凡左端点超限的
  相邻读数区间长度之和。
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Rule:
    upper_limit: float
    lower_limit: float
    tolerance_seconds: int = 300
    max_gap_seconds: int = 1800


@dataclass(frozen=True)
class Reading:
    ts: datetime
    temperature: float


@dataclass(frozen=True)
class RuleVersion:
    """一条规则版本：rule 自 effective_at（含）起生效。"""

    version: int
    effective_at: datetime
    rule: Rule


def is_violation(temperature: float, rule: Rule) -> bool:
    return temperature > rule.upper_limit or temperature < rule.lower_limit


def _deviation(temperature: float, rule: Rule) -> float:
    """超出限值的幅度（限值内为 0）。"""
    if temperature > rule.upper_limit:
        return temperature - rule.upper_limit
    if temperature < rule.lower_limit:
        return rule.lower_limit - temperature
    return 0.0


def detect_events(readings: list[Reading], rule: Rule) -> list[dict]:
    """对单个设备的历史读数做事件切分，返回按时间排序的事件列表。"""
    ordered = sorted(readings, key=lambda r: r.ts)
    events: list[dict] = []
    current: list[Reading] | None = None  # 当前未闭合事件窗口内的全部读数
    recovery_start: datetime | None = None  # 当前事件内恢复正常区间的起点
    prev_ts: datetime | None = None

    for r in ordered:
        # 数据间隔过长：强制切断当前事件
        if (
            current is not None
            and prev_ts is not None
            and (r.ts - prev_ts).total_seconds() > rule.max_gap_seconds
        ):
            events.append(_summarize(current, rule))
            current = None
            recovery_start = None

        if is_violation(r.temperature, rule):
            if current is None:
                current = [r]
            elif (
                recovery_start is not None
                and (r.ts - recovery_start).total_seconds() > rule.tolerance_seconds
            ):
                # 恢复正常已超过容忍时长，旧事件闭合、新事件开始
                events.append(_summarize(current, rule))
                current = [r]
            else:
                # 连续超限，或短暂恢复后再超限：同一事件延续
                current.append(r)
            recovery_start = None
        elif current is not None:
            # 事件窗口内的正常读数（短暂恢复或最终恢复），保留用于时长计算
            current.append(r)
            if recovery_start is None:
                recovery_start = r.ts

        prev_ts = r.ts

    if current:
        events.append(_summarize(current, rule))
    return events


def _summarize(window: list[Reading], rule: Rule) -> dict:
    """把一个事件窗口内的读数汇总为事件记录。window 已按时间排序。"""
    violating = [r for r in window if is_violation(r.temperature, rule)]
    start = violating[0].ts
    last_violation = violating[-1].ts

    # 终点：最后超限之后首个恢复正常的读数；没有则说明事件仍在持续
    end = last_violation
    ongoing = True
    for r in window:
        if r.ts > last_violation:
            end = r.ts
            ongoing = False
            break

    span = [r for r in window if start <= r.ts <= end]

    violation_seconds = 0.0
    for a, b in zip(span, span[1:]):
        if is_violation(a.temperature, rule):
            violation_seconds += (b.ts - a.ts).total_seconds()

    has_high = any(r.temperature > rule.upper_limit for r in violating)
    has_low = any(r.temperature < rule.lower_limit for r in violating)
    event_type = "mixed" if (has_high and has_low) else ("high" if has_high else "low")

    peak = max(span, key=lambda r: _deviation(r.temperature, rule))

    return {
        "start_time": start,
        "end_time": end,
        "ongoing": ongoing,
        "duration_seconds": (end - start).total_seconds(),
        "violation_seconds": violation_seconds,
        "event_type": event_type,
        "peak_temp": peak.temperature,
        "peak_time": peak.ts,
        "max_temp": max(r.temperature for r in span),
        "min_temp": min(r.temperature for r in span),
        "reading_count": len(span),
    }


def rule_at(ts: datetime, versions: list[RuleVersion]) -> RuleVersion | None:
    """返回 ts 时刻已生效的最新规则版本（effective_at <= ts），没有则 None。

    versions 必须按 effective_at 升序排列。
    """
    if not versions:
        return None
    keys = [v.effective_at for v in versions]
    idx = bisect_right(keys, ts) - 1
    return versions[idx] if idx >= 0 else None


def detect_versioned_events(
    readings: list[Reading], versions: list[RuleVersion]
) -> list[dict]:
    """按规则版本识别事件。

    对每条读数应用当时已生效的最新版本；版本切换点强制结束旧事件，
    切换点两侧分别用各自版本独立识别，禁止跨版本合并。
    早于首个版本生效时间的读数没有适用规则，直接忽略。
    返回的每个事件带有 rule_version 与 rule_effective_at。
    """
    ordered_versions = sorted(versions, key=lambda v: v.effective_at)
    effective_times = [v.effective_at for v in ordered_versions]
    ordered = sorted(readings, key=lambda r: r.ts)

    events: list[dict] = []
    block: list[Reading] = []          # 当前版本连续片段内的读数
    current_version: RuleVersion | None = None

    def _flush() -> None:
        # 版本切换是硬边界：旧片段的事件到此为止，绝不与新版本合并
        if block and current_version is not None:
            for ev in detect_events(block, current_version.rule):
                ev["rule_version"] = current_version.version
                ev["rule_effective_at"] = current_version.effective_at
                events.append(ev)

    for r in ordered:
        idx = bisect_right(effective_times, r.ts) - 1
        if idx < 0:
            # 首个版本生效之前的读数：无适用规则
            continue
        rv = ordered_versions[idx]
        if current_version is not None and rv is not current_version:
            _flush()
            block = []
        current_version = rv
        block.append(r)

    _flush()
    return events
