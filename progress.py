"""准备期进度页：bootstrap 干活时，浏览器里能实时看到进度。

设计：
  * 纯标准库（依赖还没装好，它必须先能用）
  * 先占住目标端口，把进度页顶上去；准备完成后让位给 web_ui
  * 页面轮询 /api/progress；看到 done 就自动重载（那时 web_ui 已经接管）
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RVC 实时变声 · 准备中</title>
<style>
:root{--bg:#0e1015;--panel:#161922;--line:#272c38;--text:#e8eaf0;--dim:#8b93a7;
--accent:#6c8cff;--ok:#39d0a0;--warn:#f0b34a}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:radial-gradient(1200px 600px at 15% -10%,#1b2233,var(--bg) 55%);
color:var(--text);font:14px/1.6 "Segoe UI",system-ui,"Noto Sans SC",sans-serif;padding:28px}
.wrap{max-width:760px;margin:0 auto}
h1{font-size:17px;margin:0 0 4px;font-weight:650}
.sub{color:var(--dim);font-size:12.5px;margin-bottom:22px}
.card{background:linear-gradient(180deg,var(--panel),#14171f);border:1px solid var(--line);
border-radius:14px;padding:20px;margin-bottom:14px}
.step{font-size:15px;font-weight:600;margin-bottom:10px}
.bar{height:8px;border-radius:6px;background:#0f1219;border:1px solid var(--line);overflow:hidden}
.bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,#4356c9,#6c8cff);
transition:width .25s ease}
.pct{float:right;color:var(--dim);font-variant-numeric:tabular-nums;font-size:12.5px}
.msg{color:var(--dim);font-size:12.5px;margin-top:10px;min-height:19px}
pre{background:#0f1219;border:1px solid var(--line);border-radius:10px;padding:12px;
max-height:260px;overflow:auto;font:12px/1.55 ui-monospace,Consolas,monospace;color:#9aa6c4;margin:0}
.ok{color:var(--ok)}
footer{color:var(--dim);font-size:12px;text-align:center;margin-top:16px}
</style></head><body><div class="wrap">
<h1>RVC 实时变声</h1>
<div class="sub">正在准备运行环境，完成后会自动进入控制台。首次运行需要下载依赖，请耐心等待。</div>
<div class="card">
  <div class="step"><span id="step">初始化…</span><span class="pct" id="pct"></span></div>
  <div class="bar"><i id="bar"></i></div>
  <div class="msg" id="msg"></div>
</div>
<pre id="log">等待日志…</pre>
<footer>若长时间无进展，请看命令行窗口的输出</footer>
</div>
<script>
async function tick(){
  try{
    const r = await fetch('/api/progress',{cache:'no-store'});
    const d = await r.json();
    document.getElementById('step').textContent = d.step || '准备中';
    document.getElementById('bar').style.width = (d.pct||0)+'%';
    document.getElementById('pct').textContent = (d.pct? d.pct.toFixed(0)+'%' : '');
    document.getElementById('msg').textContent = d.msg || '';
    const lg = document.getElementById('log');
    lg.textContent = (d.logs||[]).join('\\n') || '等待日志…';
    lg.scrollTop = lg.scrollHeight;
    if(d.done){ setTimeout(()=>location.reload(), 1200); return; }
  }catch(e){ /* 服务正在交接，稍后再试 */ }
  setTimeout(tick, 600);
}
tick();
</script></body></html>
"""


class Progress:
    def __init__(self):
        self.lock = threading.Lock()
        self.step = "初始化"
        self.pct = 0.0
        self.msg = ""
        self.logs = []
        self.done = False
        self._srv = None
        self._thr = None
        self.port = None

    # ---------- 对外 ----------
    def set(self, step=None, pct=None, msg=None):
        with self.lock:
            if step is not None:
                self.step = step
            if pct is not None:
                self.pct = max(0.0, min(100.0, float(pct)))
            if msg is not None:
                self.msg = msg

    def log(self, line):
        line = time.strftime("%H:%M:%S ") + str(line)
        with self.lock:
            self.logs.append(line)
            if len(self.logs) > 400:
                del self.logs[:-400]
        print("[准备] " + str(line), flush=True)

    def finish(self, msg="准备完成，正在进入控制台…"):
        with self.lock:
            self.done = True
            self.pct = 100.0
            self.step = "完成"
            self.msg = msg

    def snapshot(self):
        with self.lock:
            return {"step": self.step, "pct": self.pct, "msg": self.msg,
                    "logs": list(self.logs[-200:]), "done": self.done}

    # ---------- 服务 ----------
    def start(self, port):
        me = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/api/progress"):
                    body = json.dumps(me.snapshot(), ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                else:
                    body = PAGE.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except Exception:
                    pass

            def log_message(self, *a):
                pass

        try:
            self._srv = ThreadingHTTPServer(("127.0.0.1", port), H)
        except OSError as e:
            print("[准备] 进度页无法占用端口 %d（%s），继续安装但不提供网页进度" % (port, e), flush=True)
            return False
        self.port = port
        self._thr = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thr.start()
        return True

    def stop(self):
        if self._srv:
            try:
                self._srv.shutdown()
                self._srv.server_close()
            except Exception:
                pass
            self._srv = None
        time.sleep(0.3)      # 让端口彻底释放，好交给 web_ui
