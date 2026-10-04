"""环境探测：零依赖、只读、输出 JSON。

用法：
  python env_probe.py            # 打印 JSON
  python env_probe.py --pretty   # 带缩进
  python env_probe.py --out info.json

设计约束：
  * 只用标准库（依赖装好之前也要能跑）
  * 只读，不改系统
  * Windows 优先，Linux/Mac 尽力而为（缺信息就留空，不报错）
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import re
import shutil
import subprocess
import sys

IS_WIN = os.name == "nt"


def _run(cmd, timeout=12):
    """跑命令，拿 stdout；失败返回空串。"""
    try:
        kw = {}
        if IS_WIN:
            kw["creationflags"] = 0x08000000      # CREATE_NO_WINDOW，别弹黑框
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="ignore", **kw)
        return (p.stdout or "").strip()
    except Exception:
        return ""


def _ps(cmd, timeout=15):
    """在 Windows 上跑 PowerShell 片段。"""
    if not IS_WIN:
        return ""
    pre = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", pre + cmd], timeout)


# ---------------- 系统 ----------------
def probe_os():
    return {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "arch": platform.machine(),
        "is_windows": IS_WIN,
    }


def probe_python():
    return {
        "exe": sys.executable,
        "version": platform.python_version(),
        "bits": 64 if sys.maxsize > 2**32 else 32,
        "from_venv": sys.prefix != getattr(sys, "base_prefix", sys.prefix),
    }


def probe_mem():
    """返回总内存 GB（拿不到返回 None）。"""
    try:
        if IS_WIN:
            class M(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = M()
            m.dwLength = ctypes.sizeof(M)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return {"total_gb": round(m.ullTotalPhys / 2**30, 1),
                        "avail_gb": round(m.ullAvailPhys / 2**30, 1)}
        else:
            with open("/proc/meminfo") as f:
                txt = f.read()
            total = int(re.search(r"MemTotal:\s+(\d+)", txt).group(1)) / 2**20
            avail = int(re.search(r"MemAvailable:\s+(\d+)", txt).group(1)) / 2**20
            return {"total_gb": round(total, 1), "avail_gb": round(avail, 1)}
    except Exception:
        pass
    return None


def probe_disk(path):
    try:
        u = shutil.disk_usage(path)
        return {"path": os.path.abspath(path),
                "free_gb": round(u.free / 2**30, 1),
                "total_gb": round(u.total / 2**30, 1)}
    except Exception:
        return None


# ---------------- 显卡 ----------------
def probe_nvidia():
    """nvidia-smi 是最可靠的口径（Windows/Linux 通用）。"""
    if not shutil.which("nvidia-smi"):
        return None
    csv = _run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader,nounits"])
    if not csv:
        return None
    gpus = []
    for line in csv.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            gpus.append({"vendor": "nvidia", "name": parts[0],
                         "vram_mb": int(float(parts[1])) if parts[1].replace(".", "").isdigit() else None,
                         "driver": parts[2], "source": "nvidia-smi"})
    full = _run(["nvidia-smi"])
    m = re.search(r"CUDA(?:\s+UMD)?\s+Version:\s*([\d.]+)", full)
    cuda_max = m.group(1) if m else None
    for g in gpus:
        g["cuda_max"] = cuda_max
    return {"gpus": gpus, "driver_cuda_max": cuda_max}


def probe_gpu_windows():
    """非 NVIDIA 的显卡（AMD/Intel），用 CIM 查。"""
    out = _ps("Get-CimInstance Win32_VideoController | "
              "Select-Object Name,DriverVersion,AdapterRAM | ConvertTo-Json -Compress")
    if not out:
        return []
    try:
        data = json.loads(out)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    gpus = []
    for d in data:
        name = (d.get("Name") or "").strip()
        if not name:
            continue
        if re.search(r"virtual|idd|basic display|remote|mirror", name, re.I):
            continue          # 远程/虚拟显示适配器不算显卡
        low = name.lower()
        vendor = "nvidia" if "nvidia" in low or "geforce" in low or "rtx" in low else \
                 "amd" if "amd" in low or "radeon" in low else \
                 "intel" if "intel" in low or "arc" in low else "other"
        vram = d.get("AdapterRAM")
        gpus.append({"vendor": vendor, "name": name,
                     "driver": d.get("DriverVersion"),
                     "vram_mb": int(vram / 2**20) if isinstance(vram, int) and vram > 0 else None,
                     "source": "cim"})
    return gpus


def probe_gpus():
    nv = probe_nvidia()
    gpus = list(nv["gpus"]) if nv else []
    if IS_WIN:
        for g in probe_gpu_windows():
            # nvidia-smi 已经报过的别再报一遍
            if not (g["vendor"] == "nvidia" and any(x["vendor"] == "nvidia" for x in gpus)):
                gpus.append(g)
    return {"gpus": gpus, "driver_cuda_max": (nv or {}).get("driver_cuda_max")}


# ---------------- 音频 ----------------
def probe_audio():
    """只查设备名，够判断有没有虚拟声卡。"""
    names = []
    if IS_WIN:
        out = _ps("Get-CimInstance Win32_SoundDevice | Select-Object -ExpandProperty Name")
        names = [n.strip() for n in out.splitlines() if n.strip()]
    else:
        out = _run(["sh", "-c", "aplay -l 2>/dev/null | grep -oP '(?<=card ).*' || true"])
        names = [n.strip() for n in out.splitlines() if n.strip()]
    low = " | ".join(names).lower()
    return {
        "devices": names,
        "has_virtual_cable": ("vb-audio" in low) or ("cable" in low and "virtual" in low),
        "has_voicemeeter": "voicemeeter" in low,
    }


# ---------------- 判定 ----------------
def verdict(info):
    gpus = info.get("gpus", [])
    nv = next((g for g in gpus if g["vendor"] == "nvidia"), None)
    cuda_max = info.get("driver_cuda_max")
    reasons, warnings = [], []

    def cuda_tuple(v):
        try:
            return tuple(int(x) for x in str(v).split(".")[:2])
        except Exception:
            return (0, 0)

    torch_index = None
    if nv and (nv.get("vram_mb") or 0) >= 4096 and (not cuda_max or cuda_tuple(cuda_max) >= (11, 8)):
        tier, backend = "high", "torch-cuda"
        v = cuda_tuple(cuda_max) if cuda_max else (12, 1)
        torch_index = ("cu128" if v >= (12, 8) else "cu126" if v >= (12, 6) else
                       "cu124" if v >= (12, 4) else "cu121" if v >= (12, 1) else "cu118")
        reasons.append("检测到 NVIDIA %s（%sMB 显存）+ 驱动 CUDA %s → 高档" %
                       (nv["name"], nv.get("vram_mb"), cuda_max or "未知"))
    elif nv:
        tier, backend = "mid", "ort-dml"
        reasons.append("有 NVIDIA 显卡但不满足高档条件（显存 %sMB / CUDA %s），走 DML" %
                       (nv.get("vram_mb"), cuda_max))
        if cuda_max and cuda_tuple(cuda_max) < (11, 8):
            warnings.append("驱动较旧（CUDA %s），升级驱动可走高档" % cuda_max)
    elif gpus:
        tier, backend = "mid", "ort-dml"
        reasons.append("检测到 %s，走 DirectML" % gpus[0]["name"])
    else:
        tier, backend = "low", "ort-cpu"
        reasons.append("未检测到可用独显，走 CPU（能跑但偏慢）")

    if not info.get("audio", {}).get("has_virtual_cable"):
        warnings.append("未检测到虚拟声卡：要给 OBS 用需装 VB-Audio Virtual Cable")
    mem = info.get("mem") or {}
    if mem and mem.get("total_gb", 99) < 8:
        warnings.append("内存仅 %sGB，高档依赖解压时可能吃紧" % mem.get("total_gb"))
    need = {"high": 6.0, "mid": 1.5, "low": 1.0}[tier]
    disk = info.get("disk")
    if disk and disk["free_gb"] < need:
        warnings.append("系统盘剩余 %sGB，安装大约需要 %sGB" % (disk["free_gb"], need))

    return {"tier": tier, "backend": backend, "torch_index": torch_index,
            "reasons": reasons, "warnings": warnings, "disk_need_gb": need}


def collect(disk_path=None):
    info = {"os": probe_os(), "python": probe_python(), "mem": probe_mem(),
            "disk": probe_disk(disk_path or os.getcwd()),
            "audio": probe_audio()}
    info.update(probe_gpus())
    info["verdict"] = verdict(info)
    return info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pretty", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    info = collect()
    txt = json.dumps(info, ensure_ascii=False, indent=2 if a.pretty else None)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(txt)
    print(txt)


if __name__ == "__main__":
    main()
