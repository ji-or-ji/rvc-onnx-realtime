# 愿景：开箱即用的实时变声（RVC-ONNX-Realtime 产品化）

写于 2026-10-02。这份文件是"目标真源"，冲突时以它为准。

## 一句话

下载仓库 → 点一个脚本 → 自动探测环境 → 自动建环境 → 选配方 → 拉起 WebUI → 出声。

## 四层架构（复用现有资产，不重造）

```
契约层  configs/config.json        15 个字段，唯一参数真源（已存在）
配方层  recipes/*.json             = 参数 + 模型 + 索引 + f0模型 + 后端 + 环境要求（扩展，不是替换）
引擎层  可切换后端                  A: torch-CUDA（参数齐、最快）  B: ONNX（轻、跨平台）
遥控层  bootstrap 脚本 + Web 面板    探测/建环境/选配方/起服务；面板只当遥控器
```

现有资产：`configs/config.json`（契约）、`presets/*.json`（配方雏形）、`presets/run_preset.ps1`（启动器雏形）、`realtime_gui.py` 引擎。

## 核心矛盾（必须先决）

1. **开箱即用 ↔ 体积**：torch+CUDA 一装 3GB+；ONNX runtime 只 ~200MB。
2. **参数齐/快 ↔ 补坑**：torch 引擎现成（含 rmvpe、index 检索、降噪）；ONNX 要一项项移植。

→ 解法：**引导式，按机器选后端与配方**，而不是做一个万能包。

### 已决（2026-10-02）：不预制

仓库**不带** torch / onnxruntime / 模型，只带「代码 + 契约 + 配方定义 + bootstrap」。
重型依赖全部在用户机器上按探测结果**成套下载**。因此仓库体积恒定，安装体积取决于机器档位。

**版本成套配对规则（血泪教训）**：绝不能装 latest，必须成套匹配。
- 本机实测：CUDA 11.8 + cuDNN 8.9，而 `onnxruntime-gpu 1.18` 要 CUDA 12 + cuDNN 9
  → CUDA EP 创建失败并**静默回退 CPU**（最恶心的一种故障）
- 选 ORT-CUDA → 同时装匹配的 `nvidia-cudnn-cu12` + runtime，或用 1.17.x 配 CUDA 11.8
- 选 torch-CUDA → 先读 `nvidia-smi` 的驱动上限，再决定 cu118 / cu121 / cu124 的 index-url

- NVIDIA + 磁盘充裕 → torch/ORT-CUDA 配方（参数齐、最快）
- AMD / Intel / 无独显 → ONNX 配方（轻、慢一点）
- 追求最小体积 → ONNX 全家桶

## 最低要求档位（草案，待实测校准）

- 无独显 / 老核显：ONNX-CPU，block 0.25~0.5，延迟 0.5~1s，能用但顿
- NVIDIA GTX10 系以上：CUDA（torch 或 ORT-CUDA），block 0.10~0.15，延迟 0.2~0.3s
- AMD / Intel 新驱动：ONNX-DML，block 0.2~0.3
- 显存 < 4GB：只走 CPU / 小模型

## bootstrap 脚本职责

1. **探测**：OS / Python / GPU 厂商型号显存 / 内存 / 磁盘 / 音频设备（有无虚拟声卡）
2. **判定**：能否跑、跑哪一档、推荐配方
3. **建环境**：venv + 依赖（国内镜像；断点续传）
4. **取模型**：按配方下载 + sha256 校验（模型不塞进仓库）
5. **起服务**：web_ui + 打开浏览器
6. **可回滚**：记录已执行步骤，失败能重来

## 配方 schema（草案）

```json
{
  "id": "wolf-o3-fast",
  "label": "御风 o3 · 快跟随",
  "backend": "ort-cuda | torch-cuda | ort-dml | ort-cpu",
  "requirements": { "min_vram_mb": 4096, "platform": ["win", "linux"] },
  "model":    { "file": "assets/weights/x.pth",   "url": "...", "sha256": "..." },
  "index":    { "file": "assets/indices/x.index", "url": "...", "sha256": "..." },
  "f0_model": { "file": "assets/rmvpe.pt",        "url": "...", "sha256": "..." },
  "params":   { "block_time": 0.15, "extra_time": 1.0, "f0method": "rmvpe" }
}
```

字段名与 config.json 保持一致（含原版的拼写 `threhold`，不能改）。

## 待决问题（阻塞动手）

1. **平台范围**：Windows 优先、代码留 Linux/Mac 口子？（默认假设）
2. ~~体积上限~~ → **已决：不预制，按探测结果成套下载**
3. **模型分发**：第三方模型（如 julesbrown，是别人的声音）只存「链接 + sha256」、不打包？
4. ~~是否允许联网下载~~ → **已决：允许，首次启动即下载**（pip / HF 配国内镜像）
5. **配方维护**：随仓库发一批，还是主要让用户自己加？

## 阶段

- **S1 骨架**：契约确认 + bootstrap（探测/建环境）+ web_ui 起服务 + 配方=参数+模型引用（先复用现有引擎）
- **S2 后端可切**：web_ui 支持选后端；ONNX 侧补 rmvpe
- **S3 分发**：一键脚本（bat/sh）、国内镜像、断点续传、校验、失败回滚
- **S4 界面**：设计 skill 到手后统一重做（届时参数全摆齐）
