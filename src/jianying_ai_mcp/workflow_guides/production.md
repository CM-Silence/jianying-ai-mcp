# 视频制作与剪辑

按任务需要查阅相关章节。叙事、镜头、节奏和包装由用户要求与素材决定。

<!-- section: plan -->
## 1. 交付与素材规划

- 根据目标、受众、画幅、语言和已有素材安排内容。交付可包括可编辑草稿、MP4 和 SRT。实拍、录屏、照片、图卡、对白、音效和配乐都可放进时间线。
- 新需求先用 `plan_video_script` 生成带时码的口播与画面对照表。每镜选择主讲实拍、分项 HTML 动画或剪映库存，库存镜头用 `stock.query` 自动检索。Agent 挑选画面后用 `execute_video_script` 按同一脚本获取并落轨。已有定剪选段可用 `plan_video_edit` 固定素材与字幕。

<!-- section: sources -->
## 2. 素材分析与选段

用 `list_jianying_stock_videos` 列出剪映库存视频，从画面确认可用动作后用 `index_broll_shots` 保存时间码、描述和原素材标题。按口播用 `search_broll_shots(query,min_duration_ms=6000)` 取候选。三份参考草稿只用于学习分镜、字体和动画。

自动入轨时搜索 `availability="timeline_ready"`。能在本地解码的镜头也可用 OpenFilm 预览。仅能在剪映播放的缓存，先核对画面和时长，以 `editor_verified_ms` 入库。用 `bind_jianying_stock_selection(selection, shot_ids)` 绑定专用官方素材采集时间线中指向同一缓存的原生记录。绑定成功后状态为 `native_draft`，用剪映预览与导出。未取得可读取的记录时保留 `editor_only`。

- `analyze_media_batch` 盘点素材，提供带时间码的对白和少量画面。`get_media_batch_context` 按词或时间范围取证，`find_repeated_takes` 找相近口播供回看。
- `analyze_video_source` 可补取局部画面和字幕，`parse_media_info` 查媒体参数，`prepare_media_assets` 生成适合剪辑的本地副本。`save_video_observations` 可保存已经查阅的画面证据。
- 把 `analyze_media_batch` 返回的 `batch_id` 传给 `list_episode_takes` 或 `select_episode_take`，即可限定在本项目内按原话选段。后者返回源路径和入出点，可用 `avoid` 避开刚用过的素材。

<!-- section: edit -->
## 3. 时间线与草稿

卖家口播优先使用 `style_preset="seller_reference"`。有聊天、现场或产品画面时，用 `broll` 在提到它的那句话补画面，原声走主镜。`{"shot_id":"检索返回的 ID","at":23,"duration":6}` 全屏展示；加 `"layout":{"kind":"stacked","main":"bottom","split":0.48}` 让素材占上栏、实拍占下栏。两种构图都在第 29 秒结束。清单、数字或前后关系可用图卡，无需为每段口播都生成动画。

- 通用剪辑只需给 `plan_video_edit` 片名与有序 `clips`。每镜提供 `source`，视频可填源时间 `start/end`，照片用 `hold_seconds`。省略 `caption` 时工具为有原声的选段读取字幕或本地转写。可选 `panel={"recipe":"steps","data":{"title":"交付步骤","items":["定剪","审片"]}}` 生成同风格分屏动效。以返回的配方作为定剪版本。

- `compile_video_timeline` 按帧安排镜头、叠层、声音和字幕。`shots[].transcript_srt_path` 会随源区间裁切及变速映射。工具返回清单路径，也可额外生成 OpenFilm 项目。
- 访谈可在 `shots[].layout.kind` 逐镜选 `full`（默认）、`stacked`（上下分屏）或 `interview_box`（下方采访框）。后两种用 `top.source` 指定副画面，主镜头保留 `source` 和原声；`main="top"` 让主镜头在上栏。副画面默认 `fit="cover"` 裁切铺满，比例差异大的设计稿用 `contain`，或按栏位尺寸制作。可用 `top_fraction` 与裁切重心调整构图。
- `list_video_editing_modules(category="layout")` 可查构图模块。传入所选 `module_id` 可查看参数和示例。

### 分镜节拍编译

`compile_episode` 接收编辑级分镜，不是帧级时间线。工具负责渲染动效、按 fps 排帧、生成字幕、铺音效和配乐，返回剪映清单路径和节拍表。beat 有三种：

- `talk`：口播实拍，含 `source`、`start`、`end`，保留原声。
- `media`：其他画面，如 B-roll、录屏、产品图或 AI 生成视频。含 `source`，视频给 `start`/`end`（或 `seconds`），图片给 `seconds`。默认静音。
- `graphic`：全屏动效卡，含 `recipe`、`data`、`seconds`。

**分镜构图。** `talk` 和 `media` 可加 `layout` 分屏：本拍画面占一栏，`layout.with` 占另一栏。

```json
{"kind": "talk", "source": "D:/素材/口播.mp4", "start": 12.0, "end": 21.6, "text": "……",
 "layout": {"kind": "stacked", "main": "bottom", "split": 0.5,
            "with": {"graphic": {"recipe": "list", "data": {"items": [{"title": "快", "at": 3.0}]}}}}}
```

- `kind` 可选 `stacked`（上下分屏）或 `interview_box`（下方描边框）。`main` 设主镜头在 `bottom`（默认）还是 `top`。`split` 是上栏占比（0.30–0.65）。
- `with.graphic` 按栏位实际尺寸渲染，竖版卡不会被裁成横条。`with.media` 放其他实拍或生成视频，可设 `start`、`focus`，以及 `fit`（`cover` 裁切铺满、`contain` 完整显示）。
- list、steps、checklist 的 `items[].at`（秒，相对本拍开始）让每条随口播出现。

**结构规则（编译时检查）。**

- 用口播或画面转场衔接小节。小节标记只用于编排。
- list、steps、checklist、compare 这类复述卡，在小节里放进口播分屏随讲随出。全屏使用只限总结段：在总结段第一拍标 `"role": "summary"`。
- 全屏 `graphic` 适合开场（hook）、短停强调（quote、number、headline）和片尾（end）。一段口播较长时，用分屏或 `media` 换画面，不要靠全屏卡打断。

**总结段配音卡。** 总结时不必再切回实拍：给 graphic beat 加 `voice`，卡片展示要点的同时播放主讲原声。

```json
{"kind": "graphic", "recipe": "compare", "role": "summary",
 "voice": {"source": "D:/素材/口播.mp4", "start": 142.0, "end": 154.0, "text": "……"},
 "data": {"title": "最好的结构", "left": {...}, "right": {...}}}
```

工具从原素材裁出这段人声放到旁白轨，字幕跟着这段话说；`seconds` 省略时按 `voice` 时长。要点照常用 `items[].at` 对齐。

**转场。** `transitions` 默认 `auto`：画面类型变化（实拍 ↔ 卡片）和小节切换处自动加短转场，同机位口播之间保持硬切。可设 `all`（每个切点都过渡）或 `none`，单拍用 `transition: false` 关掉。剪映清单写入原生转场，OpenFilm 预览用同位置的可移植转场。

**字幕。** 竖屏字号 11.5（`caption_size` 可调），白字黑描边粗体。按分句断行，不在词中间切开，也不会让标点出现在行首。默认 `caption_punctuation="spaced"`：逗号、句号等停顿标点改为空格或去掉，保留问号、叹号、引号和书名号。`keep` 保留原标点，`strip` 连问号叹号一起去掉。`caption_max_chars` 设每条字数上限，竖屏默认 14。口播传 `text` 时按阅读长度分配时间；不传则重新断开该区间的转写字幕。

- **字幕落位。** 默认依据成片实际使用的画面和裁切后的人物栏位抽帧检查人脸，按整组最终文字边界避让。在 `caption_safety.review` 指出的时段复看脸、嘴型和演示区。显式指定 `caption.zone` 或 `y` 时由编排者核对该位置。
- **呈现方式。** `single` 是常规单句。源语句存在递进停顿或列举时选 `stack` 或 `blocks`，新句整句淡入，前句保持固定。`words` 属于单句入场。
- **重点标黄。** 分屏卡上的要点标题、`caption_keywords` 和拍上的 `highlights` 只要出现在字幕里，就会标成暖黄。剪映草稿逐字着色，OpenFilm 预览同样显示。
- 返回结果里 `episode.beats[].caption_zone` 记录每拍的实际落位，发布前可快速核对。

**素材。** 带 90° 旋转标记的手机或摄影机素材（剪映会按横屏登记），全屏使用时自动只转码用到的区间，生成竖正副本（`source_proxy="auto"`，可设 `off`）。分屏栏位本身会烘焙旋转。

**声音。** `music: {preset, bpm, volume, duck_volume}` 用本地合成配乐，口播期间自动压低。也可改传 `bgm` 使用自己的音乐文件，两者二选一。

- `list_motion_recipes` 返回配方、角色（开场/转场/复述/强调/片尾）和字段，配色跟随 `style_preset`。
- `highlights` 标出的重点可在字幕入点轻响一次。`caption_sfx` 接受 `false` 或剪映音效的 `{draft_path,material_id}`。`list_sound_cues` 可查随附音效。
- 固定取值（字号、落位、动画、转场、分屏、总结）见 `skills/jianying-ai-editor/references/packaging-rules.md`，照抄不要自定；`examples/episodes/packaged-talk-template.json` 是可直接改文案的骨架。
- 返回结果中的 `warnings` 会提示副画面被大幅裁切、繁体规范字体导致标点偏高等问题，发布前处理。
- `select_episode_take` 按一句台词在转写缓存中挑出最匹配的素材和区间，可直接写进 beat。`avoid` 可避开刚用过的素材，制造景别变化。
- 已有分镜 JSON 可传 `compile_episode(spec_path=..., media_root=..., include_manifest=true)`，把 `manifest` 交给 `build_draft(dry_run=false)` 发布，然后用 `export_jianying_mp4` 原生导出。`openfilm_project=true` 可生成工程供定点预览。
- `build_draft` 可读清单路径，`dry_run=true` 校验结构。清单媒体源为绝对路径，时间为微秒整数或带单位字符串。带 `_ms` 的证据是毫秒，OpenFilm 和成片抽检工具的 `start`、`end` 用秒。
- `list_video_style_presets` 可查风格，`list_jianying_presets` 可查单项资源。`shots[].chapter` 仅记录剪点。

<!-- section: finish -->
## 4. 声音、画面与字幕

- `create_video_graphic` 可做图卡或透明人物条。`create_video_animation` 可做本机浏览器动效。草稿文字轨及字幕可继续编辑。
- `caption_theme="interview"` 可套用白字、黑描边与阴影。`captions[].highlights` 可指定原文中的词语，只将匹配的词标为暖黄。
- 如需标注身份或当句重点，使用 `shots[].overlays[].role` 的 `person` 或 `emphasis`，用 `offset_frame` 和 `frames` 排期。
- `list_video_editing_modules` 可按 `category="caption"` 或 `category="callout"` 查字幕或文字标注模块。传入所选 `module_id` 可查看参数和示例。
- 直接用 `build_draft` 导入 SRT 时，`srt_imports[].highlights` 可传 `[{"cue": 1, "ranges": [[0, 2]]}]` 对第一条字幕的前两个字符着色。区间以清理后的字幕文本计数，从 0 开始，右端不含。
- `list_narration_voices` 和 `synthesize_narration` 查询及生成旁白。配乐可用本地文件、`create_music_bed` 或参考草稿中的素材。`list_jianying_audio_assets` 列出指定草稿已选的音乐与音效和本地可用状态，可将引用交给时间线的 `bgm`、`audio`。
- 时间线支持对白期间压低配乐和输出混音。`srt_imports` 可导入成片时间线的字幕，`mode="native"` 可写入剪映原生字幕任务。

<!-- section: review -->
## 5. 导出与验收

- `deliver_video_edit` 发布可编辑草稿并尝试剪映原生导出。成功后用 `review_path` 验收。返回 `manual_export_required` 时，向用户报告草稿名、绝对路径和原因，提示在剪映手动导出，然后结束导出。

- `get_jianying_capabilities` 查后端就绪状态。`build_draft` 用 `dry_run=false` 发布。`export_openfilm_draft` 用 `publish=true` 发布到剪映目录。`diagnose_jianying_draft` 可检查草稿结构和素材引用。
- `look_openfilm` 用于预览工程。已有剪映草稿可用 `export_jianying_mp4` 执行原生导出。
- `review_export` 可对实际 MP4 检查媒体信息、画面和响度。`verify_audio_dialogue` 可复核关键原话，`master_video_audio` 可按交付响度生成新 MP4。按实际检查说明结果与待确认项。
- 需要复刻上一版时，将第一次的 `review_path` 作为 `reference_review_path` 传给 `deliver_video_edit`。对照结果分别记录在 `same_decoded_video`、`same_decoded_audio` 和 `same_srt`。
- 考察叙事时，播一遍核对首镜是否交代主题、每个切点是否保留完整意思、要点出现时是否有对应画面。按时间码复听专名、数字和结论，再检查字幕有无遮挡。修改选段或文案后，存一份新配方作为下一版。
