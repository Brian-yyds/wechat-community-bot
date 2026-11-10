"""适配器角色与扩展点登记。第一阶段只实现 mock；A/B/C/D 后续再派 MVP。

角色约定：
- A inbound.local_capture：本地库只读抓取
- B outbound.uia：UI Automation 发送
- C inbound.notification：通知预览唤醒
- D outbound.ocr：OCR 降级发送

读取与发送可分开配置并组合，例如 inbound=local_capture、outbound=uia。
"""

from __future__ import annotations

# 角色标识，供配置 wechat.inbound / wechat.outbound 使用。
INBOUND_MOCK = "mock"
INBOUND_LOCAL_CAPTURE = "local_capture"  # A，尚未实现
INBOUND_NOTIFICATION = "notification"  # C，尚未实现

OUTBOUND_MOCK = "mock"
OUTBOUND_UIA = "uia"  # B，尚未实现
OUTBOUND_OCR = "ocr"  # D，尚未实现

IMPLEMENTED_INBOUND = frozenset({INBOUND_MOCK})
IMPLEMENTED_OUTBOUND = frozenset({OUTBOUND_MOCK})

PENDING_SLOTS = {
    INBOUND_LOCAL_CAPTURE: "A 本地库只读抓取",
    INBOUND_NOTIFICATION: "C 通知预览唤醒",
    OUTBOUND_UIA: "B UIA 发送",
    OUTBOUND_OCR: "D OCR 降级发送",
}
