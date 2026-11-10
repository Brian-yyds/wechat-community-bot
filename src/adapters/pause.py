"""紧急暂停开关：B（UIA 发送）与 D（OCR 降级发送）必须遵守的同一约定。

默认开关文件：
    <data_dir>/PAUSE
也可用环境变量 WECHAT_BOT_PAUSE_FILE 指向任意路径。

约定（请 UIA 发送适配器同样遵守，不要另起一套文件名或语义）：
1. 文件存在即视为「暂停发送」，不解析、不依赖文件内容。内容仅供人工阅读原因。
2. 发送适配器在每次真正点击 / 粘贴 / 调用客户端发送之前必须检查；
   暂停时不得执行任何 UI 操作或发出消息。
3. 检测到验证码、异常弹窗、登录失效或环境异常时，适配器应创建该文件并停止，
   不得自动打码、绕过验证或反复尝试登录。
4. 恢复发送：由人工确认安全后删除该文件（或调用 PauseGate.resume()）。
5. 一键暂停：在数据目录创建空的 PAUSE 文件即可。

data/ 已在 .gitignore 中，PAUSE 文件不会入库。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path


DEFAULT_PAUSE_FILENAME = "PAUSE"
PAUSE_ENV_VAR = "WECHAT_BOT_PAUSE_FILE"
DATA_DIR_ENV_VAR = "WECHAT_BOT_DATA_DIR"


class SendPausedError(RuntimeError):
    """紧急暂停已开启，禁止发送。"""


def resolve_pause_path(
    path: str | Path | None = None,
    *,
    data_dir: str | Path | None = None,
) -> Path:
    """解析暂停文件路径：显式参数 > 环境变量 > <data_dir>/PAUSE。"""
    if path is not None:
        return Path(path)
    env = os.environ.get(PAUSE_ENV_VAR)
    if env:
        return Path(env)
    root = Path(data_dir) if data_dir is not None else Path(
        os.environ.get(DATA_DIR_ENV_VAR) or "data"
    )
    return root / DEFAULT_PAUSE_FILENAME


class PauseGate:
    """检查、开启与解除紧急暂停。"""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        data_dir: str | Path | None = None,
    ) -> None:
        self.path = resolve_pause_path(path, data_dir=data_dir)

    def is_paused(self) -> bool:
        """文件存在即为暂停。"""
        return self.path.exists()

    def check(self) -> None:
        """若已暂停则抛出 SendPausedError。"""
        if self.is_paused():
            raise SendPausedError(
                f"发送已紧急暂停（发现 {self.path}）。"
                "请人工确认验证码/异常弹窗已处理，并删除该文件后再恢复。"
            )

    def pause(self, reason: str = "") -> Path:
        """创建暂停文件。reason 只写入文件供人工阅读，不参与判定。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().isoformat(timespec="seconds")
        text = f"paused_at={stamp}\n"
        if reason:
            text += f"reason={reason}\n"
        if self.path.exists():
            # 追加原因，避免覆盖更早的人工备注。
            existing = self.path.read_text(encoding="utf-8")
            if reason and reason not in existing:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(f"paused_at={stamp}\nreason={reason}\n")
        else:
            self.path.write_text(text, encoding="utf-8")
        return self.path

    def resume(self) -> bool:
        """删除暂停文件。文件本不存在时返回 False。"""
        if not self.path.exists():
            return False
        self.path.unlink()
        return True
