"""torch 引擎适配器：把 RVC 原生推理（含 rmvpe / index 检索）接到我们的音频层上。

为什么要它：ONNX 版轻，但没有 rmvpe、没有 index 检索——那是旧界面"跟随好"的关键。
为什么不用 realtime_gui 的整条链路：它的音频层是耦合在 Tk 里的，而我们的音频层
（环形缓冲 + 50ms 设备块 + CoInitializeEx）已经修好并验证过。这里只借"脑子"。

接口与 onnx_rt.Engine 保持一致：
    eng = TorchEngine(contract_dict)
    eng.push(x16_block)          # 16k float32
    audio, ms = eng.run()        # 输出 48k float32
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

SR_IN = 16000
SR_OUT = 48000


def _resample(x, sr_from, sr_to):
    if sr_from == sr_to:
        return x
    from math import gcd
    from scipy.signal import resample_poly
    g = gcd(sr_from, sr_to)
    return resample_poly(x, sr_to // g, sr_from // g).astype(np.float32)


class TorchEngine:
    """契约字段 → RVC 原生推理。字段名沿用 config.json（block_time / extra_time / threhold ...）。"""

    def __init__(self, contract, verbose=True):
        import torch
        from configs.config import Config
        from infer.rtrvc import RVC

        self._torch = torch
        c = contract or {}
        self.block_s = float(c.get("block_time", 0.25))
        self.block = max(160, int(SR_IN * self.block_s) // 160 * 160)
        self.extra = max(0, int(SR_IN * float(c.get("extra_time", 2.0))) // 160 * 160)
        self.frames = self.block // 160
        self.skip_head = self.extra // 160
        # 只取正好一块：多要一帧会让输出比输入长，导致积压漂移
        self.return_length = self.frames
        self.f0method = str(c.get("f0method") or "rmvpe")
        self.fade = int(SR_OUT * float(c.get("crossfade_length") or 0.0))

        # configs/config.py 内部会自己 arg_parse，会把 web_ui 的参数当非法参数然后 sys.exit；
        # 构造期间临时屏蔽 sys.argv。
        _saved = sys.argv
        try:
            sys.argv = [sys.argv[0]]
            conf = Config()
        finally:
            sys.argv = _saved
        self.rvc = RVC(int(c.get("pitch", 0)), float(c.get("formant", 0.0)),
                       c.get("pth_path", ""), c.get("index_path", ""),
                       float(c.get("index_rate", 0.0) or 0.0), conf)
        if getattr(self.rvc, "net_g", None) is None:
            raise RuntimeError("torch 引擎初始化失败（见上方 traceback）：模型路径或依赖有问题")
        self.sr_out = int(getattr(self.rvc, "tgt_sr", 40000))

        self.buf = np.zeros(self.extra + self.block, dtype=np.float32)
        self.tail = np.zeros(max(0, self.fade), dtype=np.float32)
        if verbose:
            print("[torch] device=%s half=%s tgt_sr=%d block=%.3fs extra=%.2fs f0=%s index=%.2f fade=%d" %
                  (conf.device, conf.is_half, self.sr_out, self.block / SR_IN,
                   self.extra / SR_IN, self.f0method, float(c.get("index_rate", 0.0) or 0.0),
                   self.fade), flush=True)

    def push(self, block16: np.ndarray):
        n = len(block16)
        if n >= len(self.buf):
            self.buf[:] = block16[-len(self.buf):]
            return
        self.buf = np.roll(self.buf, -n)
        self.buf[-n:] = block16

    def run(self):
        torch = self._torch
        t0 = time.perf_counter()
        wav = torch.from_numpy(np.ascontiguousarray(self.buf)).float().to(self.rvc.device)
        with torch.no_grad():
            out = self.rvc.infer(wav, self.block, self.skip_head,
                                 self.return_length, self.f0method)
        a = out.detach().float().cpu().numpy().reshape(-1)
        if self.sr_out != SR_OUT:
            a = _resample(a, self.sr_out, SR_OUT)
        # 交叉淡化：块边界平滑，消掉咔哒
        if self.fade > 0 and len(a) > self.fade and len(self.tail) == self.fade:
            w = np.linspace(0.0, 1.0, self.fade, dtype=np.float32)
            a[:self.fade] = a[:self.fade] * w + self.tail * (1.0 - w)
        if self.fade > 0 and len(a) >= self.fade:
            self.tail = a[-self.fade:].copy()
        return a.astype(np.float32), (time.perf_counter() - t0) * 1000


# ---------------- 离线自检 ----------------
def _selftest(wav_path, contract):
    import librosa
    wav, _ = librosa.load(wav_path, sr=SR_IN, mono=True)
    wav = wav.astype(np.float32)
    eng = TorchEngine(contract)
    blk = eng.block
    n = len(wav) // blk
    print("输入 %.2fs / %d 块（每块 %.3fs，输出应为 %.3fs @48k）" %
          (len(wav) / SR_IN, n, blk / SR_IN, blk / SR_IN))
    times, out = [], []
    for i in range(n):
        eng.push(wav[i * blk:(i + 1) * blk])
        a, ms = eng.run()
        times.append(ms)
        out.append(a)
        if i < 3:
            print("  块 %d: %.0f ms  输出 %d 样本（%.3fs）" %
                  (i, ms, len(a), len(a) / SR_OUT))
    warm = times[3:] or times
    print("AVG=%.0f ms  MIN=%.0f  MAX=%.0f  (n=%d)" %
          (sum(warm) / len(warm), min(warm), max(warm), len(warm)))
    total = np.concatenate(out)
    print("输出总长 %.2fs（输入 %.2fs）" % (len(total) / SR_OUT, len(wav) / SR_IN))
    import soundfile as sf
    sf.write("_torch_selftest_out.wav", total, SR_OUT)
    print("已写 _torch_selftest_out.wav")


if __name__ == "__main__":
    import json
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join("configs", "config.json")
    wav_path = sys.argv[2] if len(sys.argv) > 2 else r"D:\Hanako-workspace\_rec3_t.wav"
    with open(cfg_path, encoding="utf-8") as f:
        _selftest(wav_path, json.load(f))
