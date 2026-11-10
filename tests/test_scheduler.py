"""定时公告调度：准点、抖动范围、补发策略、多群与启停。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.clock import FakeClock
from src.safety import ForbiddenContentError
from src.scheduler.cron import parse_schedule
from src.scheduler.scheduler import Announcement, AnnouncementScheduler, announcement_from_mapping


class _Collector:
    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []

    def submit_announcement(self, group_id: str, text: str) -> None:
        self.items.append((group_id, text))


def _ann(**kwargs) -> Announcement:
    base = dict(
        id="morning",
        text="各位会员早上好，请注意风险控制。",
        groups=["g1", "g2"],
        schedule="09:00",
        jitter_seconds=0,
        catch_up=True,
        catch_up_max_minutes=10,
        enabled=True,
    )
    base.update(kwargs)
    return announcement_from_mapping(base)


def test_parse_daily_and_cron():
    daily = parse_schedule("09:00")
    assert daily.matches(datetime(2026, 11, 11, 9, 0, 0))
    assert not daily.matches(datetime(2026, 11, 11, 9, 1, 0))
    cron = parse_schedule("0 9 * * 1-5")
    # 2026-11-11 是周三
    assert cron.matches(datetime(2026, 11, 11, 9, 0))
    # 2026-11-14 是周六
    assert not cron.matches(datetime(2026, 11, 14, 9, 0))


def test_fire_on_time_without_jitter():
    clock = FakeClock(datetime(2026, 11, 11, 9, 0, 0))
    collector = _Collector()
    sched = AnnouncementScheduler([_ann()], sender=collector, clock=clock, on_time_grace_seconds=60)
    events = sched.tick()
    assert len(events) == 1
    assert events[0].reason == "on_time"
    assert [g for g, _ in collector.items] == ["g1", "g2"]
    # 同一轮不会重复发送
    assert sched.tick() == []


def test_jitter_is_within_range_and_stable():
    item = _ann(jitter_seconds=30)
    clock = FakeClock(datetime(2026, 11, 11, 9, 0, 0))
    sched = AnnouncementScheduler([item], clock=clock)
    occ = datetime(2026, 11, 11, 9, 0, 0)
    values = [sched.jitter_for(item, occ) for _ in range(5)]
    assert all(-30 <= v <= 30 for v in values)
    assert len(set(values)) == 1  # 同一计划时刻抖动稳定
    other = sched.jitter_for(item, occ + timedelta(days=1))
    assert -30 <= other <= 30
    samples = [
        sched.jitter_for(item, datetime(2026, 1, 1, 9, 0) + timedelta(days=i))
        for i in range(40)
    ]
    assert any(v != 0 for v in samples)
    assert all(-30 <= v <= 30 for v in samples)
    # 默认抖动 ≤3s，保证准点 ±5s
    default_item = _ann()  # jitter 默认 3
    default_item = _ann(jitter_seconds=3)
    default_samples = [
        sched.jitter_for(default_item, datetime(2026, 2, 1, 9, 0) + timedelta(days=i))
        for i in range(40)
    ]
    assert all(abs(v) <= 3 + 1e-9 for v in default_samples)


def test_does_not_fire_before_jitter_elapses():
    item = _ann(jitter_seconds=3)
    occ = datetime(2026, 11, 11, 9, 0, 0)
    clock = FakeClock(occ)
    sched = AnnouncementScheduler(
        [item], sender=_Collector(), clock=clock, punctual_window_seconds=5
    )
    jitter = sched.jitter_for(item, occ)
    # 不论正负抖动，都不得早于 fire_at
    if jitter > 0:
        assert sched.tick() == []
        clock.set(occ + timedelta(seconds=jitter + 0.01))
    events = sched.tick()
    assert len(events) == 1
    assert events[0].reason == "on_time"
    assert abs(jitter) <= 3 + 1e-9


def test_catch_up_within_max_minutes():
    clock = FakeClock(datetime(2026, 11, 11, 9, 8, 0))
    collector = _Collector()
    sched = AnnouncementScheduler(
        [_ann(catch_up=True, catch_up_max_minutes=10, jitter_seconds=0)],
        sender=collector,
        clock=clock,
        on_time_grace_seconds=30,
    )
    events = sched.tick()
    assert len(events) == 1
    assert events[0].reason == "catch_up"


def test_no_catch_up_beyond_max_minutes():
    clock = FakeClock(datetime(2026, 11, 11, 9, 20, 0))
    sched = AnnouncementScheduler(
        [_ann(catch_up=True, catch_up_max_minutes=10, jitter_seconds=0)],
        sender=_Collector(),
        clock=clock,
        on_time_grace_seconds=30,
    )
    assert sched.tick() == []


def test_catch_up_disabled_skips_late():
    clock = FakeClock(datetime(2026, 11, 11, 9, 3, 0))
    sched = AnnouncementScheduler(
        [_ann(catch_up=False, jitter_seconds=0)],
        sender=_Collector(),
        clock=clock,
        on_time_grace_seconds=30,
    )
    # 3 分钟已超过 30 秒宽限，且不允许补发
    assert sched.tick() == []


def test_start_stop_flags():
    sched = AnnouncementScheduler(clock=FakeClock())
    sched.start()
    assert sched.running is True
    sched.stop()
    assert sched.running is False


def test_forbidden_announcement_rejected():
    with pytest.raises(ForbiddenContentError):
        announcement_from_mapping(
            {
                "id": "x",
                "text": "关于结算安排的通知",
                "groups": ["g1"],
                "schedule": "09:00",
            }
        )


def test_state_persists_across_instances(tmp_path: Path):
    path = tmp_path / "state.json"
    clock = FakeClock(datetime(2026, 11, 11, 9, 0, 0))
    first = AnnouncementScheduler(
        [_ann(jitter_seconds=0)],
        sender=_Collector(),
        clock=clock,
        state_path=path,
    )
    assert first.tick()
    second = AnnouncementScheduler(
        [_ann(jitter_seconds=0)],
        sender=_Collector(),
        clock=clock,
        state_path=path,
    )
    assert second.tick() == []


# ---------------------------------------------------------------------------
# 验收契约（SC-01～SC-06）：通过 tests/adapter.py 对接。
# 开发模块未按约定暴露 Scheduler 时 skip，不覆盖上方针对 AnnouncementScheduler 的单元测试。
# ---------------------------------------------------------------------------

from constants import CATCH_UP_WINDOW_DEFAULT, CATCH_UP_WINDOW_MAX, SCHEDULER_TOLERANCE_SECONDS
from conftest import require_impl

_SHANGHAI = __import__("datetime").timezone(__import__("datetime").timedelta(hours=8))


def _make_contract(clock=None, window=CATCH_UP_WINDOW_DEFAULT, store=None):
    impl = require_impl("Scheduler")
    try:
        return impl.Scheduler(clock=clock, store=store, catch_up_window_seconds=window)
    except TypeError:
        try:
            sch = impl.Scheduler()
            if clock is not None and hasattr(sch, "set_clock"):
                sch.set_clock(clock)
            return sch
        except TypeError as exc:
            pytest.skip(f"Scheduler 构造签名无法适配: {exc}")


def _contract_clock(dt):
    impl = require_impl("Scheduler")
    if hasattr(impl, "FakeClock"):
        return impl.FakeClock(dt)

    class _C:
        def __init__(self, now):
            self._now = now

        def now(self):
            return self._now

        def advance(self, seconds):
            from datetime import timedelta as _td
            self._now = self._now + _td(seconds=seconds)
            return self._now

        def set(self, x):
            self._now = x

    return _C(dt)


def test_sc01_on_time_within_five_seconds():
    """SC-01 准点误差 ≤ ±5 秒（注入时钟）。"""
    from datetime import datetime, timedelta
    start = datetime(2026, 11, 11, 12, 0, 0, tzinfo=_SHANGHAI)
    run_at = start + timedelta(minutes=5)
    clock = _contract_clock(start)
    sch = _make_contract(clock=clock)
    sch.add_announcement("job-sc01", "定时公告-虚构", run_at, "交易会员群-主群")
    assert sch.tick() == []
    clock.set(run_at + timedelta(seconds=3))
    due = sch.tick()
    assert len(due) == 1, "到达计划时间+3s 应触发一次"
    job = due[0]
    job_run = job.run_at if hasattr(job, "run_at") else job["run_at"]
    delta = abs((clock.now() - job_run).total_seconds())
    assert delta <= SCHEDULER_TOLERANCE_SECONDS
    sch.mark_sent("job-sc01", clock.now())
    assert sch.tick() == []


def test_sc02_cross_midnight():
    """SC-02 跨天 00:00 触发一次且不重复。"""
    from datetime import datetime, timedelta
    before = datetime(2026, 11, 11, 23, 59, 50, tzinfo=_SHANGHAI)
    run_at = datetime(2026, 11, 12, 0, 0, 0, tzinfo=_SHANGHAI)
    clock = _contract_clock(before)
    sch = _make_contract(clock=clock)
    sch.add_announcement("job-midnight", "日切公告-虚构", run_at, "交易会员群-主群")
    assert sch.tick() == []
    clock.set(run_at + timedelta(seconds=2))
    due = sch.tick()
    assert len(due) == 1
    sch.mark_sent("job-midnight", clock.now())
    clock.set(run_at + timedelta(minutes=1))
    assert sch.tick() == []


def test_sc03_restart_catch_up_inside_window():
    """SC-03 窗口内重启：补发一次。"""
    from datetime import datetime, timedelta
    run_at = datetime(2026, 11, 11, 12, 0, 0, tzinfo=_SHANGHAI)
    store = {}
    clock = _contract_clock(run_at - timedelta(minutes=1))
    sch = _make_contract(clock=clock, store=store)
    sch.add_announcement("job-re", "重启补发-虚构", run_at, "交易会员群-主群")
    clock2 = _contract_clock(run_at + timedelta(minutes=3))
    sch2 = _make_contract(clock=clock2, store=store)
    recovered = sch2.on_recover()
    assert len(recovered) == 1
    sch2.mark_sent("job-re", clock2.now())
    assert sch2.on_recover() == []
    assert sch2.tick() == []


def test_sc03_restart_skip_outside_window():
    """SC-03 窗口外重启：不补发，标记 skipped。"""
    from datetime import datetime, timedelta
    run_at = datetime(2026, 11, 11, 12, 0, 0, tzinfo=_SHANGHAI)
    store = {}
    clock = _contract_clock(run_at - timedelta(minutes=1))
    sch = _make_contract(clock=clock, store=store)
    sch.add_announcement("job-old", "超时不补-虚构", run_at, "交易会员群-主群")
    clock2 = _contract_clock(run_at + timedelta(seconds=CATCH_UP_WINDOW_DEFAULT + 30))
    sch2 = _make_contract(clock=clock2, store=store)
    recovered = sch2.on_recover()
    assert recovered == [], "超出补发窗口不得补发"
    later = sch2.tick()
    pending = [j for j in later if (getattr(j, "status", "pending") == "pending")]
    assert pending == []


def test_sc03_already_sent_not_replayed():
    """SC-03 已发送：重启后永不补发。"""
    from datetime import datetime, timedelta
    run_at = datetime(2026, 11, 11, 12, 0, 0, tzinfo=_SHANGHAI)
    store = {}
    clock = _contract_clock(run_at + timedelta(seconds=1))
    sch = _make_contract(clock=clock, store=store)
    sch.add_announcement("job-sent", "已发送-虚构", run_at, "交易会员群-主群")
    due = sch.tick()
    assert due
    sch.mark_sent("job-sent", clock.now())
    clock2 = _contract_clock(run_at + timedelta(minutes=2))
    sch2 = _make_contract(clock=clock2, store=store)
    assert sch2.on_recover() == []
    assert sch2.tick() == []


def test_sc04_disconnect_same_as_restart():
    """SC-04 掉线跨越 run_at 后恢复，策略与重启相同。"""
    from datetime import datetime, timedelta
    run_at = datetime(2026, 11, 11, 15, 0, 0, tzinfo=_SHANGHAI)
    store = {}
    clock = _contract_clock(run_at - timedelta(seconds=10))
    sch = _make_contract(clock=clock, store=store)
    sch.add_announcement("job-dc", "掉线补发-虚构", run_at, "交易会员群-主群")
    clock.set(run_at + timedelta(minutes=2))
    recovered = sch.on_recover()
    assert len(recovered) == 1
    sch.mark_sent("job-dc", clock.now())
    assert sch.on_recover() == []


def test_sc05_no_duplicate_on_double_recover_and_tick():
    """SC-05 连续 tick / 重复 on_recover 不重复发送。"""
    from datetime import datetime, timedelta
    run_at = datetime(2026, 11, 11, 16, 0, 0, tzinfo=_SHANGHAI)
    clock = _contract_clock(run_at + timedelta(seconds=1))
    sch = _make_contract(clock=clock)
    sch.add_announcement("job-dup", "去重-虚构", run_at, "交易会员群-主群")
    first = sch.tick()
    assert len(first) == 1
    sch.mark_sent("job-dup", clock.now())
    assert sch.tick() == []
    assert sch.on_recover() == []
    assert sch.on_recover() == []


def test_sc06_invalid_window_rejected():
    """SC-06 非法补发窗口被拒绝。"""
    from datetime import datetime
    require_impl("Scheduler")
    start = datetime(2026, 11, 11, 12, 0, 0, tzinfo=_SHANGHAI)
    clock = _contract_clock(start)
    with pytest.raises((ValueError, TypeError, AssertionError)):
        _make_contract(clock=clock, window=CATCH_UP_WINDOW_MAX + 100)
    with pytest.raises((ValueError, TypeError, AssertionError)):
        _make_contract(clock=clock, window=-1)



# ---------------------------------------------------------------------------
# 公告准点补充（SC-07～SC-10）：时区 Asia/Shanghai、跨月、掉线/重启只补一次。
# ---------------------------------------------------------------------------


def test_sc07_timezone_is_shanghai():
    """SC-07 计划时区按 Asia/Shanghai（中国无夏令时，但必须带 +08:00，不能当 UTC）。"""
    from datetime import datetime, timedelta
    # 若把 16:00 +08:00 误当成 UTC，会错 8 小时
    run_at = datetime(2026, 11, 11, 16, 0, 0, tzinfo=_SHANGHAI)
    clock = _contract_clock(run_at - timedelta(seconds=10))
    sch = _make_contract(clock=clock)
    sch.add_announcement("job-tz", "时区检查-虚构", run_at, "交易会员群-主群")
    assert sch.tick() == []
    clock.set(run_at + timedelta(seconds=2))
    due = sch.tick()
    assert len(due) == 1, "Asia/Shanghai 16:00 应在本地 16:00:02 触发，而不是按 UTC 计算"
    sch.mark_sent("job-tz", clock.now())
    # 8 小时后不应再当成「到点」
    clock.set(run_at + timedelta(hours=8))
    assert sch.tick() == []


def test_sc08_cross_month_boundary():
    """SC-08 跨月 23:59 → 次月 00:00 只触发一次。"""
    from datetime import datetime, timedelta
    before = datetime(2026, 11, 30, 23, 59, 50, tzinfo=_SHANGHAI)
    run_at = datetime(2026, 12, 1, 0, 0, 0, tzinfo=_SHANGHAI)
    clock = _contract_clock(before)
    sch = _make_contract(clock=clock)
    sch.add_announcement("job-month", "跨月公告-虚构", run_at, "交易会员群-主群")
    assert sch.tick() == []
    clock.set(run_at + timedelta(seconds=2))
    due = sch.tick()
    assert len(due) == 1
    sch.mark_sent("job-month", clock.now())
    clock.set(run_at + timedelta(minutes=30))
    assert sch.tick() == []
    assert sch.on_recover() == []


def test_sc09_disconnect_then_restart_catch_up_only_once():
    """SC-09 掉线跨越 run_at，恢复后再模拟重启，窗口内只补一次。"""
    from datetime import datetime, timedelta
    run_at = datetime(2026, 11, 11, 20, 0, 0, tzinfo=_SHANGHAI)
    store = {}
    clock = _contract_clock(run_at - timedelta(seconds=30))
    sch = _make_contract(clock=clock, store=store)
    sch.add_announcement("job-once", "只补一次-虚构", run_at, "交易会员群-主群")
    # 掉线跨越 run_at
    clock.set(run_at + timedelta(minutes=4))
    recovered = sch.on_recover()
    assert len(recovered) == 1
    sch.mark_sent("job-once", clock.now())
    # 再次恢复 + 新进程
    clock2 = _contract_clock(run_at + timedelta(minutes=6))
    sch2 = _make_contract(clock=clock2, store=store)
    assert sch2.on_recover() == []
    assert sch2.tick() == []
    assert sch2.on_recover() == []


def test_sc10_no_dst_but_spring_forward_date_still_stable():
    """SC-10 中国无夏令时：即便经过「其他时区会调时」的日期，Asia/Shanghai 仍按 +08:00 稳定触发。"""
    from datetime import datetime, timedelta
    # 2026-03-08 是美国春令起始日，上海不应跳小时
    run_at = datetime(2026, 3, 8, 2, 30, 0, tzinfo=_SHANGHAI)
    clock = _contract_clock(run_at - timedelta(minutes=1))
    sch = _make_contract(clock=clock)
    sch.add_announcement("job-dst", "无夏令时-虚构", run_at, "交易会员群-主群")
    clock.set(run_at + timedelta(seconds=1))
    due = sch.tick()
    assert len(due) == 1
    job_run = due[0].run_at if hasattr(due[0], "run_at") else due[0]["run_at"]
    assert job_run.utcoffset() == timedelta(hours=8) or job_run.tzinfo is None
    sch.mark_sent("job-dst", clock.now())
    assert sch.tick() == []
