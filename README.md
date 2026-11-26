# 剪映 AI 编辑 MCP + Skill

Jianying AI MCP 让 Codex、VS Code、OpenCode 等 MCP 客户端根据本地素材制作剪映专业版草稿。你可以用自然语言描述剪辑需求，让 Agent 盘点素材、按原片时码选段、编排字幕和音画，再把结果交给剪映继续编辑或导出。

- **看素材、选镜头。** 批量读取媒体参数、对白和代表帧，查找重复口播，检索已索引的剪映库存镜头。
- **编排画面和声音。** 生成带时码的视听脚本或可迁移的剪辑配方，支持分屏、字幕、动效图卡、旁白和混音。
- **制作可编辑草稿。** 支持从零建稿、从参考草稿换料换字，以及用同一模板批量出稿。
- **导出和验收。** 可用 OpenFilm 渲染 HTML 镜头或项目，也可通过剪映桌面端导出 MP4。交付工具检查成片，桌面导出无法完成时会返回可手动打开的草稿路径。

[安装与第一次交付](#安装与第一次交付) · [MCP 工具](#mcp-工具) · [制作与剪辑工作流](#制作与剪辑工作流) · [参考项目](#参考项目) · [许可证](#许可证)

## 安装与第一次交付

Windows x64 在仓库根目录运行 `./install.ps1`。安装器准备 Python 3.12 环境、FFmpeg/ffprobe、Node/npm、OpenFilm、匹配的 Chromium、中文字体、离线 Whisper small、URL 下载和在线配音用的客户端，以及独立的剪映桌面 CLI。首次安装需要联网。完成后，从 `runtime/installed/` 取对应条目加入客户端。通用 stdio 用 `mcp-stdio.json`，Codex 用 `codex-mcp.toml`，VS Code 编辑器用 `vscode-mcp.json`，OpenCode 1.x 和 2.x 分别用 `opencode.json` 与 `opencode-v2.json`。配置已包含本机实际路径。

收到新需求时，先用 `plan_video_script` 写出有成片时码的视听脚本。每拍把台词与实拍、HTML 动画或库存分镜对应起来。写有 `stock.query` 的场景会主动搜索已索引的剪映视频。Agent 选定可用镜头后用 `execute_video_script` 获取实物并按同一脚本入轨。返回的草稿含原声、字幕与选材回执。[脚本编写与样例](src/jianying_ai_mcp/workflow_guides/making-storyboard.md)

已有定剪选段或配方的工作流如下。

1. **选素材。** 已经指定镜头时直接编排。需要挑选时，用 `analyze_media_batch` 查看原话和代表帧。返回的 `transcript_path` 是整批逐条对白文本，保留素材编号和原片时码。已有批次用 `export_media_batch_transcript(batch_id)` 即可生成。
2. **定剪。** 调用 `plan_video_edit`，传片名和有序镜头。视频给源入出点，图片可给停留时长。未填字幕的有声镜头会读取现有字幕或本地转写。返回的 `recipe_path` 已锁定帧数、字幕、风格、素材 SHA-256 和相对目录。
3. **交付。** `deliver_video_edit(recipe_path=...)` 在剪映草稿库新建可编辑草稿并尝试原生导出。成功后读取 `mp4_path` 和 `review_path`。返回 `manual_export_required` 时，向用户报告 `draft_name`、`draft_path` 和失败原因，提示打开该草稿手动导出。复刻时可传搬移后的 `media_root`，有上一版原生成片报告时可传 `reference_review_path` 对比解码画面、音频和 SRT。

例如，把 `media_root` 设为本仓库 `test_assets` 的绝对路径，其余 `plan_video_edit` 参数可直接使用下面的 JSON。第一镜取原片第 0.2–1.4 秒，第二镜停留 1.2 秒。

```json
{
  "name": "选段与交付", "style_preset": "clean", "music": "warm",
  "width": 1280, "height": 720, "fps": 25,
  "clips": [
    {"source": "compat_test_video.mp4", "start": 0.2, "end": 1.4, "caption": "先确定镜头"},
    {"source": "compat_test_image.png", "hold_seconds": 1.2, "chapter": "交付", "caption": "核对字幕和声音"}
  ]
}
```

要做上下分屏，可直接使用同一素材目录与[竖屏动效示例](examples/edits/packaged-interview.json)。镜头的 `panel.recipe` 选内置图卡，`panel.data` 填文案，`items[].at` 给每个要点指定镜头内出现的秒数。工具会按实际栏位尺寸渲染。带旋转标记的手机视频和需要转换的素材会在交付时自动生成本地剪辑副本。

`delivery_dir` 保存配方、可选 SRT 和交付记录。剪映成功导出的 MP4 通过帧数、画幅、声音和绿帧核查后成为 `video.mp4`。有疑似绿帧时按镜头对照工程静帧。质检失败的文件作为 `unverified_mp4_path` 留供排查，草稿名随手动导出提示返回。

### 按口播检索剪映素材并排入时间轴

《不用操心10》《大单8》《复盘11》只作分镜、字体和动画参考。新一期从本期实拍取口播，从剪映库存视频选补充画面。卖家口播使用 `style_preset="seller_reference"`。[三稿的设计记录](src/jianying_ai_mcp/workflow_guides/reference.md)

1. 用 `list_jianying_stock_videos` 查看剪映共享在线素材缓存中的可读视频。若视频已从剪映素材库另外保存，在 MCP 环境变量 `JIANYING_STOCK_ROOT` 填这个专用下载目录。结果的 `origin` 标明来源是共享缓存还是指定目录。只在该目录存放已确认出自剪映素材库的视频。
2. 用 `analyze_video_source` 抽帧，对照画面记录动作、可用区间和素材面板显示的标题或 ID。用 `index_broll_shots` 填 `source,start_ms,end_ms,description`，可加 `asset_title,asset_id,tags,use_case`。也可用 `analysis_id` 导入已保存的观察。入库和检索默认使用剪映库；明确要求复用自摄库存时，两步均指定 `source_pool="personal"`。
3. 例如口播说「包裹经过扫码」，调用 `search_broll_shots(query="包裹经过扫描器",min_duration_ms=6000,availability="timeline_ready")`，预览后取 `shot_id`。在例子里，成片第 12–18 秒保留原口播。全屏展示用 `{"shot_id":"检索所得ID","at":12,"duration":6}`，上下分屏再加 `"layout":{"kind":"stacked","main":"bottom","split":0.48}`。无合适镜头时延续讲者画面。
4. 以一个完整动作或意思为一镜，通常留 5–8 秒，需要读字或观察细节时可以更久。卖家风格默认检查 4 秒下限，包括插镜前后的短暂回切。字幕和步骤可在同一构图内依次出现。按项目约定可设 `min_scene_seconds`。

剪映专业版的部分官方视频只能在编辑器预览。`list_jianying_stock_videos` 将本地探测失败的缓存列在 `needs_editor_preview`。在官方面板核对视频画面和可用时长后，给选段填写 `editor_verified_ms`（毫秒）。对于 `editor_only` 镜头，可用 `bind_jianying_stock_selection(selection, shot_ids)` 绑定原生记录。`selection` 是专用官方素材采集稿或其可读取的时间线文件的绝对路径。采集稿内只放从官方面板添加的原始镜头，素材路径须指向剪映的共享缓存。工具按原文件和资源 ID 核对，一次绑定可跨草稿使用，全屏与分屏都保留原生素材身份。若当前版本的采集稿无法读取，镜头维持 `editor_only`。参考稿仅用于分析设计。

检索 `timeline_ready` 可选出 `native_draft` 和 `local_video` 两种镜头。原生镜头由剪映预览和导出，导出时依剪映显示的素材权益处理。需要后台输出 MP4 或抽帧预览时，筛选 `local_video`。

**后台调用顺序**为 `search_broll_shots` → `get_broll_shot` → `place_broll_shots`。第二步返回本地文件或已绑定的原生引用，以及源选段（毫秒）。第三步读取本期的分镜，在草稿目录创建新片。以下是第三步的参数，目标成片时间以秒计。成功回执里的 `readback` 来自生成的草稿文件，可核对轨道、实际时码（微秒）和音量。分屏转码后，轨道媒体的入点可与原片入点不同。

```json
{
  "spec_path": "F:/本期/分镜.json",
  "placements": [{"shot_id": "检索所得ID", "at": 12, "duration": 6, "layout": {"kind": "stacked", "main": "bottom", "split": 0.48}}],
  "dry_run": false
}
```

外部 MCP 搜索本机已索引素材。剪映 11.8 助手的在线官方素材召回依赖宿主会话和账户可用额度。供剪映内置助手复用的[分镜技能包源文件](skills/seller-stock-storyboard/SKILL.md)包含三稿的例子、选镜和回读步骤。外部 Agent 可以直接调用 `get_video_workflow(profile="reference",detail="profile")` 获取带时码的设计说明。

[32 秒示例配方](examples/edits/seller-broll.json)以 6 秒讲者开场，接 6 秒全屏、6 秒上下分屏、8 秒步骤卡，最后回到讲者 6 秒。替换本期主镜与两个库存镜头 ID 后可交给 `plan_video_edit`。

### 宿主尚未加载 MCP 连接器时

安装后的命令行可以直接发现工具、读取参数或发起调用。下面的命令均在仓库根目录运行，`plan.json` 是传给 `plan_video_edit` 的参数对象。

```powershell
$jy = '.\runtime\python\Scripts\jianying-ai-mcp.exe'
& $jy tools
& $jy tools plan_video_edit
& $jy call plan_video_edit --args-file .\plan.json
```

命令行默认使用安装配置启动 stdio 会话。需要持续 HTTP 连接时，在一个终端运行 `& $jy serve-http --port 8000`，另一个终端使用 `& $jy tools --url http://127.0.0.1:8000/mcp` 或给 `call` 加相同的 `--url`。支持 URL 的宿主可导入 `runtime/installed/mcp-http.json`。

`runtime/python/Scripts/jianying-ai-mcp.exe self-test runtime/installed/mcp-stdio.json` 在两个独立 stdio 会话间搬移素材，核对选段、素材内容、字幕、时间线、剪映草稿和手动交接。自检使用隔离草稿库。实机原生导出另以可交互的剪映窗口验收。见[工作流审查](docs/workflow_review.md)。

草稿生成的历史验证组合为剪映专业版 `10.0.5.13816`、pyJianYingDraft `0.3.0`、MCP Python SDK `1.29.0`。当前依赖 pyJianYingDraft `0.3.1`，以生成新版剪映需要的蒙版结构。原生导出的版本与窗口要求见下文「剪映桌面导出」。

## 目录

- `src/jianying_ai_mcp/`：批处理 MCP 服务
- `skills/jianying-ai-editor/`：可复用 Skill
- `docs/capability_matrix.md`：完整测试结论
- `configs/`：客户端配置样例
- `tests/`：从零生成、参考复用、批量换料及安全边界测试

## MCP 工具

当前服务共注册 53 个工具。安装后用 `jianying-ai-mcp tools <工具名>` 查看参数。桌面、语音和浏览器等后端的可用状态可用 `get_jianying_capabilities` 查询。

### 工作流与交付

| 工具 | 作用 |
| --- | --- |
| `get_video_workflow` | 按剪辑场景或环节读取操作指南。 |
| `get_jianying_capabilities` | 查看剪映版本、已配置的后端和可用能力。 |
| `plan_video_script` | 保存带成片时码的视听脚本和可执行分镜，为指定场景匹配已索引的库存候选。 |
| `execute_video_script` | 按脚本和确认的镜头选项编排并回读剪映草稿。 |
| `plan_video_edit` | 锁定镜头顺序、源入出点、字幕、风格及素材校验值，生成可迁移配方。 |
| `deliver_video_edit` | 根据配方建立剪映草稿，尝试桌面原生导出并验收 MP4，必要时返回手动导出所需信息。 |

### 素材分析与口播选段

| 工具 | 作用 |
| --- | --- |
| `parse_media_info` | 读取本地媒体的时长、尺寸、编码和音轨信息。 |
| `analyze_video_source` | 从视频或音频获取带时码的文本，按需从视频抽取代表帧。 |
| `get_video_analysis_context` | 按时段和页码读取缓存中的字幕和画面观察。 |
| `save_video_observations` | 保存 Agent 看过代表帧后整理的结构化观察。 |
| `analyze_media_batch` | 批量盘点视频、音频和图片，生成媒体清单、对白索引和预览。 |
| `get_media_batch_context` | 按素材、关键词和时间范围查阅批次内容。 |
| `export_media_batch_transcript` | 从已有批次导出带素材编号和原片时码的完整对白文本。 |
| `prepare_media_assets` | 生成已处理旋转、画幅或帧率的剪辑副本，也可单独提取 WAV。 |
| `find_repeated_takes` | 在批次转写中定位重复口播候选及其原片区间。 |
| `list_episode_takes` | 列出已有转写的素材、时长和对白条数，可限定批次。 |
| `select_episode_take` | 按给定台词匹配候选素材并返回起止时间。 |

### 库存镜头与补充画面

| 工具 | 作用 |
| --- | --- |
| `list_jianying_stock_videos` | 查看剪映共享缓存和指定本地目录中的库存视频，标出需在编辑器预览的素材。 |
| `index_broll_shots` | 把视频选段、来源和画面描述加入库存索引。 |
| `bind_jianying_stock_selection` | 将已索引镜头绑定到剪映采集稿中的原生素材引用。 |
| `search_broll_shots` | 按画面描述、时长和可用状态查询剪映库存或自摄素材。 |
| `get_broll_shot` | 获取指定镜头的源选段、可用状态和本地文件或原生引用。 |
| `place_broll_shots` | 按成片时码在分镜中安排全屏或分屏补充画面，并回读草稿轨道。 |

### 剪映草稿与模板

| 工具 | 作用 |
| --- | --- |
| `list_jianying_presets` | 按类别、关键词和 VIP 标记查找预设，返回可写入清单的名称。 |
| `list_jianying_audio_assets` | 从草稿中列出可引用的音乐、音效、素材 ID 和本地可用状态。 |
| `analyze_reference_draft` | 从明文草稿或本机可读取的加密草稿中提取剪辑蓝图。 |
| `build_draft` | 按内联清单或清单文件从零生成剪映草稿。 |
| `build_from_reference` | 复用参考蓝图，按轨道或片段替换素材和文字。 |
| `batch_from_template` | 用同一模板批量生成多份草稿。 |
| `repair_caption_references` | 校验待修复的字幕引用并输出修复后的 JSON 副本。 |
| `diagnose_jianying_draft` | 只读检查草稿的时间线、素材和引用。 |

`build_draft`、`build_from_reference` 和 `batch_from_template` 默认 `dry_run=true`，在暂存区生成并校验。传 `dry_run=false` 可发布新草稿。

### 时间线、图卡与声音

| 工具 | 作用 |
| --- | --- |
| `list_video_style_presets` | 查看时间线、图卡和浏览器动画可用的包装风格。 |
| `list_video_editing_modules` | 查询构图、字幕和文字标注模块的参数和示例。 |
| `compile_video_timeline` | 编译帧级时间线、剪映清单、SRT，以及按需生成的混音和 OpenFilm 项目。 |
| `compile_episode` | 把整期分镜中的口播、补充画面和动效编译成时间线、剪映清单和字幕。 |
| `create_video_graphic` | 生成带标题、图片和配色的静态图卡。 |
| `create_video_animation` | 用内置样式或自定义 HTML 创建 OpenFilm 动画项目。 |
| `create_media_contact_sheet` | 将本地代表帧、图卡或截图拼成带标签的预览图。 |
| `list_motion_recipes` | 查看开场、章节、流程等 HTML 动效配方的数据字段和默认时长。 |
| `create_motion_clip` | 用动效配方生成 HTML 项目，并可渲染为可入轨的 MP4。 |
| `list_sound_cues` | 列出本地合成音效和适用场景。 |
| `list_narration_voices` | 按语言查询 Windows SAPI 或在线 Edge TTS 的语音。 |
| `synthesize_narration` | 生成分段旁白 WAV 并返回各段时长。 |
| `create_music_bed` | 本地合成指定时长和风格的配乐 WAV。 |

### 渲染、桌面导出与验收

| 工具 | 作用 |
| --- | --- |
| `setup_openfilm_runtime` | 检查 OpenFilm 运行时，按需安装缺少的组件。 |
| `look_openfilm` | 预览本地 OpenFilm 项目的指定时间点或区间，返回截图和检查信息。 |
| `render_openfilm` | 将 OpenFilm 整片或指定区间渲染为 MP4。 |
| `export_openfilm_draft` | 把 OpenFilm 时间线转换为可编辑剪映草稿，支持暂存和发布。 |
| `probe_jianying_desktop` | 查看剪映安装版本、草稿目录和桌面导出后端状态。 |
| `export_jianying_mp4` | 通过可交互的剪映桌面端导出指定草稿，并校验输出文件。 |
| `review_export` | 抽样检查成片画面、黑场、综合响度和真峰值。 |
| `verify_audio_dialogue` | 重新转写实际成片或混音，核对指定时段里的关键句。 |
| `master_video_audio` | 对成片做双遍响度处理，保留视频流并复测输出音频。 |

## 制作与剪辑工作流

Agent 根据任务设计画面、叙事和声音，MCP 提供素材信息与编辑工具。`analyze_media_batch` 提供带源时间码的对白和画面预览，配乐和音效可来自本地文件或参考草稿。字体、字幕与转场可逐项选择，预设也可作为起点。开场、流程、对照和片尾可从 `list_motion_recipes` 选择，再用 `create_motion_clip` 填入本期内容。

`get_video_workflow()` 默认返回简述。`section="sources"` 可查素材工具，`section="edit"` 可查时间线编排，`section="finish"` 可查声音、画面和字幕。`profile="animation", detail="profile"` 可查图卡和 HTML 动画接口。工具也可独立调用。

### 按需查找剪辑模块

`list_video_editing_modules()` 返回简短的模块目录。可传 `category="layout"` 查构图、`category="caption"` 查字幕或 `category="callout"` 查文字标注。例如，调用 `list_video_editing_modules(category="layout", module_id="interview_box")` 可读取下方访谈框的参数和示例片段。把选中的片段合并到 `compile_video_timeline` 的参数中，补上时间线名称、素材路径和镜头帧数。用 `get_jianying_capabilities` 检查本机后端状态。

### 带标题、字幕和转场的时间线

`list_video_style_presets` 提供可选风格。`chapter` 记录小节位置并参与转场编排，画面省去小节角标。`list_jianying_presets` 可查询单项资源，`srt_mode="native"` 可生成原生字幕轨。

| `style_preset` | 适合场景 | 剪映字体 | 剪映章节转场 |
|---|---|---|---|
| `editorial` | 知识口播、访谈 | HarmonyOS Sans SC Bold | 闪黑 |
| `documentary` | 人物故事、纪实 | 思源中宋 | 叠化 |
| `broadcast` | 资讯、数据盘点 | 优设标题黑 | 向右擦除 |
| `clean` | 教程、产品演示 | 仓耳舒圆体 W02 | 向左擦除 |

需要细调时，在上一镜的 `transition` 填剪映资源名称与时长，在同一镜的 `portable_transition` 填 MP4 对应的 `kind`、偶数帧 `frames` 和可选 `color`。MP4 使用 `dip`、`wipe_left` 或 `wipe_right`。`caption_decoration` 可指定字体、底板、描边、气泡、花字和字幕动画，原生草稿保留对应资源引用。MP4 支持底板、描边与显式指定的 `portable_caption_animation`。需要检查章节衔接时，以 `design.chapter_starts_s` 定位并在切点前后抽帧。

成片需要调整响度时，将已导出的 MP4 交给 `master_video_audio` 并读取回测结果。`verify_audio_dialogue` 归一化标点和繁简，发现相近读音时返回复听线索。

### 访谈构图与文字标注

`compile_video_timeline` 的 `shots[].layout` 可按镜头选择构图。

| `kind` | 画面布局 |
|---|---|
| `full` | 全屏访谈或场景镜头。需要让不同宽高比的原片铺满画面时，可先用 `prepare_media_assets(fit="cover")` 适配目标画幅。 |
| `stacked` | 上方补充画面，下方采访画面。 |
| `interview_box` | 上方画面铺满上区，受访者位于下方居中的描边框内。 |

后两种布局的 `top.source` 指向本地照片或视频。上方素材静音，下方使用镜头 `source` 和原声。上方视频可指定 `source_start_frame` 和 `source_frames`。`top.focus` 与 `bottom_focus` 可分别调整裁切重心，`top_fraction` 可设为 `0.30`–`0.65`，`background` 可用 `#RRGGBB`。工具生成匹配窗格的裁切素材，剪映草稿保留独立的上下画面轨，也可生成 OpenFilm 项目。

`caption_theme="interview"` 使用白字、黑描边与阴影。`captions[].highlights` 接收原文中的词语，匹配内容显示为暖黄色，也可独立使用。`shots[].overlays[].role` 可选 `person`、`emphasis` 或 `section`，分别用于人物、重点和章节标注。标注是可编辑文字，有默认排版和进出场。可用 `style`、`clip`、剪映 `animations` 或 OpenFilm 的 `portable_animation`、`portable_exit_animation` 覆盖。OpenFilm 退场可选 `fade`、`sink`、`slide_left`、`wipe_out`。

```json
{
  "name": "访谈片段",
  "fps": 25,
  "shots": [{
    "source": "D:/素材/采访.mp4",
    "frames": 100,
    "layout": {"kind": "interview_box", "top": {"source": "D:/素材/产品细节.jpg"}},
    "overlays": [{"role": "person", "text": "受访者 · 产品负责人", "frames": 54}]
  }],
  "caption_theme": "interview",
  "captions": [{"start_frame": 6, "end_frame": 52, "text": "先写清交付标准", "highlights": ["交付标准"]}]
}
```

### 复用剪映曲库和音效

把想用的音乐或音效在剪映中加入参考草稿并保存，然后对此草稿调用 `list_jianying_audio_assets(draft_path=...)`。在 `compile_video_timeline` 的 `bgm` 或 `audio` 传返回的 `draft_path` 和相应条目的 `material_id`。配乐支持循环、音量和对白压低，音效可用 `start_frame` 排到转场或动作所在帧。以下 ID 使用列表返回值。

```json
{
  "bgm": {
    "jianying_audio": {"draft_path": "D:/草稿/音乐参考", "material_id": "曲库素材的 material_id"},
    "volume": 0.24, "duck_volume": 0.1, "loop": true
  },
  "audio": [
    {"jianying_audio": {"draft_path": "D:/草稿/音乐参考", "material_id": "音效素材的 material_id"}, "start_frame": 75}
  ]
}
```

剪映草稿选用的素材保留原生资源标识。`availability="local_file"` 的素材可用于离线混音和 OpenFilm。`availability="editor_reference"` 的素材由剪映在打开草稿时解析，实际可用性取决于客户端、账号和资源状态。读取加密参考草稿需要配置本机读取器。

### 分镜编译：一期片子一份节拍表

口播 + 图形包装的短片（观点口播、访谈、知识类）用 `compile_episode`。Agent 只写编辑意图，渲染、排帧、字幕、音效和配乐由工具完成。beat 有三种：

- 口播：`{"kind":"talk","source":"<绝对路径>","start":0.0,"end":6.5,"text":"台词"}`
- 其他画面：`{"kind":"media","source":"<B-roll、录屏或生成视频>","start":0,"end":4}`，默认静音
- 全屏动效卡：`{"kind":"graphic","recipe":"quote","data":{...},"seconds":2.6}`

口播和其他画面可以分屏，一栏放主镜头，另一栏放动效卡或另一段画面：

```json
{"kind": "talk", "source": "D:/素材/口播.mp4", "start": 12.0, "end": 21.6, "text": "……",
 "layout": {"kind": "stacked", "main": "bottom",
            "with": {"graphic": {"recipe": "list", "data": {"items": [{"title": "快", "at": 3.0}]}}}}}
```

动效卡按栏位尺寸渲染，竖版卡不会被裁成横条。`main: "top"` 把主镜头放在上栏。`items[].at` 让要点随口播出现。

口播 beat 的 `chapter` 只标记剪点。要点卡的标题和分点承载正文。list、steps、checklist、compare 等复述卡在小节里用分屏，全屏总结段的第一拍标 `"role": "summary"`。

总结段可以只放卡片、用主讲原声：graphic beat 加 `"voice": {"source": ..., "start": ..., "end": ..., "text": ...}`，工具裁出这段人声放到旁白轨，字幕跟着这段话走。

```json
{"kind": "graphic", "recipe": "compare", "role": "summary",
 "voice": {"source": "D:/素材/口播.mp4", "start": 142.0, "end": 154.0, "text": "……"},
 "data": {"title": "最好的结构", "left": {...}, "right": {...}}}
```

```json
{
  "name": "美工是外包还是自己养",
  "fps": 30, "width": 1080, "height": 1920,
  "style_preset": "editorial",
  "caption_theme": "interview",
  "caption_punctuation": "spaced",
  "caption_mode": "single",
  "caption_keywords": ["外包", "自养", "需求表"],
  "transitions": "auto",
  "music": {"preset": "warm", "volume": 0.16, "duck_volume": 0.07},
  "beats": [...]
}
```

`compile_episode(spec)` 返回 `manifest_path`、`srt_path`、`mix_path`、每拍起止时间 `episode.beats` 和 `warnings`，需要发布到剪映首页时调用 `build_draft(manifest_path=..., dry_run=false)`。同一套分镜换掉 `text` 和 `data`，就能批量产出同一包装风格的不同期。音效按 beat 自动落位，`sfx` 可覆盖默认映射，`false` 关闭该拍音效。

字幕按分句断行，不会出现行首标点。默认去掉逗号、句号等停顿标点，保留问号、叹号和引号（`caption_punctuation` 可选 `spaced`、`keep`、`strip`）。编辑风格字幕使用简体规范字体 HarmonyOS Sans SC：繁体规范字体（如台北黑体）的逗号句号居中，会显得偏高。带 90° 旋转标记的竖拍素材，全屏使用时只转码用到的区间，生成竖正副本。

分镜字幕默认字号 11.5。`caption_zone="auto"` 按源时间窗寻找遮挡较少的区域，也可按拍指定 `caption.zone` 或 `y`。

- `single`：整句带弹入，最通用
- `stack` 将有自然停顿的长句或递进分点放入预留的上下行，依次整句淡入。
- `blocks` 让列举项按字宽紧凑排列，空间不足时转为上下排列。
- `words` 是单条字幕的累加入场。普通口播放 `single` 即可。标注 `highlights` 后可在重点入点使用轻提示音。`caption_sfx` 接受 `false` 或来自剪映草稿的 `{draft_path, material_id}`。

重点词自动标黄：分屏卡要点、`caption_keywords` 和拍上的 `highlights` 只要出现在字幕里就着色。`transitions: "auto"` 在画面类型变化处自动加短转场，同机位口播保持硬切；可设 `all`、`none`，或用 `transition: false` 关掉单拍。

`list_motion_recipes` 列出可用配方、角色及参数，`create_motion_clip` 单独渲染一条用于预览。渲染结果按生成页面内容寻址缓存：改动配方实现后旧缓存自动失效。

### 新增或修改剪辑模块

[editing_modules](src/jianying_ai_mcp/editing_modules/) 的 `layouts`、`captions` 和 `callouts` 子包分别维护构图、字幕和文字标注。新增模块时，在对应子包创建文件，把执行逻辑、短说明、参数和可用示例放在一起，再到同一子包注册。修改现有模块时，在对应文件更新实现和说明，保持已在时间线中使用的模块 ID 与旧参数兼容。验证模块能被查询，并用示例编译一次时间线，检查对应画面轨或文字轨。

## 安装入口和兼容环境

Windows 使用文首的一键安装。已有 uv 的环境也可从仓库执行 `uv run --locked --python 3.12 jianying-ai-mcp setup`。命令返回生成的客户端配置路径。运行 `doctor` 或调用 `get_jianying_capabilities` 可查看本机媒体工具、语音模型、字体和浏览器状态。首次安装或补装都使用同一入口。

Python 的主环境和桌面 CLI 分别使用自己的 uv 锁。Node 来自 Python wheel，OpenFilm 与 Playwright 使用包内 npm 锁。安装阶段下载对应的 Chromium、固定提交的 Whisper small 权重及 Noto Sans/Serif SC 字体，校验字体 SHA-256，并实际启动浏览器、加载语音模型。生成的绝对路径配置可用于从其他工作目录启动的客户端。

Skill 位于 `skills/jianying-ai-editor`。未安装 Skill 的客户端可通过 `get_video_workflow` 读取同一工作方式。Windows 使用已安装的 SAPI 语音；显式调用 Edge TTS 或在线转写时需能访问对应服务。下载 URL 素材需要能访问源站。剪映首页发布、桌面导出及在线素材依赖本机应用和对应账号权限。

## OpenFilm 集成

1. `look_openfilm(project_path, start=2.5)` 预览指定时刻。也可传 `start` 和 `end` 检查一段，或省略时间查看整片。
2. 工程中的独立图卡可用 `render_openfilm(project_path, fps=30)` 制成素材，按实际帧率入轨。成片交给剪映原生导出。
3. `export_openfilm_draft(project_path)` 将整个时间线导出到 MCP 工作区。确认后传 `publish=true`，直接写入 `JIANYING_DRAFT_ROOT` 中的新目录。默认 `media="copy"` 复制素材，选择 `link` 时需保留原文件。工程导出固定使用 OpenFilm `0.4.1` 适配，复杂网页镜头与目标编辑器不支持的效果会成为渲染素材。

`project_path` 是含有 `film.html` 的本地绝对目录。OpenFilm 会执行自己的网页镜头并写入项目缓存，所以使用你信任的项目。安装入口已准备工作流使用的浏览器和运行时。

`review_export` 也可以独立检查剪映导出的 MP4。`start`、`end` 以秒为单位，单次最长检查 10 分钟，可通过 `max_frames` 控制抽样帧数。黑场和响度为自动检测指标，画面排版与字幕正确性可以通过返回的预览帧抽检。

## 剪映桌面导出

安装入口在 Windows 和 macOS 为 `jianying-tool 0.5.0` 建立独立环境，并把 CLI 路径写入生成的配置。使用自定义草稿库或非默认安装时，可指定 `JIANYING_DRAFT_ROOT` 和 `JIANYING_EXE`。macOS 桌面导出需要系统辅助功能权限。

1. 调用 `probe_jianying_desktop`，查看 CLI 版本、草稿目录和 `desktop_export_ready`。Windows 的上游基线是 `11.4.2.14459`，截图模板使用 100% 显示缩放。macOS 基线是 `5.5.0`。探测结果基于安装信息，实际窗口和导出参数在执行时校验。
2. 调用 `diagnose_jianying_draft(draft_path=...)` 检查本地绝对草稿路径。调用成功时看 `ok`、`errors` 和 `warnings` 判断工程状态。它也可以检查仍在工作区的明文草稿。
3. 把草稿发布到剪映目录，保持剪映窗口可交互，并停在首页或打开目标草稿。确认后调用 `export_jianying_mp4(draft_name=..., resolution="1080P", framerate=30)`。工具会预检草稿，使用唯一文件名等待导出完成，再用 ffprobe 检查 MP4、视频时长、分辨率短边和帧率。特殊画幅如果短边与所选档位不符，工具会明确报告校验失败，并保留导出文件以便检查。
4. 把返回的 `output_path` 交给 `review_export` 检查实际画面和响度。

同一用户的多个本地 MCP 进程共享桌面导出锁。桌面导出会占用剪映窗口。超时中止 CLI 后，剪映里的渲染可能继续，请检查窗口和本次输出目录后再重试。

Windows 6.x 及以下走 UI Automation，7.x 至 11.x 按窗口与导出控件尝试匹配，macOS 5.x 走辅助功能。已发表的现代 Windows 和 macOS 实测基线分别是 11.4.2.14459 和 5.5.0。发起导出时需要可交互界面，随后工具等待文件完成。无法驱动时返回 `manual_export_required`，向用户转达草稿名、路径和原因。

## 模块规范

`server.py` 注册工具并启动传输。`mcp_tools/` 按能力状态、草稿、媒体、OpenFilm、剪映桌面分组。MCP 入口统一通过 `common.run_tool` 加载配置并返回 `success`、`compatibility`，失败时返回 `error_type` 和 `error`。耗时的本地检查与渲染作业在线程中执行。

草稿、原生字幕、素材预设、媒体探测、成片检查和 OpenFilm 均有各自的业务模块。业务代码接收 `Settings`，可以直接测试，无需启动 MCP。OpenFilm 渲染和草稿导出复用同一个运行时和进程入口。新增工具在对应分组声明 `TOOLS`，底层模块保持对 MCP 的独立性。

## 环境变量

- `JIANYING_DRAFT_ROOT`：剪映草稿根目录
- `JIANYING_MCP_WORK_ROOT`：暂存和参考蓝图目录
- `JIANYING_EXE`：剪映程序路径，用于检测版本
- `JIANYING_DRAFT_CRYPTO`：可选的 `jy-draftc.exe` 兼容读取器；启用加密草稿学习/复制
- `JIANYING_MCP_TRANSPORT`：默认 `stdio`，也可使用 `sse` 或 `streamable-http`
- `JIANYING_FFMPEG` / `JIANYING_FFPROBE`：视频观察器使用的可执行文件
- `JIANYING_OPENFILM_CLI` / `JIANYING_OPENFILM_EXE`：可选的 OpenFilm npm CLI 路径及 Node.js 可执行文件
- `JIANYING_OPENFILM_BROWSER_CHANNEL`：OpenFilm 预览和渲染使用的可选浏览器通道
- `JIANYING_TOOL_EXE`：可选的独立环境 `jianying` CLI 路径，可从 `PATH` 发现
- `JIANYING_YT_DLP`：可选，仅 URL 输入需要
- `JIANYING_WHISPER_MODEL` / `JIANYING_WHISPER_LANGUAGE`：本地模型目录或模型名及可选语言提示
- `JIANYING_WHISPER_ALLOW_DOWNLOAD`：设为 `1` 才允许本地后端首次下载模型权重
- `GROQ_API_KEY` / `OPENAI_API_KEY` 用于云端转写。视频观察的 `transcription="auto"` 优先使用配置好的本地模型，缺少本地模型时可按已设置的密钥选择云端。剪辑配方默认使用本地 Whisper。

ffmpeg/ffprobe 优先使用显式配置，其次使用安装包中的二进制，再查找系统 PATH。云端转写会发送从素材中提取的压缩音频。需要在本地完成转写时，指定 `transcription="local"`。

## 加密草稿读取

剪映保存后的 `draft_content.json` 可能是加密文本。0.3 通过已审计的 MIT 开源 `jy-draftc` 接口调用本机剪映目录中的 `videoeditor.dll`，支持分析、参考复制和批量换料。读取时会先复制到一次性工作目录，解密结果用完即删，绝不原地改写参考草稿。

Windows x64 安装包包括由[仓库源码](native/jy_draft_crypto.cpp)构建的读取器和 MIT 许可。它使用本机剪映同版本的 DLL；已有独立读取器也可通过 `JIANYING_DRAFT_CRYPTO` 指定。先调用 `get_jianying_capabilities`，确认 `encrypted_draft_reader.available=true` 再读取加密草稿。

## 视频理解

优先使用原话找主题。只需对白时用 `analyze_video_source(detail="transcript", transcription="local")`，批量盘点也可用 `max_preview_frames=0`。需要看画面时，先看拼图，再用 `start/end`、`max_frames` 和 `resolution` 局部复查。Agent 实际读取过的代表帧可通过 `save_video_observations` 保存结论。

当前为语句级字幕时间戳，不是逐字强制对齐；代表帧也不是完整逐帧视频流。快动作、口型级同步、音乐节拍仍需专用分析器。

配置格式见 [OpenCode 配置](configs/opencode.example.json) 和 [通用 stdio 配置](configs/mcp-stdio.example.json)。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试前可使用 [测试与反馈清单](docs/testing_guide.md)。

## 当前边界

旁白、原创配乐和 Whisper 转写通过独立后端生成本地媒体，可导入剪映轨道。剪映已选曲库和音效素材可从参考草稿复用，在线目录和节拍卡点由剪映管理。桌面成片导出需要配置可选后端并通过版本检查。参考草稿支持明文 JSON，配置本地读取器后也可处理有权访问的剪映加密草稿。

蒙版、底层视频轨的纯色或模糊背景，以及视频和音频片段的音量关键帧可通过 `build_draft(..., allow_experimental_effects=true)` 试用。当前已覆盖草稿结构测试，剪映显示和播放效果需在目标版本抽检。字段见[编辑清单](skills/jianying-ai-editor/references/manifest-schema.md#实验性效果)。

`build_draft` 的 SRT 导入可通过 `srt_imports[].mode="native"` 生成独立的原生字幕轨，默认 `mode="text"` 保持普通文本轨行为。原生字幕已验证草稿结构，打开和统一编辑效果需在目标剪映版本确认。

工具返回剪映版本兼容提示。预设、在线资源和草稿结构可能随版本变化，当前环境可用性可通过 `get_jianying_capabilities` 查询。

## 参考项目

### 上游与主要依赖

| 项目 | 本仓库用法 | 仓库和发布包 |
| --- | --- | --- |
| Jianying AI MCP（Xanthus-Sanslab） | 本仓库的直接上游，提供原有 MCP、视频观察和草稿工作流。 | [GitHub](https://github.com/Xanthus-Sanslab/jianying-ai-mcp) |
| OpenFilm `0.4.1`（MIT） | 通过 npm 安装的 HTML 视频预览、渲染和草稿导出后端。剪映原生字幕结构也参考了该版本的导出实现。 | [GitHub](https://github.com/openfilm/openfilm) · [npm](https://www.npmjs.com/package/openfilm/v/0.4.1) · [许可证](https://github.com/openfilm/openfilm/blob/v0.4.1/LICENSE) |
| pyJianYingDraft `0.3.1`（Apache-2.0） | Python 草稿库，提供时间线、轨道、媒体片段和预设定义。 | [GitHub](https://github.com/GuanYixuan/pyJianYingDraft) · [PyPI](https://pypi.org/project/pyjianyingdraft/0.3.1/) · [许可证](https://github.com/GuanYixuan/pyJianYingDraft/blob/0.3.1/LICENSE) |
| jianying-tool `0.5.0`（Apache-2.0） | 独立安装的剪映桌面 CLI，用于版本探测和原生导出。 | [PyPI](https://pypi.org/project/jianying-tool/0.5.0/) |
| jy-draftc（MIT） | 本地加密草稿读取器的原生接口来源。 | [GitHub](https://github.com/wenshui330/jy-draftc) · [随附许可证](native/LICENSE.jy-draftc) |
| claude-video（MIT） | 视频观察与转写流程的参考。第三方声明保留了 Bradley Bonanno 的署名。 | [GitHub](https://github.com/bradautomates/claude-video) · [许可证](https://github.com/bradautomates/claude-video/blob/main/LICENSE) |
| MCP Python SDK（MIT） | 通过 FastMCP 注册工具并提供服务端传输。 | [GitHub](https://github.com/modelcontextprotocol/python-sdk) · [PyPI](https://pypi.org/project/mcp/) |

### 工作流参考

- 草稿的创建、编辑和保存流程参考 [VectCutAPI](https://github.com/sun-guannan/VectCutAPI/blob/main/mcp_server.py)。
- 可重开的声明式时间线参考 [FableCut](https://github.com/jcpsimmons/fablecut)。
- 本地媒体盘点、渲染与成片检查流程参考 [video-mcp](https://github.com/MHughesDev/video-mcp)。

## 许可证

本分支中由 CM-Silence 有权授权的原创贡献采用 **MIT**。使用、修改或再分发时应保留版权与许可声明。适用范围和完整条款见 [LICENSE](LICENSE)。

直接上游的公开主分支目前只找到部分代码的许可声明。继承代码的再分发仍须依据上游的适用许可或权利人的授权。公开发布包含这些代码的版本前，请向上游权利人确认其余继承代码的授权。[上游第三方声明](https://github.com/Xanthus-Sanslab/jianying-ai-mcp/blob/main/THIRD_PARTY_NOTICES.md)

`pyJianYingDraft` 和 `jianying-tool` 的 Apache-2.0 条款继续适用于各自的依赖。随发行物再分发第三方代码、二进制或字体时，按各自的许可保留声明和许可文本。再分发 Apache-2.0 组件及其修改时，按第 4 条保留适用的署名和 `NOTICE`，并标明修改过的文件。现有归属记录和相关条款见文末链接。[第三方声明](THIRD_PARTY_NOTICES.md) · [Apache-2.0 条款](https://www.apache.org/licenses/LICENSE-2.0)
