# Z Code Interactive TUI Environment Recovery

Date: 2026-09-27. Scope: read-only audit of the installed Z Code runtime and its official TUI distribution. No Firefly architecture, Z Code installation, user configuration, skill, MCP setting, or session DB was modified.

## Decision

The installed **Z Code Desktop 3.14.3** intentionally ships an Agent `app-server` bundle without the TUI runtime. Its bundled CLI is useful for the proven headless path, but it is not a complete standalone interactive TUI installation. Reinstalling the same Desktop release is not a supported repair for this missing package. A separate official CLI distribution is described in the [Z Code v3.14.3 repository README](https://github.com/zai-org/ZCode/blob/v3.14.3/README.en.md), but no such distribution is installed on this machine and no usable official Windows CLI installer was established in this audit. Therefore the independent TUI gate and Firefly E2E remain blocked.

## 1. Installed files and `app.asar`

Installed root: `C:\Users\FAJ\AppData\Local\Programs\ZCode`. The install manifest has 1,074 entries, including exactly one `resources\glm\zcode.cjs` and **zero** `@zcode\tui` entries. `resources\glm` contains `zcode.cjs`, `.node-bundle-meta.json`, and 16 plugin/skill package directories; it has no `node_modules\@zcode\tui`, TUI bundle, or `tui.cjs`. The installed `resources\app.asar` index contains 27,068 files; it has no `node_modules/@zcode/tui/`, TUI entry bundle, CLI distribution `zcode.mjs`, or lockfile. `app.asar.unpacked` also has no `@zcode/tui`. This is package absence, not an existing package that Node fails to resolve.

The local `.node-bundle-meta.json` identifies `runtime: electron-node`, `entry: zcode.cjs`, `platform: win32-x64`, and source `apps/zcode-cli/packages/cli/dist/zcode.cjs`. The installed `app.asar` root `package.json` identifies `@zcode/desktop`, version `3.14.3`, and `out/main/index.js`; it does not declare `@zcode/tui` as a Desktop dependency.

## 2. CLI metadata and import path

The local CLI reports `0.16.9`. Its bundled `loadTuiRuntime` function checks `node:sea`: with a normal `node.exe` process, it does `import("@zcode/tui")`; only a Node Single Executable Application (SEA) reads an embedded `zcode-tui-runtime/manifest.json`, verifies asset hashes, extracts the runtime, and imports `node_modules/@zcode/tui/dist/index.js` from the SEA cache. The local `E:\Node\node.exe` reports `isSea() == false`, so the embedded-resource branch is unavailable.

At the matching official [v3.14.3 CLI package metadata](https://github.com/zai-org/ZCode/blob/v3.14.3/apps/zcode-cli/packages/cli/package.json), `@zcode/tui` is a regular **workspace dependency**, not an optional or peer dependency. The [TUI package metadata](https://github.com/zai-org/ZCode/blob/v3.14.3/apps/zcode-cli/packages/tui/package.json) marks it `private: true` and exports `./dist/index.js`. The local desktop bundle retains the dynamic import but does not carry that workspace package.

## 3. Version and installation consistency

`ZCode.exe` reports product/file version `3.14.3.7762`; its Authenticode signature is valid and names 北京智谱华章科技股份有限公司. The installed `app.asar` reports Desktop `3.14.3`. `ZCode.exe`, `app.asar`, `zcode.cjs`, and `resources\config\provider\zcode-builtin.json` all have source modification time 2026-09-22 and install creation time around 2026-09-24 23:53. Windows uninstall registration says `ZCode 3.14.3`. The CLI's `0.16.9` is its own version track; the matching official Desktop build script embeds the Agent CLI for `app-server`. No evidence shows that the previously mentioned Desktop `3.12.3` cache is mixed into this installed root.

The decisive packaging evidence is the official [v3.14.3 Desktop Agent bundle script](https://github.com/zai-org/ZCode/blob/v3.14.3/packages/desktop/scripts/prepare-agent-node-bundle.mjs): it explicitly says the Desktop `app-server` path does not load `@zcode/tui`, so TUI is not bundled. The manifest and files match that packaging rule. This is **not established as installation damage or an update residue**. A same-version Desktop Repair/reinstall would be expected to reproduce the same package layout; none was performed.

## 4. Official entrypoints

The live Desktop process command line is `ZCode.exe ...\resources\glm\zcode.cjs app-server --stdio --surface desktop`. The installed `app.asar` main bundle also contains `spawnArgs: ["app-server", "--stdio"]`. Desktop uses its Electron UI and Agent app-server; it does not start an interactive CLI TUI for the user. `ZCode.exe` is not a CLI wrapper.

The official [Z Code CLI distribution documentation](https://github.com/zai-org/ZCode/blob/v3.14.3/README.en.md) describes a **separate** command-line distribution containing TUI, Web, and Agent, with a `zcode` command and `bin/zcode.mjs` launcher. It describes building this distribution with `pnpm build:zcode` and packaging a runtime tarball. On this machine, `where.exe zcode` found no command; `~/.zcode/runtime`, `~/.local/bin`, and the SEA asset cache are absent. No official installed `zcode.mjs` or other TUI entrypoint was found. The current Desktop root's `zcode.cjs` is therefore not a complete official interactive entrypoint.

The [official Desktop installation guide](https://zcode.z.ai/en/docs/install) offers the Desktop installer; it does not establish that reinstalling Desktop supplies the separate CLI/TUI runtime. A public [Z.ai feedback issue documenting the same Desktop-bundled CLI failure](https://github.com/zai-org/feedback/issues/270) is consistent with the local and official build-script evidence, but the conclusion here rests on the installed files and official source.

## 5. Same-environment module diagnosis and independent TUI gate

The probe used the production Node executable `E:\Node\node.exe`, the installed `zcode.cjs`, the course workspace `C:\Users\FAJ\AppData\Local\FireflyAI\learning\courses\crs-d91e981f5a92\workspace`, and both official provider-config env vars. `node:sea.isSea()` returned false; `NODE_PATH` was absent. `createRequire(zcode.cjs).resolve.paths("@zcode/tui")` started at `resources\glm\node_modules` and walked its ordinary ancestors. `createRequire(zcode.cjs).resolve("@zcode/tui")` returned `MODULE_NOT_FOUND`. No official loader, package map, or environment variable in this Desktop run supplied the missing module.

Directly executing `node zcode.cjs --resume sess_0d75c470-cacd-465f-99e3-c319f4c81af4` from the course workspace, with both provider env vars set, exited **1** with `Error: Cannot find package '@zcode/tui' imported from ...\resources\glm\zcode.cjs`. The session reference remains in Z Code's DB, but no interactive TUI appeared. A user-visible message could not be entered or answered, so the independent TUI gate **failed**. Firefly's final E2E was correctly not rerun.

## Recovery choice and eight required answers

| Question | Answer |
|---|---|
| 1. Does `@zcode/tui` exist in the current installation? | No: absent from the manifest, filesystem, `app.asar`, and `app.asar.unpacked`. |
| 2. Why cannot `zcode.cjs` find it? | Normal Node execution takes a dynamic import path; the Desktop app-server bundle does not include the workspace TUI package. The SEA extraction path does not apply to `node.exe`. |
| 3. Are Desktop and CLI versions consistent? | Desktop 3.14.3 and bundled CLI 0.16.9 have separate version numbers; file times, installed metadata, and the matching official build script support one consistent Desktop install. No 3.12.3 mix was found. |
| 4. Is this installation damage? | Not on current evidence. The official Desktop packaging rule explicitly omits TUI. |
| 5. What is the official TUI entrypoint? | The separate CLI distribution's `zcode` command / `bin/zcode.mjs`, documented by the official repository; it is not installed here. Desktop's actual entry is `app-server`. |
| 6. Is Desktop Repair/reinstall needed? | No. Same-release reinstall would not supply a package intentionally omitted from Desktop packaging. No installer was run and no user-data backup was needed. |
| 7. Was independent `--resume` visually interactive? | No. It exited 1 on missing `@zcode/tui`, before a TUI could accept input. |
| 8. Did Firefly open the correct course TUI? | Not in this phase. The independent gate failed; Interactive Learning remains blocked. |

No package was installed or copied, no official source was rebuilt or mixed into the Desktop install, and no Firefly code was changed. The existing session DB, `firefly-learning` skill, config, and MCP settings were left intact. Recovery requires an official, complete interactive CLI distribution compatible with this session format, followed first by an independent visual/input gate and only then the Firefly course E2E.

ZCODE_TUI_UNSUPPORTED
