# MCP 客户端配置

Windows x64 在仓库根目录运行 `./install.ps1`。安装器在 `runtime/installed/` 写入适配以下客户端的本机服务条目。将选中的条目合并到已有配置。通过 `uv run --locked --python 3.12 jianying-ai-mcp setup` 安装时，以命令输出的路径为准。

| 客户端 | 安装器输出 | 目标位置 |
| --- | --- | --- |
| 使用 `mcpServers` 的本地客户端 | `mcp-stdio.json` | 对应客户端的 MCP JSON 配置 |
| Codex | `codex-mcp.toml` | `config.toml` 中的 `mcp_servers` 条目。[OpenAI 官方文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) |
| VS Code 编辑器 | `vscode-mcp.json` | 用户配置或工作区的 `.vscode/mcp.json`。[VS Code 配置文档](https://code.visualstudio.com/docs/agents/reference/mcp-configuration) |
| OpenCode 1.x | `opencode.json` | `mcp` 下的服务条目。[OpenCode 迁移说明](https://opencode.ai/docs/migrate-v1/) |
| OpenCode 2.x | `opencode-v2.json` | `mcp.servers` 下的服务条目，直接向模型提供工具。[OpenCode MCP 文档](https://opencode.ai/docs/mcp-servers) |

配置中的 `command` 是隔离环境的 Python 绝对路径，参数为 `["-m", "jianying_ai_mcp"]`。媒体工具、模型、浏览器和工作目录均已定位。长片渲染可能超过客户端默认单次调用时限，可在客户端中设置适合素材长度的工具超时。

连接后用 `get_video_workflow` 取得默认流程，`get_jianying_capabilities` 查看已安装组件。文本模型可以使用镜头清单、台词和测量报告；需要确认画面内容时，读取代表帧或使用已有的画面观察。运行时通过 MCP 日志和 progress 报告进度。

如需修改草稿库，调整 `JIANYING_DRAFT_ROOT`。自定义剪映安装可设置 `JIANYING_EXE`。第三方在线转写密钥由使用者在选择对应服务时配置。
