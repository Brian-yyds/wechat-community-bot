# -*- coding: utf-8 -*-
"""复测新增用例：闸门回归、底层驱动无闸门的风险边界、标题核对与异常收敛。

新增用例只放本文件。不触碰仓库 data/PAUSE，一律用 tmp_path 隔离。
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from src.adapters.composite import CompositeAdapter
from src.adapters.mock import MockAdapter
from src.adapters.pause import PauseGate
from src.adapters.pyweixin_driver import PyWeixinDriver
from src.adapters.ui_driver import FakeUiDriver, titles_match
from src.adapters.uia import UiaOutboundAdapter, UiaSendStatus
from src.clock import FakeClock
from src.latency import LatencyStore, LatencyTrace
from src.ratelimit.bucket import BucketSpec, RateLimitConfig, RateLimiter
from src.ratelimit.sender import HumanizeConfig, HumanizedSender, NightSilentConfig, SendKind
from src.run_mode import RunMode, SendGate

from review_b.helpers import make_adapter
from review_b.test_b_bypass_and_gates import _humanized, _tiny_limiter

REPO = Path(__file__).resolve().parents[2]

# 允许出现 paste_text / press_send 调用的生产文件：驱动实现本身，以及适配器 send 内部。
_ALLOWED_PASTE_PRESS_RELPATHS = {
    "src/adapters/ui_driver.py",
    "src/adapters/pyweixin_driver.py",
    "src/adapters/uia.py",
}

_PRODUCTION_ROOTS = (REPO / "src", REPO / "scripts")


def _iter_py_files(*roots: Path):
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path


def test_uidriver_paste_and_press_have_no_gates_document_boundary(tmp_path: Path):
    """记录事实：UiDriver.paste_text / press_send 自身无 RateLimiter、运行模式、PAUSE、幂等闸门。

    风险边界：这两步只负责键盘/剪贴板动作。若生产代码绕过 HumanizedSender 与
    UiaOutboundAdapter.send，直接拿 driver 调用，就会无限制发出。本用例用 AST + 源码检索
    确认 src/ 与 scripts/ 中，除驱动实现与适配器 send 外，没有这样的调用路径。
    """
    driver = FakeUiDriver(known_chats={"交易会员群-主群"})
    driver.open_chat("交易会员群-主群")
    (tmp_path / "PAUSE").write_text("stop", encoding="utf-8")
    driver.paste_text("底层无闸门")
    driver.press_send()
    assert driver.pasted == ["底层无闸门"]
    assert driver.pressed == ["交易会员群-主群"]

    hits: list[str] = []
    for path in _iter_py_files(*_PRODUCTION_ROOTS):
        rel = path.relative_to(REPO).as_posix()
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            hits.append(f"{rel}: 无法解析 AST: {exc}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else None)
            if name not in {"paste_text", "press_send"}:
                continue
            if rel not in _ALLOWED_PASTE_PRESS_RELPATHS:
                hits.append(f"{rel}:{node.lineno}: 生产代码直接调用 {name}()，绕过 HumanizedSender / 适配器 send")
    assert hits == [], "发现绕过正式发送出口直接调用驱动的路径:\n" + "\n".join(hits)

    # 适配器内调用必须位于 UiaOutboundAdapter.send（按下发送前已复查 PAUSE 与标题）。
    send_src = inspect.getsource(UiaOutboundAdapter.send)
    assert "self.driver.paste_text" in send_src
    assert "self.driver.press_send" in send_src
    assert "_check_pause" in send_src
    assert "titles_match" in send_src

    # through_official=True 在生产代码中只应由 HumanizedSender 正式出口使用。
    unofficial: list[str] = []
    for path in _iter_py_files(*_PRODUCTION_ROOTS):
        rel = path.relative_to(REPO).as_posix()
        if rel == "src/ratelimit/sender.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "through_official" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    unofficial.append(f"{rel}:{node.lineno}")
    assert unofficial == [], (
        "生产代码中 besides HumanizedSender 还有 through_official=True，可能绕过闸门: "
        + ", ".join(unofficial)
    )


def test_production_sources_do_not_reference_driver_send_outside_allowed_files():
    """grep 补充：除允许文件外，src/scripts 不得出现 driver.paste_text / driver.press_send 字面调用。"""
    bad: list[str] = []
    for path in _iter_py_files(*_PRODUCTION_ROOTS):
        rel = path.relative_to(REPO).as_posix()
        if rel in _ALLOWED_PASTE_PRESS_RELPATHS:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.split("#", 1)[0]
            if "paste_text" in stripped or "press_send" in stripped:
                bad.append(f"{rel}:{i}: {stripped.strip()}")
    assert bad == [], "生产代码出现驱动发送字面量，可能绕过闸门:\n" + "\n".join(bad)


def test_direct_send_blocked_in_observe_announce_and_by_pause_and_limit(tmp_path: Path):
    """公开 send / send_text / UiaSender.send / Composite 在挂接后受 observe/announce/PAUSE/限频约束。"""
    clock = FakeClock()
    adapter = make_adapter(tmp_path, clock=clock)

    observe = _humanized(adapter, clock, tmp_path, mode=RunMode.OBSERVE)
    r_obs = adapter.send("g1", "observe 直接发送")
    assert not r_obs.ok and r_obs.status == UiaSendStatus.REJECTED
    assert adapter.driver.pressed == []  # type: ignore[attr-defined]

    # 换成 announce：自动回复应拒，公告可通过闸门（仍计限额）
    announce = _humanized(adapter, clock, tmp_path, mode=RunMode.ANNOUNCE)
    r_auto = adapter.send("g1", "announce 下的自动回复", kind="auto_reply")
    assert not r_auto.ok
    assert adapter.driver.pressed == []  # type: ignore[attr-defined]
    r_ann = adapter.send("g1", "announce 下的公告", kind="announcement")
    assert r_ann.ok, f"announce 模式的公告应能发出，实际 {r_ann.status} {r_ann.error}"
    assert adapter.driver.pressed == ["交易会员群-主群"]  # type: ignore[attr-defined]

    # PAUSE 优先于模式：reply 下中途暂停也拒发
    reply_adapter = make_adapter(tmp_path, clock=clock)
    _humanized(reply_adapter, clock, tmp_path, mode=RunMode.REPLY)
    (tmp_path / "PAUSE").write_text("stop", encoding="utf-8")
    # 适配器自身 pause_flag 指向 tmp_path/PAUSE，与官方闸门的 GATE_PAUSE 分开；
    # 适配器层 _check_pause 仍应拒绝。
    r_pause = reply_adapter.send("g1", "已暂停不得发", through_official=True)
    assert not r_pause.ok
    assert r_pause.status == UiaSendStatus.PAUSED
    assert reply_adapter.driver.pressed == []  # type: ignore[attr-defined]


def test_direct_send_and_official_path_share_hourly_quota(tmp_path: Path):
    """直接 send 与正式出口共用限额；--send-test 同类路径必须计入限额。"""
    clock = FakeClock()
    adapter = make_adapter(tmp_path, clock=clock)
    limiter = RateLimiter(
        RateLimitConfig(
            global_bucket=BucketSpec(20, 20.0),
            per_group=BucketSpec(20, 20.0),
            announcement=BucketSpec(20, 20.0),
            auto_reply=BucketSpec(20, 20.0),
            global_replies_per_hour=1,
        ),
        clock=clock,
    )
    sender = HumanizedSender(
        adapter,
        limiter,
        config=HumanizeConfig(
            send_delay_min=0.0,
            send_delay_max=0.0,
            typing_hook=False,
            night=NightSilentConfig(enabled=False),
        ),
        clock=clock,
        send_gate=SendGate(mode=RunMode.REPLY, pause_gate=PauseGate(tmp_path / "GATE_PAUSE")),
    )
    # 正式出口发出一条，占掉每小时 1 条的额度
    sender.submit_auto_reply("g1", "占额正式出口")
    assert sender.pump(max_items=1) == 1
    assert adapter.driver.pressed.count("交易会员群-主群") == 1  # type: ignore[attr-defined]

    # 再走正式出口应被限频拦住
    sender.submit_auto_reply("g1", "第二封正式出口")
    assert sender.pump(max_items=1) == 0

    # 直接 send 也必须计入/受同一限额，不得绕过再发
    direct = adapter.send("g1", "直接发送也应占额")
    assert not direct.ok, f"限额用尽后直接 send 仍发出: {direct.status} {direct.error}"
    assert adapter.driver.pressed.count("交易会员群-主群") == 1  # type: ignore[attr-defined]


def test_send_test_script_goes_through_humanized_sender_and_quota():
    """源码约定：scripts/selfcheck_uia.py 的 --send-test 必须走 HumanizedSender 并计入限额。"""
    src = (REPO / "scripts" / "selfcheck_uia.py").read_text(encoding="utf-8")
    assert "HumanizedSender" in src
    assert "SendGate" in src
    assert "build_rate_limiter" in src or "RateLimiter" in src
    assert "sender.pump" in src
    # 真实发送入口不得再直接 adapter.send；应 submit + pump
    maybe = src.split("def maybe_real_send", 1)[1].split("def UiaPause", 1)[0]
    assert "adapter.send(" not in maybe
    assert "sender.submit(" in maybe
    assert "sender.pump(" in maybe
    assert "计入正式" in src or "计入限额" in src or "计入正式限速" in maybe


def test_window_title_rejects_prefix_stale_and_search_mismatch(tmp_path: Path):
    """重名/同名前缀/中途切群/搜索失败：必须按 read_chat_title 严格核对，不得发错群。"""

    class PrefixDriver(FakeUiDriver):
        def open_chat(self, group_name: str) -> None:
            # 模拟搜索命中更长的同名前缀会话
            if group_name == "交易会员群":
                self.current = "交易会员群-主群"
                self.opened.append(self.current)
                return
            super().open_chat(group_name)

    prefix = PrefixDriver(known_chats={"交易会员群", "交易会员群-主群"})
    adapter = make_adapter(tmp_path, driver=prefix, group_names={"short": "交易会员群"})
    result = adapter.send("short", "不能发到更长前缀的群")
    assert not result.ok
    assert result.status == UiaSendStatus.TARGET_NOT_FOUND
    assert prefix.pressed == []
    assert "不能发到更长前缀的群" not in prefix.pasted

    class SwitchBeforePaste(FakeUiDriver):
        def focus_input(self) -> None:
            super().focus_input()
            # 用户在聚焦后把窗口切到二群
            self.current = "二群"

    switched = SwitchBeforePaste(known_chats={"交易会员群-主群", "二群"})
    adapter2 = make_adapter(tmp_path, driver=switched)
    result2 = adapter2.send("g1", "中途切群不得发出")
    assert not result2.ok
    assert result2.status == UiaSendStatus.TARGET_NOT_FOUND
    assert "二群" not in switched.pressed
    assert switched.pressed == []

    class SearchFail(FakeUiDriver):
        def open_chat(self, group_name: str) -> None:
            self.current = None
            self.opened.append(group_name)

    failed = SearchFail(known_chats={"交易会员群-主群"})
    adapter3 = make_adapter(tmp_path, driver=failed)
    result3 = adapter3.send("g1", "搜索失败不得假装成功")
    assert not result3.ok
    assert result3.status == UiaSendStatus.TARGET_NOT_FOUND
    assert failed.pressed == []


def test_window_title_spaces_ok_fullwidth_should_normalize(tmp_path: Path):
    """空白（含全角空格）应视为同一标题；全半角等价字符也应规范化后精确匹配（与适配器 D 一致）。"""
    # 空格：首尾与连续空白、全角空格
    assert titles_match("  交易会员群-主群  ", "交易会员群-主群")
    assert titles_match("交易会员群\u3000主群", "交易会员群 主群")
    assert not titles_match("交易会员群", "交易会员群-主群")
    assert not titles_match("交易会员群-主", "交易会员群-主群")

    class WidthTitleDriver(FakeUiDriver):
        def read_chat_title(self) -> str | None:
            if self.current == "交易会员群-主群":
                # 窗口标题用全角连字符
                return "交易会员群－主群"
            return self.current

        def open_chat(self, group_name: str) -> None:
            super().open_chat(group_name)

    driver = WidthTitleDriver(known_chats={"交易会员群-主群"})
    adapter = make_adapter(tmp_path, driver=driver)
    result = adapter.send("g1", "全半角应能匹配")
    assert result.ok, (
        "窗口标题仅全半角差异时应规范化后视为同一会话，避免误报 target_not_found；"
        f" 实际 status={result.status} error={result.error!r} "
        f"titles_match={titles_match('交易会员群－主群', '交易会员群-主群')}"
    )
    assert driver.pressed == ["交易会员群-主群"]


def test_non_uia_error_during_paste_is_contained_and_pauses(tmp_path: Path):
    """粘贴阶段抛出非 UiaError 时，send 应收敛为失败并暂停，不得把异常外溢。"""

    class BoomPaste(FakeUiDriver):
        def paste_text(self, text: str) -> None:
            raise RuntimeError("粘贴时意外失败")

    driver = BoomPaste(known_chats={"交易会员群-主群"})
    adapter = make_adapter(tmp_path, driver=driver)
    result = adapter.send("g1", "异常应收敛")
    assert result is not None
    assert not result.ok
    assert result.status in {UiaSendStatus.UI_NOT_READY, UiaSendStatus.PAUSED, UiaSendStatus.UNKNOWN}
    assert (tmp_path / "PAUSE").exists()
    assert driver.pressed == []


def test_corrupt_store_is_quarantined_and_alerted(tmp_path: Path):
    adapter = make_adapter(tmp_path)
    assert adapter.send("g1", "先发出", send_id="fragile").ok
    store_path = tmp_path / "data" / "uia_sent_ids.json"
    store_path.write_text("{not-json", encoding="utf-8")
    restarted = make_adapter(tmp_path)
    again = restarted.send("g1", "先发出", send_id="fragile")
    assert not again.ok
    assert not again.skipped
    assert restarted.driver.pressed == []  # type: ignore[attr-defined]
    quarantined = store_path.with_name(store_path.name + ".corrupt")
    assert quarantined.exists(), "损坏的幂等文件应被隔离，而不是静默清空"
    assert not store_path.exists() or store_path.read_text(encoding="utf-8") != "{not-json"
    assert any(a.get("code") == "idempotency_store_corrupt" for a in restarted.alerts)


def test_official_latency_includes_uia_stages_and_chat_was_open(tmp_path: Path):
    clock = FakeClock()
    driver = FakeUiDriver(known_chats={"交易会员群-主群"})
    adapter = make_adapter(tmp_path, driver=driver, clock=clock)
    driver.open_chat("交易会员群-主群")
    store = LatencyStore(tmp_path / "latency.jsonl")
    sender = _humanized(adapter, clock, tmp_path, store=store)
    trace = LatencyTrace(message_id="lat-retest", group_id="g1", kind="auto_reply")
    sender.submit_auto_reply("g1", "分段打点", trace=trace)
    assert sender.pump(max_items=1) == 1
    rows = store.read_all()
    assert rows
    row = rows[-1]
    extra = row.get("extra") or {}
    for key in ("open_chat", "paste", "send", "verify", "chat_was_open"):
        assert key in extra or key in row, f"latency.jsonl 缺少 {key}: extra={extra} keys={list(row.keys())}"
    session = row.get("session_open")
    if session is None:
        session = extra.get("chat_was_open")
    assert session is True


def test_maybe_sent_not_counted_as_success_and_empty_rejected(tmp_path: Path):
    clock = FakeClock()
    driver = FakeUiDriver(known_chats={"交易会员群-主群"}, fail_mode="rejected")
    adapter = make_adapter(tmp_path, driver=driver, clock=clock)
    store = LatencyStore(tmp_path / "latency.jsonl")
    sender = _humanized(adapter, clock, tmp_path, store=store)
    trace = LatencyTrace(message_id="maybe-1", group_id="g1", kind="auto_reply")
    sender.submit_auto_reply("g1", "校验不确定", trace=trace)
    assert sender.pump(max_items=1) == 0
    assert sender.sent_count == 0
    assert adapter.last_result is not None
    assert adapter.last_result.status == UiaSendStatus.MAYBE_SENT
    rows = store.read_all()
    assert rows and rows[-1].get("outcome") != "sent"

    clean = make_adapter(tmp_path)
    empty = clean.send("g1", "   ")
    assert not empty.ok and empty.status == UiaSendStatus.REJECTED
    assert clean.driver.pressed == []  # type: ignore[attr-defined]
