"""开箱即用引导：探测 → 判定 → 建环境 → 装依赖 → 取模型 → 起服务。

用法：
  python bootstrap.py                     # 一键（幂等，可重复跑）
  python bootstrap.py --dry-run           # 只体检 + 打印计划，不改动任何东西
  python bootstrap.py --recipe cpu-ort     # 指定配方
  python bootstrap.py --env D:\\x\\venv    # 指定复用某个已有环境
  python bootstrap.py --pip-index https://pypi.tuna.tsinghua.edu.cn/simple
  python bootstrap.py --no-serve          # 装好就停，不起服务

设计要点：
  * 幂等：每步先做健康检查，能用就跳过；状态记在 runtime/bootstrap_state.json
  * 不预制：依赖按体检结果成套下载（版本配对，绝不装 latest）
  * 可回滚：每步独立，失败时重跑不会把已完成的推倒
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
IS_WIN = os.name == "nt"
STATE_DIR = os.path.join(ROOT, "runtime")
STATE_FILE = os.path.join(STATE_DIR, "bootstrap_state.json")
CFG = os.path.join(ROOT, "configs", "config.json")

# ---------------------------------------------------------------- 依赖档位
# 注意：这里必须"成套配对"，不能装 latest（见 docs/VISION.md 的血泪规则）
CORE = ["numpy<2", "scipy", "sounddevice", "librosa", "praat-parselmouth",
        "fastapi", "uvicorn", "websockets"]
PROFILES = {
    "torch-cuda": {
        "check": "torch",
        "verify": "import torch;assert torch.cuda.is_available(),'CUDA 不可用'",
        "pytorch_first": ["torch", "torchaudio"],      # 从 cu 专属 index 装
        "pkgs": CORE + ["faiss-cpu", "PyYAML", "tqdm"],
        "desc": "torch + CUDA（约 3GB）",
    },
    "ort-dml": {
        "check": "onnxruntime",
        "verify": "import onnxruntime as o;assert 'DmlExecutionProvider' in o.get_available_providers()",
        "pkgs": CORE + ["onnxruntime-directml"],
        "desc": "onnxruntime-directml（约 300MB）",
    },
    "ort-cpu": {
        "check": "onnxruntime",
        "verify": "import onnxruntime",
        "pkgs": CORE + ["onnxruntime"],
        "desc": "onnxruntime CPU（约 200MB）",
    },
}
# 高档后端 → 用哪份配方
BACKEND_RECIPE = {"torch-cuda": "torch-cuda", "ort-dml": "dml-ort", "ort-cpu": "cpu-ort"}


def log(msg):
    print("[bootstrap] " + msg, flush=True)


def run(cmd, cwd=None, quiet=False, timeout=None):
    if not quiet:
        log("$ " + " ".join(str(c) for c in cmd))
    p = subprocess.run(cmd, cwd=cwd, timeout=timeout, text=True,
                       encoding="utf-8", errors="ignore",
                       stdout=None if not quiet else subprocess.PIPE,
                       stderr=subprocess.STDOUT)
    return p.returncode


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Boot:
    def __init__(self, args):
        self.a = args
        self.state = self._load_state()
        self.env = args.env or self.state.get("env")
        self.recipe = None

    # ---------------- 状态 ----------------
    def _load_state(self):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_state(self, **kw):
        self.state.update(kw)
        self.state["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if self.a.dry_run:
            return
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    # ---------------- 1. 探测 ----------------
    def probe(self):
        import env_probe
        info = env_probe.collect(disk_path=ROOT)
        v = info["verdict"]
        log("体检：%s（%s）" % (info["os"]["system"], info["os"]["arch"]))
        for g in info["gpus"]:
            log("  GPU: %s %s%s" % (g["vendor"], g["name"],
                                    (" %sMB" % g["vram_mb"]) if g.get("vram_mb") else ""))
        log("  判定：tier=%s backend=%s%s" % (v["tier"], v["backend"],
                                          (" torch_index=%s" % v["torch_index"]) if v.get("torch_index") else ""))
        for r in v["reasons"]:
            log("  理由：" + r)
        for w in v["warnings"]:
            log("  ⚠ " + w)
        self.save_state(probe=info)
        return info

    # ---------------- 2. 选配方 ----------------
    def load_recipe(self, rid):
        path = os.path.join(ROOT, "recipes", rid + ".json")
        if not os.path.exists(path):
            raise SystemExit("找不到配方：%s" % path)
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def decide(self, info):
        if self.a.recipe:
            r = self.load_recipe(self.a.recipe)
            log("按指定配方：%s（%s）" % (r["id"], r["label"]))
        else:
            rid = BACKEND_RECIPE[info["verdict"]["backend"]]
            r = self.load_recipe(rid)
            log("自动匹配配方：%s（%s）" % (r["id"], r["label"]))
        self.recipe = r
        self.save_state(recipe=r["id"], backend=r["backend"])
        return r

    def plan(self, info, recipe):
        v = info["verdict"]
        need = v["disk_need_gb"]
        free = (info.get("disk") or {}).get("free_gb")
        log("—" * 46)
        log("计划：")
        log("  配方      %s" % recipe["label"])
        log("  后端      %s" % recipe["backend"])
        log("  依赖      %s" % PROFILES[recipe["deps"]["profile"]]["desc"])
        log("  环境      %s" % (self.env or os.path.join(ROOT, "runtime", "venv")))
        log("  磁盘      需要约 %.1fGB，可用 %sGB" % (need, free))
        log("  服务      http://127.0.0.1:%d" % self.a.port)
        log("—" * 46)
        if free is not None and free < need:
            log("⚠ 磁盘可能不足（需 %.1fGB，余 %sGB）" % (need, free))

    # ---------------- 3. 环境 ----------------
    def env_py(self, env=None):
        env = env or self.env
        return os.path.join(env, "Scripts" if IS_WIN else "bin",
                            "python.exe" if IS_WIN else "python")

    def env_health(self, env, profile):
        """环境真的能用吗（不是看状态文件，是实跑）。"""
        py = self.env_py(env)
        if not os.path.exists(py):
            return False, "没有 python"
        spec = PROFILES[profile]
        code = "import %s;%s" % (spec["check"], spec.get("verify", "pass"))
        try:
            p = subprocess.run([py, "-c", code], capture_output=True, text=True,
                               encoding="utf-8", errors="ignore", timeout=180)
            if p.returncode == 0:
                return True, "ok"
            return False, (p.stderr or "").strip().splitlines()[-1:] and \
                (p.stderr or "").strip().splitlines()[-1] or "检查失败"
        except Exception as e:
            return False, str(e)

    def candidates(self):
        return [self.a.env] if self.a.env else [
            os.path.join(ROOT, "runtime", "venv"),
            os.path.join(ROOT, "venv"),
            os.path.join(ROOT, "venv-dml"),
        ]

    def ensure_env(self, recipe):
        profile = recipe["deps"]["profile"]
        for c in self.candidates():
            if c and os.path.exists(self.env_py(c)):
                ok, why = self.env_health(c, profile)
                if ok:
                    self.env = c
                    log("复用已有环境：%s" % c)
                    self.save_state(env=c)
                    return c
                log("跳过 %s（%s）" % (c, why))
        self.env = self.a.env or os.path.join(ROOT, "runtime", "venv")
        if self.a.dry_run:
            log("（dry-run）将创建环境：%s" % self.env)
            return self.env
        log("创建环境：%s" % self.env)
        os.makedirs(os.path.dirname(self.env), exist_ok=True)
        run([sys.executable, "-m", "venv", self.env])
        run([self.env_py(), "-m", "pip", "install", "-q", "--upgrade", "pip"])
        self.save_state(env=self.env)
        return self.env

    # ---------------- 4. 依赖 ----------------
    def pip(self, args, index_url=None, extra_index=None):
        cmd = [self.env_py(), "-m", "pip", "install", "--disable-pip-version-check"] + args
        if index_url:
            cmd += ["--index-url", index_url]
        if extra_index:
            cmd += ["--extra-index-url", extra_index]
        return run(cmd)

    def ensure_deps(self, recipe, info):
        profile = recipe["deps"]["profile"]
        spec = PROFILES[profile]
        ok, why = self.env_health(self.env, profile)
        if ok:
            # 能用就不装：防止把已有的 cu118 torch 换成 cu128，白下 2.5GB
            log("依赖已满足（%s），跳过安装" % spec["desc"])
            self.save_state(deps=profile)
            return
        if self.a.dry_run:
            log("（dry-run）将安装：%s" % spec["desc"])
            return
        mirror = self.a.pip_index
        if profile == "torch-cuda":
            ti = (info["verdict"].get("torch_index") or "cu121")
            log("先装 torch/torchaudio（%s，专属源）" % ti)
            self.pip(spec["pytorch_first"],
                     index_url="https://download.pytorch.org/whl/%s" % ti)
        log("安装其余依赖%s" % ("（镜像：%s）" % mirror if mirror else ""))
        self.pip(spec["pkgs"], index_url=mirror)
        ok, why = self.env_health(self.env, profile)
        if not ok:
            log("✗ 依赖校验失败：%s" % why)
            raise SystemExit(1)
        self.save_state(deps=profile)

    # ---------------- 5. 模型 ----------------
    def fetch_one(self, kind, ref):
        if not ref or not ref.get("file"):
            return
        dest = os.path.join(ROOT, ref["file"])
        if os.path.exists(dest):
            if ref.get("sha256") and sha256_file(dest) != ref["sha256"]:
                log("⚠ %s 校验不符：%s" % (kind, dest))
            else:
                log("%s 已就位：%s" % (kind, ref["file"]))
            return
        if not ref.get("url"):
            log("⚠ %s 缺失且未提供下载链接，请手动放到 %s" % (kind, ref["file"]))
            return
        if self.a.dry_run:
            log("（dry-run）将下载 %s -> %s" % (ref["url"], ref["file"]))
            return
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        log("下载 %s：%s" % (kind, ref["url"]))
        urllib.request.urlretrieve(ref["url"], dest)
        if ref.get("sha256") and sha256_file(dest) != ref["sha256"]:
            raise SystemExit("%s 校验失败，已中止" % kind)
        log("%s 完成" % kind)

    def ensure_models(self, recipe):
        for kind in ("model", "index", "f0_model"):
            self.fetch_one(kind, recipe.get(kind))

    # ---------------- 6. 参数 & 服务 ----------------
    def apply_params(self, recipe):
        """把配方参数写进 configs/config.json（保留原有未知字段）。"""
        try:
            with open(CFG, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
        cfg.update(recipe.get("params") or {})
        if self.a.dry_run:
            log("（dry-run）将写入参数：%s" % json.dumps(recipe.get("params"), ensure_ascii=False))
            return
        os.makedirs(os.path.dirname(CFG), exist_ok=True)
        with open(CFG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        log("参数已写入 configs/config.json")

    def serve(self, recipe):
        if self.a.no_serve:
            log("已按要求停在起服务之前。")
            return
        py = self.env_py()
        url = "http://127.0.0.1:%d" % self.a.port
        if self.a.dry_run:
            log("（dry-run）将启动：%s web_ui.py --port %d" % (py, self.a.port))
            return
        log("启动服务：%s" % url)
        subprocess.Popen([py, "web_ui.py", "--port", str(self.a.port)], cwd=ROOT)
        log("浏览器打开 %s 即可。听不见声音请检查设备选择。" % url)
        self.save_state(served=time.strftime("%Y-%m-%d %H:%M:%S"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只体检 + 打印计划")
    ap.add_argument("--recipe", default="", help="指定配方 id")
    ap.add_argument("--env", default="", help="复用/指定环境目录")
    ap.add_argument("--pip-index", default="", help="pip 镜像（国内建议清华源）")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--no-serve", action="store_true")
    a = ap.parse_args()

    log("项目目录：%s" % ROOT)
    boot = Boot(a)
    info = boot.probe()
    recipe = boot.decide(info)
    boot.plan(info, recipe)
    boot.ensure_env(recipe)
    boot.ensure_deps(recipe, info)
    boot.ensure_models(recipe)
    boot.apply_params(recipe)
    boot.serve(recipe)


if __name__ == "__main__":
    main()
