# -*- coding: utf-8 -*-
"""各方案适配器契约套件。具体用例在本目录各模块中，由 test_adapter_contracts.py 参数化执行。"""

from contracts.discovery import SCHEME_SPECS, discover_scheme, load_scheme_factory

__all__ = ["SCHEME_SPECS", "discover_scheme", "load_scheme_factory"]
