"""VC Client（w-okada/voice-changer）适配器。

接管方式：它的 REST 接口（与 Socket.IO 同端口，默认 18888）。
  * 控制与状态走 REST（FastAPI）；Socket.IO 只跑实时音频，**不是控制总线**
  * 无鉴权：靠 --host 绑定与 --allowed-origins 白名单（我们的管理台请自备令牌层）
  * 接口无版本化：客户端自身仍在调 GET /model_type，而 master 服务端已无该路由（404）
    → 属于「事实上的对外接口」，不是契约接口。解析一律防御式。

键名映射（RVC 系，与我们的契约字段完全不同）：
    pitch → tran          检索 → indexRatio       额外时长 → extraConvertSize
    原声旁路 → passThrough   f0 → f0Detector        模型 → modelSlotIndex
    设备 → serverInputDeviceId / serverOutputDeviceId（服务端出声需 enableServerAudio=1）

已知不支持：block_time（发送块长在客户端 AudioWorklet 里，REST 设不了；
            服务端设备模式只有 serverReadChunkSize）、crossfade_length、formant。
"""
from __future__ import annotations

import json
import urllib.request

from .base import Adapter

# 契约字段 → VC Client 设置键
KEYMAP = {
    "pitch": "tran",
    "index_rate": "indexRatio",
    "extra_time": "extraConvertSize",
    "f0method": "f0Detector",
    "threhold": "silentThreshold",
}
# 明确无对应键的契约字段（不要静默丢弃，要在 state()/日志里说明）
UNSUPPORTED = ("block_time", "crossfade_length", "formant", "rms_mix_rate",
               "sg_hostapi", "sr_type", "sg_wasapi_exclusive")
# 反向表：VC 键 → 契约字段
REVERSE = {v: k for k, v in KEYMAP.items()}


class VCClientAdapter(Adapter):
    id = "vcclient"
    label = "VC Client（w-okada/voice-changer）"
    verified = "master v2.2.2-beta（客户端库 1.0.182）"
    notes = ("仅服务端音频设备模式可被完全接管；无鉴权；"
             "block_time 无对应接口；接口未版本化，字段会漂移")
    stop_note = "VC Client 没有服务端停止命令，这里的「停止」= 切为原声旁路（passThrough）"
    can_start = False

    def start_engine(self):
        return {"ok": False,
                "note": "VC Client 是独立软件，需你自己启动；建议用 --host 0.0.0.0 启动以便局域网接管"}

    def __init__(self, host="127.0.0.1", port=18888):
        self.base = "http://%s:%d" % (host, port)

    # ---------------- HTTP ----------------
    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=8) as r:
            return json.loads(r.read().decode("utf-8") or "{}")

    def _post_form(self, path, fields):
        """multipart/form-data —— 它的写操作除 /test 外一律是表单，且 val 必须是字符串"""
        b = "----rvcadapter7f3a91c2"
        parts = []
        for k, v in fields.items():
            parts.append('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, k, v))
        body = ("".join(parts) + "--%s--\r\n" % b).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=body, method="POST")
        req.add_header("Content-Type", "multipart/form-data; boundary=%s" % b)
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8") or "{}"
        try:
            return json.loads(raw)
        except Exception:
            return {"ok": True}          # /load_model 成功时不回 info

    def _set(self, key, val):
        return self._post_form("/update_settings", {"key": key, "val": str(val)})

    # ---------------- 翻译层（可离线单测，不需要真的装着 VC Client） ----------------
    def plan(self, payload):
        """契约字段 → [(vc_key, val)]，以及被跳过的字段名。"""
        sets, skipped = [], []
        for k, v in (payload or {}).items():
            if k in KEYMAP:
                sets.append((KEYMAP[k], v))
            elif k == "sg_input_device":
                sets.append(("serverInputDeviceId", v))
                sets.append(("enableServerAudio", 1))     # 服务端自己出声
            elif k == "sg_output_device":
                sets.append(("serverOutputDeviceId", v))
            elif k in ("pth_path", "index_path"):
                skipped.append(k)     # 模型走槽位：upload_file → concat → load_model
            elif k in UNSUPPORTED:
                skipped.append(k)
        return sets, skipped

    # ---------------- 四个动作 ----------------
    def available(self):
        try:
            info = self._get("/info")
            st = str(info.get("status", "")).lower()
            return True, "状态：%s" % (st or "未知")
        except Exception as e:
            return False, "连不上 VC Client（%s）：确认它以 --host 0.0.0.0 启动，端口默认 18888" % e

    def state(self):
        info = self._get("/info")
        params = {}
        for vck, ctx in REVERSE.items():
            if vck in info:
                params[ctx] = info[vck]
        return {
            "running": str(info.get("status", "")).lower() in ("running", "success", "ok"),
            "engine": self.id,
            "params": params,
            "devices": {"in": info.get("serverInputDeviceId"),
                        "out": info.get("serverOutputDeviceId"),
                        "hostapi": None},
            "model": {"pth": None, "index": None,      # 它按槽位管理，没有文件路径概念
                      "slot": info.get("modelSlotIndex")},
            "extra": {"slots": len(info.get("modelSlots") or []),
                      "unsupported": list(UNSUPPORTED)},
        }

    def apply(self, payload):
        sets, skipped = self.plan(payload)
        out = {"applied": [], "skipped": skipped}
        for key, val in sets:
            self._set(key, val)
            out["applied"].append(key)
        # 模型：契约给的是文件路径，它只认槽位——这里只做槽位切换；
        # 装新模型文件要走 upload_file → concat_uploaded_file → load_model 三步（暂不自动做）
        if payload.get("model_slot") is not None:
            self._set("modelSlotIndex", payload["model_slot"])
            out["applied"].append("modelSlotIndex")
        return out

    def stop(self):
        # 它没有"停止变声"的服务端命令（客户端侧 start/stop 是本地 AudioWorklet 动作）。
        # 能做的等效动作：切原声旁路。
        self._set("passThrough", 1)
        return {"ok": True, "note": "VC Client 无服务端停止命令，已切为原声旁路"}
