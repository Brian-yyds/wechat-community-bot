# -*- coding: utf-8 -*-
"""发送正确性：群名匹配、空文本、超长、换行表情、剪贴板、焦点、错误群。"""
from __future__ import annotations

import inspect
from pathlib import Path

from src.adapters.pyweixin_driver import PyWeixinDriver
from src.adapters.ui_driver import FakeUiDriver
from src.adapters.uia import UiaSendStatus, resolve_group_name

from review_b.helpers import make_adapter


def test_group_id_maps_to_display_name_and_opens_that_chat(tmp_path: Path):
    driver = FakeUiDriver(known_chats={"交易会员群演示"})
    adapter = make_adapter(tmp_path, driver=driver, group_names={"demo-group": "交易会员群演示"})
    result = adapter.send("demo-group", "映射后发送")
    assert result.ok
    assert driver.opened == ["交易会员群演示"]
    assert driver.pressed == ["交易会员群演示"]


def test_unknown_group_is_target_not_found_and_not_sent(tmp_path: Path):
    driver = FakeUiDriver(known_chats={"交易会员群-主群"})
    adapter = make_adapter(tmp_path, driver=driver)
    result = adapter.send("不存在的群", "不该发出")
    assert not result.ok
    assert result.status == UiaSendStatus.TARGET_NOT_FOUND
    assert driver.pressed == []
    assert driver.pasted == []


def test_prefix_and_duplicate_names_use_exact_match_only_in_adapter(tmp_path: Path):
    """适配器层 resolve_group_name 只做精确字典查找，没有模糊/前缀消歧。"""
    mapping = {
        "short": "交易会员群",
        "main": "交易会员群-主群",
        "demo": "交易会员群演示",
    }
    assert resolve_group_name("short", mapping) == "交易会员群"
    assert resolve_group_name("交易会员群-主", mapping) == "交易会员群-主"  # 未配置则原样下发
    driver = FakeUiDriver(known_chats={"交易会员群", "交易会员群-主群", "交易会员群演示"})
    adapter = make_adapter(tmp_path, driver=driver, group_names=mapping)
    result = adapter.send("short", "发到短名")
    assert result.ok
    assert driver.opened == ["交易会员群"]
    assert driver.pressed == ["交易会员群"]


def test_stale_current_chat_must_not_send_to_wrong_group(tmp_path: Path):
    """若 current_chat() 返回过期值（PyWeixinDriver 只记内存），可能对已切走的窗口误发。

    用包装驱动模拟：适配器以为当前仍是主群，实际窗口已切到二群。
    """

    class StaleCurrentDriver(FakeUiDriver):
        stale_name: str | None = None

        def current_chat(self) -> str | None:
            if self.stale_name is not None:
                return self.stale_name
            return super().current_chat()

    driver = StaleCurrentDriver(known_chats={"交易会员群-主群", "二群"})
    adapter = make_adapter(tmp_path, driver=driver)
    driver.open_chat("交易会员群-主群")
    # 用户（或搜索误差）把窗口切到二群，但驱动仍报告主群
    driver.current = "二群"
    driver.stale_name = "交易会员群-主群"
    result = adapter.send("g1", "不能发错群")
    # 正确行为：必须重新确认窗口就是目标群，不能把文本粘到二群
    assert driver.pressed != ["二群"], "current_chat 过期时把消息发进了错误的群「二群」"
    assert "二群" not in driver.pressed
    assert result.ok
    assert driver.pressed == ["交易会员群-主群"]


def test_empty_and_whitespace_text_must_be_rejected(tmp_path: Path):
    adapter = make_adapter(tmp_path)
    for text in ("", "   ", "\n\t"):
        result = adapter.send("g1", text, send_id=f"empty-{text!r}")
        assert not result.ok, f"空/空白文本不应发送，实际 status={result.status}"
        assert result.status in {UiaSendStatus.REJECTED, "empty", "invalid"}
    assert adapter.driver.pressed == []  # type: ignore[attr-defined]
    assert adapter.driver.pasted == []  # type: ignore[attr-defined]


def test_newline_emoji_mention_are_passed_through_intact(tmp_path: Path):
    text = "你好😊\n第二行\n@管理员 请看一下"
    adapter = make_adapter(tmp_path)
    result = adapter.send("g1", text)
    assert result.ok
    assert adapter.driver.pasted == [text]  # type: ignore[attr-defined]


def test_long_text_is_not_silently_truncated_in_adapter(tmp_path: Path):
    text = "长消息" * 2000  # 约 8000 字，超过常见单条上限
    adapter = make_adapter(tmp_path)
    result = adapter.send("g1", text)
    # 允许拒绝或分段，但不允许悄然截断后当作一次成功发出
    if result.ok:
        pasted = adapter.driver.pasted  # type: ignore[attr-defined]
        assert pasted, "成功发送却没有粘贴内容"
        if len(pasted) == 1:
            assert pasted[0] == text, "超长文本被截断后仍一次发送成功，缺少分段策略"


def test_pyweixin_open_chat_does_not_verify_actual_title():
    src = inspect.getsource(PyWeixinDriver._pywinauto_open_chat)
    # 搜索框输入群名并回车后直接把 _current 设为目标名，没有读取窗口标题核对
    assert "self._current = group_name" in src
    has_verify = any(
        token in src
        for token in ("window_text", "window_title", "get_title", "current_chat", "verify", "标题")
    )
    assert has_verify, (
        "PyWeixinDriver._pywinauto_open_chat 在 Ctrl+F 搜索回车后不校验实际打开的会话标题，"
        "重名、同名前缀或搜索失败时可能发错群，且仍把 _current 写成目标名"
    )


def test_pyweixin_current_chat_is_memory_only():
    src = inspect.getsource(PyWeixinDriver.current_chat)
    assert "return self._current" in src
    # 不应在未查询 UI 的情况下声称知道当前会话
    queries_ui = any(tok in src for tok in ("_ensure", "_find_callable", "_top_window", "window_text"))
    assert not queries_ui, "若以后改为查询 UI，请同步更新本断言"
    # 记录缺陷：内存值不会随用户切群或搜索误差更新
    driver = PyWeixinDriver()
    driver._current = "交易会员群-主群"
    assert driver.current_chat() == "交易会员群-主群"


def test_pyweixin_clipboard_fallback_does_not_restore_or_clear():
    src = inspect.getsource(PyWeixinDriver._pywinauto_paste)
    uses_clipboard = "win32clipboard" in src and "SetClipboardText" in src
    assert uses_clipboard, "预期存在剪贴板降级路径"
    restores = any(
        tok in src
        for tok in ("GetClipboard", "SetClipboardData", "restore", "previous", "old_clip", "saved")
    )
    clears_after = "EmptyClipboard" in src.split("type_keys")[-1] if "type_keys" in src else False
    assert restores, (
        "剪贴板降级路径覆盖用户剪贴板后不恢复原内容，会干扰用户复制粘贴"
    )
    # 即便不恢复，发送后也应清空，避免消息正文留在剪贴板
    if not restores:
        assert clears_after, "发送后未清空剪贴板，消息正文残留"


def test_pyweixin_type_keys_group_name_is_unsafe_for_special_chars():
    """回归：搜索群名若走 type_keys，必须先转义 +、^、%、{、} 等特殊字符。"""
    from src.adapters.pyweixin_driver import escape_pywinauto_keys

    src = inspect.getsource(PyWeixinDriver._pywinauto_open_chat)
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert "escape_pywinauto_keys" in src, "_pywinauto_open_chat 必须转义后再 type_keys"
    assert "type_keys(escaped_name" in code
    assert "type_keys(group_name" not in code, "不得把未转义的群名直接交给 type_keys"
    raw = "群+名^测%试{a}(b)"
    assert escape_pywinauto_keys(raw) == "群{+}名{^}测{%}试{{}a{}}{(}b{)}"


def test_focus_is_stolen_by_open_and_input():
    focus_src = inspect.getsource(PyWeixinDriver.focus_input)
    open_src = inspect.getsource(PyWeixinDriver._pywinauto_open_chat)
    # 记录：真实路径会抢占窗口与输入焦点（Windows 真机必须观察对用户操作的干扰）
    assert "set_focus" in focus_src or "set_focus" in open_src
