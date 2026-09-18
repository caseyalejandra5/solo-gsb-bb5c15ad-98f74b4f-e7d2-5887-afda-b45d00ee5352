"""事件识别算法单元测试。"""

from datetime import datetime, timedelta, timezone

import pytest

from app.events import (
    OutageWindow,
    Reading,
    Rule,
    RuleVersion,
    detect_events,
    detect_versioned_events,
    merge_windows,
)

BASE = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
# 上限 8℃、下限 2℃、容忍 5 分钟、最大间隔 30 分钟
RULE = Rule(upper_limit=8.0, lower_limit=2.0, tolerance_seconds=300, max_gap_seconds=1800)


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


class TestWindowMerging:
    def w(self, start_min: float, end_min: float) -> OutageWindow:
        return OutageWindow(
            start=BASE + timedelta(minutes=start_min),
            end=BASE + timedelta(minutes=end_min),
        )

    def test_overlapping_windows_merge(self):
        merged = merge_windows([self.w(10, 20), self.w(15, 25)])
        assert merged == [self.w(10, 25)]

    def test_touching_half_open_windows_merge(self):
        # 半开区间首尾相接：[0,10) 与 [10,20) 合并
        merged = merge_windows([self.w(10, 20), self.w(0, 10)])
        assert merged == [self.w(0, 20)]

    def test_disjoint_windows_stay_separated(self):
        merged = merge_windows([self.w(0, 10), self.w(20, 30)])
        assert merged == [self.w(0, 10), self.w(20, 30)]

    def test_contained_window_is_absorbed(self):
        merged = merge_windows([self.w(0, 30), self.w(10, 20)])
        assert merged == [self.w(0, 30)]

    def test_empty_input(self):
        assert merge_windows([]) == []

    def test_start_must_be_before_end(self):
        with pytest.raises(ValueError):
            OutageWindow(start=BASE + timedelta(minutes=10),
                         end=BASE + timedelta(minutes=10))
        with pytest.raises(ValueError):
            OutageWindow(start=BASE + timedelta(minutes=11),
                         end=BASE + timedelta(minutes=10))


class TestOutageWindowEvents:
    def w(self, start_min: float, end_min: float) -> OutageWindow:
        return OutageWindow(
            start=BASE + timedelta(minutes=start_min),
            end=BASE + timedelta(minutes=end_min),
        )

    def test_readings_inside_window_are_ignored(self):
        # 窗口内的读数即使超限也不产生事件
        events = detect_versioned_events([
            r(0, 5.0),
            r(10, 9.0),    # 窗口内（start 含）
            r(20, 99.0),   # 窗口内
            r(30, 5.0),
        ], [rv(1, -1000)], [self.w(10, 30)])
        assert events == []

    def test_window_boundary_closes_event_and_violations_do_not_merge(self):
        # 窗口前持续超限、窗口后立刻又超限：边界必须闭合旧事件且禁止合并
        events = detect_versioned_events([
            r(0, 9.0),
            r(10, 9.5),   # 窗口前最后一条，仍超限
            r(20, 99.0),  # 窗口内：忽略
            r(30, 9.2),   # 窗口结束后首条（end 不含）：超限开启新事件
            r(40, 5.0),
        ], [rv(1, -1000)], [self.w(15, 30)])
        assert len(events) == 2
        old, new = events
        # 旧事件在窗口边界闭合：无后续恢复读数 → ongoing
        assert old["start_time"] == BASE + timedelta(minutes=0)
        assert old["end_time"] == BASE + timedelta(minutes=10)
        assert old["ongoing"] is True
        # 10→20 跨越窗口边界，不计入任何一侧
        assert old["violation_seconds"] == 10 * 60
        assert new["start_time"] == BASE + timedelta(minutes=30)
        assert new["end_time"] == BASE + timedelta(minutes=40)
        assert new["ongoing"] is False
        assert new["violation_seconds"] == 10 * 60

    def test_window_clears_tolerance_state(self):
        # 窗口前短暂恢复 2 分钟（< 容忍 5 分钟），窗口后再超限也不得合并
        events = detect_versioned_events([
            r(0, 9.0),
            r(10, 5.0),    # 恢复（窗口前）
            r(12, 5.0),    # 窗口内：忽略，恢复/容忍状态在此清空
            r(15, 9.0),    # 窗口结束后首条：超限开启新事件
            r(20, 5.0),
        ], [rv(1, -1000)], [self.w(11, 14)])
        assert len(events) == 2
        assert events[0]["start_time"] == BASE
        assert events[0]["end_time"] == BASE + timedelta(minutes=10)
        assert events[1]["start_time"] == BASE + timedelta(minutes=15)

    def test_no_merge_even_without_gap_across_window(self):
        # 窗口两侧读数只相隔 3 分钟（< max_gap 30 分钟），窗口仍强制分段
        events = detect_versioned_events([
            r(10, 9.0),
            r(12, 9.0),   # 窗口内忽略
            r(13, 9.0),
            r(20, 5.0),
        ], [rv(1, -1000)], [self.w(11, 13)])
        assert len(events) == 2
        assert events[0]["start_time"] == BASE + timedelta(minutes=10)
        assert events[1]["start_time"] == BASE + timedelta(minutes=13)

    def test_half_open_boundary_times(self):
        # start 时刻属于窗口（忽略）；end 时刻不属于窗口（参与判定）
        events = detect_versioned_events([
            r(10, 99.0),   # == window.start：窗口内，忽略
            r(20, 9.0),    # == window.end：窗口外，超限开启事件
            r(30, 5.0),
        ], [rv(1, -1000)], [self.w(10, 20)])
        assert len(events) == 1
        assert events[0]["start_time"] == BASE + timedelta(minutes=20)

    def test_multiple_windows_split_into_separate_events(self):
        events = detect_versioned_events([
            r(0, 9.0),
            r(10, 5.0),    # 第一个事件恢复
            r(20, 99.0),   # 窗口 1 内，忽略
            r(30, 9.0),    # 窗口 1 后再超限（与窗口 2 之间）
            r(35, 5.0),
            r(40, 99.0),   # 窗口 2 内，忽略
            r(50, 9.0),    # 窗口 2 后再超限
            r(60, 5.0),
        ], [rv(1, -1000)], [self.w(15, 45), self.w(20, 25)])
        # 合并后实际窗口为 [15,45)：20/40 的读数都在窗口内
        assert len(events) == 2
        assert events[0]["start_time"] == BASE
        assert events[1]["start_time"] == BASE + timedelta(minutes=50)

    def test_window_with_violations_only_inside_yields_no_event(self):
        events = detect_versioned_events([
            r(10, 9.0),
            r(20, 9.5),
        ], [rv(1, -1000)], [self.w(0, 30)])
        assert events == []

    def test_window_and_version_boundary_coexist(self):
        # 版本切换 + 停运窗口双重边界并存
        events = detect_versioned_events([
            r(0, 9.0),     # v1 超限
            r(10, 5.0),    # v1 恢复
            r(20, 99.0),   # 窗口内（即便 v2 已生效也忽略）
            r(30, 9.5),    # v2 下、窗口外：新事件
            r(40, 5.0),
        ], [rv(1, 0), rv(2, 15)], [self.w(15, 30)])
        assert len(events) == 2
        assert [e["rule_version"] for e in events] == [1, 2]
        assert events[0]["end_time"] == BASE + timedelta(minutes=10)
        assert events[1]["start_time"] == BASE + timedelta(minutes=30)
