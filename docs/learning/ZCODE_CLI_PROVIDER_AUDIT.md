# Firefly Learning Bridge — Z Code CLI Provider Config Audit

日期：2026-09-27
范围：仅 Z Code CLI 的真实安装/启动方式。未触碰 teach-mcp / firefly-learning skill 业务 / Memory Ownership / Resource Manifest / curriculum / Firefly UI wiring。
前置链路状态：Firefly 主界面学习模式 → LearningBridgeDialog → launch_learning_mode() 已全部打通；唯一断点是 zcode-cli 启动即退（exit 1，provider config 定位失败）。

---

## 1. zcode-builtin.json 实际存在吗？

**存在，且内容完整**（官方随桌面安装写入，非缺失文件）：

```
C:\Users\FAJ\AppData\Local\Programs\ZCode\resources\config\provider\zcode-builtin.json
大小 188,476 字节，LastWriteTime 2026/9/22 11:20:48
```

## 2. 如果存在，真实路径是什么？

如上。同目录相关布局（实测）：

```
<installRoot> = C:\Users\FAJ\AppData\Local\Programs\ZCode
<installRoot>\ZCode.exe                                  （Electron 桌面壳）
<installRoot>\resources\config\provider\zcode-builtin.json   ← 官方 builtin provider config
<installRoot>\resources\config\default.json
<installRoot>\resources\glm\zcode.cjs                    ← CLI 入口（14.8MB bundle）
<installRoot>\resources\glm\packages\...                 （插件包）
C:\Users\FAJ\.zcode\v2\provider_config.json              ← personal provider config（默认位置）
C:\Users\FAJ\.zcode\v2\runtime\provider\windows-x86_64\3.12.3\endpoint-78d7…\zcode-builtin.json
                                                         ← 桌面端刷新的 active release 缓存（版本目录 3.12.3）
```

另：用户桌面端将数据根重定向到了 `ZCODE_DATA_BASE_DIR=D:\z code 使用\数据库`（其活跃的 v2 数据在 D 盘）。

## 3. Firefly 此前实际启动了什么？

`%LOCALAPPDATA%\FireflyAI\logs\learning_entry.log`（2026-09-27 02:02:44，用户真实点击）：

```
[ZCODE_LAUNCH] command=C:\Users\FAJ\AppData\Local\Microsoft\WindowsApps\wt.exe
  -d C:\Users\FAJ\AppData\Local\FireflyAI\learning\courses\crs-d91e981f5a92\workspace
  E:\Node\node.EXE
  C:\Users\FAJ\AppData\Local\Programs\ZCode\resources\glm\zcode.cjs
  pid=8672 mode=interactive
```

结构：`wt.exe`（新终端窗口，cwd=课程 workspace）→ `node <installRoot>\resources\glm\zcode.cjs`（无参数 → TUI）。环境变量 = Firefly GUI 进程的环境**原样继承**（`dict(os.environ)`，含新增 `FIREFLY_LEARNING_CONTEXT`）——这是关键：Firefly 是从开始菜单正常启动的，进程环境里**没有任何 `ZCODE_*` 变量**。

## 4. 为什么 provider config 定位失败？

三层事实叠加（源码反混淆 + 双向复现实证）：

**(a) CLI 的解析逻辑**（`zcode.cjs` 内 `resolveBundledZCodeBuiltinProviderConfig`，`prepareCliProviderRuntimeEnv` 调用）：

```js
// dQi = prepareCliProviderRuntimeEnv：
t = env[ZCODE_BUILTIN_PROVIDER_CONFIG_FILE]; n = env[ZCODE_PERSONAL_PROVIDER_CONFIG_FILE]
o = dataBaseDir ?? env.ZCODE_DATA_BASE_DIR ?? homedir()
if (t && n) return { builtin: t, personal: n };          // ← 两个 env 都有 → 直接短路成功
s = t ?? fQi({ entrypoint: process.argv[1], ... })        // ← 否则按入口目录推导候选
// fQi 候选：
//   ① join(dirname(entrypoint), "provider", "zcode-builtin.json")
//   ② resolve(dirname(entrypoint), "../../../../../config/provider/zcode-builtin.json")
// 全不存在 → throw「无法定位 CLI ZCode Built-in Provider Config：① 以及：②」
```

**(b) 布局不匹配**：以 `resources\glm\zcode.cjs` 为入口，候选 ① = `resources\glm\provider\...`（不存在），候选 ② ≈ `C:\Users\FAJ\AppData\config\provider\...`（不存在）——而真实文件在 `resources\config\provider\`，两个候选都到不了。该推导是按 CLI 独立发行布局写的，与桌面安装布局偏差一层目录。

**(c) 为什么此前所有验证都"碰巧"成功**：桌面 ZCode.exe spawn 子进程时会注入全套 `ZCODE_*` 环境变量（实测含 `ZCODE_APP_VERSION=3.14.3`、`ZCODE_BUILTIN_PROVIDER_CONFIG_FILE`、`ZCODE_PERSONAL_PROVIDER_CONFIG_FILE`、`ZCODE_DATA_BASE_DIR`、`ZCODE_RG_BINARY` 等）。我的审计/PoC 都在桌面端拉起的 shell 里执行——CLI 读到两个 provider env 直接短路，从未走过失败的候选推导。**Firefly 是干净环境，继承不到，于是暴露**。双向复现：清除 env 跑 `-p` → EXIT=1 报同一错误；注入两个 env → EXIT=0 正常响应。

附带澄清：`--help` 与 `-p` 成功不是"headless 不需要 provider"——`requiresProviderRuntime` 对 TUI 和 `-p` 都返回需要；差异只在**执行环境是否带着桌面端注入的 env**。

## 5. Z Code 是否有正式 CLI wrapper？

**当前没有**（实测）：

- `where.exe zcode` / `where.exe zcode-cli`：无结果；`Get-Command zcode`：无。
- 安装目录可执行文件仅 `ZCode.exe`（Electron 桌面壳，不接受 `--prompt/--cwd` 语义）与 `Uninstall ZCode.exe`；无任何 `zcode.exe / *.cmd / *.bat` CLI wrapper。
- 即 `resources\glm\zcode.cjs` 就是当前发行形态的官方 CLI 入口——桌面端内部也是运行它（叠加 env 注入）。

## 6. 最终采用哪种启动方式？

**优先级 A（wrapper）不成立 → 采用优先级 B：继续 `node zcode.cjs` + 注入 CLI 官方环境变量。** launcher（`learning/launcher.py`）新增：

- `_ensure_zcode_provider_env(env, cli_path)`：当环境缺失时，按安装布局**运行时推导**并注入
  - `ZCODE_BUILTIN_PROVIDER_CONFIG_FILE = <installRoot>\resources\config\provider\zcode-builtin.json`
  - `ZCODE_PERSONAL_PROVIDER_CONFIG_FILE = <ZCODE_DATA_BASE_DIR | ~>\.zcode\v2\provider_config.json`
  两个官方 env 都存在时 CLI 短路成功；若父环境已带（如未来从 ZCode 感知的 shell 启动）则原样透传；若推导出的文件不存在则不注入——让 CLI 报它自己的结构化错误，不做进一步猜测。
- `find_official_wrapper(cli)`：每次启动重新探测官方 wrapper（installRoot 下 `zcode.exe/zcode.cmd/...`、PATH 上 `zcode/zcode-cli`）——**当前返回 None**；未来官方一旦提供 wrapper，launcher 自动切换到优先级 A，无需再改代码。

禁止项自查：未复制任何 provider 文件进 Firefly 仓库；未在代码写任何机器特定路径（全部布局推导）；未修改 zcode.cjs；无任何静默 fallback 到旧 Learning Mode。

## 7. PowerShell 独立 CLI 是否 exit 0？

**是（三段式验证，全部脱离 Firefly）：**

| 场景 | 结果 |
|---|---|
| 干净 env（移除全部 ZCODE_*）+ `-p "只回复OK"` | **EXIT=1**，报出与用户一致的错误（候选①②原文）→ 复现成立 |
| 同上 + 注入两个官方 env | **EXIT=0**，输出「OK」 |
| 注入 env + `-p` + `--cwd <课程workspace>` | **EXIT=0**，输出「OK」 |

`~/.zcode/skills/firefly-learning/` 的可发现性与 provider env 无关（skill 固定位于用户主目录 `.zcode/skills`，v0.1 PoC 已实证接管）；provider env 修复后 CLI 能活到加载 skill 的阶段。

## 8. Firefly 真实学习入口是否最终进入 Skill？

**链路已全部就绪，待用户重启 Firefly 后人工确认最后一步。** 修复后从 Firefly 启动的 CLI 将带两个 provider env → 正常启动 → 与 v0.1 PoC 相同的接管链（firefly-learning skill 读取 Learning Context → teach-mcp 调用）。自动化侧已验证到的能力：launcher argv/env 构造（回归测试）、provider env 注入（5 项新测试 + 真实安装布局断言）、CLI 独立可运行。人工验收序列：完全退出 Firefly → 重启 → 学习模式 → 选课程 → 继续学习 → 观察新终端窗口出现 zcode TUI/响应（而非 provider 报错退出）→ `learning_entry.log` 的 `[ZCODE_LAUNCH]` 与课程 `bridge/launch.log` 中 skill 接管输出。

---

## 回归与测试

- 新增 `tests/test_zcode_cli_provider_env.py`（5 项）：布局推导注入（含 `ZCODE_DATA_BASE_DIR` 重定向）、父 env 已提供时透传、布局不完整时不注入（保留 CLI 结构化错误）、**真实安装布局断言**（本机 derived 路径必须真实存在）、wrapper 存在时优先 + 移除后回落。
- 全部桥接测试面：`test_learning_bridge.py` 25、`test_learning_bridge_memory_guard.py` 8、`test_learning_entry_wiring.py` 5、`test_learning_dialog_runtime.py` 3、`test_zcode_cli_provider_env.py` 5 → **54 passed**，`py_compile` 通过。
- `learning_entry.log` 中的 `[ZCODE_LAUNCH] command=...` 记录与本次修复后 argv 一致（修复只改 env 与 wrapper 探测，不改变命令形态）。

## 结论（8 问速答）

1. 存在（188KB，官方随桌面安装写入）。
2. `<installRoot>\resources\config\provider\zcode-builtin.json`。
3. `wt.exe -d <workspace> node <installRoot>\resources\glm\zcode.cjs`（interactive TUI），env=Firefly 干净进程环境。
4. CLI 按 entrypoint 目录推导 bundled 候选（与桌面安装布局差一层），且干净环境缺少桌面端注入的 `ZCODE_BUILTIN_PROVIDER_CONFIG_FILE`/`ZCODE_PERSONAL_PROVIDER_CONFIG_FILE` 短路变量；此前所有验证都因继承桌面 env 而碰巧成功。
5. 当前无官方 wrapper（PATH/安装目录均无）；`zcode.cjs` 即官方 CLI 入口。
6. 优先级 B：保持 `node zcode.cjs` + launcher 注入两个官方 env（布局运行时推导）+ `find_official_wrapper` 持续探测以便未来自动升级到优先级 A。
7. 是——注入后 `-p` 与 `-p --cwd <workspace>` 均 exit 0（干净环境复现 exit 1 亦已实证）。
8. 全链就绪：待重启 Firefly 真实点击验收（验收要点见 §8）。
