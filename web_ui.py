"""RVC 实时变声 · Web 控制台后端

启动： python web_ui.py                    （默认 http://127.0.0.1:8899）
      python web_ui.py --host 0.0.0.0      （局域网手机/平板也能开）
      python web_ui.py --dev-block 0.05    （设备回调块长，秒；卡就调大）

架构：Python 进程 = 推理引擎 + 音频 I/O；浏览器 = 控制台（WebSocket 双向）。

音频通路设计（防卡顿的关键）：
  麦克风回调(50ms) --> in_ring 环形缓冲 --> 工作线程: 攒够 block 就推理 --> out_ring --> 扬声器回调(50ms)
  * 设备回调只做 numpy 切片拷贝，不分配、不推理
  * 输出侧先「预铺」若干块再出声，吸收抖动
  * 积压上限保护：输出积压过多丢最旧，避免延迟无限增长
  * 统计 xrun_in / xrun_out / 延迟，卡了能看出卡在哪一环
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
import uvicorn

import onnx_rt as rt

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

# 契约（configs/config.json）→ 引擎参数。字段名一律沿用原件（含拼写 threhold）
CONTRACT_MAP = {
    "block_time": ("block", float),
    "extra_time": ("ctx", float),
    "pitch": ("up_key", int),
    "threhold": ("threshold", float),
}
F0_MAP = {"pm": "pm", "fcpe": "fcpe"}      # rmvpe/harvest/crepe 在 ONNX 引擎暂不支持


def apply_contract(ctrl, path):
    """把配方参数（config.json）载入引擎配置。"""
    try:
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
    except Exception as e:
        print("[web_ui] 读不到契约 %s（%s），使用默认参数" % (path, e), flush=True)
        return
    with ctrl.lock:
        for k, (dst, cast) in CONTRACT_MAP.items():
            if c.get(k) is not None:
                try:
                    ctrl.cfg[dst] = cast(c[k])
                except Exception:
                    pass
        if c.get("sg_input_device"):
            ctrl.cfg["in_spec"] = c["sg_input_device"]
        if c.get("sg_output_device"):
            ctrl.cfg["out_spec"] = c["sg_output_device"]
        fm = str(c.get("f0method", "pm")).lower()
        if fm not in F0_MAP:
            print("[web_ui] f0method=%s 在 ONNX 引擎暂不支持，降级为 pm" % fm, flush=True)
        ctrl.cfg["f0"] = F0_MAP.get(fm, "pm")
        ctrl._thr_gate = 10 ** (ctrl.cfg["threshold"] / 20.0)
        ctrl.contract = dict(c)
        snap = dict(ctrl.cfg)
    print("[web_ui] 契约已载入：block=%.3f ctx=%.3f up_key=%d threshold=%.1f in=%s out=%s" %
          (snap["block"], snap["ctx"], snap["up_key"], snap["threshold"],
           snap["in_spec"] or "默认", snap["out_spec"] or "默认"), flush=True)


# ---------------- 引擎控制器 ----------------
class Controller:
    def __init__(self, dev_block=0.05, prefill=1):
        self.lock = threading.Lock()
        self.phase = "idle"          # idle | loading | running | error
        self.msg = ""
        self.dev_block = float(dev_block)
        self.prefill = int(prefill)  # 预铺几个推理块再出声
        self.cfg = {"ep": "cpu", "block": 0.5, "ctx": 0.25, "f0": "pm",
                    "threads": 0, "gain": 1.0, "up_key": 12, "threshold": -60.0,
                    "in_spec": "", "out_spec": "", "in_sr": 0, "out_sr": 0}
        self.stats = {"ms": 0.0, "blocks": 0, "queue": 0.0, "lat": 0.0,
                      "in": 0.0, "out": 0.0, "xrun_in": 0, "xrun_out": 0, "gate": 0,
                      "in_dev": "", "out_dev": "", "in_sr": 0, "out_sr": 0}
        self._stop = threading.Event()
        self._thr = None
        self._thr_gate = 10 ** (-60.0 / 20.0)
        self._gain = 1.0
        self.engine = "onnx"        # onnx | torch-cuda
        self.contract = {}          # 契约原文（torch 引擎需要 pth/index 等字段）

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
        if key == "threshold":
            self._thr_gate = 10 ** (float(value) / 20.0)
            with self.lock:
                self.cfg["threshold"] = float(value)
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
        cfg = self.cfg
        dev_b = self.dev_block
        ist = ost = None
        # Windows: WASAPI 依赖线程级 COM 初始化。工作线程里开流前必须 CoInitializeEx，
        # 否则 Pa_StartStream 报 Unanticipated host error (-9999)。
        coinit = None
        if sys.platform == "win32":
            try:
                import ctypes
                coinit = ctypes.windll.ole32.CoInitializeEx(None, 0x2)   # APARTMENTTHREADED
            except Exception:
                coinit = None
        try:
            self._put(phase="loading", msg="正在构建推理引擎（首次较慢）…")
            if self.engine == "torch-cuda":
                from torch_engine import TorchEngine
                eng = TorchEngine(self.contract)
            else:
                eng = rt.Engine(cfg["ep"], cfg["block"], cfg["ctx"], cfg["f0"],
                                cfg["threads"], cfg["up_key"])

            # 预热：先跑一块静音。否则首块要 1~2 秒（CUDA graph / 算子首次编译），
            # 这期间输入环会堆积，开流后一开始就积压、爆发欠载。
            self._put(msg="预热引擎（首块较慢）…")
            try:
                eng.push(np.zeros(eng.block, dtype=np.float32))
                eng.run()
            except Exception as e:
                self._put(msg="预热失败（忽略）：%s" % e)
            try:
                in_dev, in_def = rt.resolve_dev(cfg["in_spec"], True)
            except SystemExit as e:
                self._put(msg="输入设备未找到，改用系统默认（%s）" % e)
                in_dev, in_def = None, None
            try:
                out_dev, out_def = rt.resolve_dev(cfg["out_spec"], False)
            except SystemExit as e:
                self._put(msg="输出设备未找到，改用系统默认（%s）" % e)
                out_dev, out_def = None, None
            in_sr = int(cfg["in_sr"]) or in_def or rt.SR_IN
            out_sr = int(cfg["out_sr"]) or out_def or rt.SR_OUT
            block_in = int(round(eng.block * in_sr / rt.SR_IN))     # 一个推理块 = 多少输入样本
            block_out = int(round(eng.block * out_sr / rt.SR_IN))   # 一个推理块 = 多少输出样本
            dev_in = max(256, int(in_sr * dev_b))
            dev_out = max(256, int(out_sr * dev_b))

            import sounddevice as sd
            lk = threading.Lock()
            in_ring = np.zeros(max(block_in * 6, in_sr), dtype=np.float32)
            out_ring = np.zeros(max(block_out * 8, out_sr), dtype=np.float32)
            in_len, out_len = len(in_ring), len(out_ring)
            st = {"ir": 0, "iw": 0, "or": 0, "ow": 0}
            xr = {"in": 0, "out": 0}
            primed = {"v": False}
            lv = {"in": 0.0, "out": 0.0}

            def in_cb(indata, frames, t, status):
                x = indata[:, 0]
                lv["in"] = float(np.sqrt(np.mean(x * x)) + 1e-9)
                with lk:
                    if st["iw"] - st["ir"] + frames > in_len:      # 输入积压溢出：丢掉，记录
                        xr["in"] += 1
                        return
                    e = st["iw"] % in_len
                    n = min(frames, in_len - e)
                    in_ring[e:e + n] = x[:n]
                    if n < frames:
                        in_ring[:frames - n] = x[n:]
                    st["iw"] += frames

            def out_cb(outdata, frames, t, status):
                with lk:
                    avail = st["ow"] - st["or"]
                    if not primed["v"]:
                        if avail >= block_out * self.prefill:
                            primed["v"] = True
                        else:
                            outdata.fill(0.0)
                            return
                    take = min(frames, avail)
                    y = np.zeros(frames, dtype=np.float32)
                    e = st["or"] % out_len
                    n = min(take, out_len - e)
                    y[:n] = out_ring[e:e + n]
                    if n < take:
                        y[n:take] = out_ring[:take - n]
                    st["or"] += take
                    if take < frames:
                        xr["out"] += 1
                    outdata[:, 0] = y
                    lv["out"] = float(np.sqrt(np.mean(y * y)))

            ist = sd.InputStream(device=in_dev, samplerate=in_sr, channels=1,
                                 blocksize=dev_in, dtype="float32",
                                 latency="low", callback=in_cb)
            ost = sd.OutputStream(device=out_dev, samplerate=out_sr, channels=1,
                                  blocksize=dev_out, dtype="float32",
                                  latency="low", callback=out_cb)
            ist.start()
            ost.start()
            self._put(phase="running", msg="运行中",
                      in_dev=cfg["in_spec"] or "默认", out_dev=cfg["out_spec"] or "默认",
                      in_sr=in_sr, out_sr=out_sr, blocks=0,
                      xrun_in=0, xrun_out=0, queue=0.0, lat=0.0)

            times = []
            gcount = 0
            while not self._stop.is_set():
                with lk:
                    have = st["iw"] - st["ir"]
                    if have >= block_in:
                        e = st["ir"] % in_len
                        raw = np.empty(block_in, dtype=np.float32)
                        n = min(block_in, in_len - e)
                        raw[:n] = in_ring[e:e + n]
                        if n < block_in:
                            raw[n:] = in_ring[:block_in - n]
                        st["ir"] += block_in
                        queued = st["ow"] - st["or"]
                    else:
                        raw = None
                        queued = 0
                if raw is None:
                    time.sleep(0.004)
                    continue

                if self._thr_gate > 0 and float(np.sqrt(np.mean(raw * raw))) < self._thr_gate:
                    # 静音门限：不推理，直接补静音；省下的算力全留给说话那一瞬
                    a, ms = np.zeros(block_out, dtype=np.float32), 0.0
                    gcount += 1
                    gated = True
                else:
                    x16 = rt._resample(raw, in_sr, rt.SR_IN) if in_sr != rt.SR_IN else raw
                    if len(x16) < eng.block:
                        x16 = np.pad(x16, (0, eng.block - len(x16)))
                    eng.push(x16[:eng.block])
                    a, ms = eng.run()
                    if out_sr != rt.SR_OUT:
                        a = rt._resample(a, rt.SR_OUT, out_sr)
                    a = (a * self._gain).astype(np.float32)
                    gated = False

                with lk:
                    space = out_len - (st["ow"] - st["or"])
                    if len(a) > space:                 # 输出积压保护：丢最旧，防止延迟膨胀
                        drop = len(a) - space
                        st["or"] += drop
                        a = a[drop:]
                        xr["out"] += 1
                    e = st["ow"] % out_len
                    n = min(len(a), out_len - e)
                    out_ring[e:e + n] = a[:n]
                    if n < len(a):
                        out_ring[:len(a) - n] = a[n:]
                    st["ow"] += len(a)
                    q = (st["ow"] - st["or"]) / block_out

                if not gated:
                    times.append(ms)
                    if len(times) > 60:
                        times.pop(0)
                self._put(ms=(sum(times) / len(times)) if times else 0.0,
                          blocks=self.stats["blocks"] + 1,
                          queue=round(q, 2),
                          lat=round(queued / out_sr * 1000.0, 1),
                          xrun_in=xr["in"], xrun_out=xr["out"], gate=gcount, **lv)
        except BaseException as e:  # 包括 SystemExit：RVC 内部会用 sys.exit 报错
            import traceback
            traceback.print_exc()
            self._put(phase="error", msg="%s: %s" % (type(e).__name__, e))
        finally:
            for s in (ist, ost):
                try:
                    if s:
                        s.stop()
                except Exception:
                    pass
            if sys.platform == "win32" and coinit is not None:
                try:
                    import ctypes
                    ctypes.windll.ole32.CoUninitialize()
                except Exception:
                    pass
            if self.phase != "error":
                self._put(phase="idle", msg="已停止", ms=0.0, queue=0.0, lat=0.0)


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
    ap.add_argument("--dev-block", type=float, default=0.05,
                    help="设备回调块长(秒)：卡顿就调大(0.08~0.15)，延迟敏感就调小")
    ap.add_argument("--prefill", type=int, default=1, help="输出预铺块数")
    ap.add_argument("--config", default=os.path.join("configs", "config.json"),
                    help="参数契约（配方写入的文件）")
    ap.add_argument("--engine", default="", help="onnx | torch-cuda（默认 onnx）")
    a = ap.parse_args()
    CTRL.dev_block = a.dev_block
    CTRL.prefill = a.prefill
    CTRL.engine = a.engine or "onnx"
    apply_contract(CTRL, a.config)
    print("[web_ui] 引擎=%s" % CTRL.engine, flush=True)
    print("RVC Web console -> http://%s:%d  (dev-block=%.3fs prefill=%d)"
          % (a.host, a.port, a.dev_block, a.prefill), flush=True)
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
