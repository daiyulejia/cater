# Cater 对话工作台

这是 Cater 可选的 React 对话工作台。Python 运行时继续保持零第三方运行依赖；需要使用聊天式任务空间时，再启用本目录的前端依赖。

## 本地运行

只有从源码修改工作台时才需要构建。发布包会自带最近一次已验证的静态工作台。

构建并同步到 Python 包：

```powershell
.\scripts\build_web.ps1
```

首次执行前请先在 `web/` 目录运行一次 `pnpm install`。

然后回到仓库根目录，正常启动 Cater：

```powershell
python -m cater.cli web --port 8766
```

访问 `http://127.0.0.1:8766`。Cater 会从同一个本地地址提供工作台、API 和完整审计回放。

开发前端时，可以分别运行 `python -m cater.cli web --port 8766` 与 `pnpm dev`，再访问 `http://127.0.0.1:5173`。Vite 会把开发请求代理给 Cater 的 API。

## 产品边界

- 工作台只是事件流的对话投影，不是第二份事实来源。
- 人类方向、任务指派、Agent 产出、决策与失败都会进入可检查的事件链。
- 界面默认先分发任务并启动一轮协作；是否进入治理仍由真实分歧和风险信号决定。
