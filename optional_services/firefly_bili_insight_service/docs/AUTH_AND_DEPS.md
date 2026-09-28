# 认证边界与依赖审计（2026-09-26）

本文记录服务真实能力边界的源码级结论与实测证据，供使用者与审计者参考。

## 结论速览

- **需要安装 B站桌面客户端吗？—— 不需要。**
- **需要登录 B站账号吗？—— 可选（匿名即可用全部四个 action）。**

## AUTH_CAPABILITY_MATRIX

登录态来源：且仅来自环境变量 / `.env`
（`BILIBILI_SESSDATA` / `BILIBILI_BILI_JCT` / `BILIBILI_BUVID3` /
`BILIBILI_BUVID4` / `BILIBILI_DEDEUSERID`，经上游 pydantic-settings 读取，
空 = 匿名）。服务不读浏览器 Cookie、不支持 cookies.txt、不弹扫码
（上游 `auth/bilibili_login.py` 的 QR 登录属于其 Web 应用，本服务未引用）。

| 能力 | 匿名 | 登录 | 说明 |
|---|---|---|---|
| health | ✅ | ✅ | 与登录无关 |
| metadata（公开视频） | ✅ 实测 PASS | ✅ | bilibili-api `get_info/get_tags` 匿名可用 |
| metadata 的 `has_subtitle_flag` | ⚠️ 不可靠 | ✅ | B站字幕接口要求登录；匿名实测返回 `has_subtitle_flag=false, subtitle_check_failed=true` |
| transcribe（本地 ASR） | ✅ 实测 PASS | ✅ | 下载公开视频流 + faster-whisper 本地推理，全程无需账号 |
| frame | ✅ 实测 PASS | ✅ | 同上 |
| 官方字幕内容 | ❌ 未接线 | — | 上游 `SubtitleService` 存在但本服务不调用；transcribe 永远走 ASR |
| 登录可见/会员视频 | ❌ | ✅ | bilibili-api Credential 生效范围 |
| 更高清视频流 | — | ✅（可选） | 服务下载策略固定选 ≈720p，登录不改变结果 |

`USER_LOGIN_REQUIRED = OPTIONAL`：匿名覆盖公开视频全部四个 action（2026-09-26
四项实测全 PASS）；登录只扩展字幕标志准确度与登录可见视频。

## Cookie 安全设计

- 服务只在**运行时**从环境变量 / 服务目录 `.env` 读取登录态；`.env` 已被
  `.gitignore` 排除。
- 代码路径无 Cookie 日志输出（审计：上游源码 grep
  `logger/print × sessdata/bili_jct/cookie` 零命中）。
- 上游 `auth/` 模块会把登录态写入其 Web 应用数据库——本服务不引用该模块，
  不落库、不存储。
- **禁止**把 `SESSDATA`/`bili_jct` 写进任何仓库、issue、截图或示例文件。
  示例文件只能出现空值键名（见 `.env.example`）。

## B站桌面客户端 / 浏览器依赖审计

- 无 Bilibili 桌面客户端依赖：全链路无 `bilibili.exe`、本地 App API、
  桌面客户端路径（源码 grep 零命中；`bilibili_api.utils.network.select_client`
  是 bilibili-api 库**内部 HTTP 客户端选择器**，与桌面应用无关）。
- 无浏览器自动化：selenium / playwright / DrissionPage 零命中。
- 无 yt-dlp：下载 = bilibili-api `get_download_url` 取 DASH 流地址 +
  ffmpeg 带 Referer 头远程切片（`services/video_download.py`）。

`BILIBILI_DESKTOP_APP_REQUIRED = NO`

## 运行时外部工具

| 工具 | 必需性 | 来源 | 发现方式 |
|---|---|---|---|
| ffmpeg | 必需（下载切片/音频提取/抽帧） | 官方构建（Windows 推荐 gyan.dev essentials 或 `winget install ffmpeg`） | `PATH`（`shutil.which`），无内置绝对路径 |
| ffprobe | 必需（随 ffmpeg 分发） | 同上 | `PATH` |
| Whisper 模型 | transcribe 必需 | HuggingFace `Systran/faster-whisper-small`（MIT 许可），**首次 transcribe 自动下载**，缓存于 `~/.cache/huggingface`（本机实测 464MB） | huggingface_hub 标准缓存；国内可设 `HF_ENDPOINT=https://hf-mirror.com` |

ASR 配置来自服务目录 `config.yaml`：`model=small, device=cpu, compute_type=int8,
language=zh`。注意：上游默认缺省是 `large-v3`，本服务刻意用 small 换取速度；
改 `config.yaml` 即可换模型，无需改代码。

## 与上游 BiliInsight 的关系（License 说明）

本仓库只包含**薄封装**（service.py + bili_compat.py）与文档；全部视频理解
能力来自上游 [Shanoa2/BiliInsight](https://github.com/Shanoa2/BiliInsight)
（GPL-3.0）。service.py 进程内 import 上游 `bilichat.services.*`，构成 GPL
意义上的衍生作品，故本仓库整体以 **GPL-3.0** 发布。上游源码不 vendor 进本仓库：
运行时通过 `BILIINSIGHT_SRC`（默认 `../BiliInsight/src`，兄弟目录克隆约定）定位。
`bili_compat.py` 是对 chromadb 的惰性 stub（上游零改动解耦）。

## 本仓库相对原始开发版的差异（去机器化）

原始开发版（作者本机）与本发布副本仅两处差异，均在 `service.py`：

1. `BILIINSIGHT_SRC` 默认值：作者机器上的一个绝对路径 →
   `SERVICE_DIR.parent / "BiliInsight" / "src"`（兄弟克隆约定，
   环境变量覆盖机制不变）。
2. 新增上游源码缺失时的 fail-fast：启动即向 stderr 打印克隆指引并退出码 2，
   而不是抛出难懂的 `ModuleNotFoundError`。

算法、协议、错误分类与原版逐字节一致。
