"""内置引擎适配器：通过 control_server 的 HTTP 接口驱动原版 realtime_gui。

为什么这算"官方接口"：这个控制接口是我们自己插进原版进程的，我们说了算，不存在版本脆弱问题。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

from .base import CONTRACT_FIELDS, Adapter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BuiltinAdapter(Adapter):
    id = "builtin"
    label = "内置（原版 realtime_gui）"
    verified = "RVC WebUI 2.3 + 我们的 control_server"
    notes = "需先启动 启动.bat（引擎进程自带控制接口）"
    stop_note = "真停止变声（引擎窗口仍驻留，随时可再开始）"
    can_start = True

    def __init__(self, host="127.0.0.1", port=8898, token=""):
        self.base = "http://%s:%d" % (host, port)
        self.port = port
        self.token = token

    # ---------- 内部 ----------
    def _req(self, path, data=None):
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(self.base + path, data=body,
                                     method="POST" if data is not None else "GET")
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("X-Token", self.token)
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode("utf-8") or "{}"
        return json.loads(raw)

    # ---------- 四个动作 ----------
    def available(self):
        try:
            st = self._req("/api/state")
            return True, "变声中" if st.get("streaming") else "已就绪（未开始变声）"
        except Exception as e:
            return False, "连不上内置引擎控制接口：%s" % e

    def state(self):
        s = self._req("/api/state")
        return {
            "running": bool(s.get("streaming")),
            "engine": self.id,
            "params": {k: s.get(k) for k in CONTRACT_FIELDS if k in s},
            "devices": {"in": s.get("sg_input_device"), "out": s.get("sg_output_device"),
                        "hostapi": s.get("sg_hostapi")},
            "model": {"pth": s.get("pth_path"), "index": s.get("index_path")},
        }

    def devices(self):
        # 设备清单由引擎侧提供（它进程里才能枚举到真实设备）
        return self._req("/api/devices")

    def apply(self, payload):
        # 内置引擎的接口本来就是契约字段名，直通即可
        body = {k: v for k, v in (payload or {}).items() if k in CONTRACT_FIELDS}
        return self._req("/api/apply", body)

    def stop(self):
        return self._req("/api/stop", {})

    def start_engine(self):
        """替用户把内置引擎拉起来：起 realtime_gui.py，再等控制接口就绪才回报。

        注意：起的是同一套 venv（sys.executable），工作目录放在项目根。
        """
        if self.available()[0]:
            return {"ok": True, "note": "引擎已在运行"}
        try:
            subprocess.Popen([sys.executable, "realtime_gui.py"], cwd=ROOT)
        except Exception as e:
            return {"ok": False, "note": "拉起进程失败：%s" % e}
        deadline = time.time() + 40
        while time.time() < deadline:
            time.sleep(1.5)
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    return {"ok": True, "note": "引擎已启动，控制接口就绪"}
            except Exception:
                pass
        return {"ok": False, "note": "已拉起进程，但 40 秒内接口未就绪（去看引擎窗口的报错）"}
