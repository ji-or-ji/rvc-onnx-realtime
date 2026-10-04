"""适配器契约：每个引擎一个适配器，对上层提供同一套动作。

设计规矩见 docs/ADAPTER.md。要点：
  * 无状态：状态一律现查，不缓存、不假设引擎行为
  * 契约字段名沿用原版 config.json（含拼写 threhold）
  * 不碰对方文件（除非该引擎唯一接口就是文件）
"""
from __future__ import annotations

# 下发与回读统一使用的字段（= 原版 config.json 的键名）
CONTRACT_FIELDS = [
    "sg_hostapi", "sg_input_device", "sg_output_device", "sr_type", "sg_wasapi_exclusive",
    "pth_path", "index_path", "f0method",
    "threhold", "pitch", "formant", "rms_mix_rate", "index_rate",
    "block_time", "crossfade_length", "extra_time",
]


class Adapter:
    """所有引擎适配器的基类。子类必须覆盖下面四个动作。"""

    id = ""             # 机器名，如 builtin / vcclient / rvc-fabric
    label = ""          # 给人看的名字
    verified = ""       # 已验证的对方版本（私有协议必填）
    notes = ""          # 已知脆弱点 / 使用前提
    stop_note = ""      # 「停止」在本引擎里到底意味着什么（会显示在界面上）
    can_start = False   # 管理台能否替它把引擎拉起来

    def available(self):
        """-> (bool, 原因)。探测对方是否就绪；异常一律在这里吞掉并变成原因。"""
        raise NotImplementedError

    def state(self):
        """-> 统一状态 dict（见 docs/ADAPTER.md 第二节）。"""
        raise NotImplementedError

    def apply(self, payload):
        """-> dict。payload 使用契约字段名；适配器负责翻译成对方的语言。"""
        raise NotImplementedError

    def stop(self):
        """-> dict。停止变声（含义因引擎而异，见 stop_note）。"""
        raise NotImplementedError

    def start_engine(self):
        """-> dict。替用户把引擎拉起来（做不到就说明原因，不要假装）。"""
        return {"ok": False, "note": "该引擎需自行启动（管理台不会替它拉起）"}

    # 给管理台用的一行摘要
    def info(self):
        try:
            ok, why = self.available()
        except Exception as e:                      # 适配器再烂也不该把上层搞崩
            ok, why = False, "探测失败：%s" % e
        return {"id": self.id, "label": self.label, "verified": self.verified,
                "notes": self.notes, "stop_note": self.stop_note,
                "can_start": bool(self.can_start),
                "available": bool(ok), "reason": why}
