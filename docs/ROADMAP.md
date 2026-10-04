# 路线图 / 待办

> 这份文件是"待办真源"。**做完一项就删一项**，别让它变成许愿池。

## 必做（用户已确认方向）

### 1. 修 `web_ui.py` 的异常占用（**未修，只是绕开了**）
- 现象：`web_ui.py --port 8899 --engine torch-cuda`，**5 秒吃掉 58.5 CPU 秒（≈11.7 核）**，线程数 96
- 已知：8899 有客户端连着；同时存在两个实例（venv 的 + 系统 Python 的），吃满 CPU 的是**持有 8899 那个**
- 为什么必须修：不修，非 N 卡机器就没有可用的路线，「ONNX 轻量档」形同虚设（跨平台承诺也落空）
- 待查方向（按嫌疑排序）：
  1. `_run_duplex` 的 `sd.Stream(blocksize=block_dev)` 是否真生效——若设备回调被以极小缓冲高频触发，每次回调都跑一遍 torch 推理，正好解释 CPU/GPU 双高
  2. 回调异常频率时 `_put` 每次回调都写状态（含字典构造）也会放大开销
  3. torch 的 CPU 线程数（`Config()` 未限制 → 默认吃满全部核）
  4. WS ticker 与 gate 逻辑（可能性较低）
- 复现方式（注意：会真实吃 CPU，测完立刻清进程）：
  ```
  起 web_ui --engine torch-cuda → 观察 idle 占用
  → WS 下发 start → 观察运行占用与 blocks/秒
  → 对比 blocks/秒 是否等于 1/block_time（若不是，缓冲区尺寸没生效）
  ```
- 排查前的纪律：先看整机 CPU 前三名，别把自己的残留算到别人头上

## 参考：RVC Fabric（图灵镜）功能清单 — 待评估，非承诺

- 仓库：https://github.com/Turing-Mirror/RVC-Fabric （MIT；CNB 制品镜像：https://cnb.cool/Turing-Mirror/RVC-Fabric-Releases ）
- **架构（已核实）**：Tauri(Rust) + React 桌面外壳 ↔ Python 承担全部计算，**两者用 JSON 文件协议通信**；上游 RVC WebUI 的 Gradio 作为高级功能随包保留。
  → 与我们「引擎 / 外壳分离」同构；差别只在外壳：他们选 Tauri（开箱即用、可深度定制），我们选浏览器（免安装 + 手机遥控）。
- 可直接借鉴的三处：
  1. 他们 2026-10-04 提交「**引擎：实时处理抽成公共模块，离线渲染和实时变声共用一份**」——正是我们 `web_ui` 与 `realtime_gui` 两条音频路径的痛点解法
  2. 仓库内置 `VBCABLE/`：虚拟声卡随安装流程一起装（我们 bootstrap 可照做）
  3. 致谢含 TorchGate → 噪声门/压缩是 **Python 后级** 实现 ⇒ **A/B 原声切换与 DSP 链都能在 Python 侧做**，不必动前端

它比我们多的能力：

| 功能 | 它的效果 | 我们的现状 | 代价 |
| --- | --- | --- | --- |
| 一键切原声（A/B） | 变声/原声即时切换 | 无 | 低：需在 audio_callback 里加直通分支 |
| 预设导入/导出（可分享） | 参数预设可导出分享 | 已有预设，但只在本机 | 低：面板加导出/导入 JSON |
| DSP 链：噪声门 / 压缩 / 5 段 EQ | 内置效果链 | 无（只有响应阈值） | 中：实现在输出端，纯 numpy/scipy 可做 |
| 音色库网格 UI | 网格化音色库 | 下拉框 | 低（纯前端） |
| 变声中无缝切音色 | 不打断音频切换 | 需 stop → load(~30s) → start | 高：预加载双模型 + 交叉淡化（显存×2） |
| 自动安装虚拟声卡 | 安装流程内置 | 手册教你手动装 | 中：bootstrap 可静默装 VB-Cable |
| 社区音色广场 | 官方源 + 第三方源，多线程下载 | 无 | 高：涉托管与版权，建议不动 |
| 自动识别硬件并下运行时 | 首次启动自动补全 | 已有同类设计（bootstrap） | — |

**形态差异**：它是「打包好的本机应用」；我们是「原版引擎 + 浏览器遥控台」。我们的差异点是**手机/局域网遥控**与**引擎行未改（模型来源自由）**。

**战略取舍（待拍板）**：要补 A/B 与 DSP，就必须动 `realtime_gui.py` 的音频回调——这会改变「引擎原样」这个卖点。守零改动 vs 深度定制，需明确选一条。

## 方向（2026-10-04 用户提出）：局域网管理台 —— 多引擎适配

**身份换位**：不再“做一个变声引擎”，而是“管各种变声引擎的上层”。

```
上层管理台   我们的面板：局域网/手机遥控、预设、模型库、外观、令牌
中层适配器   统一动作：启动 / 停止 / 状态 / 下发参数 / 切模型 / 切音色
下层引擎     ① 内置（原版 realtime_gui + control_server = 我们替它做的接口）
             ② VC Client（w-okada）—— 有官方 WebSocket API（首选对接对象）
             ③ RVC Fabric（Turing-Mirror）—— 外壳↔引擎为 JSON 文件协议（可摸黑接管，脆弱）
             ④ RVC WebUI 2.3 —— Gradio（HTTP/WS）
```

**接管优先级原则**：优先接**官方对外接口**；私有协议（文件/IPC）当二等公民，并在 UI 上标注“随对方版本可能失效”。

**随之而来的设计变更**：
- 面板新增一个维度「引擎」；预设里也要包含“用哪个引擎”
- 适配器必须无状态化：状态由适配器自己查，不假设引擎行为

**进度**：契约与内置适配器已就位（`docs/ADAPTER.md`、`adapters/base.py`、`adapters/builtin.py`），已对着控制接口实测通过。

**第一步**：定义适配器接口，然后接一家**有官方接口**的（VC Client 的 WS）来证明“兼容多产品”是真的。

**风险**：第三方私有协议随版本失效；每个适配器需标注“已验证版本”。

### 调研结论：RVC Fabric 架构（已核实，2026-10-04）

**纠正**：之前推测的 `infer/modules/vc/pipeline.py` **不是实时模块，是离线渲染管线**。那条“实时处理抽成公共模块”提交指的是 **`tools/realtime_block.py`**。

实时链路真实文件：`infer/lib/rtrvc.py`（实时推理引擎）、`tools/realtime_block.py`（共用“一块进一块出”）、`tools/audio_io_process.py`（独立声卡 IO 进程 + 共享内存环形缓冲）、`tools/block_geometry.py`（分块几何单一来源）、`tools/dsp_fx.py`（后级 DSP 链）、`tools/win_realtime.py`（Windows 调度提升）、`tools/worker_protocol.py` + `app/src-tauri/src/protocol.rs`（文件式命令协议）。

**架构：跨进程三层**
1. 声卡 IO 进程（multiprocessing）：sounddevice 回调里**只做 memcpy** 写共享内存环形缓冲，零推理
2. 引擎进程的音频线程：取一块 → `realtime_block.process_block`（内含 `rtrvc.infer`）→ 写回
3. 引擎主循环：每 80ms 轮询 `command.json`（控制面）+ 抽 `_model_events` 队列；**音频线程不写磁盘/状态**

控制面是文件 mailbox（seq 认领 + 回执），角色上等同于我们的 `control_server`，只是传输介质从 HTTP 换成原子替换的 JSON 文件。

**三个关键答案**
- **Bypass（切原声）**：软旁路。`process_block` 里仅 `function == "vc"` 才跑模型；否则拿输入当输出，**继续走同一条下游**（DSP → SOLA → 音量 → 软限幅）。平滑靠“每块本来就过 SOLA”，没有专门的旁路斜坡。
- **DSP 链**：全在 Python/numpy、**变声之后**：噪声门 → 压缩器 → 5 段图形 EQ（60/250/1k/4k/8k，RBJ 双二阶）→ 输出增益 → tanh 软限幅；有 scipy 则一次 `sosfilt`。
- **无缝切音色**：**不是双模型交叉淡化**。① 热参数 `change_key / change_formant / change_index_rate`（下一块生效）；② 模型热换复用 `last_rvc`：**hubert 共享不重载**，同 pth 连 net_g 也复用，只有换 pth 才重载；换的过程后台事件式（`VC_SWAPPING`），旧模型继续跑。

**可借鉴（对我们对症）**
1. **音频回调与推理彻底隔开，回调只搬样本**——正是我们“回调内推理”那些坑（xrun、CPU 飙升）的解药
2. 环形缓冲欠载时**消费清零**（一次 miss 变成一段静音，而不是卡带循环）
3. 实时与离线共用一个 `process_block`（避免“调参调出来的和听到的不是同一个声音”）
4. 分块几何单一来源
5. 热参数即时生效不重载；状态写盘只在主循环

**慎用/不抄**：文件轮询 mailbox（但它的握手：seq 认领/回执超时/原子替换值得抄）；Windows 专用调度提升（MMCss / EcoQoS / timeBeginPeriod，非 Windows 无效）；DirectML/A 卡成堆绕行；多进程带来的产品化成本（隐藏黑框、pid 台账、null-byte 检测）。

**三个未确认项**（读不到 `gui_v1.py` ~199KB 的中段，因 >50KB 文件只能取头尾）：① 音频线程真实实现 ② bypass 切换瞬间怎么处理缓冲 ③ 换音色有无跨淡化。想钉死这三点，需重派一个**可写**的子代理（只读模式下 browser/exec 会被拦），或人工贴出中段。

### 控制面（已核实，2026-10-04）——RVC Fabric 能否被外部接管

`User_Data/runtime_control/` 下的文件式回路：
- `command.json`（下行：seq / cmd / ts + 负载）、`status.json`（上行状态）、`sts.json`（离线批量进度，独立文件）、`command.seq`（单调序号）、`worker.pid` 与 `worker.pids`（进程台账）
- **三段式握手**：派发 seq → 认领 `last_cmd_seq` → 完结标记（`devices_seq` / `stop_seq` / `model_apply`）
- 命令集：`start / stop / quit / list_devices / convert / sts_cancel / prewarm / set`
- **参数、换模型、切原声都走同一条 `set`**（在飞时报 `vc.swapping`）；`set` 负载中的 **`function` 就是变声/原声/FX 的开关**
- 轮询：引擎侧 80ms 读命令；壳侧 ~400ms 读状态；派发后 20ms 轮询回执
- 另两条通道：**命名共享内存 PCM 桥**（魔数 `FABPCM01`，名 `Local\RVCFabricPcm-{pid}-{epoch}`，**Python 是唯一写入方**，Rust 的 voice bus 读走与音乐混音）；一次性 worker 走 argv + stdout。**无 HTTP/WS 服务端**（Cargo.toml 只有出站 reqwest/ripget）

**接管结论：可行，但本质是「顶替它的壳」，不能与壳并存。**
前提：同机同用户 + 能找到产品根 + 复刻 ``env_for_runtime``（``TM_VOICE_ROOT`` / ``TM_REALTIME_WORKER=1`` / PATH 前置 Runtime / ``weight_root`` / ``index_root`` / ``rmvpe_root`` / TEMP / ``TM_ACCEL``）+ 原子写 + seq 单调 + 等完成标记。

**五条风险**：① 单槽 mailbox 会盖掉前一条命令（必须发一条等回执再发下一条） ② seq 所有权冲突 ③ Windows 文件占用（WinError 5/32）需重试 ④ 进程归属与孤儿清理（它只杀自己 spawn 的 pid） ⑤ **协议无版本号**——必须钉死 1.6.0。
另：若引擎跑「原生语音输出」模式，还要先完成 PCM 桥握手；退回自带输出时仅 JSON 文件即可。

**分包模型（值得拄）**：nvidia / amd / nvidia50 三个变体各自是**一整棵独立包（含各自 Runtime）**，不是“一个 Runtime + 开关”；运行期变体装在 ``User_Data/runtimes/<variant>/``，当前变体记在 ``app_config.json``，``accel_backend`` → 环境变量 ``TM_ACCEL``。

### 控制面（已核实，2026-10-04）——VC Client (w-okada/voice-changer)

**先纠正一个误称**：所谓“官方 WebSocket 远程控制”是半个误称。同一端口（默认 18888，默认只绑 127.0.0.1）上：**REST（FastAPI）承担全部控制与状态；Socket.IO（`/test`）只跑实时音频，不是控制总线**。

- 启动远程 = 两件事：`--host` 放开绑定 + `--allowed-origins` 把管理台 Origin 加白名单（没有单独的“远程模式”开关）
- REST 端点：`GET /info`（全量状态）、`GET /performance`、**`POST /update_settings`（主控制通道，multipart 表单，`key`+`val`，val 一律字符串）**、`POST /load_model`、`POST /upload_file`+`concat_uploaded_file`（装新模型）、`GET /onnx`、`POST /test`（唯一 JSON body）
- **键名与我们的契约完全不同**：音高=`tran`、检索=`indexRatio`、额外时长=`extraConvertSize`、旁路=`passThrough`、f0=`f0Detector`、模型=`modelSlotIndex`、设备=`serverInputDeviceId`/`serverOutputDeviceId`（服务端出声需 `enableServerAudio=1`）
- **无鉴权**：只靠 `--host` 绑定 + Origin 白名单（我们的令牌层反而是超前的一步）
- 已验证版本：master v2.2.2-beta（客户端库 1.0.182）

**坑**：① 键名按引擎类型分派（RVC 用 `tran`，so-vits 另有 `clusterInferRatio`/`noiseScale`，MMVC 用 `f0Factor`），一套键打不了所有引擎 ② **chunk（采样长度）没有 REST 键**（在客户端 AudioWorklet 里），纯服务端场景就是不支持 ③ 表单/JSON 混用，且 `/load_model` 成功时不回 info ④ 无鉴权 + Origin 校验两处规则不一致（REST 无 Origin 就放行，Socket.IO 会校验）⑤ **接口无版本化**——客户端仍在调 `GET /model_type`，而 master 已无此路由（404）

## 已完成（留档，勿重复）

- **修复**：遥控应用参数时**先停流**（避免 CUDA Graph 捕获期间 `empty_cache` 断言崩溃）
- **预设包**（`presets.json` + 面板 `01 预设`）：设备+模型+参数打包，一键换场景；已实测整包下发
- **模型「选中即加载」**：`03 模型` 下拉选中即切换（约 30 秒）
- 令牌鉴权（局域网入口）
- 模型库（别名 + 路径导入 + 上传导入 + 删除）
- 面板：滑块、字号档位、手机端布局、深色模式、6 套配色
- 断点续传、准备期进度页、环境探测与判定、bootstrap 骨架

## 未做（暂缓）

- 依赖清单与 RVC 官方 `requirements` 对齐
- 非 N 卡机器（DML/CPU）端到端验证
- `web_ui` 与 `control_server` 两条引擎路线统一（长期）
