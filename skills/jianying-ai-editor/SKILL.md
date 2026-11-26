---
name: jianying-ai-editor
description: 使用剪映 AI MCP 分析视频和图片素材、制作图卡或动画、编排并验收剪映草稿与成片。用于素材选段、口播访谈、录屏教程、产品短片和参考模板。
---

# 剪映 AI 剪辑

## 默认交付

会话中找不到剪辑工具时，用安装目录的 `runtime/python/Scripts/jianying-ai-mcp.exe tools` 发现工具，用 `call 工具名 --args-file 参数.json` 直接调用。批次的 `transcript_path` 提供全部原片对白。已分析的批次可用 `export_media_batch_transcript` 补导。

1. 明确交付画幅、受众和要讲清的一件事。有指定选段时直接使用。需要挑素材时，调用 `analyze_media_batch`，按源时间码选择能支持叙事的画面和原话。
   制作卖家案例口播时，以 `style_preset="seller_reference"` 开始，按客户故事、前后对照或方法清单决定包装。先在素材库找能举证的画面。
2. 先用 `plan_video_script(brief,spec)` 产出带时码的视听脚本，并向用户展示脚本表。每镜写本期原话、画面要说明什么、构图与素材。主体由实拍承载；列举观点用随原话逐项出现的 HTML 卡；场景或流程用剪映库存，在该拍写 `stock.query`（如“手持胶带机沿纸箱中缝封口”）。按客户故事、前后对照或方法清单选择结构，一镜通常 5–8 秒。
3. 规划工具会按场景时长搜索本机剪映索引。Agent 主动核对候选的画面与来源，将场景编号和选定的 `shot_id` 传给 `execute_video_script`。该工具获取媒体、按同一份脚本发布、回读全屏或分屏轨道。遇到 `editor_only` 或空结果，可换能说明同一动作的关键词，或明确记录 `keep_camera`。按返回的 `caption_safety.review` 复查具体时段。需要成片时再按交付目标导出该草稿。
4. 已有锁定选段和配方时，原有 `plan_video_edit` / `deliver_video_edit` 可继续复刻和导出。素材迁移传 `media_root`。

交付 MP4 时，在脚本执行阶段传 `openfilm_project=true`，用回执 `render_call` 在后台渲染同一时间线。剪映专用原生引用以剪映渲染为准。导出后在第二项字幕入场前后各看一帧，核对首项的字号、位置和脸部遮挡。

剪辑使用 MCP 自带的工具和包装配方。常用的 `style_preset` 包括口播访谈的 `editorial`、操作演示的 `clean`、纪实的 `documentary` 和资讯的 `broadcast`。客户端可直接通过 `get_video_workflow` 取得说明。

`analyze_media_batch` 提供实拍和图片预览、带时间码的对白。主题未定时，原声和画面都能提供线索。配乐可以铺情绪和节奏，音效可以强调动作或衔接。两者都可用本地音频或参考草稿里的剪映素材，`list_jianying_audio_assets` 可查名称、时长和可用状态。`create_music_bed` 可本地合成简单配乐。

访谈可选全屏或上下分屏，字幕可用白字黑边加阴影，关键词可标黄。人物身份或重点可用独立文字。选动效时用 `list_motion_recipes` 查看配方及字段。

按需用 `list_video_editing_modules` 查构图（`layout`）、字幕（`caption`）或文字标注（`callout`）。先用 `category` 浏览相关模块，再传 `module_id` 读取参数与示例。

## 分镜编译（批量出片）

脚本执行复用 `compile_episode` 进行渲染、排帧和铺音效。直接调试已写好的单独分镜时也可调用它。

**盘点素材**

`analyze_media_batch` 批量转写并缓存。将其返回的 `batch_id` 传给 `select_episode_take(line, batch_id=...)`，按句子在本项目挑取镜头和源入出点。`list_episode_takes(batch_id=...)` 可查看本批次有转写的素材。

**剪映库存镜头。** 三份参考草稿只用于学习分镜、字体和动画。调用 `list_jianying_stock_videos` 盘点剪映缓存或 `JIANYING_STOCK_ROOT` 专用下载目录，抽帧后用 `index_broll_shots` 记录画面、来源标题和源区间。按口播用 `search_broll_shots(query=...,min_duration_ms=6000)` 取库存候选。需要复用用户另行提供的自摄素材时，可在独立的 `personal` 库检索。

自动入轨时搜索 `availability="timeline_ready"`。能在本地解码的镜头也可用 OpenFilm 预览。仅能在剪映播放的缓存，先核对画面和时长，以 `editor_verified_ms` 入库。用 `bind_jianying_stock_selection(selection, shot_ids)` 绑定专用官方素材采集时间线中指向同一缓存的原生记录。绑定成功后状态为 `native_draft`，用剪映预览与导出。未取得可读取的记录时保留 `editor_only`。

**构图与停留。** 一个动作或完整意思通常留 5–8 秒。步骤卡等到最后一项读清再切走。卖家风格默认检查 4 秒下限，包含很短的回切。原声、字幕和动画可以在同一镜内继续。`broll` 的 `{"shot_id":"所选ID","at":12,"duration":6}` 覆盖第 12–18 秒，加 `"layout":{"kind":"stacked","main":"bottom","split":0.48}` 可保留下栏讲者。聊天或图表需保全边缘时用 `fit:"contain"`。

**后台入轨。** 搜索后按 ID 调用 `get_broll_shot`，核对画面描述、源片范围与 `timeline_ready`。调用 `place_broll_shots`，提供本期分镜 `spec` 或 `spec_path`、所选 `placements` 和新片名（分镜的 `name`）。默认试编译，正式落轨传 `dry_run=false`。以返回的 `readback` 核对实际轨道、目标时间、源时间和静音。若返回 `not_timeline_ready`，保留原口播画面或改选可用镜头。

**设计分镜**

beat 有三种：
- 口播：`{"kind":"talk","source":"<绝对路径>","start":0.0,"end":6.5,"text":"台词（可选，省略则读转写）"}`
- 其他画面：`{"kind":"media","source":"<B-roll、录屏或生成视频>","start":0,"end":4}`，图片用 `seconds`，默认静音。
- 全屏动效卡：`{"kind":"graphic","recipe":"<配方>","data":{...},"seconds":3.0}`

口播和其他画面可以分屏：加 `"layout": {"kind":"stacked","main":"bottom","with":{"graphic":{...}}}`，主镜头在下、动效卡在上。`main:"top"` 反过来；`with` 也可以是 `{"media":{"source":...}}`，用来叠实拍或生成视频。动效卡按栏位尺寸渲染。list、steps、checklist 的 `items[].at` 让每条随口播出现。

一期片子的节奏：
- 小节由口播和剪点交代。`chapter` 写入时间线标记。
- 小节里讲到的要点放进口播分屏，随讲随出，不要在小节结尾再用全屏卡复述一遍。
- 全屏要点卡（list、steps、checklist、compare）只放在总结段。总结段第一拍标 `"role":"summary"`，否则编译会报错。
- 总结段可以只放卡片、用主讲原声：给 graphic beat 加 `"voice": {"source": ..., "start": ..., "end": ..., "text": ...}`，工具会提取这段人声并让字幕跟着走，不需要再切回实拍。
- 长口播用分屏或 `media` 换画面，数字、金句可用全屏短停，最后接 `end`。
- 转场默认在画面类型变化处自动加短转场（`transitions: "auto"`），长片可设 `"all"`。

**字幕。** 每拍先用普通单句。原话有递进或列举关系时再选择分组。详细制作与验收步骤由 `list_video_editing_modules(category="caption")` 返回。

- `stack` 使用固定的上下行，`blocks` 按字宽紧凑排列。`words` 是单句入场。
- 重点词自动标黄（暖黄 `#FFDB4D`）：分屏卡上的要点、`caption_keywords` 和拍上的 `highlights` 只要出现在字幕里就会着色。一条最多两处。
- 字幕每条按分句断行，不会出现在行首，也默认去掉逗号句号等停顿标点。

固定取值见 [包装规范](references/packaging-rules.md)，照抄即可，不要自定字号、位置或动画名。

`list_motion_recipes` 返回各配方的角色和字段，配色跟随 `style_preset`。

**编译分镜**

```json
{
  "name": "片名",
  "fps": 30,
  "width": 1080,
  "height": 1920,
  "style_preset": "editorial",
  "caption_theme": "interview",
  "music": {"preset":"warm","volume":0.16,"duck_volume":0.07},
  "beats": [...]
}
```
`compile_episode(spec)` 返回 `manifest_path`、每拍起止时间 `episode.beats` 和制作提示。有字幕时返回 `srt_path`，需要试听可传 `mixdown=true`。`build_draft(manifest_path=...)` 默认为试运行，需要发布时传 `dry_run=false`。

字幕按分句断行，默认去掉逗号、句号等停顿标点（`caption_punctuation`）。带旋转标记的竖拍素材会自动生成竖正副本。

**批量替换**：同一套分镜，把 beat 的 `text`/`data` 换成新题材，就能批量产出同一包装风格的不同期片子。完整示例见 `examples/episodes/`。

卖家口播可用 `get_video_workflow(profile="reference", detail="profile")` 查询《不用操心10》的聊天举证、《大单8》的前后对照和《复盘11》的逐项提问。换题时按新原声定位镜头与落字，从官方素材面板选取补充画面。可替换主讲素材与库存镜头 ID 的分镜样例见 `examples/episodes/seller-project-review.json`。

**音效。** `caption_sfx` 只在标注重点词的字幕入点使用，可指定剪映音效或设为 `false`。普通口播留白。

**发布前自检**（逐条核对，不符就回去改分镜）：

1. 每条字幕字号 ≥ 11.5，没有缩到 10 以下塞整句。
2. 字幕没压住脸或演示区域；分屏时在主讲人一栏。
3. 堆叠的每个断点都与口播停顿或列举对应，已出现的句子留在原位。
4. 一条字幕最多两处黄字，且是名词或关键短语。
5. 画面省去小节角标，全屏复述卡位于总结段。
6. 转场只在画面类型变化处，同机位口播之间硬切。
7. 单拍口播超过 8 秒时核对画面内是否有新的信息。例如《复盘11》的长镜头内，三个问题由口播依次带出。
8. 方法清单可用「卡片 + 原声」收束，客户故事可保留讲者说出反馈或结果。
9. `warnings` 为空或已逐条处理。

清单和结构校验只说明字段齐全：`keyframes`、`items[].at` 只代表排了运动和时点，没打开画面核对过的写「待目检」。逐步骤的做法随工具返回：`get_video_workflow(section="edit")` 一并给分镜指引，`list_video_editing_modules(category="caption")` 给字幕指引，`list_motion_recipes` 给动效指引。

[视频观察](references/video-observer.md) · [包装规范](references/packaging-rules.md) · [剪辑清单](references/manifest-schema.md) · [能力与兼容性](references/capabilities.md) · [加密草稿](references/encrypted-drafts.md) · [客户端配置](references/client-config.md)
