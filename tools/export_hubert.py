import os, sys, time
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
import numpy as np
import torch
import onnxruntime as ort

from infer.hubert import load_hubert_model

print("loading hubert on CPU ...", flush=True)
hm = load_hubert_model(torch.device("cpu"), False)
hm.eval()


class Wrap(torch.nn.Module):
    """RVC v2 用最后一层 hidden state 作为特征。"""

    def __init__(self, m):
        super().__init__()
        self.m = m

    def forward(self, input_values):
        out = self.m(input_values=input_values, attention_mask=None,
                     output_hidden_states=False, return_dict=True)
        return out.last_hidden_state


w = Wrap(hm).eval()
dummy = torch.randn(1, 16000)
fp = "_hubert.onnx"
try:
    with torch.no_grad():
        torch.onnx.export(
            w, (dummy,), fp,
            input_names=["input_values"], output_names=["features"],
            opset_version=17,
            dynamic_axes={"input_values": {1: "T"}, "features": {1: "T"}},
        )
    print("exported", fp, os.path.getsize(fp) // 1024 // 1024, "MB", flush=True)
except Exception:
    import traceback; traceback.print_exc(); sys.exit(1)

SR = 16000
for secs in (0.06, 0.25, 0.5, 1.0, 2.0):
    n = int(SR * secs)
    x = np.random.randn(1, n).astype(np.float32)
    line = "  %.2fs" % secs
    for prov in (["CPUExecutionProvider"], ["DmlExecutionProvider"]):
        try:
            so = ort.SessionOptions()
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            sess = ort.InferenceSession(fp, so, providers=prov)
            for i in range(4):
                t = time.perf_counter()
                sess.run(None, {"input_values": x})
                dt = (time.perf_counter() - t) * 1000.0
            line += "  %s=%.0fms" % (prov[0].replace("ExecutionProvider", ""), dt)
        except Exception as e:
            line += "  %s=FAIL(%s)" % (prov[0], str(e)[:60])
    print(line, flush=True)
