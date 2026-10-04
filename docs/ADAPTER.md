# 引擎适配器契约（Adapter Contract）

目标：管理台**不绑任何一家引擎**。每个引擎配一个适配器，对上层提供同一套动作。

## 一、一个适配器必须提供的四个动作

| 动作 | 签名 | 说明 |
| --- | --- | --- |
| 可用性 | `available() -> (bool, reason)` | 探测对方进程/端口/文件是否就绪，并给出人话原因 |
| 状态 | `state() -> dict` | 统一状态（见下） |
| 下发 | `apply(payload) -> dict` | 参数 + 模型 + 设备，一次下发 |
| 停止 | `stop() -> dict` | 停止变声（**不退出**对方程序） |

## 二、统一状态 `state()`

超集结构，某家取不到的字段**留空**，不要编：

```json
{
  "running": true,
  "engine": "builtin",
  "params": { "pitch": 12, "block_time": 0.06, "...": "契约字段名" },
  "devices": { "in": "麦克风 ...", "out": "扬声器 ...", "hostapi": "MME" },
  "model":   { "pth": "assets/weights/x.pth", "index": "assets/indices/x.index" },
  "extra":   { "latency_ms": 60, "underruns": 0 }
}
```

## 三、下发 `apply(payload)`

扁平字典，**一律使用契约字段名**（沿用原版 `configs/config.json`，含原版拼写 `threhold`）：

```
sg_hostapi  sg_input_device  sg_output_device  sr_type  sg_wasapi_exclusive
pth_path  index_path  f0method
threhold  pitch  formant  rms_mix_rate  index_rate
block_time  crossfade_length  extra_time
```

每个适配器负责把这套字段**翻译成对方引擎的语言**；管理台不关心对方怎么叫。

## 四、接管方式与优先级

| 引擎 | 接管方式 | 性质 | 风险 |
| --- | --- | --- | --- |
| 内置（原版 realtime_gui） | 我们插进原版进程的 HTTP 接口（`control_server`，8898） | 我们自己的接口 | 无 |
| VC Client（w-okada） | **官方 WebSocket API** | 官方接口（首选） | 低 |
| RVC Fabric（Turing-Mirror） | 外壳↔引擎的 JSON 文件协议 | **私有**协议 | 高：随对方版本失效 |
| RVC WebUI 2.3 | Gradio（HTTP/WS） | 半官方 | 中 |

## 五、四条规矩

1. **优先官方接口**。私有协议一律在 UI 上标注「随对方版本可能失效」，代码里写死 `verified = "<已验证版本>"`
2. **适配器无状态**：状态一律现查，不假设引擎行为、不缓存
3. **不碰对方文件**（除非该引擎唯一的接口就是文件），尤其**不写对方的配置**
4. **模型库与预设由管理台持有**，切换时只把路径下发给适配器

## 六、为什么这样切

我们的资产是"管理"（局域网遥控、预设、模型库、手机端），不是"推理"。引擎换谁都不该动上层。
反过来，任何一家引擎只要暴露**一个能下发参数与切模型的口子**，我们就能把它接进来——官方接口最好，私有协议次之，没有接口的（原版 realtime_gui）就由我们替它补一个。
