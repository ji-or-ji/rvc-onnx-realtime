import os, sys, time
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

import numpy as np
import torch

F0 = "fcpe"
EXTRA = 2.0
DEV = ""
if len(sys.argv) > 1 and sys.argv[1]:
    F0 = sys.argv[1]
if len(sys.argv) > 2 and sys.argv[2]:
    EXTRA = float(sys.argv[2])
if len(sys.argv) > 3:
    DEV = sys.argv[3]
sys.argv = [sys.argv[0]]

from configs.config import Config
cfg = Config()

if DEV == "cpu":
    cfg.device = torch.device("cpu")
    cfg.is_half = False
    print("OVERRIDE -> CPU", flush=True)

print("DEVICE =", cfg.device, "| half =", cfg.is_half, "| extra =", EXTRA, flush=True)

from infer.rtrvc import RVC

t0 = time.perf_counter()
rvc = RVC(12, 0.0, "assets/weights/julesbrown.pth", "assets/indices/julesbrown.index", 0.28, cfg)
print("model_load_sec = %.2f" % (time.perf_counter() - t0), flush=True)

import librosa
data, sr = librosa.load(r"D:\Hanako-workspace\_rec3_t.wav", sr=16000, mono=True)
N = int(16000 * (EXTRA + 0.06 + 0.04))
buf = np.zeros(N, dtype=np.float32)
m = min(N, len(data))
buf[N - m:] = data[:m]
wav = torch.from_numpy(buf).to(cfg.device)

block = 960
times = []
for i in range(12):
    t = time.perf_counter()
    rvc.infer(wav, block, 0, block, F0)
    dt = (time.perf_counter() - t) * 1000.0
    times.append(dt)
    print("%2d  %8.1f ms" % (i, dt), flush=True)

warm = times[3:]
if warm:
    print("AVG=%.1f ms  (buffer %.2fs)" % (sum(warm) / len(warm), N / 16000), flush=True)
