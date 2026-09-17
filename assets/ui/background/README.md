# background/ — 控制台背景资源

## 自动加载规则

`ui/theme.py::v2_background_style()` 按以下优先级生成控制台背景：

1. 本目录存在 `background.png` 时 → 以 `border-image` 平铺为窗口背景；
2. 不存在时 → 回退为代码绘制的深空蓝紫渐变（`theme.V2.GRADIENT_STOPS`）。

## 约束

- 文件名固定为 `background.png`（代码按固定名探测，不硬编码绝对路径）。
- 建议尺寸 ≥ 1280×800，深空蓝紫低饱和风格。
- 请勿放入版权素材；替换图片自备授权。
- 删除本目录下图片即可恢复默认渐变。
