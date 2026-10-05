"""网络音频：传输层（WebSocket 二进制 PCM）+ 可插拔的设备层。

- 会话规则在 audiohub.py（输入全局唯一、申请制 + 可抢占、超时自动释放）
- 这里只负责「搬运」与「设备」：谁持有输入权，它的 PCM 才被写进本地播放端；
  设备采集到的 PCM 广播给所有听众。
- 设备层可插拔：找不到虚拟线时，会话层照常工作（只是音频进不去 / 出不来的地方会明说）。

路由（对应 docs/AUDIO-NET.md）：
  远端麦克风 --WS--> 本服务 --写--> 「虚拟播放端 A」  →  引擎把输入设备选成「虚拟采集端 A」
  引擎播到「虚拟播放端 B」 --本服务从「虚拟采集端 B」读--> --WS--> 所有听众

用法：
  python audiosrv.py --selftest      # 不开真设备，用合成音源自测整条通道
  python audiosrv.py --port 8900     # 正常启动（自动按名字找虚拟线）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import threading
import time
from collections import deque

from audiohub import AudioHub

HERE = os.path.dirname(os.path.abspath(__file__))

# 设备名关键词（找不到就是没装线，会话层照常跑）
IN_PLAY_HINTS = ["CABLE Input", "CABLE-A Input", "VoiceMeeter Input"]
OUT_CAP_HINTS = ["CABLE Output", "CABLE-B Output", "VoiceMeeter Output"]


class AudioService:
    def __init__(self, hub: AudioHub, host="0.0.0.0", port=8900,
                 chunk_ms=60, monitor=None, synthetic=False, token=""):
        self.hub = hub
        self.host = host
        self.port = port
        self.chunk_ms = int(chunk_ms)
        self.monitor = monitor
        self.synthetic = synthetic
        self.token = token or ""
        self.sr = 48000
        self.clients = set()          # websockets
        self._by_ws = {}              # ws -> client id
        self.in_q = deque()           # 待写入播放端的 PCM（来自输入持有者）
        self.out_q = deque()          # 待广播的 PCM（来自采集端 / 合成源）
        self.stats = {"in_bytes": 0, "out_bytes": 0, "dropped": 0}
        self.devices = {"in_play": None, "out_cap": None, "note": ""}
        self._streams = []
        self._loop = None
        self._thread = None

    # ---------------- 设备层 ----------------
    def _find(self, hints, want_input):
        import sounddevice as sd
        cands = []
        for i, d in enumerate(sd.query_devices()):
            if want_input and d["max_input_channels"] <= 0:
                continue
            if (not want_input) and d["max_output_channels"] <= 0:
                continue
            api = sd.query_hostapis(d["hostapi"])["name"]
            if "WDM-KS" in api:
                continue          # WDM-KS 名字会被截断、也最脆弱，不选它
            for h in hints:
                if h.lower() in d["name"].lower():
                    cands.append((i, d["name"]))
                    break
        if not cands:
            return None, None
        cands.sort(key=lambda t: 0 if "WASAPI" in t[1] else 1)   # WASAPI 优先（无则任意）
        return cands[0]

    def open_devices(self):
        """打开两路设备：写播放端（远端麦克风送进引擎）、读采集端（引擎输出广播出去）。"""
        import sounddevice as sd
        import numpy as np
        self._np = np
        ok = []
        i_in, n_in = self._find(IN_PLAY_HINTS, want_input=False)
        if i_in is None:
            self.devices["note"] = "没找到虚拟播放端（缺第一对线）——远端麦克风无处可送"
        else:
            self.devices["in_play"] = n_in
            np_ = np

            def cb(outdata, frames, t, status):
                need, parts = frames, []
                while need > 0 and self.in_q:
                    c = self.in_q[0]
                    take = min(len(c), need)
                    parts.append(c[:take])
                    if take == len(c):
                        self.in_q.popleft()
                    else:
                        self.in_q[0] = c[take:]
                    need -= take
                y = np_.concatenate(parts) if parts else np_.zeros(0, dtype=np.float32)
                if len(y) < frames:
                    y = np_.pad(y, (0, frames - len(y)))
                outdata[:, 0] = y

            st = sd.OutputStream(device=i_in, samplerate=self.sr, channels=1,
                                 dtype="float32", blocksize=int(self.sr * self.chunk_ms / 1000),
                                 callback=cb)
            st.start()
            self._streams.append(st)
            ok.append("播放端 %s" % n_in)

        i_out, n_out = self._find(OUT_CAP_HINTS, want_input=True)
        # 同一对线会自己咬自己：写进 CABLE Input 的东西会立刻从 CABLE Output 出来，
        # 于是远端麦克风会被当成“引擎输出”马上广播回给所有人（回音/啸叫）。
        if i_out is not None and self.devices["in_play"]:
            import re
            def bare(s):
                s = re.sub(r"\b(input|output|in|out)\b", "", s, flags=re.I)
                return re.sub(r"\s+", " ", s).strip().lower()
            # 取前 12 字比对：容忍某些宿主 API 的设备名截断
            if bare(self.devices["in_play"])[:12] == bare(n_out)[:12]:
                self.devices["note"] = (
                    "输入与输出落在同一对虚拟线上（自己咬自己）：远端麦克风会被当成引擎输出"
                    "立刻广播回来。建议再装一对（VB-Cable A+B / VoiceMeeter）做分离。")
                i_out = None          # 宁可不广播，也不造回音
        if i_out is None:
            self.devices["note"] = (self.devices["note"] + "；" if self.devices["note"] else "") + \
                "没找到虚拟采集端（缺第二对线）——听众听不到引擎输出（可退化用 WASAPI loopback）"
        else:
            self.devices["out_cap"] = n_out

            def cb2(indata, frames, t, status):
                self.out_q.append(indata[:, 0].copy())
                while len(self.out_q) > 12:
                    self.out_q.popleft()
                    self.stats["dropped"] += 1

            st2 = sd.InputStream(device=i_out, samplerate=self.sr, channels=1,
                                 dtype="float32", blocksize=int(self.sr * self.chunk_ms / 1000),
                                 callback=cb2)
            st2.start()
            self._streams.append(st2)
            ok.append("采集端 %s" % n_out)
        return ok

    def _tone(self):
        """合成源（自测用）：440Hz 正弦，按块产出。"""
        while True:
            n = int(self.sr * self.chunk_ms / 1000)
            t = self._np.arange(n) / self.sr + self._phase
            self._phase += n / self.sr
            self.out_q.append((0.2 * self._np.sin(2 * self._np.pi * 440 * t)).astype("float32"))
            while len(self.out_q) > 12:
                self.out_q.popleft()
            time.sleep(self.chunk_ms / 1000)

    def _start_synthetic(self):
        import numpy as np
        self._np = np
        self._phase = 0.0
        threading.Thread(target=self._tone, daemon=True).start()

    # ---------------- 传输层 ----------------
    def is_holder(self, client):
        return bool(self.hub.holder and self.hub.holder["client"] == client)

    async def _broadcast_state(self):
        st = self.hub.state(time.time())
        msg = json.dumps({"type": "state", **st}, ensure_ascii=False)
        for ws in list(self.clients):
            try:
                await ws.send(msg)
            except Exception:
                self.clients.discard(ws)

    async def _handler(self, ws, path=None):
        cid = "c%d" % id(ws)
        self.clients.add(ws)
        self._by_ws[ws] = cid
        label = "未命名设备"
        try:
            async for msg in ws:
                now = time.time()
                if isinstance(msg, (bytes, bytearray)):
                    if self.is_holder(cid):
                        self.in_q.append(self._np.frombuffer(bytes(msg), dtype="<i2")
                                         .astype("float32") / 32768.0)
                        self.stats["in_bytes"] += len(msg)
                    continue
                try:
                    m = json.loads(msg)
                except Exception:
                    continue
                cmd = m.get("cmd")
                if cmd == "hello":
                    if self.token and m.get("k") != self.token:
                        await ws.send(json.dumps({"type": "error", "reason": "令牌不对"},
                                                 ensure_ascii=False))
                        await ws.close()
                        return
                    label = (m.get("label") or label)[:40]
                    self.hub.seen(cid, label, now)
                    await ws.send(json.dumps({"type": "hello_ok", "client": cid}, ensure_ascii=False))
                    await self._broadcast_state()
                elif cmd == "claim":
                    ok, note, _ = self.hub.claim(cid, label, now)
                    await ws.send(json.dumps({"type": "claim_result", "ok": ok, "note": note,
                                              **self.hub.state(now)}, ensure_ascii=False))
                    await self._broadcast_state()
                elif cmd == "takeover":
                    ok, note, _ = self.hub.takeover(cid, label, now)
                    await ws.send(json.dumps({"type": "claim_result", "ok": ok, "note": note,
                                              "took_over": True, **self.hub.state(now)},
                                             ensure_ascii=False))
                    await self._broadcast_state()
                elif cmd == "release":
                    ok, note, _ = self.hub.release(cid, now)
                    await ws.send(json.dumps({"type": "release_result", "ok": ok, "note": note},
                                             ensure_ascii=False))
                    await self._broadcast_state()
                elif cmd == "ping":
                    self.hub.seen(cid, label, now)
        except Exception:
            pass
        finally:
            self.clients.discard(ws)
            self._by_ws.pop(ws, None)
            self.hub.disconnect(cid, time.time())
            try:
                await self._broadcast_state()
            except Exception:
                pass

    async def _pump(self):
        """把采集端（或合成源）的 PCM 广播给所有听众。"""
        while True:
            await asyncio.sleep(self.chunk_ms / 1000.0)
            if not self.out_q or not self.clients:
                continue
            chunk = self.out_q.popleft()
            pcm = (chunk * 32767.0).astype("<i2").tobytes()
            self.stats["out_bytes"] += len(pcm)
            for ws in list(self.clients):
                try:
                    await ws.send(pcm)
                except Exception:
                    self.clients.discard(ws)

    async def _serve(self):
        import websockets
        async with websockets.serve(self._handler, self.host, self.port):
            await self._pump()

    def start(self):
        import asyncio
        self._thread = threading.Thread(target=lambda: asyncio.run(self._serve()), daemon=True)
        self._thread.start()
        time.sleep(0.6)

    def stop(self):
        for s in self._streams:
            try:
                s.stop()
            except Exception:
                pass
        self._streams = []


# ---------------- 自检：不开真设备，只用合成源跑完整条通道 ----------------
async def _selftest(port):
    import websockets
    base = "ws://127.0.0.1:%d" % port

    async def talk(ws, cmds):
        out = []
        await ws.send(json.dumps({"cmd": "hello", "label": cmds[0]}))
        out.append(await ws.recv())
        for c in cmds[1:]:
            await ws.send(json.dumps({"cmd": c}))
            # 合成源会不断混进音频帧，所以不能只读三次：限时找结果
            t0 = time.time()
            while time.time() - t0 < 2.0:
                try:
                    m = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except Exception:
                    break
                out.append(m)
                if isinstance(m, str):
                    j = json.loads(m)
                    if j.get("type") in ("claim_result", "release_result"):
                        break
        return out

    async with websockets.connect(base) as a, websockets.connect(base) as b:
        ra = await talk(a, ["A-手机", "claim"])
        print("A 申请   ->", ra[-1])
        rb = await talk(b, ["B-平板", "claim"])
        print("B 申请   ->", rb[-1])
        rb2 = await talk(b, ["B-平板", "takeover"])
        print("B 抢占   ->", rb2[-1])
        # A 现在应收到 revoked（可能已混在上面几条里）
        # A 不再是持有者：发 PCM 不应被计入；B 是持有者：应被计入
        import numpy as np
        before = None
        await a.send(np.zeros(960, dtype="<i2").tobytes())
        await b.send((np.ones(960) * 3000).astype("<i2").tobytes())
        await asyncio.sleep(0.8)
        # B 作为听众应收音频帧（有限次收集，音频一直在流所以不能等到超时）
        got = 0
        t0 = time.time()
        while time.time() - t0 < 2.0 and got < 30:
            try:
                m = await asyncio.wait_for(b.recv(), timeout=0.5)
            except Exception:
                break
            if isinstance(m, (bytes, bytearray)):
                got += 1
        print("B 收到音频帧:", got)
        await b.send(json.dumps({"cmd": "release"}))
        m = await b.recv()
        print("B 释放   ->", m)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8900)
    ap.add_argument("--chunk-ms", type=int, default=60)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    svc = AudioService(AudioHub(), host=a.host, port=a.port, chunk_ms=a.chunk_ms,
                       synthetic=a.selftest)
    if a.selftest:
        svc._start_synthetic()
        svc.start()
        print("== 合成源自测（无真实设备）==")
        asyncio.run(_selftest(a.port))
        print("== stats:", svc.stats)
        svc.stop()
    else:
        opened = svc.open_devices()
        print("[网络音频] 已打开：%s" % (", ".join(opened) or "（无）"), flush=True)
        if svc.devices["note"]:
            print("[网络音频] 注意：%s" % svc.devices["note"], flush=True)
        svc.start()
        print("[网络音频] WebSocket 在 ws://%s:%d" % (a.host, a.port), flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            svc.stop()
