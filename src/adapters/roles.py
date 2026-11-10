"""适配器角色与扩展点登记。已实现 mock、A local_capture、B uia、C notification、D ocr。

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
INBOUND_LOCAL_CAPTURE = "local_capture"  # A，已实现只读 MVP
INBOUND_NOTIFICATION = "notification"  # C，已实现（预览唤醒，非全量）

OUTBOUND_MOCK = "mock"
OUTBOUND_UIA = "uia"  # B，已实现 MVP（box 上用 FakeUiDriver）
OUTBOUND_OCR = "ocr"  # D，已实现（降级发送）

IMPLEMENTED_INBOUND = frozenset({INBOUND_MOCK, INBOUND_LOCAL_CAPTURE, INBOUND_NOTIFICATION})
IMPLEMENTED_OUTBOUND = frozenset({OUTBOUND_MOCK, OUTBOUND_UIA, OUTBOUND_OCR})

PENDING_SLOTS: dict[str, str] = {}
