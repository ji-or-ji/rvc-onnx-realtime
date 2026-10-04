import os, sys, time
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
import numpy as np
import torch
import onnxruntime as ort

from infer.rtrvc import get_synthesizer

PTH = "assets/weights/julesbrown.pth"
print("loading net_g on CPU ...", flush=True)
net_g, cpt = get_synthesizer(PTH, torch.device("cpu"))
net_g.eval()


class Wrap(torch.nn.Module):
    def __init__(self, net, P):
        super().__init__()
        self.net = net
        self.P = P

    def forward(self, phone, lengths, coarse, continuous, speaker):
        return self.net.infer(phone, lengths, coarse, continuous, speaker, 0, self.P, self.P)[0]


for P in (25, 50):
    w = Wrap(net_g, P).eval()
    phone = torch.randn(1, P, 768)
    lengths = torch.LongTensor([P])
    coarse = torch.zeros(1, P, dtype=torch.long)
    continuous = torch.zeros(1, P, dtype=torch.float32)
    speaker = torch.LongTensor([0])
    fp = "_rvcgen_p%d.onnx" % P
    try:
        with torch.no_grad():
            torch.onnx.export(
                w, (phone, lengths, coarse, continuous, speaker), fp,
                input_names=["phone", "lengths", "coarse", "continuous", "speaker"],
                output_names=["audio"], opset_version=17,
            )
        print("exported", fp, os.path.getsize(fp) // 1024, "KB", flush=True)
    except Exception as e:
        import traceback; traceback.print_exc()
        print("export failed", P, type(e).__name__, str(e)[:300]); continue

    feeds = {
        "phone": phone.numpy(), "lengths": lengths.numpy(),
        "coarse": coarse.numpy(), "continuous": continuous.numpy(),
        "speaker": speaker.numpy(),
    }
    for prov in (["CPUExecutionProvider"], ["DmlExecutionProvider"]):
        try:
            so = ort.SessionOptions()
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            sess = ort.InferenceSession(fp, so, providers=prov)
            for i in range(5):
                t = time.perf_counter()
                out = sess.run(None, feeds)
                dt = (time.perf_counter() - t) * 1000.0
            print("  P=%d %-22s %.0f ms  (out samples=%d)" % (P, prov[0], dt, out[0].shape[-1]), flush=True)
        except Exception as e:
            print("  P=%d %-22s FAIL %s %s" % (P, prov[0], type(e).__name__, str(e)[:180]), flush=True)
