# models/ — Voice Models（不随仓库分发）

**为什么这里没有模型文件？**

1. `firefly-chinese.pth` / `firefly-chinese_v2.index` 是第三方训练的
   《崩坏：星穹铁道》流萤角色声模型（AGPL-3.0，角色语音版权归 miHoYo/HoYoverse）——
   不得在任何 git 托管平台再分发；
2. HuBERT / RMVPE 权重体积大（~370MB）且应由官方渠道获取。

所有资产的元数据（放置位置、大小、**SHA256**、来源、许可证状态）都在
[`manifest.json`](manifest.json) 中逐项登记。

## 必需模型（4 个）

| 文件 | 放到（相对 voice_module 根） | 大小 | 来源 | 许可证状态 |
|---|---|---|---|---|
| `firefly-chinese.pth` | `models/firefly/` | 55,227,813 B | [Waterwzy/RVC-firefly-finetuning](https://huggingface.co/Waterwzy/RVC-firefly-finetuning) | AGPL-3.0 + 角色声版权，**UNKNOWN_DO_NOT_PUBLISH** |
| `firefly-chinese_v2.index` | `models/firefly/` | 31,588,619 B | 同上（与 .pth 成对） | 同上 |
| `hubert_base.pt` | `models/` | 189,507,909 B | [lj1995/VoiceConversionWebUI](https://huggingface.co/lj1995/VoiceConversionWebUI)（官方 RVC 资产） | DOWNLOAD_ONLY |
| `rmvpe.pt` | `models/` | 181,184,272 B | 同上 | DOWNLOAD_ONLY |

完整 SHA256 见 `manifest.json`。本目录保留的 `firefly/LICENSE`（AGPL-3.0 全文）
与 `firefly/README.md`（模型卡）是原始模型仓库随附的许可合规文档。

## 校验

```powershell
# PowerShell
Get-FileHash .\models\firefly\firefly-chinese.pth -Algorithm SHA256
```

```bash
# 或一键（检查全部 4 个必需模型的 SHA256）
python scripts/check_environment.py --models-only
```

哈希与 `manifest.json` 不一致 = 下载损坏，不要带病启动。

## 缺模型时会发生什么

服务**启动即失败，不会静默降级**：lifespan 加载说话人时抛出

```
FileNotFoundError: 说话人 firefly 模型不存在: models\firefly\firefly-chinese.pth
```

（uvicorn 进程退出，`GET /health` 无响应。）把文件放到位后重新启动即可。
