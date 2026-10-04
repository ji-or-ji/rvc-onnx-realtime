"""给原版 realtime_gui 装一只遥控手（不改它的音频/推理）。

原理：所有改动都通过 FreeSimpleGUI 的 write_event_value 交回 Tk 主循环，
再由原版自己的 set_values() 应用 —— 等价于用户自己拖了滑杆。
这样音频路径、参数语义、坑的规避方式全部保持原样。

用法（由 realtime_gui.py 启动后自动调用）：
    import control_server; control_server.start(gui)
接口：
    GET  /              控制面板（panel.html，瑞士国际主义风格）
    GET  /api/state     当前参数
    GET  /api/devices   设备清单
    POST /api/apply     {"pitch": 8, ...} 应用并（重新）开始
    POST /api/stop      停止
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GUI = None
PORT = 8898
HERE = os.path.dirname(os.path.abspath(__file__))

FIELDS = ["pth_path", "index_path", "sg_hostapi", "sg_wasapi_exclusive",
          "sg_input_device", "sg_output_device", "sr_type", "threhold",
          "pitch", "formant", "block_time", "crossfade_length", "extra_time",
          "I_noise_reduce", "O_noise_reduce", "rms_mix_rate", "index_rate",
          "f0method"]


def _page():
    try:
        with open(os.path.join(HERE, "panel.html"), encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return "<!DOCTYPE html><meta charset='utf-8'><h1>panel.html 读取失败</h1><pre>%s</pre>" % e


def _contract():
    """configs/config.json 作为基础值，保证 pth/index/hostapi 等不为空。"""
    try:
        with open(os.path.join(HERE, "configs", "config.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _merged(payload):
    base = _contract()
    base.update(payload or {})
    return base


def state():
    g = GUI.gui_config
    d = {k: getattr(g, k, None) for k in FIELDS}
    # 原版属性名与契约字段名不完全一致，这里对齐
    d["crossfade_length"] = getattr(g, "crossfade_time", None)
    d["sg_wasapi_exclusive"] = getattr(g, "wasapi_exclusive", None)
    d["streaming"] = getattr(GUI, "stream", None) is not None
    return d


def devices():
    import sounddevice as sd
    out = []
    for i, d in enumerate(sd.query_devices()):
        io = ("in" if d["max_input_channels"] > 0 else "") + \
             ("out" if d["max_output_channels"] > 0 else "")
        out.append({"i": i, "io": io, "name": d["name"],
                    "api": sd.query_hostapis(d["hostapi"])["name"],
                    "sr": int(d["default_samplerate"])})
    return {"devices": out}


class _H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(b)
        except Exception:
            pass

    def do_GET(self):
        try:
            if self.path.startswith("/api/state"):
                self._send(200, json.dumps(state(), ensure_ascii=False))
            elif self.path.startswith("/api/devices"):
                self._send(200, json.dumps(devices(), ensure_ascii=False))
            else:
                self._send(200, _page(), "text/html; charset=utf-8")
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else "{}"
        try:
            payload = json.loads(raw or "{}")
        except Exception:
            payload = {}
        try:
            if self.path.startswith("/api/apply"):
                # 线程安全：交给 Tk 主循环，由原版 set_values() 应用
                GUI.window.write_event_value("-CTRL-APPLY", _merged(payload))
                self._send(200, json.dumps({"ok": True}))
            elif self.path.startswith("/api/stop"):
                GUI.window.write_event_value("-CTRL-STOP", None)
                self._send(200, json.dumps({"ok": True}))
            else:
                self._send(404, "{}")
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))

    def log_message(self, *a):
        pass


def _lan_ip():
    """取本机在局域网里的 IPv4（拿不到就回退 127.0.0.1）。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        try:
            s.close()
        except Exception:
            pass


def start(gui, port=PORT, host="0.0.0.0"):
    """在原版进程内启动控制接口（守护线程）。

    host 默认 0.0.0.0：手机/平板在同一局域网都能开（首次会弹一次 Windows 防火墙询问）。
    注意：局域网上没有鉴权，谁能访问这个端口谁就能改参数。想只给本机，传 host="127.0.0.1"。
    """
    global GUI, PORT
    GUI, PORT = gui, port
    srv = ThreadingHTTPServer((host, port), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("[遥控] 本机   http://127.0.0.1:%d" % port, flush=True)
    if host not in ("127.0.0.1", "localhost"):
        print("[遥控] 局域网 http://%s:%d   （手机同网可开）" % (_lan_ip(), port), flush=True)
    return srv
