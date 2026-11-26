from __future__ import annotations

import json
import os
import sys

from mcp.server.fastmcp import FastMCP

from . import openfilm_runtime
from .mcp_tools import (
    creator, design, desktop, drafts, jianying_audio, media, openfilm, production, repeated_takes, status, workflow,
)


mcp = FastMCP(
    "JianyingAIEditor",
    instructions=(
        "提供视频、图片、带时间码的对白、音乐和音效信息，以及剪辑、渲染与检查工具。"
        "收到创作需求，先根据本期原声和三稿设计写出带时码的视听脚本，调用 plan_video_script。"
        "每镜给出原话、画面目的、来源和构图。实拍讲主体，HTML 分项说明观点，场景动作写 stock.query 自动检索。"
        "Agent 主动选候选，再用 execute_video_script 按脚本获取、入轨并回读。无合适素材记录 keep_camera。"
        "字幕按语义，普通陈述单句，递进或列举整项淡入，横排约半字间距，过宽转上下排列。"
        "已有定剪配方可用 plan_video_edit、deliver_video_edit。"
        "卖家口播按三个参考草稿的 seller_reference 风格编排，参考稿只学分镜字体动画。按台词用 search_broll_shots 找剪映库存画面，通常每镜留 5–8 秒，"
        "用 get_broll_shot 获取选段，place_broll_shots 在后台生成全屏或人物分屏的新草稿。"
        "三稿的设计时码见 get_video_workflow(profile='reference',detail='profile')。需要选原话时用 analyze_media_batch。"
        "用 list_video_editing_modules 找构图、字幕和文字标注模块。"
    ),
)

for group in (workflow, status, drafts, jianying_audio, media, repeated_takes, creator, design, production, openfilm, desktop):
    for tool in group.TOOLS:
        mcp.add_tool(tool)


def _compile_episode_cli(arguments: list[str]) -> dict:
    from pathlib import Path

    from .config import Settings
    from .storyboard import compile_episode, read_episode_file

    spec_path = Path(arguments[0]).expanduser().resolve()
    media_root = arguments[arguments.index("--media-root") + 1] if "--media-root" in arguments else None
    spec = read_episode_file(spec_path, media_root)
    preview = int(arguments[arguments.index("--preview") + 1]) if "--preview" in arguments else 0
    settings = Settings.from_env()
    return compile_episode(
        settings, spec, preview_frames=preview,
        mixdown="--mixdown" in arguments, openfilm_project="--openfilm" in arguments,
    )


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if command in {"-h", "--help", "help"}:
        print("jianying-ai-mcp [tools [工具名] | call 工具名 [--args JSON | --args-file 文件] | serve-http [--port 端口]]\n"
              "tools 和 call 可传 --url 连接本地 HTTP 服务。无子命令时启动 MCP stdio。\n"
              "安装与验收命令有 setup、doctor、self-test、openfilm-status、install-openfilm 和 compile-episode。")
        return
    if command in {"tools", "call", "serve-http"}:
        from .client_cli import run_client, serve_http
        if command == "serve-http":
            serve_http(mcp, sys.argv[2:])
        else:
            run_client(command, sys.argv[2:])
        return
    if command == "setup":
        from .setup_runtime import setup
        print(json.dumps(setup(), ensure_ascii=False, indent=2))
        return
    if command == "doctor":
        from .config import Settings
        print(json.dumps(status._capabilities(Settings.from_env()), ensure_ascii=False, indent=2))
        return
    if command == "self-test":
        import asyncio
        from pathlib import Path
        from .self_test import run
        result = asyncio.run(run(Path(sys.argv[2]) if len(sys.argv) > 2 else None))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["passed"]:
            raise SystemExit(1)
        return
    if command in {"install-openfilm", "install_openfilm"}:
        print(json.dumps(openfilm_runtime.install_runtime(), ensure_ascii=False, indent=1))
        return
    if command in {"openfilm-status", "openfilm_status"}:
        print(json.dumps(openfilm_runtime.describe_runtime(), ensure_ascii=False, indent=1))
        return
    if command in {"compile-episode", "compile_episode"}:
        if len(sys.argv) < 3:
            raise SystemExit("用法: jianying-ai-mcp compile-episode <spec.json> [--mixdown] [--preview N]")
        print(json.dumps(_compile_episode_cli(sys.argv[2:]), ensure_ascii=False, indent=1))
        return
    transport = os.environ.get("JIANYING_MCP_TRANSPORT", "stdio")
    if transport not in {"stdio", "sse", "streamable-http"}:
        raise ValueError(f"不支持的 JIANYING_MCP_TRANSPORT: {transport}")
    mcp.run(transport=transport)
