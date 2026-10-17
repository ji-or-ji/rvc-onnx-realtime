"""RVC Fabric（图灵镜）适配器。

接管方式：它自己的「壳 ↔ 引擎」私有 JSON 文件协议。
  * 控制目录：<product root>/User_Data/runtime_control/
  * command.json  shell → worker   {seq, cmd, ...}，cmd ∈ start / stop / set / list_devices
  * status.json   worker → shell   state / devices / delay_ms / underrun / compute_backend
  * worker 是 headless 的 tools/realtime_worker.py（TM_REALTIME_WORKER=1）

已知边界（README/ADAPTER.md 里也写了）：
  * 这是私有协议，随对方版本漂移；verified 写的是实测过的版本。
  * 设备/采样率/块长只能在 start 时读 configs/inuse/config.json，热改改不了，
    所以这类字段要「写配置 → 停流 → 重开」。音高/共鸣/检索强度/阈值可以热推。
  * 图和它的壳同时驱动同一个 worker 会互相踩状态。建议先退出它的外壳，
    由管理台自己拉起 headless worker。

字段翻译几乎是一比一：它的 config 键名就是原版 RVC 的键名（含拼写 threhold），
与我们的契约字段同名，所以这里只做「哪些能热改、哪些要重开流」的分流。
"""
from __future__ import annotations

import ctypes
import glob
import json
import os
import subprocess
import time

from .base import CONTRACT_FIELDS, Adapter

# 引擎能在变声途中直接吃的字段（见 gui_v1._worker_apply_hot）
HOT_FIELDS = ("pitch", "formant", "index_rate", "rms_mix_rate", "threhold", "f0method")
# 只能进 configs/inuse/config.json、并在 start 时读取的字段
RESTART_FIELDS = ("sg_hostapi", "sg_input_device", "sg_output_device", "sr_type",
                  "sg_wasapi_exclusive", "pth_path", "index_path",
                  "block_time", "crossfade_length", "extra_time")

CMD_ACK_TIMEOUT = 5.0
START_TIMEOUT = 120.0
STOP_TIMEOUT = 15.0


def _is_ascii_path(p):
    try:
        str(p).encode("ascii")
        return True
    except Exception:
        return False


class RvcFabricAdapter(Adapter):
    id = "rvc-fabric"
    label = "RVC Fabric（图灵镜）"
    verified = "1.6.0"
    notes = ("私有 JSON 文件协议；建议先退出它的外壳由管理台接管 worker；"
             "设备/块长变更会重开一次音频流")
    stop_note = "真停止变声（worker 进程仍驻留，下次 start 直接复用）"
    can_start = True

    def __init__(self, root=""):
        self.root = os.path.abspath(root) if root else self._guess_root()
        self.control = os.path.join(self.root, "User_Data", "runtime_control")
        self.command_path = os.path.join(self.control, "command.json")
        self.status_path = os.path.join(self.control, "status.json")
        self.seq_path = os.path.join(self.control, "command.seq")
        self.config_path = os.path.join(self.root, "configs", "inuse", "config.json")
        self.worker_script = os.path.join(self.root, "tools", "realtime_worker.py")

    # ---------------- 路径与进程 ----------------
    @staticmethod
    def _guess_root():
        cands = [os.environ.get("RVC_FABRIC_ROOT", "")]
        cands += [
            r"D:\Program Files\RVC Fabric",
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "RVC Fabric"),
            os.path.join(os.environ.get("ProgramFiles", ""), "RVC Fabric"),
        ]
        for c in cands:
            if c and os.path.isfile(os.path.join(c, "tools", "realtime_worker.py")):
                return c
        return cands[1] or cands[2]

    def _runtime_python(self):
        pats = [
            os.path.join(self.root, "User_Data", "runtimes", "*", "pythonw.exe"),
            os.path.join(self.root, "Runtime", "pythonw.exe"),
            os.path.join(self.root, "runtime", "pythonw.exe"),
        ]
        found = []
        for p in pats:
            found += glob.glob(p)
        if not found:
            return None
        # 优先 nvidia / amd 目录，其次任意
        found.sort(key=lambda p: (0 if "nvidia" in p.lower() else
                                  1 if "dml" in p.lower() or "amd" in p.lower() else 2, p))
        return found[0]

    @staticmethod
    def _pid_alive(pid):
        if not pid:
            return False
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)

    # ---------------- JSON 读写 ----------------
    @staticmethod
    def _read_json(path):
        for _ in range(6):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                return d if isinstance(d, dict) else {}
            except FileNotFoundError:
                return {}
            except Exception:
                time.sleep(0.03)
        return {}

    @staticmethod
    def _write_json(path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".%d.%d.tmp" % (os.getpid(), time.time_ns())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        for i in range(8):
            try:
                os.replace(tmp, path)
                return True
            except PermissionError:
                time.sleep(0.02 * (i + 1))
        # 最后一次直接写，宁可覆盖也不要丢命令
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass
        return False

    # ---------------- 协议 ----------------
    def _next_seq(self):
        cur = 0
        try:
            with open(self.seq_path, "r", encoding="utf-8") as f:
                cur = int((f.read() or "0").strip() or 0)
        except Exception:
            cur = 0
        cmd = self._read_json(self.command_path)
        try:
            cur = max(cur, int(cmd.get("seq") or 0))
        except Exception:
            pass
        n = cur + 1
        try:
            with open(self.seq_path, "w", encoding="utf-8") as f:
                f.write(str(n))
        except Exception:
            pass
        return n

    def _send(self, cmd, extra=None, ack_timeout=CMD_ACK_TIMEOUT):
        """写命令邮箱并等 worker 认领（last_cmd_seq >= seq）。"""
        seq = self._next_seq()
        body = {"seq": seq, "cmd": cmd, "ts": time.time()}
        if extra:
            body.update(extra)
        if not self._write_json(self.command_path, body):
            raise RuntimeError("命令写入失败（command.json 被占用）")
        deadline = time.time() + ack_timeout
        while time.time() < deadline:
            st = self._read_json(self.status_path)
            try:
                if int(st.get("last_cmd_seq") or 0) >= seq:
                    return seq
            except Exception:
                pass
            time.sleep(0.05)
        raise RuntimeError("worker 未在 %.1fs 内认领 %s（seq=%d）" % (ack_timeout, cmd, seq))

    def _wait_state(self, want, timeout):
        deadline = time.time() + timeout
        last = {}
        while time.time() < deadline:
            last = self._read_json(self.status_path)
            if str(last.get("state") or "") in want:
                return last
            pid = last.get("pid") or 0
            if pid and not self._pid_alive(pid):
                return last
            time.sleep(0.2)
        return last

    # ---------------- 信息 ----------------
    def _worker(self):
        st = self._read_json(self.status_path)
        pid = int(st.get("pid") or 0)
        if pid and not self._pid_alive(pid):
            return st, 0
        return st, pid

    def available(self):
        if not os.path.isdir(self.root):
            return False, "找不到 RVC Fabric 安装目录：%s" % self.root
        if not os.path.isfile(self.worker_script):
            return False, "安装目录里没有 tools\\realtime_worker.py"
        if not self._runtime_python():
            return False, "Runtime 未补全（先在图灵镜里下载运行时）"
        st, pid = self._worker()
        if not pid:
            return False, "worker 未运行（管理台可以替你拉起）"
        state = str(st.get("state") or "unknown")
        if state == "error":
            return False, "worker 报错：%s" % (st.get("error") or st.get("message") or "未知")
        return True, "worker 在跑（%s）" % state

    def state(self):
        st, _pid = self._worker()
        cfg = self._read_json(self.config_path)
        params = {k: cfg.get(k) for k in CONTRACT_FIELDS if k in cfg}
        return {
            "running": str(st.get("state") or "") == "running",
            "engine": self.id,
            "params": params,
            "devices": {"in": st.get("sg_input_device") or cfg.get("sg_input_device"),
                        "out": st.get("sg_output_device") or cfg.get("sg_output_device"),
                        "hostapi": st.get("sg_hostapi") or cfg.get("sg_hostapi")},
            "model": {"pth": cfg.get("pth_path"), "index": cfg.get("index_path")},
            "extra": {
                "state": st.get("state"),
                "delay_ms": st.get("delay_ms"),
                "infer_ms": st.get("infer_ms"),
                "underrun": st.get("underrun"),
                "compute_backend": st.get("compute_backend"),
                "compute_device": st.get("compute_device"),
                "message": st.get("message"),
            },
        }

    def devices(self):
        st = self._read_json(self.status_path)
        return {
            "hostapis": st.get("hostapis") or [],
            "inputs": st.get("input_devices") or [],
            "outputs": st.get("output_devices") or [],
            "default_input": st.get("default_input_device"),
            "default_output": st.get("default_output_device"),
            "note": "名称是 sounddevice 的截断名（约 32 字符），下发时要原样用",
        }

    # ---------------- 下发 ----------------
    def apply(self, payload):
        payload = payload or {}
        hot = {k: payload[k] for k in HOT_FIELDS
               if k in payload and payload[k] is not None}
        structural = {k: payload[k] for k in RESTART_FIELDS
                      if k in payload and payload[k] is not None}
        unknown = [k for k in payload if k not in CONTRACT_FIELDS]

        out = {"ok": True, "engine": self.id, "applied": [], "skipped": unknown,
               "restarted": False, "note": ""}

        if structural:
            cfg = self._read_json(self.config_path)
            changed = {k: v for k, v in structural.items() if cfg.get(k) != v}
            if changed:
                if not _is_ascii_path(self.root):
                    return {"ok": False,
                            "error": "安装路径含非 ASCII 字符，图灵镜的 faiss 检索在这种路径下不可用"}
                cfg.update(changed)
                if not self._write_json(self.config_path, cfg):
                    return {"ok": False, "error": "写入 configs/inuse/config.json 失败"}
                out["applied"] += list(changed)
            running = str(self._read_json(self.status_path).get("state") or "") == "running"
            need_restart = bool(changed) and running and any(
                k in changed for k in ("sg_hostapi", "sg_input_device", "sg_output_device",
                                       "sr_type", "sg_wasapi_exclusive", "block_time",
                                       "crossfade_length", "extra_time"))
            if need_restart:
                self._stop_stream()
                out["restarted"] = True
            if changed or not running:
                self._send("start", {}, ack_timeout=CMD_ACK_TIMEOUT)
                st = self._wait_state(("running", "error", "idle"), START_TIMEOUT)
                if str(st.get("state") or "") == "error":
                    return {"ok": False, "error": st.get("error") or st.get("message") or "start 失败",
                            "applied": out["applied"], "state": st.get("state")}

        if hot:
            try:
                self._send("set", hot)
                out["applied"] += list(hot)
                # worker 的热参数只改内存里的 gui_config，不会回写配置文件；
                # 但下一次 start 是从配置文件读的，所以这里补写一次，保持
                # 「state() 看到的」和「下次开流用的」一致。不重开流。
                cfg = self._read_json(self.config_path)
                cfg.update(hot)
                self._write_json(self.config_path, cfg)
            except Exception as e:
                out["ok"] = False
                out["error"] = str(e)

        if not hot and not structural:
            out["note"] = "没有可下发的字段"
        return out

    def _stop_stream(self):
        try:
            seq = self._send("stop", {})
        except Exception:
            return False
        deadline = time.time() + STOP_TIMEOUT
        while time.time() < deadline:
            st = self._read_json(self.status_path)
            try:
                if int(st.get("stop_seq") or 0) >= seq:
                    return True
            except Exception:
                pass
            if str(st.get("state") or "") not in ("running", "starting"):
                return True
            time.sleep(0.2)
        return False

    def stop(self):
        st, pid = self._worker()
        if not pid:
            return {"ok": True, "note": "worker 没在跑，无需停止"}
        if str(st.get("state") or "") == "running":
            self._stop_stream()
        else:
            try:
                self._send("stop", {})
            except Exception as e:
                return {"ok": False, "error": str(e)}
        return {"ok": True, "note": "已停止变声，worker 进程保留"}

    def start_engine(self):
        ok, why = self.available()
        if ok:
            return {"ok": True, "note": "worker 已在运行", "reason": why}
        py = self._runtime_python()
        if not py:
            return {"ok": False, "note": "Runtime 未补全，请先在图灵镜里完成运行时下载"}
        if not os.path.isfile(self.worker_script):
            return {"ok": False, "note": "找不到 tools\\realtime_worker.py"}
        env = os.environ.copy()
        env["TM_REALTIME_WORKER"] = "1"
        env["TM_VOICE_ROOT"] = self.root
        try:
            subprocess.Popen([py, os.path.join("tools", "realtime_worker.py")],
                             cwd=self.root, env=env,
                             creationflags=0x08000000,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            return {"ok": False, "note": "拉起 worker 失败：%s" % e}
        st = self._wait_state(("idle", "running", "error"), START_TIMEOUT)
        state = str(st.get("state") or "")
        if state == "error":
            return {"ok": False, "note": st.get("error") or st.get("message") or "worker 启动失败"}
        if state in ("idle", "running"):
            return {"ok": True, "note": "worker 就绪（%s，%s）" %
                    (state, st.get("compute_device") or st.get("compute_backend") or "未知后端")}
        return {"ok": False, "note": "worker 已拉起，但 %.0fs 内未就绪（看 User_Data/logs/worker）" % START_TIMEOUT}
