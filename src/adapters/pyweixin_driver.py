"""可选的真实 Windows UIA 驱动（pyweixin / pywinauto），延迟导入。

设计约定：
- 本模块在 Linux / macOS 上导入不得报错；
- 缺少依赖或非 Windows 平台时，在实际调用时给出清晰提示；
- 不要假装已经在真实微信客户端上验证过可用性；
- 发现掉线、锁屏、验证码时只上报，绝不自动反复重登或打码。
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from src.adapters.ui_driver import (
    ChatNotFoundError,
    SendTimeoutError,
    UiaDependencyError,
    UiDriver,
    UiNotReadyError,
)

logger = logging.getLogger(__name__)

_MISSING_HINT = (
    "真实 UIA 发送需要在用户 Windows 电脑上安装可选依赖："
    "优先 pip install pyweixin（Hello-Mr-Crab/pywechat 仓库中支持微信 4.1 的模块），"
    "或 pip install pywinauto。"
    "当前环境未安装这些库，无法驱动官方 PC 微信窗口。"
    "本驱动尚未在真实微信客户端上做验收，请先运行 scripts/selfcheck_uia.py 做 dry-run。"
)

_PLATFORM_HINT = (
    "真实 UIA 只能在已登录官方 PC 微信的 Windows 桌面会话中运行。"
    "Linux box 请使用 FakeUiDriver 做模拟测试，不要指望在此直接操作微信窗口。"
)


def describe_backend() -> dict[str, Any]:
    """探测可选依赖，不进行真实 UI 操作。可在任何平台调用。"""
    info: dict[str, Any] = {
        "platform": sys.platform,
        "is_windows": sys.platform == "win32",
        "pyweixin": False,
        "pywinauto": False,
        "backend": None,
        "hint": None,
    }
    try:
        import pyweixin  # type: ignore  # noqa: F401

        info["pyweixin"] = True
        info["backend"] = "pyweixin"
        info["pyweixin_version"] = getattr(pyweixin, "__version__", "unknown")
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        info["pyweixin_import_error"] = str(exc)
    try:
        import pywinauto  # type: ignore

        info["pywinauto"] = True
        if not info["backend"]:
            info["backend"] = "pywinauto"
        info["pywinauto_version"] = getattr(pywinauto, "__version__", "unknown")
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        info["pywinauto_import_error"] = str(exc)
    if sys.platform != "win32":
        info["hint"] = _PLATFORM_HINT
    elif not info["pyweixin"] and not info["pywinauto"]:
        info["hint"] = _MISSING_HINT
    else:
        info["hint"] = (
            "已检测到可选依赖，但本仓库未在真实微信客户端上验证过该驱动，"
            "请以 selfcheck_uia.py 的探测结果为准，不要把它当作已验收。"
        )
    return info


class PyWeixinDriver(UiDriver):
    """尽力而为的 pyweixin / pywinauto 包装。

    延迟导入：构造时不强制加载 Windows 库。
    若 API 与当前库版本不匹配，会抛出清晰错误，而不是静默假装发送成功。
    """

    def __init__(self) -> None:
        self._backend_name: str | None = None
        self._backend: Any = None
        self._messages: Any = None
        self._current: str | None = None
        self._loaded = False

    def _ensure(self) -> None:
        if self._loaded:
            return
        if sys.platform != "win32":
            raise UiaDependencyError(_PLATFORM_HINT)
        errors: list[str] = []
        # 优先 pyweixin（跟微信 4.1 较紧），其次 pywinauto 作为更底层备选。
        try:
            self._try_load_pyweixin()
            self._loaded = True
            return
        except ImportError as exc:
            errors.append(f"pyweixin: {exc}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"pyweixin 已安装但无法适配 API: {exc}")
        try:
            self._try_load_pywinauto()
            self._loaded = True
            return
        except ImportError as exc:
            errors.append(f"pywinauto: {exc}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"pywinauto 已安装但无法适配 API: {exc}")
        raise UiaDependencyError(_MISSING_HINT + " 详情: " + "；".join(errors))

    def _try_load_pyweixin(self) -> None:
        # 常见入口随上游版本变化，逐个尝试；都失败则明确告知未适配。
        import pyweixin  # type: ignore  # noqa: F401

        candidates = [
            "pyweixin.WechatAuto",
            "pyweixin.WeChatAuto",
            "pyweixin",
        ]
        module = None
        last_err: Exception | None = None
        for name in candidates:
            try:
                parts = name.split(".")
                module = __import__(name, fromlist=["*"] if len(parts) > 1 else [])
                break
            except ImportError as exc:
                last_err = exc
                module = None
        if module is None:
            raise UiaDependencyError(f"无法导入 pyweixin 子模块: {last_err}")

        messages = (
            getattr(module, "Messages", None)
            or getattr(module, "Message", None)
            or getattr(module, "msgs", None)
        )
        self._backend_name = "pyweixin"
        self._backend = module
        self._messages = messages
        logger.info(
            "已加载 pyweixin 后端（尚未在真实微信上验收）。Messages=%s",
            type(messages).__name__ if messages is not None else "无",
        )

    def _try_load_pywinauto(self) -> None:
        import pywinauto  # type: ignore
        from pywinauto import Application  # type: ignore

        self._backend_name = "pywinauto"
        self._backend = pywinauto
        # 连接已启动的 Weixin / WeChat 进程，失败则留给调用点处理。
        app = None
        last_err: Exception | None = None
        for title in ("Weixin", "WeChat", "微信"):
            try:
                app = Application(backend="uia").connect(title_re=f".*{title}.*", timeout=2)
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
        if app is None:
            try:
                app = Application(backend="uia").connect(path="Weixin.exe", timeout=2)
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                try:
                    app = Application(backend="uia").connect(path="WeChat.exe", timeout=2)
                except Exception as exc2:  # noqa: BLE001
                    last_err = exc2
        if app is None:
            raise UiNotReadyError(
                "已安装 pywinauto，但未能连接到微信窗口（请确认官方客户端已登录且窗口可见）。"
                f" 原始错误: {last_err}"
            )
        self._messages = app
        logger.info("已通过 pywinauto 连接到微信进程（尚未在真实微信上验收）")

    # --- UiDriver ---

    def find_chat_window(self) -> bool:
        self._ensure()
        if self._backend_name == "pywinauto" and self._messages is not None:
            try:
                win = self._top_window()
                return bool(win) and win.is_visible()
            except Exception as exc:  # noqa: BLE001
                logger.warning("查找微信窗口失败: %s", exc)
                return False
        # pyweixin：若能导入，认为调用方需要再做窗口级探测；无法确认时返回 True
        # 会在后续 open_chat 中暴露问题，避免在这里假装失败或成功。
        return self._messages is not None or self._backend is not None

    def open_chat(self, group_name: str) -> None:
        self._ensure()
        fn = self._find_callable(
            [
                "open_chat",
                "open_dialog_window",
                "Navigations.open_chat",
                "switch_chat",
                "search_chat",
            ]
        )
        if fn is not None:
            try:
                fn(group_name)
                self._current = group_name
                return
            except Exception as exc:  # noqa: BLE001
                msg = str(exc).lower()
                if "not found" in msg or "找不到" in msg or "no such" in msg:
                    raise ChatNotFoundError(f"找不到目标群窗口: {group_name}") from exc
                if "timeout" in msg or "超时" in msg:
                    raise SendTimeoutError(f"打开会话超时: {group_name}") from exc
                raise UiNotReadyError(f"打开会话失败: {exc}") from exc
        if self._backend_name == "pywinauto":
            self._pywinauto_open_chat(group_name)
            return
        raise UiaDependencyError(
            "当前 pyweixin/pywinauto 版本没有可识别的 open_chat 接口，"
            "本仓库未验证真实可用性，请对照上游文档更新 PyWeixinDriver。"
        )

    def focus_input(self) -> None:
        self._ensure()
        fn = self._find_callable(["focus_input", "click_input", "activate_input_box"])
        if fn is not None:
            fn()
            return
        if self._backend_name == "pywinauto":
            try:
                win = self._top_window()
                # 常见输入框类型；找不到时让后续 paste 再失败
                for spec in ({"control_type": "Edit"}, {"class_name": "Edit"}):
                    try:
                        win.child_window(**spec).set_focus()
                        return
                    except Exception:  # noqa: BLE001
                        continue
            except Exception as exc:  # noqa: BLE001
                raise UiNotReadyError(f"无法聚焦输入框: {exc}") from exc

    def paste_text(self, text: str) -> None:
        self._ensure()
        fn = self._find_callable(["paste_text", "set_input", "type_keys", "fill_input"])
        if fn is not None:
            fn(text)
            return
        if self._backend_name == "pywinauto":
            self._pywinauto_paste(text)
            return
        raise UiaDependencyError(
            "当前后端没有可识别的粘贴接口，本仓库未验证真实可用性。"
        )

    def press_send(self) -> None:
        self._ensure()
        fn = self._find_callable(["press_send", "click_send", "send", "press_enter"])
        if fn is not None:
            fn()
            return
        if self._backend_name == "pywinauto":
            try:
                win = self._top_window()
                win.type_keys("{ENTER}")
                return
            except Exception as exc:  # noqa: BLE001
                raise UiNotReadyError(f"按下发送失败: {exc}") from exc
        raise UiaDependencyError("当前后端没有可识别的发送接口，本仓库未验证真实可用性。")

    def verify_sent(self, text: str) -> bool:
        self._ensure()
        fn = self._find_callable(["verify_sent", "last_message", "get_last_message"])
        if fn is not None:
            try:
                result = fn()
                if isinstance(result, bool):
                    return result
                if isinstance(result, str):
                    return text in result
                if result is None:
                    return False
                return text in str(result)
            except Exception as exc:  # noqa: BLE001
                logger.warning("校验已发送失败: %s", exc)
                return False
        # 没有可靠校验接口时返回 False，由上层标 maybe_sent，避免误判成功。
        logger.warning("当前后端无法校验是否发送成功（未验证的真实驱动），将视为未确认")
        return False

    def is_logged_in(self) -> bool:
        try:
            self._ensure()
        except UiaDependencyError:
            return False
        fn = self._find_callable(["is_logged_in", "check_login"])
        if fn is not None:
            try:
                return bool(fn())
            except Exception:  # noqa: BLE001
                return False
        # 能连到窗口只说明进程在，不能等同已登录；保守地看窗口是否可见。
        return self.is_window_visible()

    def is_locked(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            import ctypes

            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            # OpenInputDesktop 在锁屏时通常失败或拿到与当前桌面不同的句柄。
            handle = user32.OpenInputDesktop(0, False, 0)
            if not handle:
                return True
            user32.CloseDesktop(handle)
        except Exception:  # noqa: BLE001
            return False
        return False

    def is_window_visible(self) -> bool:
        try:
            self._ensure()
        except (UiaDependencyError, UiNotReadyError):
            return False
        if self._backend_name == "pywinauto":
            try:
                win = self._top_window()
                return bool(win.is_visible())
            except Exception:  # noqa: BLE001
                return False
        fn = self._find_callable(["is_window_visible", "is_wechat_open"])
        if fn is not None:
            try:
                return bool(fn())
            except Exception:  # noqa: BLE001
                return False
        return self.find_chat_window()

    def has_blocking_dialog(self) -> bool:
        try:
            self._ensure()
        except (UiaDependencyError, UiNotReadyError):
            return False
        fn = self._find_callable(["has_dialog", "has_captcha", "has_blocking_dialog"])
        if fn is not None:
            try:
                return bool(fn())
            except Exception:  # noqa: BLE001
                return False
        return False

    def current_chat(self) -> str | None:
        return self._current

    def probe(self) -> dict[str, Any]:
        info = describe_backend()
        info.update(
            {
                "loaded": self._loaded,
                "backend_in_use": self._backend_name,
                "current_chat": self._current,
            }
        )
        if sys.platform != "win32":
            info["logged_in"] = False
            info["locked"] = False
            info["window_visible"] = False
            info["chat_window_found"] = False
            return info
        try:
            self._ensure()
            info["logged_in"] = self.is_logged_in()
            info["locked"] = self.is_locked()
            info["window_visible"] = self.is_window_visible()
            info["blocking_dialog"] = self.has_blocking_dialog()
            info["chat_window_found"] = self.find_chat_window()
        except Exception as exc:  # noqa: BLE001
            info["error"] = str(exc)
        return info

    def close(self) -> None:
        self._messages = None
        self._backend = None
        self._loaded = False

    # --- 内部工具 ---

    def _find_callable(self, names: list[str]):
        sources = [self._messages, self._backend]
        for name in names:
            if "." in name:
                # 模块级 Navigations.open_chat 这类路径
                head, tail = name.split(".", 1)
                for src in sources:
                    obj = getattr(src, head, None) if src is not None else None
                    if obj is None and self._backend_name == "pyweixin":
                        obj = getattr(self._backend, head, None) if self._backend is not None else None
                    fn = getattr(obj, tail, None) if obj is not None else None
                    if callable(fn):
                        return fn
                continue
            for src in sources:
                if src is None:
                    continue
                fn = getattr(src, name, None)
                if callable(fn):
                    return fn
                # 有些版本把方法放在类上，需要先实例化
                cls = getattr(src, name[0].upper() + name[1:], None) if False else None
                if cls:
                    pass
        return None

    def _top_window(self):
        app = self._messages
        if app is None:
            raise UiNotReadyError("尚未连接到微信进程")
        if hasattr(app, "top_window"):
            return app.top_window()
        if hasattr(app, "window"):
            return app.window()
        raise UiNotReadyError("当前 pywinauto 连接没有 top_window")

    def _pywinauto_open_chat(self, group_name: str) -> None:
        """保守实现：用搜索框寻找会话。失败则明确抛错，不假装成功。"""
        try:
            win = self._top_window()
            win.set_focus()
            # Ctrl+F 打开搜索在不同版本上并不稳定，失败就告诉调用方未验证。
            win.type_keys("^f")
            win.type_keys(group_name, with_spaces=True)
            win.type_keys("{ENTER}")
            self._current = group_name
        except Exception as exc:  # noqa: BLE001
            raise ChatNotFoundError(
                f"无法用 pywinauto 打开会话 {group_name}（未经验证的降级路径）: {exc}"
            ) from exc

    def _pywinauto_paste(self, text: str) -> None:
        try:
            win = self._top_window()
            # 优先 set_edit_text；失败则尝试剪贴板，再失败则报错。
            for spec in ({"control_type": "Edit"},):
                try:
                    edit = win.child_window(**spec)
                    if hasattr(edit, "set_edit_text"):
                        edit.set_edit_text(text)
                        return
                except Exception:  # noqa: BLE001
                    continue
            try:
                import win32clipboard  # type: ignore

                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardText(text)
                finally:
                    win32clipboard.CloseClipboard()
                win.type_keys("^v")
                return
            except Exception as exc:  # noqa: BLE001
                raise UiNotReadyError(f"无法把文本写入输入框: {exc}") from exc
        except UiNotReadyError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise UiNotReadyError(f"粘贴文本失败: {exc}") from exc
