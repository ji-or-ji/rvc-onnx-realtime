import os, sys, time
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
import torch
import numpy as np

from configs.config import Config
cfg = Config()
DML = cfg.device
print("cfg device =", DML, flush=True)

from infer.rtrvc import get_synthesizer

PTH = "assets/weights/julesbrown.pth"
# 48k 模型：p_len 帧 ≈ p_len*160/16000 秒音频，输出 p_len*2*upp? 直接按帧计
CASES = [6, 25, 50]


def bench(device, tag):
    t0 = time.perf_counter()
    net_g, cpt = get_synthesizer(PTH, device)
    load = time.perf_counter() - t0
    net_g.eval()
    print(f"[{tag}] load={load:.2f}s  tgt_sr={cpt['config'][-1]}", flush=True)
    for p_len in CASES:
        feats = torch.randn(1, p_len, 768, device=device)
        lengths = torch.LongTensor([p_len]).to(device)
        pitch = torch.zeros(1, p_len, dtype=torch.long, device=device)
        pitchf = torch.zeros(1, p_len, dtype=torch.float32, device=device)
        sid = torch.LongTensor([0]).to(device)
        for i in range(3):
            t = time.perf_counter()
            with torch.no_grad():
                net_g.infer(feats, lengths, pitch, pitchf, sid, 0, p_len, p_len)
            dt = (time.perf_counter() - t) * 1000
            if i == 2:
                secs = p_len * 160 / 16000
                print(f"[{tag}] p_len={p_len} ({secs:.2f}s audio): {dt:.0f} ms", flush=True)


bench(DML, "iGPU-DML")
bench(torch.device("cpu"), "CPU")
