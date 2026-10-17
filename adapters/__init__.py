"""引擎适配器注册表。管理台从这里取"有哪些引擎可管"。"""
from __future__ import annotations

from .base import Adapter, CONTRACT_FIELDS          # noqa: F401
from .builtin import BuiltinAdapter
from .fabric import RvcFabricAdapter
from .vcclient import VCClientAdapter

REGISTRY = {
    "builtin": BuiltinAdapter,
    "vcclient": VCClientAdapter,
    # 私有 JSON 文件协议：verified 随对方版本漂移，实测 1.6.0
    "rvc-fabric": RvcFabricAdapter,
}


def all_adapters(token="", builtin_port=8898, vcclient_host="127.0.0.1", vcclient_port=18888,
                 fabric_root=""):
    """返回全部适配器实例（含未就绪的；由 info() 报可用性）。"""
    return [
        BuiltinAdapter(port=builtin_port, token=token),
        VCClientAdapter(host=vcclient_host, port=vcclient_port),
        RvcFabricAdapter(root=fabric_root),
    ]
