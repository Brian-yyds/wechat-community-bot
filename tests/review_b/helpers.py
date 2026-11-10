# -*- coding: utf-8 -*-
"""适配器 B 补测共用装配。一律把 data/PAUSE 放到 tmp_path。"""
from __future__ import annotations

from pathlib import Path

from src.adapters.ui_driver import FakeUiDriver
from src.adapters.uia import UiaConfig, UiaOutboundAdapter
from src.clock import FakeClock

DEFAULT_CHATS = {"交易会员群-主群", "交易会员群", "交易会员群演示", "文件传输助手", "二群"}


def make_config(tmp_path: Path, **kwargs) -> UiaConfig:
    data = dict(
        data_dir=str(tmp_path / "data"),
        pause_flag=str(tmp_path / "PAUSE"),
        input_pause_min=0.0,
        input_pause_max=0.0,
        group_switch_interval_min=0.0,
        group_switch_interval_max=0.0,
        min_send_interval_seconds=0.0,
        preheat_on_start=False,
        keep_chat_open=False,
        max_retries=2,
        send_timeout_seconds=8.0,
        group_names={"g1": "交易会员群-主群", "g2": "二群"},
    )
    data.update(kwargs)
    return UiaConfig(**data)


def make_adapter(
    tmp_path: Path,
    driver: FakeUiDriver | None = None,
    clock=None,
    **kwargs,
) -> UiaOutboundAdapter:
    return UiaOutboundAdapter(
        driver=driver or FakeUiDriver(known_chats=set(DEFAULT_CHATS)),
        config=make_config(tmp_path, **kwargs),
        clock=clock or FakeClock(),
        project_root=tmp_path,
    )
