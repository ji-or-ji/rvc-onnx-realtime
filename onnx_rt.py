"""RVC 实时变声 · ONNX 版（后端可切：CPU / 2060-DML / 核显-DML）

用法：
  python onnx_rt.py --list-devices                 # 列出所有音频设备
  python onnx_rt.py --selftest --ep cpu            # 离线自测（只报耗时）
  python onnx_rt.py --ep dml0 --in "USB Audio" --out "CABLE Input"

  --ep           cpu | dml0(2060) | dml1(核显) | cuda
  --block        块长秒数（延迟≈block+ctx+推理）
  --ctx          上下文秒数（给特征/音高的历史，越小越省）
  --f0           pm(快,CPU) | fcpe(torch,较慢)
  --threads      限制 ORT 计算线程（给游戏留核）
  --in / --out   输入/输出设备：序号，或名称里的关键词（如 "CABLE Input" / "USB Audio"）
  --in-sr/--out-sr  设备采样率，0 = 自动取设备默认（会自动重采样，无需设备支持 16k/48k）
  --gain         输出增益

虚拟声卡工作流（给 OBS 直接捕获）：
  1) 装 VB-Audio Virtual Cable（免费）→ 系统里出现 "CABLE Input"(播放) / "CABLE Output"(录音)
  2) 本工具：--out "CABLE Input"
  3) OBS：麦克风/音频输入 → 选 "CABLE Output"
  4) 想自己也听到：OBS 里加个音频监听，或用 VoiceMeeter 混合
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


# ---------- 音频设备 ----------
def list_devices():
    import sounddevice as sd
    log("idx | io  | name | hostapi | default_sr")
    for i, d in enumerate(sd.query_devices()):
        tag = ("IN " if d["max_input_channels"] > 0 else "") + ("OUT" if d["max_output_channels"] > 0 else "")
        log("%3d | %-3s | %s | %s | %d" % (i, tag.strip() or "-", d["name"],
                                           sd.query_hostapis(d["hostapi"])["name"], int(d["default_samplerate"])))


def resolve_dev(spec, want_input: bool):
    """spec: None/'' -> 系统默认; 纯数字 -> 序号; 其他 -> 名称关键词。"""
    import sounddevice as sd
    devs = sd.query_devices()
    if not spec:
        return None, None
    ch = "max_input_channels" if want_input else "max_output_channels"
    if str(spec).isdigit():
        i = int(spec)
        if i < 0 or i >= len(devs):
            raise SystemExit("设备序号越界: %s" % spec)
        return i, int(devs[i]["default_samplerate"])
    key = str(spec).lower()
    for i, d in enumerate(devs):
        if key in d["name"].lower() and d[ch] > 0:
            return i, int(d["default_samplerate"])
    raise SystemExit("找不到%s设备（关键词 %r）。用 --list-devices 看名字。" %
                     ("输入" if want_input else "输出", spec))


def _resample(x, sr_from, sr_to):
    if sr_from == sr_to:
        return x
    from math import gcd
    g = gcd(sr_from, sr_to)
    from scipy.signal import resample_poly
    return resample_poly(x, sr_to // g, sr_from // g).astype(np.float32)


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
        feats = self.hub.run(None, {"input_values": self.buf[None].astype(np.float32)})[0]
        feats = np.concatenate([feats, feats[:, -1:, :]], axis=1)
        f = torch.from_numpy(feats)
        f = torch.nn.functional.interpolate(f.permute(0, 2, 1), scale_factor=2).permute(0, 2, 1)
        phone = f[:, :self.P, :].contiguous().numpy().astype(np.float32)
        if phone.shape[1] < self.P:
            pad = np.zeros((1, self.P - phone.shape[1], 768), dtype=np.float32)
            phone = np.concatenate([phone, pad], axis=1)
        win = self.buf[-(self.block + 800):]
        coarse, pitchf = self.pitch(win)
        if coarse.shape[0] < self.P:
            coarse = torch.cat([coarse[:1].repeat(self.P - coarse.shape[0]), coarse])
            pitchf = torch.cat([pitchf[:1].repeat(self.P - pitchf.shape[0]), pitchf])
        coarse = coarse[-self.P:].unsqueeze(0).numpy().astype(np.int64)
        pitchf = pitchf[-self.P:].unsqueeze(0).numpy().astype(np.float32)
        audio = self.gen.run(None, {
            "phone": phone, "lengths": np.array([self.P], dtype=np.int64),
            "coarse": coarse, "continuous": pitchf,
            "speaker": np.array([0], dtype=np.int64)})[0]
        return audio.reshape(-1).astype(np.float32), (time.perf_counter() - t0) * 1000


def selftest(eng, wav):
    log("=== selftest ===")
    blocks = len(wav) // eng.block
    times, out = [], []
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
    import soundfile as sf
    sf.write("_rt_selftest_out.wav", np.concatenate(out), SR_OUT)
    log("wrote _rt_selftest_out.wav")


def realtime(eng, in_spec, out_spec, in_sr, out_sr, gain=1.0):
    import sounddevice as sd

    in_dev, in_default_sr = resolve_dev(in_spec, True)
    out_dev, out_default_sr = resolve_dev(out_spec, False)
    in_sr = int(in_sr) or in_default_sr or SR_IN
    out_sr = int(out_sr) or out_default_sr or SR_OUT
    block_in = int(round(eng.block * in_sr / SR_IN))
    log("设备: in=%s(%sHz%s)  out=%s(%sHz%s)" %
        (in_spec or "默认", in_sr, "" if in_sr == SR_IN else " -> 内部16k",
         out_spec or "默认", out_sr, "" if out_sr == SR_OUT else " <- 模型48k"))

    q_in = deque()
    q_out = deque()
    stats = []

    def in_cb(indata, frames, t, status):
        q_in.append(indata[:, 0].copy())

    def out_cb(outdata, frames, t, status):
        need, buf = frames, np.empty(0, dtype=np.float32)
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

    ist = sd.InputStream(device=in_dev, samplerate=in_sr, channels=1,
                         blocksize=block_in, dtype="float32", callback=in_cb)
    ost = sd.OutputStream(device=out_dev, samplerate=out_sr, channels=1,
                          blocksize=int(round(eng.block * out_sr / SR_IN)),
                          dtype="float32", callback=out_cb)
    ist.start()
    ost.start()
    log("已开启，说话吧（Ctrl+C 退出）")
    try:
        while True:
            if not q_in:
                time.sleep(0.005)
                continue
            raw = q_in.popleft()
            x16 = _resample(raw, in_sr, SR_IN) if in_sr != SR_IN else raw
            if len(x16) < eng.block:
                x16 = np.pad(x16, (0, eng.block - len(x16)))
            eng.push(x16[:eng.block])
            a, ms = eng.run()
            if out_sr != SR_OUT:
                a = _resample(a, SR_OUT, out_sr)
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
    ap.add_argument("--in", dest="in_spec", default="")
    ap.add_argument("--out", dest="out_spec", default="")
    ap.add_argument("--in-sr", type=int, default=0)
    ap.add_argument("--out-sr", type=int, default=0)
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--selftest-wav", default=r"D:\Hanako-workspace\_rec3_t.wav")
    a = ap.parse_args()

    if a.list_devices:
        list_devices()
        return

    eng = Engine(a.ep, a.block, a.ctx, a.f0, a.threads)
    if a.selftest:
        import librosa
        wav, _ = librosa.load(a.selftest_wav, sr=SR_IN, mono=True)
        selftest(eng, wav.astype(np.float32))
    else:
        realtime(eng, a.in_spec, a.out_spec, a.in_sr, a.out_sr, a.gain)


if __name__ == "__main__":
    main()
