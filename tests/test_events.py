"""事件识别算法单元测试。"""

from datetime import datetime, timedelta, timezone

from app.events import (
    MaintenanceWindow,
    Reading,
    Rule,
    RuleVersion,
    detect_events,
    detect_versioned_events,
    in_window,
    split_by_windows,
)

BASE = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
# 上限 8℃、下限 2℃、容忍 5 分钟、最大间隔 30 分钟
RULE = Rule(upper_limit=8.0, lower_limit=2.0, tolerance_seconds=300, max_gap_seconds=1800)


def w(start_minutes: float, end_minutes: float) -> MaintenanceWindow:
    return MaintenanceWindow(
        start=BASE + timedelta(minutes=start_minutes),
        end=BASE + timedelta(minutes=end_minutes),
    )


def rv(version: int, effective_minutes: float, rule: Rule = RULE) -> RuleVersion:
    return RuleVersion(
        version=version,
        effective_at=BASE + timedelta(minutes=effective_minutes),
        rule=rule,
    )


def r(minutes: float, temp: float) -> Reading:
    return Reading(ts=BASE + timedelta(minutes=minutes), temperature=temp)


class TestNoEvent:
    def test_all_in_range(self):
        assert detect_events([r(0, 5.0), r(10, 6.0), r(20, 4.0)], RULE) == []

    def test_empty(self):
        assert detect_events([], RULE) == []

    def test_boundary_values_are_not_violations(self):
        assert detect_events([r(0, 8.0), r(10, 2.0)], RULE) == []


class TestSingleEvent:
    def test_basic_event_fields(self):
        events = detect_events([
            r(0, 5.0),    # 正常
            r(10, 9.0),   # 超限开始
            r(20, 10.5),  # 峰值
            r(30, 8.5),
            r(40, 5.0),   # 恢复
            r(50, 5.0),
        ], RULE)
        assert len(events) == 1
        ev = events[0]
        assert ev["start_time"] == BASE + timedelta(minutes=10)
        assert ev["end_time"] == BASE + timedelta(minutes=40)
        assert ev["ongoing"] is False
        assert ev["event_type"] == "high"
        assert ev["peak_temp"] == 10.5
        assert ev["peak_time"] == BASE + timedelta(minutes=20)
        assert ev["max_temp"] == 10.5
        assert ev["min_temp"] == 5.0
        # 累计超限：10→20、20→30、30→40 三段左端点均超限，共 30 分钟
        assert ev["violation_seconds"] == 30 * 60
        assert ev["duration_seconds"] == 30 * 60

    def test_low_temperature_event(self):
        events = detect_events([
            r(0, 1.0),   # 低于下限
            r(10, 0.5),  # 峰值（最低）
            r(20, 4.0),  # 恢复
        ], RULE)
        assert len(events) == 1
        ev = events[0]
        assert ev["event_type"] == "low"
        assert ev["peak_temp"] == 0.5
        assert ev["violation_seconds"] == 20 * 60

    def test_ongoing_event_uses_last_violation_as_end(self):
        events = detect_events([r(0, 9.0), r(10, 9.5)], RULE)
        assert len(events) == 1
        ev = events[0]
        assert ev["ongoing"] is True
        assert ev["end_time"] == BASE + timedelta(minutes=10)
        # 最后一个超限点之后没有读数，不计时长
        assert ev["violation_seconds"] == 10 * 60

    def test_consecutive_violations_stay_one_event(self):
        # 持续超限、上报间隔 20 分钟（< max_gap），中间没有恢复 → 同一事件
        events = detect_events([
            r(0, 9.0),
            r(20, 9.5),
            r(40, 8.5),
            r(60, 5.0),
        ], RULE)
        assert len(events) == 1
        assert events[0]["violation_seconds"] == 60 * 60


class TestToleranceMerging:
    def test_brief_recovery_does_not_split(self):
        # 恢复正常持续 2 分钟（< 容忍 5 分钟）后再次超限 → 同一事件
        events = detect_events([
            r(0, 9.0),
            r(2, 5.0),   # 短暂恢复
            r(4, 9.2),   # 再次超限
        ], RULE)
        assert len(events) == 1
        ev = events[0]
        assert ev["start_time"] == BASE
        assert ev["end_time"] == BASE + timedelta(minutes=4)
        assert ev["ongoing"] is True
        # 只有 0→2 这一段左端点超限
        assert ev["violation_seconds"] == 2 * 60

    def test_recovery_longer_than_tolerance_splits(self):
        # 恢复正常持续 9 分钟（> 容忍 5 分钟）→ 两个事件
        events = detect_events([
            r(0, 9.0),
            r(1, 5.0),    # 恢复
            r(10, 9.0),   # 已恢复 9 分钟 → 新事件
            r(20, 5.0),
        ], RULE)
        assert len(events) == 2
        assert events[0]["start_time"] == BASE
        assert events[0]["end_time"] == BASE + timedelta(minutes=1)
        assert events[1]["start_time"] == BASE + timedelta(minutes=10)
        assert events[1]["end_time"] == BASE + timedelta(minutes=20)

    def test_recovery_exactly_at_tolerance_stays_one_event(self):
        events = detect_events([
            r(0, 9.0),
            r(1, 5.0),   # 恢复
            r(6, 9.0),   # 恢复恰好 5 分钟 = 容忍时长，不切断
            r(7, 5.0),
        ], RULE)
        assert len(events) == 1


class TestGapSplitting:
    def test_long_gap_splits_even_when_still_violating(self):
        # 两次超限读数间隔 40 分钟 > max_gap 30 分钟 → 强制分段
        events = detect_events([
            r(0, 9.0),
            r(40, 9.5),
            r(50, 5.0),
        ], RULE)
        assert len(events) == 2
        assert events[0]["start_time"] == BASE
        assert events[0]["end_time"] == BASE  # 单点事件，起止相同
        assert events[0]["violation_seconds"] == 0
        assert events[1]["start_time"] == BASE + timedelta(minutes=40)
        assert events[1]["end_time"] == BASE + timedelta(minutes=50)

    def test_gap_in_normal_region_also_splits(self):
        events = detect_events([
            r(0, 9.0),
            r(1, 5.0),    # 恢复
            r(40, 9.0),   # 间隔 39 分钟 > max_gap → 新事件
        ], RULE)
        assert len(events) == 2


class TestOrdering:
    def test_unsorted_input_is_handled(self):
        events = detect_events([
            r(20, 5.0),
            r(0, 9.0),
            r(10, 9.5),
        ], RULE)
        assert len(events) == 1
        assert events[0]["start_time"] == BASE
        assert events[0]["violation_seconds"] == 20 * 60


class TestVersionedEvents:
    def test_single_version_behaves_like_detect_events(self):
        readings = [r(0, 9.0), r(10, 5.0), r(20, 9.0), r(30, 5.0)]
        events = detect_versioned_events(readings, [rv(1, -1000)])
        assert len(events) == 2
        assert all(e["rule_version"] == 1 for e in events)
        assert all(e["rule_effective_at"] == BASE - timedelta(minutes=1000)
                   for e in events)

    def test_switch_point_splits_event_and_tags_versions(self):
        # 版本切换（30 分钟处）两侧都在超限 → 必须切成两个事件
        events = detect_versioned_events([
            r(0, 9.0),
            r(20, 9.5),
            r(30, 9.2),   # 起适用 v2
            r(40, 5.0),
        ], [rv(1, 0), rv(2, 30)])
        assert len(events) == 2
        old, new = events
        assert old["rule_version"] == 1
        assert old["start_time"] == BASE
        assert old["end_time"] == BASE + timedelta(minutes=20)  # 切换点闭合
        assert old["ongoing"] is True
        assert new["rule_version"] == 2
        assert new["rule_effective_at"] == BASE + timedelta(minutes=30)
        assert new["start_time"] == BASE + timedelta(minutes=30)
        assert new["end_time"] == BASE + timedelta(minutes=40)
        # 20→30 的区间跨越版本边界，不计入任何一侧的超限时长
        assert old["violation_seconds"] == 20 * 60
        assert new["violation_seconds"] == 10 * 60

    def test_no_cross_version_tolerance_merge(self):
        # v1 超限后短暂恢复，切换到 v2 后立刻再超限；
        # 即便恢复时长（2 分钟）< 容忍时长（5 分钟）也禁止跨版本合并
        events = detect_versioned_events([
            r(0, 9.0),
            r(10, 5.0),   # 恢复（v1）
            r(12, 9.2),   # v2 下再次超限
            r(20, 5.0),
        ], [rv(1, 0), rv(2, 11)])
        assert len(events) == 2
        assert [e["rule_version"] for e in events] == [1, 2]
        assert events[0]["end_time"] == BASE + timedelta(minutes=10)
        assert events[1]["start_time"] == BASE + timedelta(minutes=12)

    def test_each_reading_uses_rule_effective_at_that_time(self):
        # v1 上限 6℃ → 7℃ 算超限；v2（10 分钟起）上限放宽到 8℃ → 7℃ 正常
        rule_low = Rule(upper_limit=6.0, lower_limit=2.0,
                        tolerance_seconds=300, max_gap_seconds=1800)
        events = detect_versioned_events([
            r(0, 7.0),    # v1：超限
            r(5, 7.0),    # v1：仍超限
            r(10, 7.0),   # v2：限值放宽，同一温度变为正常读数
        ], [rv(1, 0, rule_low), rv(2, 10, RULE)])
        # 只有 v1 下的一个事件；v2 片段没有超限读数，不产生事件，也不回切旧事件
        assert len(events) == 1
        ev = events[0]
        assert ev["rule_version"] == 1
        assert ev["event_type"] == "high"
        assert ev["start_time"] == BASE
        assert ev["end_time"] == BASE + timedelta(minutes=5)
        assert ev["violation_seconds"] == 5 * 60

    def test_readings_before_first_version_are_ignored(self):
        events = detect_versioned_events([
            r(0, 99.0),   # 首个版本 5 分钟才生效 → 忽略
            r(10, 9.0),   # v1 下超限
            r(20, 5.0),
        ], [rv(1, 5)])
        assert len(events) == 1
        assert events[0]["rule_version"] == 1
        assert events[0]["start_time"] == BASE + timedelta(minutes=10)

    def test_versions_unsorted_are_handled(self):
        events = detect_versioned_events([
            r(0, 9.0), r(20, 9.0), r(30, 5.0),
        ], [rv(2, 15), rv(1, 0)])
        assert len(events) == 2
        assert [e["rule_version"] for e in events] == [1, 2]

    def test_no_versions_no_events(self):
        assert detect_versioned_events([r(0, 9.0)], []) == []


class TestMaintenanceWindows:
    def test_half_open_boundary_membership(self):
        win = [w(10, 20)]
        assert in_window(BASE + timedelta(minutes=10), win) is True   # start 含
        assert in_window(BASE + timedelta(minutes=15), win) is True
        assert in_window(BASE + timedelta(minutes=20), win) is False  # end 不含
        assert in_window(BASE + timedelta(minutes=5), win) is False

    def test_split_drops_in_window_readings_and_segments(self):
        fragments = split_by_windows(
            [r(0, 9.0), r(10, 99.0), r(15, 99.0), r(20, 9.0)],
            [w(10, 20)],
        )
        assert fragments == [[r(0, 9.0)], [r(20, 9.0)]]

    def test_readings_inside_window_are_ignored(self):
        # 窗口内即便有“超限”读数也不产生事件
        events = detect_versioned_events([
            r(0, 5.0),
            r(10, 99.0),  # 窗口内，忽略
            r(15, 99.0),  # 窗口内，忽略
            r(20, 5.0),
        ], [rv(1, -1000)], [w(10, 20)])
        assert events == []

    def test_boundary_times_start_inclusive_end_exclusive(self):
        # 读数恰好在 start（10）→ 窗口内忽略；恰好在 end（20）→ 参与判定
        events = detect_versioned_events([
            r(10, 99.0),  # 窗口起点：忽略
            r(20, 9.0),   # 窗口终点：窗口外，算超限并开启事件
            r(30, 5.0),
        ], [rv(1, -1000)], [w(10, 20)])
        assert len(events) == 1
        assert events[0]["start_time"] == BASE + timedelta(minutes=20)
        assert events[0]["end_time"] == BASE + timedelta(minutes=30)

    def test_cross_window_violations_never_merge(self):
        # 窗口前后都在超限，且时间间隔很短（远小于容忍/max_gap）：必须切成两个事件
        events = detect_versioned_events([
            r(0, 9.0),
            r(8, 9.5),    # 窗口前最后超限
            r(10, 99.0),  # 窗口内，忽略
            r(12, 99.0),  # 窗口内，忽略
            r(14, 9.2),   # 窗口后首条超限 → 新事件
            r(20, 5.0),
        ], [rv(1, -1000)], [w(10, 14)])
        assert len(events) == 2
        before, after = events
        # 窗口边界闭合旧事件：无后续恢复读数，ongoing=True，终点为最后超限时间
        assert before["start_time"] == BASE
        assert before["end_time"] == BASE + timedelta(minutes=8)
        assert before["ongoing"] is True
        assert before["violation_seconds"] == 8 * 60
        assert after["start_time"] == BASE + timedelta(minutes=14)
        assert after["end_time"] == BASE + timedelta(minutes=20)
        assert after["ongoing"] is False
        assert after["violation_seconds"] == 6 * 60

    def test_window_closes_event_even_without_reading_at_boundary(self):
        # 窗口前后没有恰好在边界上的读数，边界仍强制闭合
        events = detect_versioned_events([
            r(0, 9.0),
            r(5, 9.0),
            r(18, 9.0),
            r(25, 5.0),
        ], [rv(1, -1000)], [w(10, 15)])
        assert len(events) == 2
        assert events[0]["end_time"] == BASE + timedelta(minutes=5)
        assert events[0]["ongoing"] is True
        assert events[1]["start_time"] == BASE + timedelta(minutes=18)

    def test_window_clears_recovery_tolerance_state(self):
        # 窗口前已短暂恢复；窗口清空容忍状态后，窗口结束后的首条超限是新事件。
        # 无窗口时恢复间隔仅 4 分钟（< 容忍 5 分钟）本会合并；
        # 窗口 [3,5) 把恢复切开（窗口内没有读数，边界本身也强制分段）。
        events = detect_versioned_events([
            r(0, 9.0),
            r(2, 5.0),    # 恢复（窗口前）
            r(6, 9.0),    # 窗口后首条超限 → 新事件
            r(10, 5.0),
        ], [rv(1, -1000)], [w(3, 5)])
        assert len(events) == 2
        assert events[0]["start_time"] == BASE
        assert events[0]["end_time"] == BASE + timedelta(minutes=2)
        # 窗口前最后一条读数已恢复正常：旧事件在窗口边界闭合，ongoing=False
        assert events[0]["ongoing"] is False
        assert events[1]["start_time"] == BASE + timedelta(minutes=6)
        assert events[1]["end_time"] == BASE + timedelta(minutes=10)

    def test_window_with_in_window_reading_clears_tolerance(self):
        # 窗口内有读数的情形：0 超限 → 2 恢复 → 4 窗口内读数（忽略）→ 6 再超限。
        # 若无窗口，恢复时长 4 分钟 < 容忍 5 分钟，会合并；有窗口必须切开。
        events = detect_versioned_events([
            r(0, 9.0),
            r(2, 5.0),
            r(4, 99.0),  # 窗口内，忽略
            r(6, 9.0),
            r(10, 5.0),
        ], [rv(1, -1000)], [w(4, 6)])
        assert len(events) == 2
        assert events[0]["end_time"] == BASE + timedelta(minutes=2)
        assert events[1]["start_time"] == BASE + timedelta(minutes=6)

    def test_window_plus_version_boundary(self):
        # 窗口跨越版本切换点：两种硬边界同时存在，仍然只在实际超限片段产生事件
        events = detect_versioned_events([
            r(0, 9.0),     # v1 超限
            r(5, 9.0),     # v1 超限
            r(20, 99.0),   # v2 窗口内，忽略
            r(30, 5.0),    # v2 正常
        ], [rv(1, 0), rv(2, 10)], [w(15, 25)])
        assert len(events) == 1
        ev = events[0]
        assert ev["rule_version"] == 1
        assert ev["end_time"] == BASE + timedelta(minutes=5)
        assert ev["ongoing"] is True
