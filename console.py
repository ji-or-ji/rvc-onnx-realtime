"""管理台（独立进程）——一台管多家引擎。

它不再住在引擎里：自己持有模型库、预设与令牌，通过适配器去驱动各家引擎。
这样"加第三家"才不用动面板，也才能真正做到「谁家好用就管谁」。

  GET  /                  面板
  GET  /api/engines       有哪些引擎可管（可用性 / 已验证版本 / 备注）
  GET  /api/state         当前引擎状态（?engine=xxx）
  GET  /api/devices       设备清单（?engine=xxx，由适配器给）
  POST /api/apply         下发参数/模型/设备（body 里带 engine）
  POST /api/stop          停止变声（各引擎含义不同，见 /api/engines 的 stop_note）
  POST /api/engine/start  让管理台把引擎拉起来（能启动的引擎才支持）
  GET/POST/DELETE /api/models | /api/presets    管理台自有数据
  POST /api/upload        上传模型 / 索引文件（按扩展名归位）

端口分工：管理台 8899；内置引擎自己的控制接口留在 8898（那是 builtin 适配器要去连的）。
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from adapters import all_adapters

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8899
BUILTIN_PORT = 8898
TOKEN = ""
AUDIO = None          # AudioHub（--audio 启用后才有）
AUDIO_SVC = None      # AudioService
MODELS_FILE = os.path.join(HERE, "models.json")
PRESETS_FILE = os.path.join(HERE, "presets.json")
WEIGHTS_DIR = os.path.join(HERE, "assets", "weights")
INDICES_DIR = os.path.join(HERE, "assets", "indices")


# ---------------- 令牌 ----------------
def _token_file():
    return os.path.join(HERE, "control_token.txt")


def load_token():
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


# ---------------- 适配器 ----------------
def engines():
    return all_adapters(token=TOKEN, builtin_port=BUILTIN_PORT)


def pick(eng_id):
    """按 id 取适配器；不给 id 时取第一个“可用”的，否则退回 builtin。"""
    es = engines()
    if eng_id:
        for a in es:
            if a.id == eng_id:
                return a
        return None
    for a in es:
        try:
            ok, _ = a.available()
            if ok:
                return a
        except Exception:
            pass
    return es[0] if es else None


# ---------------- 管理台自有数据 ----------------
def _read_json(path, key):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        v = d.get(key) if isinstance(d, dict) else d
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _write_json(path, key, items):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({key: items}, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def models_payload():
    return {"models": _read_json(MODELS_FILE, "models")}


def presets_payload():
    return {"presets": _read_json(PRESETS_FILE, "presets")}


def _page():
    try:
        with open(os.path.join(HERE, "panel.html"), encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return "<!DOCTYPE html><meta charset='utf-8'><h1>panel.html 读取失败</h1><pre>%s</pre>" % e


def _audio_page():
    try:
        with open(os.path.join(HERE, "audio.html"), encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return "<!DOCTYPE html><meta charset='utf-8'><h1>audio.html 读取失败</h1><pre>%s</pre>" % e


class _H(BaseHTTPRequestHandler):
    # ---------- 公共 ----------
    def _authed(self):
        if not TOKEN:
            return True
        k = self.headers.get("X-Token")
        if not k:
            k = (parse_qs(urlparse(self.path).query).get("k") or [""])[0]
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

    def _q(self):
        return parse_qs(urlparse(self.path).query)

    def log_message(self, *a):
        pass

    # ---------- GET ----------
    def do_GET(self):
        try:
            if self.path.startswith("/api/") and not self._authed():
                self._send(401, json.dumps({"error": "token required"}, ensure_ascii=False))
                return
            eng = (self._q().get("engine") or [""])[0]

            if self.path.startswith("/api/engines"):
                self._send(200, json.dumps({"engines": [a.info() for a in engines()]},
                                           ensure_ascii=False))
            elif self.path.startswith("/api/state"):
                a = pick(eng)
                if not a:
                    self._send(404, json.dumps({"error": "没有这个引擎：%s" % eng}, ensure_ascii=False))
                    return
                ok, why = a.available()
                if not ok:
                    self._send(200, json.dumps({"engine": a.id, "available": False,
                                                "reason": why}, ensure_ascii=False))
                    return
                st = a.state()
                st["available"] = True
                self._send(200, json.dumps(st, ensure_ascii=False))
            elif self.path.startswith("/api/devices"):
                a = pick(eng)
                if not a or not hasattr(a, "devices"):
                    self._send(200, json.dumps({"devices": [], "note": "该适配器不提供设备清单"},
                                               ensure_ascii=False))
                    return
                self._send(200, json.dumps(a.devices(), ensure_ascii=False))
            elif self.path.startswith("/api/models"):
                self._send(200, json.dumps(models_payload(), ensure_ascii=False))
            elif self.path.startswith("/api/presets"):
                self._send(200, json.dumps(presets_payload(), ensure_ascii=False))
            elif self.path.startswith("/api/audio/state"):
                if AUDIO is None:
                    self._send(200, json.dumps({"note": "网络音频未启用（启动时加 --audio）"},
                                               ensure_ascii=False))
                else:
                    import time as _t
                    st = AUDIO.state(_t.time())
                    st["enabled"] = True
                    st["chunk_ms"] = getattr(AUDIO_SVC, "chunk_ms", None)
                    st["devices"] = getattr(AUDIO_SVC, "devices", None)
                    st["stats"] = getattr(AUDIO_SVC, "stats", None)
                    self._send(200, json.dumps(st, ensure_ascii=False))
            elif self.path.startswith("/audio"):
                self._send(200, _audio_page(), "text/html; charset=utf-8")
            else:
                self._send(200, _page(), "text/html; charset=utf-8")
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))

    # ---------- POST ----------
    def do_POST(self):
        q = self._q()
        if self.path.startswith("/api/upload"):
            if not self._authed():
                self._send(401, json.dumps({"error": "token required"}, ensure_ascii=False))
                return
            name = os.path.basename((q.get("name") or [""])[0]).strip()
            low = name.lower()
            n = int(self.headers.get("Content-Length") or 0)
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
                        ch = self.rfile.read(min(1 << 20, left))
                        if not ch:
                            break
                        f.write(ch)
                        left -= len(ch)
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

            if self.path.startswith("/api/models"):
                alias = str(payload.get("alias") or "").strip()
                if not alias:
                    self._send(400, json.dumps({"error": "别名不能为空"}, ensure_ascii=False)); return
                ms = [m for m in _read_json(MODELS_FILE, "models") if m.get("alias") != alias]
                ms.append({"alias": alias, "pth": payload.get("pth", ""),
                           "index": payload.get("index", "")})
                _write_json(MODELS_FILE, "models", ms)
                self._send(200, json.dumps(models_payload(), ensure_ascii=False))
            elif self.path.startswith("/api/presets"):
                name = str(payload.get("name") or "").strip()
                if not name:
                    self._send(400, json.dumps({"error": "预设名不能为空"}, ensure_ascii=False)); return
                body = {k: v for k, v in payload.items() if k != "name"}
                ps = [p for p in _read_json(PRESETS_FILE, "presets") if p.get("name") != name]
                ps.append({"name": name, **body})
                _write_json(PRESETS_FILE, "presets", ps)
                self._send(200, json.dumps(presets_payload(), ensure_ascii=False))
            elif self.path.startswith("/api/engine/start"):
                a = pick(str(payload.get("engine") or ""))
                if not a:
                    self._send(404, json.dumps({"error": "没有这个引擎"}, ensure_ascii=False)); return
                self._send(200, json.dumps(a.start_engine(), ensure_ascii=False))
            elif self.path.startswith("/api/apply"):
                a = pick(str(payload.get("engine") or ""))
                if not a:
                    self._send(404, json.dumps({"error": "没有这个引擎"}, ensure_ascii=False)); return
                body = {k: v for k, v in payload.items() if k != "engine"}
                self._send(200, json.dumps(a.apply(body), ensure_ascii=False))
            elif self.path.startswith("/api/stop"):
                a = pick(str(payload.get("engine") or ""))
                if not a:
                    self._send(404, json.dumps({"error": "没有这个引擎"}, ensure_ascii=False)); return
                self._send(200, json.dumps(a.stop(), ensure_ascii=False))
            else:
                self._send(404, "{}")
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))

    # ---------- DELETE ----------
    def do_DELETE(self):
        if not self._authed():
            self._send(401, json.dumps({"error": "token required"}, ensure_ascii=False))
            return
        try:
            q = self._q()
            if self.path.startswith("/api/presets"):
                name = (q.get("name") or [""])[0]
                _write_json(PRESETS_FILE, "presets",
                            [p for p in _read_json(PRESETS_FILE, "presets") if p.get("name") != name])
                self._send(200, json.dumps(presets_payload(), ensure_ascii=False))
            else:
                alias = (q.get("alias") or [""])[0]
                _write_json(MODELS_FILE, "models",
                            [m for m in _read_json(MODELS_FILE, "models") if m.get("alias") != alias])
                self._send(200, json.dumps(models_payload(), ensure_ascii=False))
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))


def _lan_ips():
    import socket
    ips = []
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                ips.append(ip)
    except Exception:
        pass
    if not ips:
        ips = ["127.0.0.1"]
    ips.sort(key=lambda x: (not x.startswith("192.168."), x))
    return ips


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--audio", action="store_true", help="同时启用网络音频（WS PCM + 设备层）")
    ap.add_argument("--audio-port", type=int, default=8900)
    a = ap.parse_args()
    tk = load_token()
    suffix = ("?k=" + tk) if tk else ""
    srv = ThreadingHTTPServer((a.host, a.port), _H)
    print("[管理台] 本机   http://127.0.0.1:%d%s" % (a.port, suffix), flush=True)
    if a.host not in ("127.0.0.1", "localhost"):
        for ip in _lan_ips()[:3]:
            print("[管理台] 局域网 http://%s:%d%s" % (ip, a.port, suffix), flush=True)
    for e in engines():
        i = e.info()
        print("[管理台] 引擎 %-10s %-28s %s" %
              (i["id"], i["label"], ("可用：" + i["reason"]) if i["available"] else ("未就绪：" + i["reason"])),
              flush=True)

    if a.audio:
        global AUDIO, AUDIO_SVC
        from audiohub import AudioHub
        from audiosrv import AudioService
        AUDIO = AudioHub()
        AUDIO_SVC = AudioService(AUDIO, host=a.host, port=a.audio_port, token=tk)
        opened = AUDIO_SVC.open_devices()
        AUDIO_SVC.start()
        print("[管理台] 网络音频 ws://%s:%d　打开的设备：%s" %
              (a.host, a.audio_port, ", ".join(opened) or "（无）"), flush=True)
        if AUDIO_SVC.devices.get("note"):
            print("[管理台] 网络音频注意：%s" % AUDIO_SVC.devices["note"], flush=True)
        for ip in _lan_ips()[:1]:
            print("[管理台] 设备页 http://%s:%d/audio%s" % (ip, a.port, suffix), flush=True)

    srv.serve_forever()


if __name__ == "__main__":
    main()
