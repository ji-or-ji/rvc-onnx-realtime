import os, sys, time
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
import torch

from configs.config import Config
cfg = Config()
DML = cfg.device
print("cfg device =", DML, flush=True)

from infer.hubert import load_hubert_model, extract_hubert_features
from infer.fcpe import FCPEInfer

SR = 16000
CASES = [0.06, 0.25, 0.50]
VER = "v2"


def bench(device, tag):
    print(f"--- {tag} ---", flush=True)
    t0 = time.perf_counter()
    hm = load_hubert_model(device, False)
    print(f"[{tag}] hubert load={time.perf_counter()-t0:.2f}s", flush=True)
    fc = FCPEInfer(device)
    for secs in CASES:
        n = int(SR * secs)
        wav = torch.randn(n, device=device)
        for i in range(3):
            t = time.perf_counter()
            with torch.no_grad():
                extract_hubert_features(hm, wav.view(1, -1), VER)
            dt = (time.perf_counter() - t) * 1000
            if i == 2:
                print(f"[{tag}] hubert {secs}s: {dt:.0f} ms", flush=True)
        for i in range(3):
            t = time.perf_counter()
            with torch.no_grad():
                fc.infer(wav, SR)
            dt = (time.perf_counter() - t) * 1000
            if i == 2:
                print(f"[{tag}] fcpe   {secs}s: {dt:.0f} ms", flush=True)


bench(DML, "iGPU-DML")
bench(torch.device("cpu"), "CPU")
