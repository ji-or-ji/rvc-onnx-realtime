"""RVC 实时变声 · Web 控制台后端

启动： python web_ui.py            （默认 http://127.0.0.1:8899）
      python web_ui.py --port 9000 --host 0.0.0.0   （局域网手机/平板也能开）

架构：Python 进程 = 推理引擎 + 音频 I/O；浏览器 = 控制台（WebSocket 双向）。
推理与音频都留在 Python 里，界面完全跨平台。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
from collections import deque

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
import uvicorn

import onnx_rt as rt

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


# ---------------- 引擎控制器 ----------------
class Controller:
    def __init__(self):
        self.lock = threading.Lock()
        self.phase = "idle"          # idle | loading | running | error
        self.msg = ""
        self.cfg = {"ep": "cpu", "block": 0.5, "ctx": 0.25, "f0": "pm",
                    "threads": 0, "gain": 1.0, "up_key": 12,
                    "in_spec": "", "out_spec": "", "in_sr": 0, "out_sr": 0}
        self.stats = {"ms": 0.0, "blocks": 0, "queue": 0, "in": 0.0, "out": 0.0,
                      "in_dev": "", "out_dev": "", "in_sr": 0, "out_sr": 0}
        self._stop = threading.Event()
        self._thr = None
        self._gain = 1.0

    # --- 对外 ---
    def snapshot(self):
        with self.lock:
            return {"phase": self.phase, "msg": self.msg, "cfg": dict(self.cfg),
                    **self.stats}

    def start(self, cfg: dict):
        if self._thr and self._thr.is_alive():
            self.stop(wait=True)
        with self.lock:
            for k in self.cfg:
                if k in cfg and cfg[k] is not None:
                    self.cfg[k] = cfg[k]
            self.cfg["up_key"] = int(self.cfg["up_key"])
            self._gain = float(self.cfg["gain"])
        self._stop.clear()
        self._thr = threading.Thread(target=self._run, daemon=True)
        self._thr.start()

    def stop(self, wait=False):
        self._stop.set()
        if wait and self._thr:
            self._thr.join(timeout=8)

    def set_live(self, key, value):
        if key == "gain":
            self._gain = float(value)
            with self.lock:
                self.cfg["gain"] = float(value)
            return True
        return False

    # --- 内部 ---
    def _put(self, **kw):
        with self.lock:
            for k, v in kw.items():
                if k == "phase":
                    self.phase = v
                elif k == "msg":
                    self.msg = v
                else:
                    self.stats[k] = v

    def _run(self):
        cfg, lv, times = self.cfg, {"in": 0.0, "out": 0.0}, []
        try:
            self._put(phase="loading", msg="正在构建推理引擎（首次较慢）…")
            eng = rt.Engine(cfg["ep"], cfg["block"], cfg["ctx"], cfg["f0"],
                            cfg["threads"], cfg["up_key"])
            in_dev, in_def = rt.resolve_dev(cfg["in_spec"], True)
            out_dev, out_def = rt.resolve_dev(cfg["out_spec"], False)
            in_sr = int(cfg["in_sr"]) or in_def or rt.SR_IN
            out_sr = int(cfg["out_sr"]) or out_def or rt.SR_OUT
            block_in = int(round(eng.block * in_sr / rt.SR_IN))

            import sounddevice as sd
            q_in, q_out = deque(), deque()

            def in_cb(indata, frames, t, status):
                x = indata[:, 0].copy()
                lv["in"] = float(np.sqrt(np.mean(x * x)) + 1e-9)
                q_in.append(x)

            def out_cb(outdata, frames, t, status):
                need, parts = frames, []
                while need > 0 and q_out:
                    chunk = q_out[0]
                    take = min(len(chunk), need)
                    parts.append(chunk[:take])
                    if take == len(chunk):
                        q_out.popleft()
                    else:
                        q_out[0] = chunk[take:]
                    need -= take
                if need > 0:
                    parts.append(np.zeros(need, dtype=np.float32))
                y = np.concatenate(parts)
                outdata[:] = y.reshape(-1, 1)
                lv["out"] = float(np.sqrt(np.mean(y * y)))

            ist = sd.InputStream(device=in_dev, samplerate=in_sr, channels=1,
                                 blocksize=block_in, dtype="float32", callback=in_cb)
            ost = sd.OutputStream(device=out_dev, samplerate=out_sr, channels=1,
                                  blocksize=int(round(eng.block * out_sr / rt.SR_IN)),
                                  dtype="float32", callback=out_cb)
            ist.start()
            ost.start()
            self._put(phase="running", msg="运行中",
                      in_dev=cfg["in_spec"] or "默认", out_dev=cfg["out_spec"] or "默认",
                      in_sr=in_sr, out_sr=out_sr, blocks=0)
            while not self._stop.is_set():
                if not q_in:
                    time.sleep(0.005)
                    continue
                raw = q_in.popleft()
                x16 = rt._resample(raw, in_sr, rt.SR_IN) if in_sr != rt.SR_IN else raw
                if len(x16) < eng.block:
                    x16 = np.pad(x16, (0, eng.block - len(x16)))
                eng.push(x16[:eng.block])
                a, ms = eng.run()
                if out_sr != rt.SR_OUT:
                    a = rt._resample(a, rt.SR_OUT, out_sr)
                q_out.append(a * self._gain)
                times.append(ms)
                if len(times) > 60:
                    times.pop(0)
                self._put(ms=sum(times) / len(times),
                          blocks=self.stats["blocks"] + 1, queue=len(q_out), **lv)
        except Exception as e:  # noqa
            self._put(phase="error", msg="%s: %s" % (type(e).__name__, e))
        finally:
            try:
                ist.stop(); ost.stop()      # noqa: F821
            except Exception:
                pass
            if self.phase != "error":
                self._put(phase="idle", msg="已停止", ms=0.0, queue=0)
            with self.lock:
                self.stats["blocks"] = self.stats["blocks"]


CTRL = Controller()

# ---------------- Web ----------------
app = FastAPI()
_clients: set = set()


def dev_list():
    import sounddevice as sd
    out = []
    for i, d in enumerate(sd.query_devices()):
        io = ("in" if d["max_input_channels"] > 0 else "") + ("out" if d["max_output_channels"] > 0 else "")
        out.append({"i": i, "io": io, "name": d["name"],
                    "api": sd.query_hostapis(d["hostapi"])["name"],
                    "sr": int(d["default_samplerate"])})
    return out


@app.get("/")
def index():
    return FileResponse(os.path.join(WEB, "index.html"))


@app.get("/api/devices")
def api_devices():
    return JSONResponse({"devices": dev_list()})


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    _clients.add(sock)
    try:
        await sock.send_text(json.dumps({"type": "hello", "payload": CTRL.snapshot()}))
        while True:
            raw = await sock.receive_text()
            try:
                m = json.loads(raw)
            except Exception:
                continue
            cmd = m.get("cmd")
            if cmd == "start":
                CTRL.start(m.get("cfg") or {})
            elif cmd == "stop":
                CTRL.stop(wait=True)
            elif cmd == "set":
                CTRL.set_live(m.get("key"), m.get("value"))
            elif cmd == "devices":
                await sock.send_text(json.dumps({"type": "devices", "devices": dev_list()}))
            await sock.send_text(json.dumps({"type": "state", "payload": CTRL.snapshot()}))
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(sock)


@app.on_event("startup")
async def _ticker():
    async def loop():
        while True:
            if _clients:
                msg = json.dumps({"type": "state", "payload": CTRL.snapshot()})
                for s in list(_clients):
                    try:
                        await s.send_text(msg)
                    except Exception:
                        _clients.discard(s)
            await asyncio.sleep(0.25)
    asyncio.create_task(loop())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8899)
    a = ap.parse_args()
    print("RVC Web console -> http://%s:%d" % (a.host, a.port), flush=True)
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
