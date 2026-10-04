# RVC-ONNX-Realtime

把 RVC 实时变声接到 **ONNX Runtime** 上，绕开 `torch-directml` 的性能坑，并横向对比
**CPU / 独显(DML) / 核显(DML)** 三种后端的真实耗时。

> 背景：在 RVC-WebUI（2.3，带 CUDA Graph 的那版）里用 DirectML 跑核显时，每 60ms 音频块要
> 11 秒，比实时慢 180 倍。查下来**锅不在核显算力，在 `torch-directml` 这个运行时**——
> 大量算子回退 CPU、每算子开销巨大、不支持 fp16。换 ONNX Runtime 后同样硬件快 10~25 倍。

---

## 一、实测数据

单位：每次调用（处理一个音频块）的耗时。硬件：i5 + Intel UHD 730 核显 + RTX 2060 SUPER。

### 生成器（合成网络）

| 块长 | CPU | DML (2060) | DML (核显 UHD730) | torch-DML (核显，对照) |
|---|---|---|---|---|
| 0.25s | 80 ms | 17 ms | 288 ms | 183 ms |
| 0.50s | 151 ms | 21 ms | 566 ms | 590 ms |

### hubert（特征提取）

| 输入长度 | CPU | DML (2060) | DML (核显) |
|---|---|---|---|
| 0.5s | 23 ms | 9 ms | 57 ms |
| 1.0s | 37 ms | 14 ms | 97 ms |
| 2.0s | 67 ms | 18 ms | 186 ms |

### 整条流水线（0.5s 块 + 0.25s 上下文 + pm 音高）

| 后端 | 耗时 | 判断 |
|---|---|---|
| CPU | 537 ms/块 | 比实时慢约 7%，会逐渐落后（可优化） |
| 2060-ONNX | 36 ms/块 | 14 倍实时余量，GPU 占用约 7% |
| 核显 | 不可用 | 单生成器就 566ms，直接出局 |

**结论：核显（UHD 730）跑 RVC 实时不成立**（比 CPU 还慢 3.6 倍）；
真正有意义的是 **2060 走 ONNX**（最快、占用小）或 **CPU 走 ONNX**（完全不碰显卡）。

---

## 二、文件

```
onnx_rt.py                   实时工具（后端可切，含离线自测）
tools/export_hubert.py       导出 hubert -> ONNX（动态长度）
tools/export_gen.py          导出生成器 -> ONNX（固定块长）
tools/bench_matrix.py        三后端 × 多尺寸 基准矩阵
tools/bench_pipeline.py      整条流水线基准（可指定 extra/设备）
tools/bench_net_isolated.py  单独量生成器（torch 版，用于对照）
tools/bench_hubert_isolated.py 单独量 hubert（torch 版，用于对照）
presets/                     实时界面(realtime_gui)的参数预设 + 启动器
patches/                     对 RVC-WebUI 源码的三处补丁
launcher/                    一键启动脚本（放到 RVC 根目录用）
```

## 三、用法

依赖（装在 RVC 的 venv 里即可）：

```bash
pip install onnx onnxruntime-directml soundfile librosa
```

导出模型（首次运行 `onnx_rt.py` 会自动导出；也可手动）：

```bash
python tools/export_hubert.py
python tools/export_gen.py
```

离线自测（不出声，只报耗时）：

```bash
python onnx_rt.py --selftest --ep cpu --block 0.5 --ctx 0.25 --f0 pm
```

实时（麦克风 → 扬声器）：

```bash
python onnx_rt.py --ep dml0 --block 0.5 --ctx 0.25 --f0 pm
```

参数：

| 参数 | 说明 |
|---|---|
| `--ep` | `cpu` / `dml0`(独显) / `dml1`(核显) / `cuda`(需 onnxruntime-gpu) |
| `--block` | 块长秒数，实时要求「单次耗时 ≤ block」 |
| `--ctx` | 上下文秒数（给特征/音高的历史），越小越省 |
| `--f0` | `pm`（parselmouth，快）或 `fcpe`（torch，较慢较好） |
| `--threads` | 限制 ORT 计算线程数（给游戏/其他程序留核） |
| `--in` / `--out` | 输入/输出设备：**序号**或**名称关键词**（如 `"CABLE Input"`、`"USB Audio"`） |
| `--in-sr` / `--out-sr` | 设备采样率，`0` = 用设备默认；**不等于 16k/48k 时自动重采样** |
| `--gain` | 输出增益 |
| `--list-devices` | 列出所有音频设备（带序号），选设备前先跑这个 |

### 接虚拟声卡（给 OBS 直接捕获）

```
1) 装 VB-Audio Virtual Cable（免费，装完重启）
     -> 系统出现 "CABLE Input"(播放) / "CABLE Output"(录音)
2) 本工具：  --out "CABLE Input"
     （要听自己说话就再加一路：用 VoiceMeeter，或 OBS 里开监听）
3) OBS：音频输入设备 -> 选 "CABLE Output"
```

麦克风建议用 **WASAPI** 那一项（通常 48k，稳）；用 MME 的 44.1k 也行，工具会自动重采样到 16k。

## 四、补丁（相对 RVC-WebUI 2.3）

| 补丁 | 文件 | 作用 |
|---|---|---|
| 01 | `infer/module/models.py` | `_f02sine` 里 `F.pad(rad_acc,(0,0,1,-1))` 正负混合 padding，DirectML 不支持；改为等价 `cat` 写法 |
| 02 | `configs/config.py` | DirectML 适配器选择：新增环境变量 `RVC_DML_DEVICE`（序号或名称，如 `Intel`）。**不设的话默认用 0 号适配器，通常是独显**，会误以为在跑核显 |
| 03 | `realtime_gui.py` | 保存配置时漏了 `formant`（性别因子/声线粗细），导致每次重开都丢；已补上 |

> 补丁用 `git diff` 生成，打之前请确认上游版本一致。

## 五、坑与注意

1. **DirectML 默认适配器是 0 号**（多数机器上是独显）。想跑核显必须显式指定 `device_id=1`
   （ORT）或 `RVC_DML_DEVICE`（torch）。否则所有"核显"测试其实是独显跑出来的。
2. **ONNX 是固定形状**：生成器的块长/上下文在导出时写死，改了要重新导出。
3. 装 `onnxruntime-directml` 会把 `numpy` 顶到 2.x（本机 torch 2.4.1 实测仍可用）。
4. hubert 导出约 360MB、生成器约 110MB，**不要提交进仓库**，用脚本现场导出。
