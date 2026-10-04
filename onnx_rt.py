"""RVC 实时变声 · ONNX 版（后端可切：CPU / 2060-DML / 核显-DML）

用法：
  # 离线自测（不出声，只测耗时）
  python onnx_rt.py --selftest --ep cpu
  # 实时（麦克风 -> 扬声器）
  python onnx_rt.py --ep cpu --block 0.5 --ctx 0.25 --f0 pm

  --ep     cpu | dml0(2060) | dml1(核显) | cuda
  --block  块长秒数（延迟≈block+ctx+推理）
  --ctx    上下文秒数（给特征/音高的历史，越小越省）
  --f0     pm(快,CPU) | fcpe(torch,较慢)
  --threads 限制 ORT 计算线程（给游戏留核）
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import deque

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

import numpy as np
import torch
import onnxruntime as ort

SR_IN = 16000
SR_OUT = 48000
PTH = "assets/weights/julesbrown.pth"
HUBERT_ONNX = "_hubert.onnx"


def log(*a):
    print(*a, flush=True)


def prov_of(ep: str):
    if ep == "cpu":
        return ["CPUExecutionProvider"]
    if ep == "dml0":
        return [("DmlExecutionProvider", {"device_id": 0})]
    if ep == "dml1":
        return [("DmlExecutionProvider", {"device_id": 1})]
    if ep == "cuda":
        return [("CUDAExecutionProvider", {"device_id": 0}), "CPUExecutionProvider"]
    raise SystemExit("unknown --ep " + ep)


def mk_session(fp, ep, threads=0):
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    if threads > 0:
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = max(1, min(4, threads))
    return ort.InferenceSession(fp, so, providers=prov_of(ep))


# ---------- ONNX 导出 ----------
class _GenWrap(torch.nn.Module):
    def __init__(self, net, head, length):
        super().__init__()
        self.net = net
        self.head = int(head)
        self.length = int(length)

    def forward(self, phone, lengths, coarse, continuous, speaker):
        return self.net.infer(phone, lengths, coarse, continuous, speaker,
                              self.head, self.length, self.length)[0]


def ensure_gen_onnx(head, length, path):
    if os.path.exists(path):
        return path
    log("导出生成器 ONNX: head=%d length=%d -> %s" % (head, length, path))
    from infer.rtrvc import get_synthesizer
    net_g, _ = get_synthesizer(PTH, torch.device("cpu"))
    net_g.eval()
    P = head + length
    w = _GenWrap(net_g, head, length).eval()
    phone = torch.randn(1, P, 768)
    args = (phone, torch.LongTensor([P]), torch.zeros(1, P, dtype=torch.long),
            torch.zeros(1, P, dtype=torch.float32), torch.LongTensor([0]))
    with torch.no_grad():
        torch.onnx.export(w, args, path,
                          input_names=["phone", "lengths", "coarse", "continuous", "speaker"],
                          output_names=["audio"], opset_version=17)
    return path


def ensure_hubert_onnx():
    if os.path.exists(HUBERT_ONNX):
        return HUBERT_ONNX
    log("导出 hubert ONNX ->", HUBERT_ONNX)
    from infer.hubert import load_hubert_model

    class W(torch.nn.Module):
        def __init__(self, m):
            super().__init__()
            self.m = m

        def forward(self, input_values):
            return self.m(input_values=input_values, attention_mask=None,
                          output_hidden_states=False, return_dict=True).last_hidden_state

    hm = load_hubert_model(torch.device("cpu"), False).eval()
    w = W(hm).eval()
    with torch.no_grad():
        torch.onnx.export(w, (torch.randn(1, 16000),), HUBERT_ONNX,
                          input_names=["input_values"], output_names=["features"],
                          opset_version=17,
                          dynamic_axes={"input_values": {1: "T"}, "features": {1: "T"}})
    return HUBERT_ONNX


# ---------- f0 ----------
class Pitch:
    """音高提取 + mel 离散化（对齐 rtrvc 的实现）。"""

    def __init__(self, method, up_key):
        self.method = method
        self.up_key = up_key
        self.f0_min, self.f0_max = 50, 1100
        self.f0_mel_min = 1127 * np.log(1 + self.f0_min / 700)
        self.f0_mel_max = 1127 * np.log(1 + self.f0_max / 700)
        self._fc = None
        if method == "pm":
            import parselmouth  # noqa
        elif method == "fcpe":
            from infer.fcpe import FCPEInfer
            self._fc = FCPEInfer(torch.device("cpu"))

    def _post(self, f0_np):
        f0 = torch.from_numpy(np.asarray(f0_np, dtype=np.float32))
        mel = 1127 * torch.log(1 + f0 / 700)
        mel[mel > 0] = (mel[mel > 0] - self.f0_mel_min) * 254 / (self.f0_mel_max - self.f0_mel_min) + 1
        mel[mel <= 1] = 1
        mel[mel > 255] = 255
        return torch.round(mel).long(), f0

    def __call__(self, x16: np.ndarray):
        n = x16.shape[0]
        p_len = n // 160 + 1
        if self.method == "pm":
            import parselmouth
            f0_min = 65
            l_pad = int(np.ceil(1.5 / f0_min * 16000))
            s = parselmouth.Sound(np.pad(x16, (l_pad, l_pad + 1)), 16000).to_pitch_ac(
                time_step=0.01, voicing_threshold=0.6, pitch_floor=f0_min, pitch_ceiling=1100)
            f0 = s.selected_array["frequency"]
            if len(f0) < p_len:
                f0 = np.pad(f0, (0, p_len - len(f0)))
            f0 = f0[:p_len]
        else:
            f0 = self._fc.infer(torch.from_numpy(x16).unsqueeze(0).float(),
                                sr=16000, decoder_mode="local_argmax",
                                threshold=0.006).squeeze().detach().cpu().numpy()
            if len(f0) < p_len:
                f0 = np.pad(f0, (0, p_len - len(f0)))
            f0 = f0[:p_len]
        uv = f0 == 0
        if np.any(~uv):
            f0[uv] = np.interp(np.where(uv)[0], np.where(~uv)[0], f0[~uv])
        f0 = f0 * (2 ** (self.up_key / 12))
        return self._post(f0)


# ---------- 核心 ----------
class Engine:
    def __init__(self, ep, block_s, ctx_s, f0method, threads, up_key=12):
        self.block = int(SR_IN * block_s) // 160 * 160
        self.ctx = int(SR_IN * ctx_s) // 160 * 160
        self.head = self.ctx // 160
        self.length = self.block // 160
        self.P = self.head + self.length
        self.ep = ep
        log("block=%.3fs(%d) ctx=%.3fs(%d) frames P=%d" %
            (self.block / SR_IN, self.block, self.ctx / SR_IN, self.ctx, self.P))
        self.gen = mk_session(ensure_gen_onnx(self.head, self.length,
                                              "_rtgen_c%d_b%d.onnx" % (self.head, self.length)),
                              ep, threads)
        self.hub = mk_session(ensure_hubert_onnx(), ep, threads)
        self.pitch = Pitch(f0method, up_key)
        self.buf = np.zeros(self.ctx + self.block, dtype=np.float32)

    def push(self, block16: np.ndarray):
        self.buf = np.roll(self.buf, -len(block16))
        self.buf[-len(block16):] = block16

    def run(self):
        t0 = time.perf_counter()
        # 1) hubert
        feats = self.hub.run(None, {"input_values": self.buf[None].astype(np.float32)})[0]
        # 2) 对齐到 2 倍帧率（与 rtrvc 一致：末尾补一帧再插值）
        feats = np.concatenate([feats, feats[:, -1:, :]], axis=1)
        f = torch.from_numpy(feats)
        f = torch.nn.functional.interpolate(f.permute(0, 2, 1), scale_factor=2).permute(0, 2, 1)
        phone = f[:, :self.P, :].contiguous().numpy().astype(np.float32)
        if phone.shape[1] < self.P:
            pad = np.zeros((1, self.P - phone.shape[1], 768), dtype=np.float32)
            phone = np.concatenate([phone, pad], axis=1)
        # 3) pitch
        win = self.buf[-(self.block + 800):]
        coarse, pitchf = self.pitch(win)
        if coarse.shape[0] < self.P:
            coarse = torch.cat([coarse[:1].repeat(self.P - coarse.shape[0]), coarse])
            pitchf = torch.cat([pitchf[:1].repeat(self.P - pitchf.shape[0]), pitchf])
        coarse = coarse[-self.P:].unsqueeze(0).numpy().astype(np.int64)
        pitchf = pitchf[-self.P:].unsqueeze(0).numpy().astype(np.float32)
        # 4) 生成器
        audio = self.gen.run(None, {
            "phone": phone, "lengths": np.array([self.P], dtype=np.int64),
            "coarse": coarse, "continuous": pitchf,
            "speaker": np.array([0], dtype=np.int64)})[0]
        return audio.reshape(-1).astype(np.float32), (time.perf_counter() - t0) * 1000


def selftest(eng, wav):
    log("=== selftest ===")
    blocks = len(wav) // eng.block
    times = []
    out = []
    for i in range(blocks):
        eng.push(wav[i * eng.block:(i + 1) * eng.block])
        a, ms = eng.run()
        times.append(ms)
        out.append(a)
        if i < 3 or i % 5 == 0:
            log("  block %2d: %.0f ms (block=%.0fms)" % (i, ms, eng.block / SR_IN * 1000))
    warm = times[3:]
    if warm:
        log("AVG=%.0f ms  MIN=%.0f  MAX=%.0f  (n=%d)" % (sum(warm) / len(warm), min(warm), max(warm), len(warm)))
    audio = np.concatenate(out)
    import soundfile as sf
    sf.write("_rt_selftest_out.wav", audio, SR_OUT)
    log("wrote _rt_selftest_out.wav", len(audio) / SR_OUT, "s")


def realtime(eng, in_name, out_name, gain=1.0):
    import sounddevice as sd

    q_in = deque()
    q_out = deque()
    stats = []

    def in_cb(indata, frames, t, status):
        if status:
            pass
        q_in.append(indata[:, 0].copy())

    def out_cb(outdata, frames, t, status):
        need = frames
        buf = np.empty(0, dtype=np.float32)
        while need > 0 and q_out:
            chunk = q_out[0]
            take = min(len(chunk), need)
            buf = np.concatenate([buf, chunk[:take]])
            if take == len(chunk):
                q_out.popleft()
            else:
                q_out[0] = chunk[take:]
            need -= take
        if need > 0:
            buf = np.concatenate([buf, np.zeros(need, dtype=np.float32)])
        outdata[:] = buf.reshape(-1, 1)

    ist = sd.InputStream(samplerate=SR_IN, channels=1, blocksize=eng.block,
                         dtype="float32", callback=in_cb)
    ost = sd.OutputStream(samplerate=SR_OUT, channels=1, blocksize=eng.block * 3,
                          dtype="float32", callback=out_cb)
    ist.start()
    ost.start()
    log("已开启：说话吧（Ctrl+C 退出）  设备: in=%s out=%s" % (in_name or "default", out_name or "default"))
    try:
        while True:
            if not q_in:
                time.sleep(0.005)
                continue
            blk = q_in.popleft()
            eng.push(blk)
            a, ms = eng.run()
            q_out.append(a * gain)
            stats.append(ms)
            if len(stats) % 20 == 0:
                log("  推理平均 %.0f ms / %d 块   输出积压 %d" % (sum(stats[-20:]) / 20, len(stats), len(q_out)))
    except KeyboardInterrupt:
        pass
    finally:
        ist.stop(); ost.stop()
        log("停止。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ep", default="cpu")
    ap.add_argument("--block", type=float, default=0.5)
    ap.add_argument("--ctx", type=float, default=0.25)
    ap.add_argument("--f0", default="pm")
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--gain", type=float, default=1.0)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--selftest-wav", default=r"D:\Hanako-workspace\_rec3_t.wav")
    a = ap.parse_args()

    eng = Engine(a.ep, a.block, a.ctx, a.f0, a.threads)
    if a.selftest:
        import librosa
        wav, _ = librosa.load(a.selftest_wav, sr=SR_IN, mono=True)
        selftest(eng, wav.astype(np.float32))
    else:
        realtime(eng, None, None, a.gain)


if __name__ == "__main__":
    main()
