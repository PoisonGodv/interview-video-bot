"""pytest 公共配置: integration 标记默认跳过(需网络/ffmpeg/chromium)。"""
from __future__ import annotations

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="运行依赖网络/ffmpeg/chromium 的集成测试",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: 需要外部资源(网络/ffmpeg/chromium), 默认跳过, --run-integration 启用",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-integration"):
        return
    skip = pytest.mark.skip(reason="integration 测试, 需要 --run-integration")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
