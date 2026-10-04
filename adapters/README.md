# 引擎适配器：怎么再加一家

上层管理台不绑任何一家引擎。加一家新引擎，只需要写一个适配器。契约见 `docs/ADAPTER.md`。

## 三步

**1. 写一个类**，继承 `adapters/base.py` 的 `Adapter`，实现四个动作：

```python
class MyEngineAdapter(Adapter):
    id = "myengine"                 # 机器名（小写，进 URL 用）
    label = "某引擎（说明）"          # 给人看的
    verified = "对方版本号"           # ★ 私有协议必填；官方接口也建议写
    notes = "使用前提与已知脆弱点"

    def available(self): ...        # -> (bool, 原因)
    def state(self): ...            # -> 统一状态 dict
    def apply(self, payload): ...   # -> dict（payload 用契约字段名）
    def stop(self): ...             # -> dict
```

**2. 把它登记进 `adapters/__init__.py`** 的 `REGISTRY`，并加进 `all_adapters()`。

**3. 对着它跑一次 `info()`**：未就绪时必须**优雅返回原因**，不许抛异常（基类 `info()` 已兜底，但 `available()` 内部也别抛出）。

## 必须遵守的四条

1. **无状态**：状态一律现查，不缓存、不假设对方行为
2. **翻译**：契约字段 → 对方语言，映射表写在代码里（如 `adapters/vcclient.py` 的 `KEYMAP`），**不要静默丢弃**——不支持的字段要能报出来
3. **不碰对方文件**（除非该引擎唯一的接口就是文件），尤其不写对方配置
4. **每个适配器标 `verified`**：接口没有版本号的时候，这个字段就是我们唯一的防线

## 已有的三档（按接管优先级）

| 适配器 | 接管方式 | 性质 | 风险 |
| --- | --- | --- | --- |
| `builtin` | 我们插进原版 realtime_gui 的 HTTP 接口（8898） | 我们自己的接口 | 无 |
| `vcclient` | REST（同端口 18888；Socket.IO 只跑音频） | 可用对外接口、无鉴权、**未版本化** | 中 |
| （待定）`rvc-fabric` | `User_Data/runtime_control/` 下的 JSON 文件协议 | **私有**协议，且是「顶替它的壳」 | 高：必须钉死 1.6.0 |

## 加一家之前的检查清单

- [ ] 它有**对外接口**吗？官方接口 > 私有协议 > 没有（那就得我们替它补一个，像 `builtin` 那样）
- [ ] 接口**有版本号**吗？没有就把当前版本写进 `verified`
- [ ] 它**有没有鉴权**？没有的话，我们这层要负责暴露范围
- [ ] 它需要**和我们抢同一块声卡**吗？（同一台机器上两家同时开就会抢设备）
- [ ] 我们要**替它管到什么程度**：只切参数？还是连模型文件上传也管？

## 参考实现

- `adapters/builtin.py` —— 契约字段直通（因为那套字段名就是从它来的）
- `adapters/vcclient.py` —— 需要翻译：`pitch→tran`、`index_rate→indexRatio`、`extra_time→extraConvertSize`、`f0method→f0Detector`、设备走 `serverInputDeviceId/serverOutputDeviceId`（服务端出声还要 `enableServerAudio=1`）
