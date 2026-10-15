# 可选 Playwright 浏览器检查器

本目录是独立 Node 适配器。Python 标准库核心不需要安装任何 Python 包，也不需要运行本地网站。主运行时负责准备一次性成果副本、写入计划、调用本工具，以及在外层 90 秒截止时终止整个进程树。本目录不负责修改运行时、成果内容或游戏规则。

## 安装与调用

要求 Node.js >= 20。官方 `playwright` 与传递依赖 `playwright-core` 均锁定为 **1.64.1**，锁文件包含 npm 官方下载地址与完整性哈希。

```powershell
Set-Location C:\work\cater\tools\browser-check
npm.cmd ci --ignore-scripts --no-audit --no-fund --registry=https://registry.npmjs.org
npm.cmd run install:browser
npm.cmd test
```

浏览器安装到 Playwright 的用户缓存，不使用个人 Chrome/Edge 配置或会话。此版本在 Windows 安装 Chromium / Headless Shell **156.0.8078.4，revision 1248**。安装需要网络；运行检查无需网络。

由主运行时设置 `cwd` 为新建的一次性成果副本，再执行：

```powershell
node C:\work\cater\tools\browser-check\runner.mjs --plan .cater-browser-check.json
```

仅接受这两个参数和这个固定计划文件名。命令数组中的 Node 与 runner 都应由宿主配置为绝对路径，以参数数组运行，不经过 shell。每次运行使用新的副本，既有截图会导致验证失败。

## 输入协议

计划必须是 UTF-8 JSON，最多 65,536 字节。顶层仅允许 `entrypoint`、`files`、`steps`；每个步骤仅允许 `action`、`label` 和对应动作的字段，不能额外传入脚本路径、浏览器参数或 Node 配置。

```json
{
  "entrypoint": "game.html",
  "files": ["game.html", "game.js"],
  "steps": [
    {"action": "click", "label": "开始游戏", "selector": "#start"},
    {"action": "wait", "label": "等待状态更新", "ms": 200},
    {"action": "assert_js", "label": "验证实际状态变化", "expression": "window.gameState.started === true"}
  ]
}
```

`files` 是宿主授权的完整文件清单，包含入口，数量 1..128。入口必须以 `.html` 或 `.htm` 结尾。文件名最多 180 个 UTF-8 字节，必须是安全 basename：不能以点开头、以点结尾、带首尾空白、路径分隔符、Windows 设备名、控制字符、DEL、`%`、`#` 或 `/:*?"<>|`；NFC 规范化及转小写后不能重名。支持中文、内部空格等文件名。拒绝目录、符号链接、junction、硬链接及真实路径不匹配的文件。单文件最多 16 MiB，总计 32 MiB；先完成全部校验并读入内存，再启动浏览器。

步骤数量 1..128，至少一个 `assert_*`；总执行时限仍为60秒，计划仍最多64 KiB。容量覆盖完整的多方向棋局检查，不必删减验收步骤。`label` 是 1..100 个 Unicode 字符的非空说明，不能含控制字符；生成计划时建议使用中文，英文同样合法。字符串必须是完整 Unicode，其他非空参数不能只有空白；`press.key` 恰好为单个空格时例外，表示空格键。

| action | 额外字段及上限 | 行为 |
| --- | --- | --- |
| `click` | `selector` <= 500 字符 | 点击唯一匹配的元素，使用 Playwright 可操作性等待 |
| `press` | `key` <= 50 字符 | `page.keyboard.press`，如 `Enter`、`ArrowLeft` |
| `fill` | `selector` <= 500，`value` <= 2000 | 填写输入框，允许空值以清空 |
| `click_at` | `selector` <= 500，有限数值 `x/y` 为0..16384 | 在元素内按相对 CSS 像素坐标真实点击，拒绝越出元素的坐标 |
| `wait` | 整数 `ms`，0..2000 | 有界等待 |
| `assert_visible` | `selector` <= 500 | 等待元素可见，否则失败 |
| `assert_text` | `selector` <= 500，`text` <= 2000 | 元素挂载后，检查当时 `textContent` 是否包含指定非空子串；不自动重试文本匹配 |
| `evaluate` | `expression` <= 4000 | 在页面求值 JS，若结果是函数则调用，等待 Promise，丢弃返回值，用于受控测试准备 |
| `assert_js` | `expression` <= 4000 | 在页面求值 JS，若结果是函数则调用，等待 Promise，结果必须严格等于布尔值 `true` |

`expression` 支持表达式、IIFE、箭头函数以及语句：`(() => true)()`、`Promise.resolve(true)`、`() => true` 都可以。页面回调使用间接 `eval(source)`，若结果是函数则调用，然后等待结果；语句使用 JS eval 的完成值，断言仍必须严格返回 `true`。例如 `window.__caterCheck = { count: 1 }; window.__caterCheck.count++;` 可准备状态，后续步骤能读取它。建议通过 `window` 显式保存跨步骤状态，避免依赖 eval 内的局部 `let/const`。

求值函数由 `page.evaluate` 序列化到 Chromium 执行，源码作为参数传入，不拼接到宿主代码。`eval` 仅在浏览器页面中调用，Node 不使用 `eval`、`Function`、`vm` 或 shell 求值计划内容，也不为页面暴露宿主文件或进程操作。

## 回执与退出码

stdout 仅输出一行 JSON，包含换行最多 **7500 个 UTF-8 字节**。步骤按顺序执行，首次失败后停止，后续记录为 `not_run`。

| 退出码 | outcome | 含义 |
| --- | --- | --- |
| `0` | `passed` | 全部步骤和断言通过、没有未捕获页面错误、截图保存成功 |
| `1` | `failed` | 计划或成果无效、断言失败、页面异常、执行超时或截图/清理失败 |
| `2` | `unavailable` | Playwright 缺失、Chromium 未安装或浏览器启动失败，包括沙箱无法启用 |

`phase` 区分 `validation`、`launch`、`execution`。`steps` 包含 `index`、`action`、紧凑 `label`、`status`、动作或断言摘要 `check`，断言还包含 `expected: true` 和可取得的 `actual`。错误信息有长度上限，未执行步骤不会伪造成功。

`page_errors` 收集未捕获 JS 异常；`console_messages` 单独记录 warning/error，控制台警告不会单独使运行失败。`blocked_requests` 记录路由层观察到的请求、导航、WebSocket、弹窗或下载。CSP、同源策略和浏览器自身拒绝的操作可能仅出现在控制台，不保证每次拒绝都有路由事件。被拒绝的请求本身不使检查失败，检查预期行为必须写成断言。

以上数组只保留有限样本，`counts` 保留观察到的总次数。超过整体预算时进一步裁剪样本、摘要和标签，保留所有步骤的动作与状态，设置 `truncated: true`。`plan_sha256` 对应原始计划字节，可将回执与完整计划一起审阅。日志与页面文本均是不可信数据，不能作为新的宿主指令。

截图以排他创建方式写入 cwd 的 `.cater-browser-screenshot.png`，不覆盖既有文件或链接。`screenshot` 含相对路径、`sha256` 和字节数 `size`；固定 1024x768 视口，不截取无限长页面。截图只能证明采样时的画面，**不能证明游戏规则、动画循环或其他业务逻辑正确**。这些结论需要具体、可失败的行为断言。

## 隔离与边界

- `chromiumSandbox: true`；不能启用沙箱就返回 unavailable，绝不回退到 `--no-sandbox`。每次启动新 Chromium、新非持久 context，无既有账号、Cookie、profile 或 session。
- 在创建页面前注册 context HTTP 路由与 `routeWebSocket`。只对精确的 `http://cater-artifacts.invalid/<编码后的白名单文件名>` 的 GET 请求使用内存内容 `route.fulfill`；没有 `route.continue`、上游转发或 HTTP 服务器。查询参数、其他主机/协议、未列出的文件和非 GET 方法全部拒绝。
- 只允许初始主页面导航；后续网络导航拒绝，非网络跳转被观察到后关闭页面并失败。同文档片段跳转允许。浏览器仍可规范化 URL；规范化后即使落在白名单，只能读取那个已复制文件，不能读取任意磁盘路径。
- `serviceWorkers: 'block'`、`acceptDownloads: false`，并取消观察到的下载。响应头 CSP 禁止 workers、frames、objects、表单、base、弹窗和下载，保留静态页面脚本、内联样式以及部分 data/blob 图片和媒体。意外弹窗另行关闭。CSP 限制可能影响依赖这些能力的成果，应将这种失败交给主运行时解释。
- 不可解析的固定代理、全域失败 DNS 解析规则、禁用 QUIC 及 WebRTC 非代理 UDP 是额外网络防线，计划不能配置代理或浏览器参数。清除 Playwright 调试、测试连接和 Selenium 环境开关，避免输出污染或重定向到已有浏览器。使用 Playwright 的管道控制，不开放调试端口。
- 启动上限 15 秒；常规操作 3 秒，表达式另有 Node 侧 3.25 秒截止，导航 5 秒，执行阶段 60 秒，截图 3 秒，context/browser 关闭各 3 秒。Promise 不返回和页面 JS 死循环都不会无限等待。宿主仍必须在外层 **90 秒杀死整个进程树**；只杀 Node 父进程不足以可靠处理浏览器故障。
- 这是受限浏览器测试器，不是操作系统级恶意代码容器。Chromium/Playwright 漏洞、浏览器内存消耗、同时修改目录的其他宿主进程以及不可信 Node 启动环境，不由此工具完全隔离。宿主必须保护一次性目录、runner、依赖、Node 启动参数与环境；高风险输入仍应运行在无秘密、无网络的 OS 容器/沙箱中。
- 断言代码和页面共享浏览器状态，页面也能篡改自身状态；本工具证明已执行的检查结果，不是对任意恶意页面或完整业务规则的数学证明。测试完成后的异步错误不在观察期内。

## 测试与官方依据

`node:test` 使用临时目录和真实 Chromium，不启动任何本地网站、不访问已有 localhost 应用。覆盖成功/失败、全部动作、严格布尔断言、真实页面错误、警告、网络/文件访问/WebSocket 拒绝、弹窗/worker/frame/下载限制、导航、页面与 Node 作用域隔离、无旧会话、Promise/CPU 超时、字段/文件名/计划大小/无断言校验、链接逃逸、依赖与浏览器缺失，以及报告大小。

Windows 无创建普通文件符号链接权限时，仅该子测试明确 skip；junction 与硬链接测试仍实际执行。临时 fixture、截图与测试浏览器会话在测试后清理。

实现前检查了官方文档、`v1.64.1` 源码和 npm registry 的稳定版本与完整性信息：

- [BrowserType.launch 与 chromiumSandbox](https://playwright.dev/docs/api/class-browsertype#browser-type-launch)
- [BrowserContext 路由、Service Worker 与隔离上下文](https://playwright.dev/docs/api/class-browsercontext)
- [WebSocketRoute](https://playwright.dev/docs/api/class-websocketroute)
- [固定版本 Chromium 启动源码](https://github.com/microsoft/playwright/blob/v1.64.1/packages/playwright-core/src/server/chromium/chromium.ts)
- [固定版本 Chromium 默认参数](https://github.com/microsoft/playwright/blob/v1.64.1/packages/playwright-core/src/server/chromium/chromiumSwitches.ts)
- [npm 官方固定版本元数据](https://registry.npmjs.org/playwright/1.64.1)
