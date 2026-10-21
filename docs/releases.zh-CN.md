# 干净构建与安装验证

这是贡献者的发布前检查，不是模型试跑，也不会部署或重启正在使用的工作台。

## 准备与执行

需要 Python 3.11+ 和 pip。构建使用 setuptools；它是开发工具，不是 Cater 的运行依赖。项目采用 SPDX 许可证声明，构建后端最低版本按 [Python Packaging 官方指南](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/) 设为 77.0.3。

在可信开发环境中先准备构建工具。这一步可能访问包索引：

```bash
python -m pip install "setuptools>=77.0.3"
```

在仓库根目录执行：

```bash
python scripts/build_release.py
```

默认在 `dist/releases/` 下生成独立目录，也可指定一个**尚不存在**的目录：

```bash
python scripts/build_release.py --output dist/releases/candidate-01
```

脚本不会自动安装缺失的构建工具，不使用包索引下载构建或运行依赖，不接入模型。子进程使用最小环境变量，并显式禁用 pip 配置文件，避免机器已有的安装选项混入。仓库已经包含编译好的网页；修改前端后，应先按 [前端说明](../web/README.md) 重新构建。

### 选用独立 UI 产物

已有独立 Vite 构建时，可直接组合当前后端与该产物，不替换原随包静态目录：

```powershell
python -B scripts/build_release.py --source . --web-assets .cater/build/web-candidate --output dist/releases/candidate-ui-01 --timeout 180
```

`--output` 必须尚不存在，且不能位于所选 UI 树内；每轮换用新目录。不带 `--web-assets` 时，继续使用原有随包 UI 和快照字节。Python 接口为 `source_snapshot(root, web_assets=path)` 和 `build_release(root, output, web_assets=path, timeout=180)`。

显式选择时，跳过旧 `cater/static/workroom` 的收集，即使旧 UI 缺失也可构建。只将当前后端的包源码、登记数据、构建元数据，以及所选 UI 的捕获字节写入新输出的 `source/`；UI 映射到标准 `cater/static/workroom/` 路径并包含 `workroom-manifest.json`，不复制整个工作区。输入 UI 根只接受 `index.html`、可选 `favicon.svg`、`assets/` 和该清单；不接受链接、额外根资源、未登记资源或不完整清单。资源仍需符合项目已声明的包数据布局，wheel 校验会拒绝漏包。

脚本不重新编译 UI，也不写入任何源文件或所选 UI。它复核捕获一致性，并在构建结束重新计算同一后端与所选 UI 的组合指纹；捕获期间观察到不一致，或结束复核时内容不同，会失败并保留证据。这不是对输入树的持续监控或原子快照：构建中暂时改变、结束前恢复原字节的变更，不保证检出。请先稳定本轮输入，使用独立且可信的输入目录及祖先目录。

`release.json` 显式记录 `web_assets` 输入路径；`ui_version` 是所选编译产物（含 manifest）的内容哈希。它只证明该产物被一致地装入 wheel、安装目录并由 HTTP 提供，**不证明它来自最新前端源码**。源码与编译产物的对应、安装态真实桌面和手机视口全站验证仍需外部 QA；前端源码修改也不会自动触发本脚本重编译。

通过后使用本轮 `installed/`，从安装目录之外启动独立候选服务，并显式指定全新的 QA 数据库和空闲端口；不要覆盖已有安装、复用用户数据库或重启正在使用的工作台。发布冒烟只覆盖下述资源和有限 API，不能替代全站验收。

## 检查什么

1. 从当前工作区的实际字节建立干净源快照，不回退到上次 Git 提交。只收集包源码、明确登记的数据和构建元数据，不复用根目录的 `build/`、`*.egg-info`、`web/dist`、`.cater` 或 `node_modules`。
2. 校验随包或显式选定的网页及构建清单。缺少文件、捕获时发现未登记资源或不一致，以及结束复核时选定输入内容不同，都会失败；不保证检出中间改变后又恢复的情况。
3. 使用 pip/setuptools 构建 wheel；独立检查包文件集合、正文、入口、许可证、零运行依赖和 [wheel RECORD 的哈希与大小](https://packaging.python.org/en/latest/specifications/binary-distribution-format/)。
4. 安装到本轮专属空目录，在源码之外、禁用全局站点包的隔离 Python 进程中导入。核对安装字节、版本及已安装的 CLI 入口，执行 `--version`，不以源码导入代替安装验证。
5. 启动临时本机 HTTP 服务，逐个读取已安装网页资源，并核对任务 API、事件链及零模型调用。检查结束后关闭服务，不触碰用户正在使用的数据库。

每个构建、安装及冒烟进程都有超时和进程生命周期管理，回执保留退出状态与有界输出。可用 `--timeout 180` 调整每一步的超时，最多 600 秒；失败不会自动重试。

## 读取结果

只有命令退出码为 0 且 `release.json` 的 `status` 为 `verified`，才表示本轮检查通过。

| 路径 | 用途 |
| --- | --- |
| `release.json` | 源快照、网页和 wheel 的 SHA-256、实际工具版本、检查结果 |
| `expected.json` | wheel 已核对的包文件清单及逐文件哈希 |
| `toolchain.json`、`build.json`、`install.json`、`smoke.json` | 各步骤的进程回执与失败线索 |
| `smoke/installed-check.json` | 安装态导入、CLI、网页、API 与事件链结果 |
| `source/`、`installed/` | 本轮源快照和安装副本，供复查 |
| `wheels/*.whl` | 待分发的安装包 |

脚本拒绝覆盖旧目录。失败时保留已生成的快照、日志和候选包；先看失败步骤的回执，修正后换一个新目录重跑。源快照前的配置错误不会生成发布目录；后续失败记录为 `failed`，不能把残留 wheel 当作已验收包。CI 打包失败时会把这些步骤回执打印到日志，不上传整个构建目录。

不要把整个输出目录当作发行附件。审核包内容后，只分发所需 wheel 和核对信息；快照、数据库及日志留在本地。

## 保证边界

- 这是内容一致性和安装可用性检查，不是签名、秘密扫描、真实模型质量验收或全平台构建保证。
- SHA-256 用于核对实际字节。不承诺不同后端版本、操作系统或 ZIP 时间戳下生成完全相同的 wheel 字节。
- 构建会执行可信项目的 Python 构建后端。进程管理、最小环境变量和无包索引参数**不是操作系统文件或网络沙箱**；不要用它执行不可信项目。
- 本机 HTTP 检查显式绕过代理。脚本不为用户任务开放新工具权限、不修改调用预算，也不恢复暂停任务。
- CI 在 Windows/Linux、Python 3.11/3.13 上执行同一入口；某次 CI 通过只证明该次矩阵实际运行的范围。
