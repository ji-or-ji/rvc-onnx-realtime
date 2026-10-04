# 配方（recipe）

一份配方 = **参数 + 模型引用 + 后端 + 环境要求**。bootstrap 按机器体检结果挑一份来用。

## schema

```jsonc
{
  "id": "torch-cuda",                 // 唯一标识，文件名建议一致
  "label": "高档 · torch-CUDA（…）",   // 给人看的名字
  "backend": "torch-cuda",            // torch-cuda | ort-dml | ort-cpu
  "desc": "说明文字",
  "requirements": {
    "tier": ["high"],                 // 适配哪些档位：high | mid | low
    "min_vram_mb": 4096,
    "platform": ["win", "linux"]      // win | linux | darwin
  },
  "deps": { "profile": "torch-cuda" },// 依赖档位名，bootstrap 内部映射到具体包
  "model":    { "file": "assets/weights/x.pth",   "url": "", "sha256": "" },
  "index":    { "file": "assets/indices/x.index", "url": "", "sha256": "" },
  "f0_model": { "file": "assets/rmvpe.pt",        "url": "", "sha256": "" },
  "params": {                         // ★ 一律使用 config.json 的契约字段名
    "threhold": -60.0,                //   注意原版就拼作 threhold，不要改
    "pitch": 12.0,
    "formant": -1.75,
    "rms_mix_rate": 0.7,
    "index_rate": 0.28,
    "block_time": 0.06,
    "crossfade_length": 0.04,
    "extra_time": 2.0,
    "f0method": "rmvpe"
  }
}
```

## 规则

- `params` **只用契约字段名**（见 `configs/config.json`）。各家引擎的差异由适配层翻译，例如：
  - ONNX 引擎：`block_time → block`、`extra_time → ctx`；`f0method=rmvpe` 目前降级为 `pm`
- 模型三件套（model / index / f0_model）`url` 为空表示**不自动下载**：
  - 本地已有该文件 → 直接用
  - 本地没有 → 提示用户自行放置
- 要发配方给别人的话，把 `url` + `sha256` 填上，bootstrap 就会自动下载并校验。

## 平台说明

仓库**不打包模型**（第三方模型涉及声音版权）。分发时只放链接与校验值。
