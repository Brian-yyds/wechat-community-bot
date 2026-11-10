#!/usr/bin/env python3
"""适配器 A 自检：库路径、只读打开、游标、模拟库端到端。

可在 Windows 与其他平台运行。不写微信原库，不提取密钥，也不打印密钥。

用法（在仓库根目录）：
    python scripts/selfcheck_local_capture.py --simulate
    python scripts/selfcheck_local_capture.py --config config/local.toml
    python scripts/selfcheck_local_capture.py --db path/to/decrypted.db
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SHANGHAI = timezone(timedelta(hours=8))
MIN_PYTHON = (3, 11)


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _ok(flag: bool, name: str, msg: str) -> bool:
    print(("OK  " if flag else "FAIL"), f"{name}:", msg)
    return flag


def check_python() -> tuple[bool, str]:
    v = sys.version_info
    ok = (v.major, v.minor) >= MIN_PYTHON
    return ok, f"Python {v.major}.{v.minor}.{v.micro}（需要 >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}）"


def check_key_provider() -> tuple[bool, str]:
    sys.path.insert(0, str(_root()))
    from src.adapters.local_db import EnvFileKeyProvider

    provider = EnvFileKeyProvider()
    if provider.is_configured():
        # 只报告「已配置」，绝不打印密钥或文件内容。
        return True, "已检测到密钥配置（内容已隐藏，本自检不会读取明文库以外的用途）"
    return True, "未配置密钥（明文或已解密库不需要；若 require_key=true 则会启动失败）"


def _load_config(root: Path, config_path: Path | None) -> tuple[bool, str, dict]:
    sys.path.insert(0, str(root))
    from src.config_util import ConfigError, load_config

    try:
        data = load_config(config_path, project_root=root)
    except ConfigError as exc:
        return False, str(exc), {}
    except Exception as exc:  # noqa: BLE001
        return False, f"配置加载失败: {exc}", {}
    return True, "配置已加载（打印时会脱敏）", data


def _section(config: dict) -> dict:
    from src.adapters.local_capture import extract_local_capture_config

    return extract_local_capture_config(config)


def check_db_path(root: Path, db_path: Path | None, config: dict) -> tuple[bool, str, Path | None]:
    section = _section(config)
    raw = db_path or section.get("db_path") or os.environ.get("WECHAT_LOCAL_DB_PATH") or ""
    if not raw:
        return False, "未提供 db_path（可用 --simulate 先跑模拟库，或传入 --db / 配置 / WECHAT_LOCAL_DB_PATH）", None
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    if not path.exists():
        return False, f"库文件不存在: {path}（将优雅降级，但自检视为未通过）", path
    return True, f"库文件存在: {path}", path


def check_readonly(db_path: Path) -> tuple[bool, str]:
    from src.adapters.local_db import LocalDbCorruptError, LocalDbError, LocalDbReader

    reader = LocalDbReader(db_path, cursor_store={})
    try:
        reader.assert_readonly()
        reader.poll(limit=1)
    except LocalDbCorruptError as exc:
        return False, f"无法只读打开或表结构不匹配，请在 Windows 上核对 schema 映射: {exc}"
    except LocalDbError as exc:
        return False, f"只读打开失败: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"只读打开出现未预期错误: {exc}"
    finally:
        reader.close()
    return True, "只读打开成功，探测写入已被拒绝或不可用"


def check_cursor_dir(root: Path, config: dict) -> tuple[bool, str, Path]:
    section = _section(config)
    bot = config.get("bot") or {}
    data_dir = Path(bot.get("data_dir") or "data")
    if not data_dir.is_absolute():
        data_dir = root / data_dir
    cursor = Path(section.get("cursor_path") or (data_dir / "local_capture_cursor.json"))
    if not cursor.is_absolute():
        cursor = root / cursor
    try:
        cursor.parent.mkdir(parents=True, exist_ok=True)
        probe = cursor.parent / ".selfcheck_local_capture_write"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return False, f"游标目录不可写: {cursor.parent} ({exc})", cursor
    return True, f"游标目录可写: {cursor.parent}", cursor


def _seed_sim(path: Path) -> None:
    from src.adapters.local_db import build_simulated_wechat_db

    start = datetime(2026, 11, 11, 10, 0, 0, tzinfo=SHANGHAI)
    groups = [
        {"wxid": "muted@chatroom", "name": "免打扰交易群", "muted": 1, "collapsed": 0, "last_opened_at": None},
        {"wxid": "fold@chatroom", "name": "折叠交易群", "muted": 0, "collapsed": 1, "last_opened_at": None},
        {"wxid": "idle@chatroom", "name": "未打开交易群", "muted": 0, "collapsed": 0, "last_opened_at": None},
        {"wxid": "open@chatroom", "name": "当前打开的群", "muted": 0, "collapsed": 0, "last_opened_at": start.isoformat()},
    ]
    contacts = [{"wxid": "wxid_member1", "nickname": "会员甲"}, {"wxid": "wxid_self", "nickname": "本小号"}]
    messages = []
    for i, gid in enumerate(["muted@chatroom", "fold@chatroom", "idle@chatroom", "open@chatroom"]):
        messages.append(
            {
                "msg_id": f"sim-{i}",
                "group_id": gid,
                "sender_id": "wxid_member1",
                "sender_name": "会员甲",
                "ts": start + timedelta(seconds=i),
                "content": f"模拟消息{i}",
                "msg_type": 1,
            }
        )
    messages.append(
        {
            "msg_id": "sim-image",
            "group_id": "idle@chatroom",
            "sender_id": "wxid_member1",
            "content": "",
            "ts": start + timedelta(seconds=10),
            "msg_type": 3,
        }
    )
    messages.append(
        {
            "msg_id": "sim-self",
            "group_id": "open@chatroom",
            "sender_id": "wxid_self",
            "sender_name": "本小号",
            "ts": start + timedelta(seconds=11),
            "content": "我自己发的",
            "msg_type": 1,
        }
    )
    build_simulated_wechat_db(path, contacts=contacts, groups=groups, messages=messages)


def check_simulate(root: Path, data_dir: Path) -> tuple[bool, str]:
    from src.adapters.local_db import LocalDbReader, catch_up, insert_simulated_message, reconcile

    db_path = data_dir / "simulated_wechat.db"
    store = data_dir / "local_capture_cursor.json"
    if store.exists():
        store.unlink()
    _seed_sim(db_path)
    whitelist = ["muted@chatroom", "fold@chatroom", "idle@chatroom", "open@chatroom"]
    reader = LocalDbReader(
        db_path,
        cursor_store=store,
        group_whitelist=whitelist,
        self_ids=["wxid_self"],
    )
    try:
        reader.assert_readonly()
        first = reader.poll()
        ids = [m["msg_id"] for m in first]
        if reader.poll():
            return False, "第二次拉取不应重复吐出已读消息"
        expected = ["sim-0", "sim-1", "sim-2", "sim-3", "sim-image", "sim-self"]
        report = reconcile(expected, ids)
        if not report.ok:
            return False, f"模拟库对账失败 miss={report.missed_ids} dup={report.duplicate_ids}"
        self_row = next(m for m in first if m["msg_id"] == "sim-self")
        if not self_row.get("is_self"):
            return False, "自己的消息未标记 is_self"
        image = next(m for m in first if m["msg_id"] == "sim-image")
        if image.get("is_text"):
            return False, "非文本消息应标注为非文本占位"
        if "detected_at" not in first[0]:
            return False, "事件缺少 detected_at"
        # 重启后续读
        insert_simulated_message(
            db_path,
            msg_id="sim-new",
            group_id="muted@chatroom",
            sender_id="wxid_member1",
            content="重启后新消息",
            ts=datetime(2026, 11, 11, 10, 1, tzinfo=SHANGHAI),
        )
        reader2 = LocalDbReader(db_path, cursor_store=store, group_whitelist=whitelist, self_ids=["wxid_self"])
        second = reader2.poll()
        if [m["msg_id"] for m in second] != ["sim-new"]:
            return False, f"游标重启后续读异常: {[m['msg_id'] for m in second]}"
        # 对账缺口补抓（人为制造一个未发出 ID 的报告）
        from src.adapters.local_db import ReconcileReport

        fake = ReconcileReport(expected=1, processed_unique=0, missed_ids=["sim-0"], catch_up_ids=["sim-0"])
        # sim-0 已在 seen_ids 中，补抓应返回空（不重复）
        replay = catch_up(reader2, fake)
        if replay:
            return False, "已处理消息被补抓重复发出"
        reader2.close()
    finally:
        reader.close()
    return True, f"模拟库端到端通过：首次 {len(first)} 条，重启后续读 1 条，免打扰/折叠/未打开群均已覆盖"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="适配器 A 本地库只读抓取自检")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--db", default=None, help="已解密或模拟库路径")
    parser.add_argument("--simulate", action="store_true", help="使用内置模拟库做端到端自检")
    args = parser.parse_args(argv)
    root = _root()
    os.chdir(root)
    sys.path.insert(0, str(root))

    print("======== 适配器 A 自检开始 ========")
    return _run(root, args)


def _run(root: Path, args) -> int:
    flags: list[bool] = []
    ok, msg = check_python()
    flags.append(_ok(ok, "Python", msg))

    cfg_ok, cfg_msg, config = _load_config(root, Path(args.config) if args.config else None)
    if args.simulate and not args.config:
        cfg_ok, cfg_msg, config = True, "模拟模式，未强制加载真实配置", {}
    flags.append(_ok(cfg_ok, "配置", cfg_msg))

    ok, msg = check_key_provider()
    flags.append(_ok(ok, "密钥", msg))

    if cfg_ok:
        dir_ok, dir_msg, cursor = check_cursor_dir(root, config)
    else:
        dir_ok, dir_msg, cursor = False, "跳过（配置未加载）", root / "data" / "local_capture_cursor.json"
    flags.append(_ok(dir_ok, "游标目录", dir_msg))

    if args.simulate or not (args.db or _section(config).get("db_path") or os.environ.get("WECHAT_LOCAL_DB_PATH")):
        sim_dir = (cursor.parent if dir_ok else root / "data") / "selfcheck_local_capture"
        sim_dir.mkdir(parents=True, exist_ok=True)
        ok, msg = check_simulate(root, sim_dir)
        flags.append(_ok(ok, "模拟端到端", msg))
        if not args.db and not _section(config).get("db_path"):
            print("INFO 真实库路径未配置，已只跑模拟库。在用户 Windows 上请再对真实 db_path 执行本脚本。")
    else:
        path_ok, path_msg, db_path = check_db_path(root, Path(args.db) if args.db else None, config)
        flags.append(_ok(path_ok, "库路径", path_msg))
        if path_ok and db_path is not None:
            ro_ok, ro_msg = check_readonly(db_path)
            flags.append(_ok(ro_ok, "只读打开", ro_msg))
            # 再跑一遍模拟，保证脚本在 Windows 上也能验证游标逻辑
            sim_dir = cursor.parent / "selfcheck_local_capture"
            sim_dir.mkdir(parents=True, exist_ok=True)
            ok, msg = check_simulate(root, sim_dir)
            flags.append(_ok(ok, "模拟端到端", msg))

    all_ok = all(flags)
    print("======== 适配器 A 自检", "通过" if all_ok else "未通过", "========")
    print("说明：真实微信 4.x 表结构必须在用户 Windows 上验证；本脚本不会提取密钥，也不会写原库。")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
