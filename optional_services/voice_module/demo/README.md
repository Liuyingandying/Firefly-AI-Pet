# 流萤语音体验 Demo

把一句话变成流萤（崩坏：星穹铁道）的声音并播放。

## 快速开始

```powershell
cd E:\Firefly_PageLens\voice\voice_module\demo

# 1) 编辑 input.txt (默认已有一句)
#    可选第一行声明情绪:
#      emotion: comfort
#      今天也辛苦了，要早点休息哦。

# 2) 生成并播放
python generate_voice.py
```

输出 `output.wav`（最新结果）并自动播放，同时归档到 `output\20260916_101525_comfort.wav`（日期_时间_情绪），每次结果都保留。

## 换文本 / 换情绪

```powershell
python generate_voice.py --emotion happy          # 指定情绪
python generate_voice.py --text "你好，我是流萤。"  # 临时文本 (不改 input.txt)
python generate_voice.py --no-play                # 只生成不播放
```

情绪省略时按文本关键词自动推断（如"辛苦""休息"→comfort）。可选: neutral / happy / excited / comfort / sad，
对应语速/变调/能量参数见 `..\config\emotion.yaml`。

## 环境要求

- 首次运行每个进程需加载模型 (~20-40s)，属正常冷启动; 低延迟体验请用常驻服务 `..\api\server.py`
- 需要 GPU (RTX 4060 验证通过)、edge-tts 可访问外网
- 当前目录结构: `input.txt` → `generate_voice.py` → `output.wav` + `output/` 归档
