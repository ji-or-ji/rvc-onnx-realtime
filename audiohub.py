"""网络音频的会话与锁：输入全局唯一（申请制 + 可抢占），输出可多路。

这里只管"谁有发言权"这条规则，不碰音频数据 —— 所以可以单独测。
规则（用户定）：
  1. 任何时刻只允许一个输入源
  2. 输出可多路，每路音量在客户端本地调（服务端不参与）
  3. 默认申请制；被占时由界面问用户是否抢占
  4. 掉线 / 心跳超时自动释放

时间单位为秒；用传入的 now 便于测试（不依赖真实时钟）。
"""


class AudioHub:
    def __init__(self, heartbeat_timeout=10.0):
        self.heartbeat_timeout = float(heartbeat_timeout)
        self.holder = None          # {"client": id, "label": 显示名, "since": t, "seen": t}
        self.listeners = {}         # client -> {"label":..., "seen": t}

    # ---------- 工具 ----------
    def _reap(self, now):
        """心跳超时的客户端一律清掉；持有者超时则释放（并把事件返回给调用方广播）。"""
        events = []
        if self.holder and now - self.holder["seen"] > self.heartbeat_timeout:
            events.append({"type": "revoked", "client": self.holder["client"],
                           "reason": "心跳超时，已自动释放输入权"})
            self.holder = None
        for c in list(self.listeners):
            if now - self.listeners[c]["seen"] > self.heartbeat_timeout:
                del self.listeners[c]
        return events

    def state(self, now):
        self._reap(now)
        return {
            "input": ({"client": self.holder["client"], "label": self.holder["label"],
                       "since": self.holder["since"]} if self.holder else None),
            "listeners": [{"client": c, "label": v["label"]} for c, v in self.listeners.items()],
        }

    # ---------- 心跳 / 连接 ----------
    def seen(self, client, label, now):
        """客户端活着；顺带把它登记成听众（输入持有者也在听众里，它自己也听）。"""
        if self.holder and self.holder["client"] == client:
            self.holder["seen"] = now
        self.listeners.setdefault(client, {"label": label, "seen": now})
        self.listeners[client]["seen"] = now
        if self.listeners[client].get("label") != label:
            self.listeners[client]["label"] = label
        return self._reap(now)

    def disconnect(self, client, now):
        events = []
        if self.holder and self.holder["client"] == client:
            events.append({"type": "revoked", "client": client, "reason": "该设备已断开"})
            self.holder = None
        self.listeners.pop(client, None)
        return events

    # ---------- 输入权 ----------
    def claim(self, client, label, now):
        """申请输入权。返回 (是否批准, 说明, 事件列表)。"""
        events = self._reap(now)
        self.seen(client, label, now)
        if self.holder is None:
            self.holder = {"client": client, "label": label, "since": now, "seen": now}
            events.append({"type": "granted", "client": client})
            return True, "已获得输入权", events
        if self.holder["client"] == client:
            return True, "你已经是输入源（已续期）", events
        return False, "当前输入源是「%s」；需要抢占请再确认一次" % self.holder["label"], events

    def takeover(self, client, label, now):
        """抢占输入权。返回 (是否成功, 说明, 事件列表)。"""
        events = self._reap(now)
        self.seen(client, label, now)
        old = self.holder
        if old and old["client"] == client:
            return True, "你已经持有输入权", events
        self.holder = {"client": client, "label": label, "since": now, "seen": now}
        if old:
            events.append({"type": "revoked", "client": old["client"],
                           "reason": "输入权被「%s」抢占" % label})
        events.append({"type": "granted", "client": client, "took_over": bool(old)})
        return True, ("已从「%s」手中接过输入权" % old["label"]) if old else "已获得输入权", events

    def release(self, client, now):
        events = self._reap(now)
        if self.holder and self.holder["client"] == client:
            self.holder = None
            events.append({"type": "released", "client": client})
            return True, "已释放输入权", events
        return False, "你当前不是输入源", events


# ---------------- 自检：把规则跑一遍 ----------------
if __name__ == "__main__":
    def show(step, ret, hub, now):
        ok, note, evs = ret
        print("[%s] ok=%s | %s" % (step, ok, note))
        for e in evs:
            print("      事件 %s" % e)
        print("      状态 %s" % hub.state(now))

    h = AudioHub(heartbeat_timeout=10.0)
    print("=== 1. A 申请 -> 批准 ===")
    show("A.claim", h.claim("A", "手机-小季", 0.0), h, 0.0)

    print("=== 2. B 申请 -> 应被拒，并告知持有者 ===")
    show("B.claim", h.claim("B", "平板-客厅", 1.0), h, 1.0)

    print("=== 3. B 抢占 -> 成功，A 收到 revoked ===")
    show("B.takeover", h.takeover("B", "平板-客厅", 2.0), h, 2.0)

    print("=== 4. A 再申请 -> 应被拒（现在 B 持有）===")
    show("A.claim", h.claim("A", "手机-小季", 3.0), h, 3.0)

    print("=== 5. B 继续心跳；到 14s 时 A 已超时被清、B 仍持有 ===")
    h.seen("B", "平板-客厅", 13.0)
    print("      %s" % h.state(14.0))

    print("=== 6. B 掉线 -> 自动释放 ===")
    for e in h.disconnect("B", 14.5):
        print("      事件 %s" % e)
    print("      %s" % h.state(14.5))
