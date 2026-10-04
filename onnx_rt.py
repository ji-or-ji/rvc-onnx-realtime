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

# 运行期不 import torch：torch 自带 cuDNN 9，会顶掉 onnxruntime CUDA EP 需要的 cuDNN 8。
# 只有导出 ONNX 模型时才在函数内部按需 import torch。
# onnxruntime 也不在模块级导入（挪到 mk_session 里惰性导入）：
# 只要不用 ONNX 引擎，缺 onnxruntime 也能跑（torch 高档档位就不需要它）。

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
    import onnxruntime as ort
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


def resolve_dev(spec, want_input: bool, prefer_wasapi=True, hostapi=None):
    """spec: None/'' -> 系统默认; 纯数字 -> 序号; 其他 -> 名称关键词。

    同名设备往往在多个宿主 API 下都存在（MME / DirectSound / WASAPI / WDM-KS）。
    MME 是模拟层、抖动大；同名时优先 WASAPI（config 里的 sg_hostapi 优先）。
    """
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
    hits = [i for i, d in enumerate(devs) if key in d["name"].lower() and d[ch] > 0]
    if not hits:
        raise SystemExit("找不到%s设备（关键词 %r）。用 --list-devices 看名字。" %
                         ("输入" if want_input else "输出", spec))

    def api_of(i):
        return sd.query_hostapis(devs[i]["hostapi"])["name"]

    def rank(i):
        a = api_of(i).lower()
        if hostapi and hostapi.lower().replace("windows ", "") in a:
            return 0
        if prefer_wasapi and "wasapi" in a:
            return 1
        return 2

    best = min(hits, key=rank)          # min 稳定，同级取最先出现者
    return best, int(devs[best]["default_samplerate"])


def _resample(x, sr_from, sr_to):
    if sr_from == sr_to:
        return x
    from math import gcd
    g = gcd(sr_from, sr_to)
    from scipy.signal import resample_poly
    return resample_poly(x, sr_to // g, sr_from // g).astype(np.float32)


# ---------- ONNX 导出 ----------
def ensure_gen_onnx(head, length, path):
    if os.path.exists(path):
        return path
    import torch                                    # 仅导出时需要（会把 CUDA EP 顶掉，故不常驻）
    log("导出生成器 ONNX: head=%d length=%d -> %s" % (head, length, path))
    from infer.rtrvc import get_synthesizer

    class _GenWrap(torch.nn.Module):
        def __init__(self, net, head, length):
            super().__init__()
            self.net = net
            self.head = int(head)
            self.length = int(length)

        def forward(self, phone, lengths, coarse, continuous, speaker):
            return self.net.infer(phone, lengths, coarse, continuous, speaker,
                                  self.head, self.length, self.length)[0]

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
    import torch                                    # 仅导出时需要
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
            import torch
            from infer.fcpe import FCPEInfer
            self._fc = FCPEInfer(torch.device("cpu"))

    def _post(self, f0_np):
        f0 = np.asarray(f0_np, dtype=np.float32)
        mel = 1127 * np.log(1 + f0 / 700)
        pos = mel > 0
        mel[pos] = (mel[pos] - self.f0_mel_min) * 254 / (self.f0_mel_max - self.f0_mel_min) + 1
        mel[mel <= 1] = 1
        mel[mel > 255] = 255
        return np.round(mel).astype(np.int64), f0

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
            import torch
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
        log("EP 实际生效:", self.gen.get_providers())
        self.pitch = Pitch(f0method, up_key)
        self.buf = np.zeros(self.ctx + self.block, dtype=np.float32)

    def push(self, block16: np.ndarray):
        self.buf = np.roll(self.buf, -len(block16))
        self.buf[-len(block16):] = block16

    def run(self):
        t0 = time.perf_counter()
        feats = self.hub.run(None, {"input_values": self.buf[None].astype(np.float32)})[0]
        feats = np.concatenate([feats, feats[:, -1:, :]], axis=1)
        # 特征 x2 上采样：等价于 RVC 的 F.interpolate(scale_factor=2, mode='nearest')，纯 numpy 免 torch
        phone = np.repeat(feats, 2, axis=1)[:, :self.P, :].astype(np.float32)
        if phone.shape[1] < self.P:
            pad = np.zeros((1, self.P - phone.shape[1], 768), dtype=np.float32)
            phone = np.concatenate([phone, pad], axis=1)
        win = self.buf[-(self.block + 800):]
        coarse, pitchf = self.pitch(win)
        if coarse.shape[0] < self.P:
            k = self.P - coarse.shape[0]
            coarse = np.concatenate([np.repeat(coarse[:1], k), coarse])
            pitchf = np.concatenate([np.repeat(pitchf[:1], k), pitchf])
        coarse = coarse[-self.P:][None].astype(np.int64)
        pitchf = pitchf[-self.P:][None].astype(np.float32)
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


def realtime(eng, in_spec, out_spec, in_sr, out_sr, gain=1.0, dev_block=0.05, prefill=1):
    """环形缓冲 + 小设备块长：设备回调只做拷贝，推理与音频彻底解耦，避免卡顿。"""
    import threading
    import sounddevice as sd

    # Windows: WASAPI 依赖线程级 COM 初始化；工作线程里开流前必须 CoInitializeEx。
    coinit = None
    if sys.platform == "win32":
        try:
            import ctypes
            coinit = ctypes.windll.ole32.CoInitializeEx(None, 0x2)
        except Exception:
            coinit = None

    in_dev, in_default_sr = resolve_dev(in_spec, True)
    out_dev, out_default_sr = resolve_dev(out_spec, False)
    in_sr = int(in_sr) or in_default_sr or SR_IN
    out_sr = int(out_sr) or out_default_sr or SR_OUT
    block_in = int(round(eng.block * in_sr / SR_IN))
    block_out = int(round(eng.block * out_sr / SR_IN))
    dev_in, dev_out = max(256, int(in_sr * dev_block)), max(256, int(out_sr * dev_block))
    log("设备: in=%s(%sHz%s)  out=%s(%sHz%s)  回调块=%.0fms" %
        (in_spec or "默认", in_sr, "" if in_sr == SR_IN else " -> 内部16k",
         out_spec or "默认", out_sr, "" if out_sr == SR_OUT else " <- 模型48k", dev_block * 1000))

    lk = threading.Lock()
    in_ring = np.zeros(max(block_in * 6, in_sr), dtype=np.float32)
    out_ring = np.zeros(max(block_out * 8, out_sr), dtype=np.float32)
    in_len, out_len = len(in_ring), len(out_ring)
    st = {"ir": 0, "iw": 0, "or": 0, "ow": 0}
    xr = {"in": 0, "out": 0}
    primed = [False]

    def in_cb(indata, frames, t, status):
        x = indata[:, 0]
        with lk:
            if st["iw"] - st["ir"] + frames > in_len:
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
            if not primed[0]:
                if avail < block_out * prefill:
                    outdata.fill(0.0)
                    return
                primed[0] = True
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

    ist = sd.InputStream(device=in_dev, samplerate=in_sr, channels=1, blocksize=dev_in,
                         dtype="float32", latency="low", callback=in_cb)
    ost = sd.OutputStream(device=out_dev, samplerate=out_sr, channels=1, blocksize=dev_out,
                          dtype="float32", latency="low", callback=out_cb)
    ist.start()
    ost.start()
    log("已开启，说话吧（Ctrl+C 退出）")
    times, done = [], 0
    try:
        while True:
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
                    raw, queued = None, 0
            if raw is None:
                time.sleep(0.004)
                continue
            x16 = _resample(raw, in_sr, SR_IN) if in_sr != SR_IN else raw
            if len(x16) < eng.block:
                x16 = np.pad(x16, (0, eng.block - len(x16)))
            eng.push(x16[:eng.block])
            a, ms = eng.run()
            if out_sr != SR_OUT:
                a = _resample(a, SR_OUT, out_sr)
            a = (a * gain).astype(np.float32)
            with lk:
                space = out_len - (st["ow"] - st["or"])
                if len(a) > space:
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
            times.append(ms)
            if len(times) > 60:
                times.pop(0)
            done += 1
            if done % 20 == 0:
                log("  推理平均 %.0f ms/块  输出延迟 %.0fms  欠载 in/out %d/%d" %
                    (sum(times) / len(times), queued / out_sr * 1000, xr["in"], xr["out"]))
    except KeyboardInterrupt:
        pass
    finally:
        ist.stop(); ost.stop()
        if sys.platform == "win32" and coinit is not None:
            try:
                import ctypes
                ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass
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
    ap.add_argument("--export-models", action="store_true",
                    help="只导出该 block/ctx 对应的 ONNX 后退出（需 torch+onnx，用非 CUDA venv 跑）")
    ap.add_argument("--dev-block", type=float, default=0.05,
                    help="设备回调块长(秒)，音频卡顿就调大(0.08~0.15)")
    ap.add_argument("--prefill", type=int, default=1, help="输出预铺块数，抗抖动")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--selftest-wav", default=r"D:\Hanako-workspace\_rec3_t.wav")
    a = ap.parse_args()

    if a.list_devices:
        list_devices()
        return

    if a.export_models:
        head = int(SR_IN * a.ctx) // 160
        length = int(SR_IN * a.block) // 160
        ensure_gen_onnx(head, length, "_rtgen_c%d_b%d.onnx" % (head, length))
        ensure_hubert_onnx()
        log("导出完成：head=%d length=%d" % (head, length))
        return

    eng = Engine(a.ep, a.block, a.ctx, a.f0, a.threads)
    if a.selftest:
        import librosa
        wav, _ = librosa.load(a.selftest_wav, sr=SR_IN, mono=True)
        selftest(eng, wav.astype(np.float32))
    else:
        realtime(eng, a.in_spec, a.out_spec, a.in_sr, a.out_sr, a.gain, a.dev_block, a.prefill)


if __name__ == "__main__":
    main()
