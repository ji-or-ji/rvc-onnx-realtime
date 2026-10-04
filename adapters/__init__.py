"""引擎适配器注册表。管理台从这里取"有哪些引擎可管"。"""
from __future__ import annotations

from .base import Adapter, CONTRACT_FIELDS          # noqa: F401
from .builtin import BuiltinAdapter

REGISTRY = {
    "builtin": BuiltinAdapter,
    # "vcclient": VCClientAdapter,        ← 下一步：官方 WebSocket API
    # "rvc-fabric": RvcFabricAdapter,     ← 待定：私有 JSON 文件协议（随对方版本可能失效）
}


def all_adapters(token="", builtin_port=8898):
    """返回全部适配器实例（含未就绪的；由 info() 报可用性）。"""
    return [BuiltinAdapter(port=builtin_port, token=token)]
