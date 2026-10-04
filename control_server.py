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
TOKEN = ""          # 启动时从 control_token.txt 读/生成；RVC_NO_TOKEN=1 可关闭鉴权


def _token_file():
    return os.path.join(HERE, "control_token.txt")


def load_token():
    """读或生成访问令牌（持久化，重启不变，手机收藏的地址不会失效）。"""
    global TOKEN
    if os.environ.get("RVC_NO_TOKEN") == "1":
        TOKEN = ""
        return TOKEN
    try:
        with open(_token_file(), encoding="utf-8") as f:
            t = f.read().strip()
        if t:
            TOKEN = t
            return TOKEN
    except Exception:
        pass
    import secrets
    TOKEN = secrets.token_urlsafe(12)
    try:
        with open(_token_file(), "w", encoding="utf-8") as f:
            f.write(TOKEN)
    except Exception:
        pass
    return TOKEN

FIELDS = ["pth_path", "index_path", "sg_hostapi", "sg_wasapi_exclusive",
          "sg_input_device", "sg_output_device", "sr_type", "threhold",
          "pitch", "formant", "block_time", "crossfade_length", "extra_time",
          "I_noise_reduce", "O_noise_reduce", "rms_mix_rate", "index_rate",
          "f0method"]

MODELS_FILE = os.path.join(HERE, "models.json")      # 模型库（登记表）
PRESETS_FILE = os.path.join(HERE, "presets.json")    # 预设包（设备+模型+参数）
WEIGHTS_DIR = os.path.join(HERE, "assets", "weights")
INDICES_DIR = os.path.join(HERE, "assets", "indices")


def _load_models():
    try:
        with open(MODELS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        ms = d.get("models") if isinstance(d, dict) else d
        return ms if isinstance(ms, list) else []
    except Exception:
        return []


def _save_models(models):
    try:
        with open(MODELS_FILE, "w", encoding="utf-8") as f:
            json.dump({"models": models}, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _resolve(p):
    """把用户给的路径解析成本机绝对路径（支持相对项目根，也支持绝对路径）。"""
    if not p:
        return None
    p = str(p).strip().strip('"')
    if not p:
        return None
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(HERE, p))


def models_payload():
    cur = {}
    try:
        g = GUI.gui_config
        cur = {"pth_path": getattr(g, "pth_path", ""),
               "index_path": getattr(g, "index_path", "")}
    except Exception:
        pass
    return {"models": _load_models(), "current": cur,
            "weights_dir": "assets/weights", "indices_dir": "assets/indices"}


def _load_presets():
    try:
        with open(PRESETS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        ps = d.get("presets") if isinstance(d, dict) else d
        return ps if isinstance(ps, list) else []
    except Exception:
        return []


def _save_presets(presets):
    try:
        with open(PRESETS_FILE, "w", encoding="utf-8") as f:
            json.dump({"presets": presets}, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


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
    def _authed(self):
        """令牌校验：优先请求头 X-Token，其次 URL 上的 ?k=。关掉令牌时直接放行。"""
        if not TOKEN:
            return True
        from urllib.parse import urlparse, parse_qs
        k = self.headers.get("X-Token")
        if not k:
            q = parse_qs(urlparse(self.path).query)
            k = (q.get("k") or [""])[0]
        return k == TOKEN

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
            if self.path.startswith("/api/") and not self._authed():
                self._send(401, json.dumps({"error": "token required"}, ensure_ascii=False))
                return
            if self.path.startswith("/api/state"):
                self._send(200, json.dumps(state(), ensure_ascii=False))
            elif self.path.startswith("/api/models"):
                self._send(200, json.dumps(models_payload(), ensure_ascii=False))
            elif self.path.startswith("/api/presets"):
                self._send(200, json.dumps({"presets": _load_presets()}, ensure_ascii=False))
            elif self.path.startswith("/api/devices"):
                self._send(200, json.dumps(devices(), ensure_ascii=False))
            else:
                self._send(200, _page(), "text/html; charset=utf-8")
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))

    def do_POST(self):
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        if self.path.startswith("/api/upload"):
            # 上传模型/索引：raw body，按扩展名归位（避开 multipart 解析）
            if not self._authed():
                self._send(401, json.dumps({"error": "token required"}, ensure_ascii=False))
                return
            name = os.path.basename((q.get("name") or [""])[0]).strip()
            n = int(self.headers.get("Content-Length") or 0)
            low = name.lower()
            if not name or not (low.endswith(".pth") or low.endswith(".index")):
                self._send(400, json.dumps({"error": "只接收 .pth / .index"}, ensure_ascii=False))
                return
            dest_dir = WEIGHTS_DIR if low.endswith(".pth") else INDICES_DIR
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, name)
            try:
                with open(dest, "wb") as f:
                    left = n
                    while left > 0:
                        chunk = self.rfile.read(min(1 << 20, left))
                        if not chunk:
                            break
                        f.write(chunk)
                        left -= len(chunk)
                rel = os.path.relpath(dest, HERE).replace("\\", "/")
                self._send(200, json.dumps({"ok": True, "path": rel,
                                            "bytes": os.path.getsize(dest)}, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
            return

        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else "{}"
        try:
            payload = json.loads(raw or "{}")
        except Exception:
            payload = {}
        try:
            if not self._authed():
                self._send(401, json.dumps({"error": "token required"}, ensure_ascii=False))
                return
            if self.path.startswith("/api/presets"):
                name = str(payload.get("name") or "").strip()
                if not name:
                    self._send(400, json.dumps({"error": "预设名不能为空"}, ensure_ascii=False)); return
                body = dict(payload)
                body.pop("name", None)
                ps = [p for p in _load_presets() if p.get("name") != name]
                ps.append({"name": name, **body})
                _save_presets(ps)
                self._send(200, json.dumps({"presets": _load_presets()}, ensure_ascii=False))
            elif self.path.startswith("/api/models"):
                alias = str(payload.get("alias") or "").strip()
                pth, idx = _resolve(payload.get("pth")), _resolve(payload.get("index"))
                if not alias:
                    self._send(400, json.dumps({"error": "别名不能为空"}, ensure_ascii=False)); return
                if not pth or not os.path.exists(pth):
                    self._send(400, json.dumps({"error": "找不到 .pth 文件：%s" % payload.get("pth")}, ensure_ascii=False)); return
                if not idx or not os.path.exists(idx):
                    self._send(400, json.dumps({"error": "找不到 .index 文件（原版引擎要求索引必填）：%s" % payload.get("index")}, ensure_ascii=False)); return
                ms = [m for m in _load_models() if m.get("alias") != alias]
                ms.append({"alias": alias,
                           "pth": os.path.relpath(pth, HERE).replace("\\", "/"),
                           "index": os.path.relpath(idx, HERE).replace("\\", "/")})
                _save_models(ms)
                self._send(200, json.dumps(models_payload(), ensure_ascii=False))
            elif self.path.startswith("/api/apply"):
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

    def do_DELETE(self):
        from urllib.parse import urlparse, parse_qs
        if not self._authed():
            self._send(401, json.dumps({"error": "token required"}, ensure_ascii=False))
            return
        try:
            q = parse_qs(urlparse(self.path).query)
            if self.path.startswith("/api/presets"):
                name = (q.get("name") or [""])[0]
                _save_presets([p for p in _load_presets() if p.get("name") != name])
                self._send(200, json.dumps({"presets": _load_presets()}, ensure_ascii=False))
            else:
                alias = (q.get("alias") or [""])[0]
                ms = [m for m in _load_models() if m.get("alias") != alias]
                _save_models(ms)
                self._send(200, json.dumps(models_payload(), ensure_ascii=False))
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))

    def log_message(self, *a):
        pass


def _lan_ips():
    """本机局域网候选地址（192.168 优先，过滤 127/169.254）。

    机器上常有 Radmin VPN / Hyper-V / WSL 等虚拟网卡，单取一个很容易取错，
    所以这里返回全部候选，启动日志照着打印，手机挑能连的那个。
    """
    import socket
    ips = []
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip.startswith("127.") or ip.startswith("169.254."):
                continue
            ips.append(ip)
    except Exception:
        pass
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips = [s.getsockname()[0]]
            s.close()
        except Exception:
            ips = ["127.0.0.1"]
    ips.sort(key=lambda x: (not x.startswith("192.168."), x))
    return ips


def start(gui, port=PORT, host="0.0.0.0"):
    """在原版进程内启动控制接口（守护线程）。

    host 默认 0.0.0.0：手机/平板在同一局域网都能开（首次会弹一次 Windows 防火墙询问）。
    鉴权：默认开启，令牌存在 control_token.txt（首次自动生成，重启不变）；
          打开带 ?k=<令牌> 的地址一次，浏览器/面板就会记住。
          RVC_NO_TOKEN=1 环境变量可关闭鉴权。
    """
    global GUI, PORT
    GUI, PORT = gui, port
    tk = load_token()
    srv = ThreadingHTTPServer((host, port), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    suffix = ("?k=" + tk) if tk else ""
    print("[遥控] 本机   http://127.0.0.1:%d%s" % (port, suffix), flush=True)
    if host not in ("127.0.0.1", "localhost"):
        for ip in _lan_ips()[:3]:
            print("[遥控] 局域网 http://%s:%d%s" % (ip, port, suffix), flush=True)
        print("[遥控] （手机连同一个 Wi-Fi，用上面能打开的那个地址；带 ?k= 的整串都要复制）", flush=True)
    return srv
