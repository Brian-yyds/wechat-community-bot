"""入口：把适配器、管线、调度器与拟人发送串起来。

用法：
    python -m src.main --demo
    python -m src.main --config config/example.toml
"""

from __future__ import annotations

import argparse
import logging
import json
import sys
from datetime import datetime
from pathlib import Path
from random import Random
from typing import Any

from src.adapters.base import Group
from src.adapters.factory import build_adapter
from src.adapters.mock import MockAdapter
from src.clock import Clock, FakeClock, SystemClock
from src.config_util import ConfigError, load_config, project_root_from_here
from src.logging_util import setup_logging
from src.pipeline.classify import Classifier
from src.pipeline.dedup import Deduplicator
from src.pipeline.extract import Extractor
from src.pipeline.pipeline import Pipeline, ProcessResult
from src.pipeline.guard import IdentityConfig, ReplyGuard, StormConfig
from src.pipeline.provider import RuleReplyProvider
from src.pipeline.reply import ReplyEngine, rule_from_mapping
from src.pipeline.sink import JsonlSink
from src.adapters.pause import PauseGate
from src.latency import LATENCY_JSONL_NAME, LatencyStore
from src.ratelimit.bucket import BucketSpec, RateLimitConfig, RateLimiter
from src.ratelimit.sender import HumanizeConfig, HumanizedSender, NightSilentConfig, parse_hhmm
from src.run_mode import RunModeError, SendGate, parse_run_mode
from src.scheduler.scheduler import AnnouncementScheduler, announcement_from_mapping
from src.safety import ForbiddenContentError


DEMO_MESSAGES = [
    {"content": "你好，在吗？", "sender_name": "会员甲", "sender_id": "user-001"},
    {"content": "你好，在吗？", "sender_name": "会员甲", "sender_id": "user-001"},  # 重复
    {"content": "今天行情怎么看？有没有风险提示", "sender_name": "会员乙", "sender_id": "user-002"},
    {"content": "请问开户需要准备什么资料？", "sender_name": "会员丁", "sender_id": "user-005"},
    {"content": "哈哈", "sender_name": "会员丙", "sender_id": "user-003"},
    {
        "content": "加微信免费领取资料点击 https://example.com/promo",
        "sender_name": "陌生号",
        "sender_id": "user-004",
    },
    {
        "content": "【公告】今晚八点有复习交流，欢迎 @管理员 一起参加。详情 https://example.com/meet",
        "sender_name": "管理员",
        "sender_id": "user-admin",
        "mentions": ["管理员"],
    },
    {"content": "收到", "sender_name": "会员乙", "sender_id": "user-002"},
]


def _bucket(raw: dict[str, Any] | None, capacity: float, refill: float) -> BucketSpec:
    raw = raw or {}
    return BucketSpec(
        capacity=float(raw.get("capacity", capacity)),
        refill_per_second=float(raw.get("refill_per_second", refill)),
    )


def _limit_int(section: dict[str, Any], key: str, default: int | None) -> int | None:
    """读取可配的次数上限。缺省用 default；显式空值表示关闭该上限。"""
    if key not in section:
        return default
    value = section[key]
    if value in (None, ""):
        return None
    return int(value)


def build_rate_limiter(
    config: dict[str, Any],
    clock: Clock,
    data_dir: Path | None = None,
) -> tuple[RateLimiter, HumanizeConfig]:
    section = config.get("rate_limit") or {}
    state_path = None
    if data_dir is not None:
        raw_path = section.get("window_state_path")
        state_path = Path(raw_path) if raw_path else (Path(data_dir) / "rate_limit_window.json")
    limiter = RateLimiter(
        RateLimitConfig(
            global_bucket=_bucket(section.get("global"), 8, 0.25),
            per_group=_bucket(section.get("per_group"), 3, 0.1),
            announcement=_bucket((section.get("quotas") or {}).get("announcement"), 4, 0.1),
            auto_reply=_bucket((section.get("quotas") or {}).get("auto_reply"), 6, 0.2),
            global_replies_per_hour=_limit_int(section, "global_replies_per_hour", 40),
            group_replies_per_hour=_limit_int(section, "group_replies_per_hour", 20),
            global_per_minute=_limit_int(section, "global_per_minute", 8),
            group_per_minute=_limit_int(section, "group_per_minute", 3),
        ),
        clock=clock,
        state_path=state_path,
    )
    night_raw = section.get("night_silent") or {}
    night = NightSilentConfig(
        enabled=bool(night_raw.get("enabled", True)),
        start=parse_hhmm(str(night_raw.get("start", "00:30"))),
        end=parse_hhmm(str(night_raw.get("end", "07:00"))),
        announcement_policy=str(night_raw.get("announcement_policy", "hold")),
        auto_reply_policy=str(night_raw.get("auto_reply_policy", "drop")),
    )
    humanize_sec = section.get("humanize") or {}

    def _delay(key: str, default: float) -> float:
        if key in section and section[key] is not None:
            return float(section[key])
        if key in humanize_sec and humanize_sec[key] is not None:
            return float(humanize_sec[key])
        return float(default)

    humanize = HumanizeConfig(
        send_delay_min=_delay("send_delay_min", 2.0),
        send_delay_max=_delay("send_delay_max", 6.0),
        open_session_delay_min=_delay("open_session_delay_min", 1.5),
        open_session_delay_max=_delay("open_session_delay_max", 2.4),
        announcement_delay_min=_delay("announcement_delay_min", 2.0),
        announcement_delay_max=_delay("announcement_delay_max", 6.0),
        typing_hook=bool(section.get("typing_hook", True)),
        typing_duration_min=float(section.get("typing_duration_min", 0.3)),
        typing_duration_max=float(section.get("typing_duration_max", 1.0)),
        night=night,
        max_rate_wait_seconds=float(section.get("max_rate_wait_seconds", 30)),
    )
    return limiter, humanize


def build_app(
    config: dict[str, Any],
    *,
    project_root: Path,
    clock: Clock | None = None,
    rng: Random | None = None,
    data_dir: Path | None = None,
    force_mode: str | None = None,
) -> dict[str, Any]:
    clock = clock or SystemClock()
    bot_cfg = config.get("bot") or {}
    data_dir = Path(data_dir or bot_cfg.get("data_dir") or (project_root / "data"))
    if not data_dir.is_absolute():
        data_dir = project_root / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    run_cfg = config.get("run") or {}
    mode_raw = run_cfg.get("mode")
    # 优先级：有效状态文件 > 配置 run.mode > 默认 observe。
    # --mode 或 run.mode_force=true 才会覆盖已有状态。
    mode_force = bool(run_cfg.get("mode_force")) or force_mode not in (None, "")
    if force_mode not in (None, ""):
        mode_raw = force_mode
        intended_source = "--mode"
    elif bool(run_cfg.get("mode_force")):
        intended_source = "mode_force"
    elif "mode" in run_cfg and mode_raw not in (None, ""):
        intended_source = "config"
    else:
        intended_source = "default"
    try:
        run_mode = parse_run_mode(mode_raw)
    except RunModeError as exc:
        # 非法配置默认安全：回到 observe 继续只读运行。
        # 不得把 mode_force 置为 True，以免覆盖已经生效的状态文件。
        logging.getLogger("wechat_bot").warning("非法运行模式，已回退到 observe: %s", exc)
        run_mode = parse_run_mode("observe")
        mode_force = False
        intended_source = "invalid"
    pause_gate = PauseGate(data_dir=data_dir)
    latency_raw = (config.get("pipeline") or {}).get("latency_path")
    latency_path = Path(latency_raw) if latency_raw else (data_dir / LATENCY_JSONL_NAME)
    if not latency_path.is_absolute():
        latency_path = project_root / latency_path
    latency_store = LatencyStore(latency_path)
    send_gate = SendGate(
        mode=run_mode,
        pause_gate=pause_gate,
        state_path=data_dir / "run_mode.json",
        config_explicit=mode_force,
        intended_source=intended_source,
    )
    logging.getLogger("wechat_bot").info(
        "运行模式生效：%s（来源：%s）",
        send_gate.get_mode(),
        send_gate.describe_source(),
    )
    run_mode = send_gate.mode

    groups = []
    for raw in config.get("groups") or [{"group_id": "demo-group", "name": "交易会员群演示"}]:
        groups.append(Group(group_id=str(raw["group_id"]), name=str(raw.get("name") or raw["group_id"])))
    adapter = build_adapter(config, groups=groups, clock=clock)

    limiter, humanize = build_rate_limiter(config, clock, data_dir=data_dir)
    sender = HumanizedSender(
        adapter,
        limiter,
        config=humanize,
        clock=clock,
        rng=rng or Random(),
        send_gate=send_gate,
        latency_store=latency_store,
    )

    pipe_cfg = config.get("pipeline") or {}
    ident_raw = (config.get("bot") or {}).get("identity") or pipe_cfg.get("identity") or {}
    storm_raw = pipe_cfg.get("storm") or (config.get("reply_guard") or {})
    identity = IdentityConfig(
        self_ids=tuple(ident_raw.get("self_ids") or ident_raw.get("wxids") or ()),
        self_names=tuple(ident_raw.get("self_names") or ident_raw.get("names") or ()),
        bot_ids=tuple(ident_raw.get("bot_ids") or ()),
        bot_names=tuple(ident_raw.get("bot_names") or ()),
        bot_name_keywords=tuple(ident_raw.get("bot_name_keywords") or ("机器人", "小助手")),
    )
    storm = StormConfig(
        group_burst_max=int(storm_raw.get("group_burst_max", 5)),
        group_window_seconds=float(storm_raw.get("group_window_seconds", 60)),
        group_cool_seconds=float(storm_raw.get("group_cool_seconds", 180)),
        global_burst_max=int(storm_raw.get("global_burst_max", 15)),
        global_window_seconds=float(storm_raw.get("global_window_seconds", 60)),
        global_cool_seconds=float(storm_raw.get("global_cool_seconds", 180)),
    )
    dedup_state = data_dir / "processed_messages.json"
    dedup = Deduplicator(
        window_seconds=float(pipe_cfg.get("dedup_window_seconds", 600)),
        clock=clock,
        state_path=dedup_state,
    )
    reply_engine = ReplyEngine(clock=clock)
    replies = list(config.get("replies") or [])
    qa_file = pipe_cfg.get("qa_file")
    if qa_file:
        path = Path(qa_file)
        if not path.is_absolute():
            path = project_root / path
        reply_engine.load_from_file(path)
    if replies:
        reply_engine.load_from_mappings(replies, source="config.replies")

    jsonl_path = Path(pipe_cfg.get("jsonl_path") or (data_dir / "records.jsonl"))
    if not jsonl_path.is_absolute():
        jsonl_path = project_root / jsonl_path

    results: list[ProcessResult] = []
    pipeline = Pipeline(
        dedup=dedup,
        classifier=Classifier(),
        extractor=Extractor(),
        reply_engine=reply_engine,
        reply_provider=RuleReplyProvider(reply_engine),
        reply_guard=ReplyGuard(identity=identity, storm=storm, clock=clock),
        sink=JsonlSink(jsonl_path),
        sender=sender,
        clock=clock,
        on_result=results.append,
        latency_store=latency_store,
    )
    adapter.subscribe(pipeline.handle_event)

    sched_state = data_dir / "scheduler_state.json"
    scheduler = AnnouncementScheduler(
        sender=sender,
        clock=clock,
        state_path=sched_state,
    )
    for raw in config.get("announcements") or []:
        scheduler.add(announcement_from_mapping(raw))

    return {
        "adapter": adapter,
        "sender": sender,
        "pipeline": pipeline,
        "scheduler": scheduler,
        "results": results,
        "data_dir": data_dir,
        "jsonl_path": jsonl_path,
        "latency_path": latency_path,
        "latency_store": latency_store,
        "send_gate": send_gate,
        "run_mode": run_mode,
        "run_mode_source": send_gate.mode_source,
        "clock": clock,
    }


def run_demo(
    *,
    config_path: str | Path | None = None,
    project_root: Path | None = None,
    data_dir: Path | None = None,
    clock: Clock | None = None,
    rng: Random | None = None,
    fire_announcement: bool = True,
    inject: list[dict[str, Any]] | None = None,
    demo_fast: bool = True,
    mode: str | None = None,
) -> dict[str, Any]:
    """跑通「注入消息 → 分类 → 自动回复 → 定时公告 → 限速发出」的演示。"""
    project_root = project_root or project_root_from_here()
    logger = setup_logging("INFO")
    config = load_config(config_path, project_root=project_root, logger=logger)

    if demo_fast:
        # 演示默认缩短拟人延迟，避免长时间等待；测试可传入 FakeClock。
        rate = dict(config.get("rate_limit") or {})
        rate.setdefault("send_delay_min", 0.0)
        rate.setdefault("send_delay_max", 0.05)
        rate.setdefault("open_session_delay_min", 0.0)
        rate.setdefault("open_session_delay_max", 0.05)
        rate.setdefault("announcement_delay_min", 0.0)
        rate.setdefault("announcement_delay_max", 0.05)
        rate.setdefault("typing_duration_min", 0.0)
        rate.setdefault("typing_duration_max", 0.02)
        night = dict(rate.get("night_silent") or {})
        night["enabled"] = False
        rate["night_silent"] = night
        config = dict(config)
        config["rate_limit"] = rate
        # 演示必须实际发出，才能看完自动回复和公告。用 mode_force 覆盖可能残留的状态文件。
        run = dict(config.get("run") or {})
        run["mode"] = "reply"
        run["mode_force"] = True
        config["run"] = run

    if clock is None:
        clock = FakeClock(datetime(2026, 11, 11, 9, 0, 5)) if demo_fast else SystemClock()
    if rng is None:
        rng = Random(2026)

    app = build_app(
        config,
        project_root=project_root,
        clock=clock,
        rng=rng,
        data_dir=data_dir,
        force_mode=mode,
    )
    adapter: MockAdapter = app["adapter"]
    sender: HumanizedSender = app["sender"]
    scheduler: AnnouncementScheduler = app["scheduler"]

    adapter.start()
    adapter.inject_from_records(inject if inject is not None else DEMO_MESSAGES)
    sent_replies = sender.pump()

    fired = []
    if fire_announcement:
        if scheduler.list_announcements():
            # 先按配置尝试 tick（演示时钟对齐 09:00 公告）；若未到点则手动触发第一条。
            fired = scheduler.tick()
            if not fired:
                first = scheduler.list_announcements()[0]
                fired = [scheduler.fire_now(first.id)]
        sent_ann = sender.pump()
    else:
        sent_ann = 0

    adapter.stop()

    summary = {
        "results": [
            {
                "message_id": r.message.message_id,
                "content": r.message.content,
                "duplicate": r.duplicate,
                "category": r.category.value if r.category else None,
                "reply_id": r.reply_id,
                "reply_text": r.reply_text,
                "extraction": r.extraction.to_dict() if r.extraction else None,
            }
            for r in app["results"]
        ],
        "sent": [
            {"group_id": s.group_id, "text": s.text, "timestamp": s.timestamp.isoformat()}
            for s in adapter.sent_messages
        ],
        "typing": [(g, t.isoformat()) for g, t in adapter.typing_events],
        "fired_announcements": [
            {
                "id": f.announcement_id,
                "reason": f.reason,
                "occurrence": f.occurrence.isoformat(),
                "groups": list(f.groups),
            }
            for f in fired
        ],
        "sent_reply_count": sent_replies,
        "sent_announcement_count": sent_ann,
        "jsonl_path": str(app["jsonl_path"]),
        "data_dir": str(app["data_dir"]),
    }
    logger.info(
        "演示完成：处理 %s 条，去重 %s 条，发出 %s 条（自动回复泵出 %s，公告泵出 %s）",
        len(summary["results"]),
        sum(1 for r in app["results"] if r.duplicate),
        len(summary["sent"]),
        sent_replies,
        sent_ann,
    )
    return summary


def _print_demo(summary: dict[str, Any]) -> None:
    print("======== 交易会员群机器人 演示 ========")
    print("功能范围：监控群消息 / 整理识别 / 自动回复 / 定时公告")
    print("严禁：博彩、六合彩、下注、结算、收款相关功能\n")
    print("【消息处理】")
    for item in summary["results"]:
        flag = "重复跳过" if item["duplicate"] else (item["category"] or "未分类")
        extra = f" → 回复[{item['reply_id']}]" if item["reply_id"] else ""
        print(f"  - {item['content']!s}  [{flag}]{extra}")
    print("\n【实际发出】")
    if not summary["sent"]:
        print("  （无）")
    for item in summary["sent"]:
        print(f"  - 到 {item['group_id']}: {item['text']}")
    print("\n【定时公告触发】")
    if not summary["fired_announcements"]:
        print("  （无）")
    for item in summary["fired_announcements"]:
        print(f"  - {item['id']} reason={item['reason']} groups={item['groups']}")
    print(f"\n结构化记录: {summary['jsonl_path']}")
    print("演示结束。")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="交易会员群微信机器人（第一阶段骨架）")
    parser.add_argument("--demo", action="store_true", help="使用 MockAdapter 跑通端到端演示")
    parser.add_argument("--config", type=str, default=None, help="配置文件路径（toml/json/yaml）")
    parser.add_argument("--data-dir", type=str, default=None, help="数据目录（JSONL 与调度状态）")
    parser.add_argument("--json", action="store_true", help="演示结果以 JSON 打印")
    parser.add_argument(
        "--mode",
        type=str,
        default=None,
        help="强制运行模式 observe/announce/reply，覆盖状态文件与配置（等同 run.mode_force）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.demo:
        print("请使用 python -m src.main --demo 运行本地演示。第一阶段尚未接入真实微信协议。", file=sys.stderr)
        return 2
    try:
        data_dir = Path(args.data_dir) if args.data_dir else None
        summary = run_demo(config_path=args.config, data_dir=data_dir, mode=args.mode)
    except ForbiddenContentError as exc:
        print(f"已停止：{exc}", file=sys.stderr)
        return 3
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 4
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_demo(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
