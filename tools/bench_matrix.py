import os, time
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import onnxruntime as ort

PROVS = [("CPU", ["CPUExecutionProvider"]),
         ("DML0(2060)", [("DmlExecutionProvider", {"device_id": 0})]),
         ("DML1(iGPU)", [("DmlExecutionProvider", {"device_id": 1})])]


def bench(fp, feeds, tag, runs=6):
    out = []
    for name, prov in PROVS:
        try:
            so = ort.SessionOptions()
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            sess = ort.InferenceSession(fp, so, providers=prov)
            for i in range(runs):
                t = time.perf_counter()
                sess.run(None, feeds)
                dt = (time.perf_counter() - t) * 1000.0
            out.append("%s=%.0fms" % (name, dt))
        except Exception as e:
            out.append("%s=FAIL(%s)" % (name, str(e)[:40]))
    print("%-22s %s" % (tag, "  ".join(out)), flush=True)


print("=== generator ===", flush=True)
for P in (25, 50, 100):
    if not os.path.exists("_rvcgen_p%d.onnx" % P):
        continue
    feeds = {
        "phone": np.random.randn(1, P, 768).astype(np.float32),
        "lengths": np.array([P], dtype=np.int64),
        "coarse": np.zeros((1, P), dtype=np.int64),
        "continuous": np.zeros((1, P), dtype=np.float32),
        "speaker": np.array([0], dtype=np.int64),
    }
    bench("_rvcgen_p%d.onnx" % P, feeds, "gen P=%d (%.2fs)" % (P, P * 160 / 16000))

print("=== hubert ===", flush=True)
for secs in (0.5, 1.0, 2.0):
    n = int(16000 * secs)
    bench("_hubert.onnx", {"input_values": np.random.randn(1, n).astype(np.float32)},
          "hubert %.1fs" % secs)
