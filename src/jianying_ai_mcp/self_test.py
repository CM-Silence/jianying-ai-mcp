"""Exercise the installed stdio contract twice, including a media relocation."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .openfilm_runtime import runtime_home


async def _call(session, name, arguments):
    result = await session.call_tool(name, arguments)
    data = result.structuredContent or json.loads(result.content[0].text)
    handoff = name == "deliver_video_edit" and data.get("status") == "manual_export_required" and data.get("draft_ready")
    if result.isError or not (data.get("success") or handoff):
        raise RuntimeError(f"{name}: {data.get('error', data)}")
    return data


def _media_decisions(value):
    if isinstance(value, list):
        return [_media_decisions(item) for item in value]
    if isinstance(value, dict):
        return {key: _media_decisions(item) for key, item in value.items()}
    if isinstance(value, str) and Path(value).is_absolute() and Path(value).is_file():
        with Path(value).open("rb") as file:
            return hashlib.file_digest(file, "sha256").hexdigest()
    return value


async def run(config_path: Path | None = None) -> dict:
    config = config_path or runtime_home() / "mcp-stdio.json"
    client = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["jianying-ai"]
    env = client["env"]
    base = Path(env["JIANYING_MCP_WORK_ROOT"]) / "self-tests"
    base.mkdir(parents=True, exist_ok=True)
    job = Path(tempfile.mkdtemp(dir=base))
    sources = job / "素材"
    sources.mkdir()
    media = sources / "test.mp4"
    ffmpeg = env["JIANYING_FFMPEG"]
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=640x360:rate=25", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                    "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(media)], stdin=subprocess.DEVNULL, check=True)
    media.with_suffix(".srt").write_text("1\n00:00:00,200 --> 00:00:01,100\n锁定选段\n", encoding="utf-8")
    deliveries = []
    for run_number in (1, 2):
        work = job / f"run-{run_number}"
        work.mkdir()
        restricted_path = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32") if os.name == "nt" else "/usr/bin:/bin"
        server = StdioServerParameters(command=client["command"], args=client["args"], cwd=work,
                                       env={**env, "PATH": restricted_path, "JIANYING_MCP_WORK_ROOT": str(work),
                                            "JIANYING_DRAFT_ROOT": str(work / "drafts")})
        async with stdio_client(server) as streams, ClientSession(*streams, read_timeout_seconds=timedelta(minutes=15)) as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            if not {"plan_video_script", "execute_video_script", "plan_video_edit", "deliver_video_edit",
                    "search_broll_shots", "get_broll_shot", "place_broll_shots"} <= names:
                raise RuntimeError("已安装的 MCP 缺少脚本、库存或配方工具")
            if run_number == 1:
                plan = await _call(session, "plan_video_edit", {
                    "name": "剪辑复刻样片", "media_root": str(sources), "style_preset": "clean", "music": "warm",
                    "width": 640, "height": 360, "fps": 25, "clips": [
                        {"source": media.name, "start": 0.2, "end": 1.4, "highlights": ["选段"]},
                        {"source": media.name, "start": 1.6, "end": 2.8, "chapter": "交付", "caption": "核对画面与声音"},
                    ],
                })
                recipe = Path(plan["recipe_path"])
                arguments = {"recipe_path": str(recipe)}
            else:
                moved = job / "relocated"
                sources.rename(moved)
                relocated_recipe = moved / "edit.json"
                shutil.copyfile(recipe, relocated_recipe)
                arguments = {"recipe_path": str(relocated_recipe), "media_root": str(moved)}
            result = await _call(session, "deliver_video_edit", arguments)
            manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
            timeline = json.loads(Path(result["timeline_path"]).read_text(encoding="utf-8"))
            # Hash source files while they still occupy this replay's paths.
            result["decisions"] = _media_decisions({"tracks": manifest["tracks"], "captions": timeline["captions"],
                                                    "srt_imports": manifest.get("srt_imports"), "shots": timeline["shots"]})
            result["total_frames"] = timeline["total_frames"]
            deliveries.append(result)
    subtitles = [Path(run["srt_path"]).read_bytes() for run in deliveries]
    expected = {"锁定选段", "核对画面与声音"}
    checks = {"edit_decisions_and_assets": deliveries[0]["decisions"] == deliveries[1]["decisions"],
              "subtitles": subtitles[0] == subtitles[1] and expected <= set(subtitles[0].decode("utf-8-sig").splitlines()),
              "frames": all(run["total_frames"] == 60 for run in deliveries),
              "editable_drafts": all(Path(run["draft_path"], "draft_content.json").is_file() for run in deliveries),
              "native_handoff": all(run.get("status") == "manual_export_required" and "mp4_path" not in run
                                    and run["draft_name"] in run["user_message"] for run in deliveries)}
    result = {"passed": all(checks.values()), "checks": checks, "native_mp4_verified": False,
              "draft_path": deliveries[-1]["draft_path"], "recipe_path": str(relocated_recipe), "media_root": str(moved),
              "report_path": str(job / "self-test.json")}
    Path(result["report_path"]).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
