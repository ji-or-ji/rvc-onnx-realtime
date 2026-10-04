"""给原版 realtime_gui 装一只遥控手（不改它的音频/推理）。

原理：所有改动都通过 FreeSimpleGUI 的 write_event_value 交回 Tk 主循环，
再由原版自己的 set_values() 应用 —— 等价于用户自己拖了滑杆。
这样音频路径、参数语义、坑的规避方式全部保持原样。

用法（由 realtime_gui.py 启动后自动调用）：
    import control_server; control_server.start(gui)
接口：
    GET  /              简易控制面板
    GET  /api/state     当前参数
    GET  /api/devices   设备清单
    POST /api/apply     {"pitch": 8, ...} 应用并（重新）开始
    POST /api/stop      停止
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GUI = None
PORT = 8898

FIELDS = ["pth_path", "index_path", "sg_hostapi", "sg_wasapi_exclusive",
          "sg_input_device", "sg_output_device", "sr_type", "threhold",
          "pitch", "formant", "block_time", "crossfade_length", "extra_time",
          "I_noise_reduce", "O_noise_reduce", "rms_mix_rate", "index_rate",
          "f0method"]

PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RVC 遥控面板</title><style>
:root{--bg:#0e1015;--p:#161922;--l:#272c38;--t:#e8eaf0;--d:#8b93a7;--a:#6c8cff;--ok:#39d0a0}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(1200px 600px at 15% -10%,#1b2233,var(--bg) 55%);
color:var(--t);font:14px/1.5 "Segoe UI",system-ui,"Noto Sans SC",sans-serif;padding:22px}
.w{max-width:860px;margin:0 auto}h1{font-size:17px;margin:0 0 3px}
.s{color:var(--d);font-size:12.5px;margin-bottom:16px}
.c{background:linear-gradient(180deg,var(--p),#14171f);border:1px solid var(--l);border-radius:14px;padding:16px;margin-bottom:13px}
.c h2{margin:0 0 12px;font-size:12px;letter-spacing:.8px;text-transform:uppercase;color:var(--d)}
.g{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}
label{display:block;font-size:12px;color:var(--d);margin-bottom:4px}
input,select{width:100%;padding:8px 9px;border-radius:9px;background:#0f1219;border:1px solid var(--l);color:var(--t);font:inherit}
button{font:inherit;cursor:pointer;border:1px solid var(--l);background:#1c202b;color:var(--t);padding:9px 15px;border-radius:10px}
button.p{background:linear-gradient(135deg,var(--a),#7d5cff);border-color:transparent;font-weight:600}
button.d{background:#3a1f24;border-color:#5c2b33;color:#ffb3b3}
.row{display:flex;gap:9px;align-items:center;margin-top:12px}
.dot{width:8px;height:8px;border-radius:50%;background:#555}
.dot.on{background:var(--ok)}.m{color:var(--d);font-size:12.5px}
</style></head><body><div class="w">
<h1>RVC 遥控面板</h1><div class="s">引擎是原版 realtime_gui（音频路径未改动），这里只是遥控器。</div>
<div class="c"><h2>设备</h2><div class="g">
<div><label>设备类型</label><select id="sg_hostapi"></select></div>
<div><label>输入</label><select id="sg_input_device"></select></div>
<div><label>输出</label><select id="sg_output_device"></select></div>
</div></div>
<div class="c"><h2>参数</h2><div class="g">
<div><label>响应阈值</label><input id="threhold" type="number" step="1"></div>
<div><label>音调</label><input id="pitch" type="number" step="1"></div>
<div><label>性别因子</label><input id="formant" type="number" step="0.05"></div>
<div><label>检索特征占比</label><input id="index_rate" type="number" step="0.01"></div>
<div><label>响度因子</label><input id="rms_mix_rate" type="number" step="0.01"></div>
<div><label>采样长度</label><input id="block_time" type="number" step="0.01"></div>
<div><label>淡入淡出</label><input id="crossfade_length" type="number" step="0.01"></div>
<div><label>额外推理时长</label><input id="extra_time" type="number" step="0.5"></div>
<div><label>f0 算法</label><select id="f0method"><option>rmvpe</option><option>pm</option><option>fcpe</option></select></div>
</div>
<div class="row"><button class="p" id="apply">应用并开始</button>
<button class="d" id="stop">停止</button>
<span class="m"><span class="dot" id="dot"></span> <span id="st">读取中…</span></span></div>
</div></div>
<script>
const $=id=>document.getElementById(id);
const NUM=["threhold","pitch","formant","index_rate","rms_mix_rate","block_time","crossfade_length","extra_time"];
async function load(){
  const s=await (await fetch('/api/state')).json();
  NUM.forEach(k=>{ if(s[k]!==null&&s[k]!==undefined) $(k).value=s[k]; });
  if(s.f0method) $("f0method").value=s.f0method;
  const dv=await (await fetch('/api/devices')).json();
  const apis=[...new Set(dv.devices.map(d=>d.api))];
  $("sg_hostapi").innerHTML=apis.map(a=>`<option>${a}</option>`).join("");
  $("sg_hostapi").value=s.sg_hostapi||apis[0];
  fill("sg_input_device",dv.devices.filter(d=>d.io.includes("in")),s.sg_input_device);
  fill("sg_output_device",dv.devices.filter(d=>d.io.includes("out")),s.sg_output_device);
  const on=!!s.streaming; $("dot").className="dot"+(on?" on":"");
  $("st").textContent=on?"运行中":"已停止";
}
function fill(id,list,cur){
  const sel=$(id); sel.innerHTML=list.map(d=>`<option value="${d.name}">${d.name} · ${d.api}</option>`).join("");
  if(cur) sel.value=cur;
}
$("apply").onclick=async()=>{
  const body={sg_hostapi:$("sg_hostapi").value,sg_input_device:$("sg_input_device").value,
    sg_output_device:$("sg_output_device").value,f0method:$("f0method").value};
  NUM.forEach(k=>body[k]=parseFloat($(k).value));
  await fetch('/api/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  $("st").textContent="已下发，稍候…"; setTimeout(load,1500);
};
$("stop").onclick=async()=>{ await fetch('/api/stop',{method:'POST'}); setTimeout(load,800); };
load(); setInterval(load,3000);
</script></body></html>
"""


def _contract():
    """读 configs/config.json 作为基础值（保证 pth/index/hostapi 等不为空）。"""
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configs", "config.json")
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _merged(payload):
    base = _contract()
    base.update(payload or {})
    return base


def state():
    g = GUI.gui_config
    d = {k: getattr(g, k, None) for k in FIELDS}
    # 原版的属性名与契约字段名不完全一致，这里对齐
    d["crossfade_length"] = getattr(g, "crossfade_time", None)
    d["sg_wasapi_exclusive"] = getattr(g, "wasapi_exclusive", None)
    d["streaming"] = getattr(GUI, "stream", None) is not None
    return d


def devices():
    import sounddevice as sd
    out = []
    for i, d in enumerate(sd.query_devices()):
        io = ("in" if d["max_input_channels"] > 0 else "") + \
             ("out" if d["max_output_channels"] > 0 else "")
        out.append({"i": i, "io": io, "name": d["name"],
                    "api": sd.query_hostapis(d["hostapi"])["name"],
                    "sr": int(d["default_samplerate"])})
    return {"devices": out}


class _H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(b)
        except Exception:
            pass

    def do_GET(self):
        try:
            if self.path.startswith("/api/state"):
                self._send(200, json.dumps(state(), ensure_ascii=False))
            elif self.path.startswith("/api/devices"):
                self._send(200, json.dumps(devices(), ensure_ascii=False))
            else:
                self._send(200, PAGE, "text/html; charset=utf-8")
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else "{}"
        try:
            payload = json.loads(raw or "{}")
        except Exception:
            payload = {}
        try:
            if self.path.startswith("/api/apply"):
                GUI.window.write_event_value("-CTRL-APPLY", _merged(payload))   # 线程安全
                self._send(200, json.dumps({"ok": True}))
            elif self.path.startswith("/api/stop"):
                GUI.window.write_event_value("-CTRL-STOP", None)
                self._send(200, json.dumps({"ok": True}))
            else:
                self._send(404, "{}")
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))

    def log_message(self, *a):
        pass


def start(gui, port=PORT, host="127.0.0.1"):
    """在原版进程内启动控制接口（守护线程）。"""
    global GUI, PORT
    GUI, PORT = gui, port
    srv = ThreadingHTTPServer((host, port), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    print("[遥控] 面板已就绪：http://%s:%d" % (host, port), flush=True)
    return srv
