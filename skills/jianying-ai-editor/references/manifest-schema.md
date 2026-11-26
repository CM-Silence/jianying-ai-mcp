# 紧凑编辑清单

## 最小示例

```json
{
  "draft": {"name": "产品短片_01", "width": 1920, "height": 1080, "fps": 30},
  "tracks": [
    {
      "type": "video",
      "name": "主画面",
      "segments": [
        {
          "source": "<ABSOLUTE_VIDEO_PATH>",
          "start": "0s",
          "duration": "3s",
          "source_start": "1s",
          "source_duration": "3s",
          "speed": 1.0,
          "transition": {"name": "叠化", "duration": "0.4s"}
        }
      ]
    },
    {
      "type": "text",
      "name": "标题",
      "segments": [
        {
          "text": "示例标题",
          "start": "0s",
          "duration": "2s",
          "style": {"size": 8, "bold": true, "align": 1},
          "border": {"color": [0, 0, 0], "width": 20},
          "clip": {"transform_y": 0.65}
        }
      ]
    }
  ]
}
```

## 公共字段

- 时间使用微秒整数或 `0.5s`、`2s` 等字符串。
- 所有片段：`start`、`duration`。
- 媒体片段：`source` 绝对路径；可选 `source_start`、`source_duration`、`speed`、`volume`、`change_pitch`。
- `clip`：`alpha`、`rotation`、`scale_x/y`、`transform_x/y`、`flip_horizontal/vertical`。

## 视频或图片片段

- `transition`: `{name, duration?}`
- `filter`: `{name, intensity?}`
- `effects`: `[{name, params?}]`
- `mix_mode`: 如 `滤色`
- `chroma`: `{color: "#00FF00FF", intensity?, edge_smooth?, spill?}`
- `animations`: `[{category: video_intro|video_outro|video_group, name, duration?}]`
- `keyframes`: `[{property: position_x|position_y|uniform_scale|rotation|alpha, time, value}]`

图片也放在 `video` 轨，`source` 指向本地图片。

## 音频片段

- `fade`: `{in, out}`
- `effects`: `[{category: scene|tone|speech_to_song, name, params?}]`

## 实验性效果

调用 `build_draft` 时，在 `manifest` 外传 `allow_experimental_effects=true`。先用 `dry_run=true` 验证写入，再用小样检查剪映实际效果。

- 视频或图片片段的 `mask` 使用 `{name, center_x?, center_y?, size?, rotation?, feather?, invert?, rect_width?, round_corner?}`。用 `list_jianying_presets(category="mask")` 查询可用名称。中心用像素，旋转用角度，大小和矩形宽度用大于 0 且不超过 1 的比例，羽化和圆角用 0–100。宽度和圆角仅用于矩形蒙版。
- 底层视频轨的 `background_filling` 使用 `{"type": "blur", "blur": 0.2}` 或 `{"type": "color", "color": "#000000FF"}`。模糊强度范围为 0–1，颜色必须是 `#RRGGBBAA`。
- 视频及音频片段的 `keyframes` 可加 `{"property": "volume", "time": "0.5s", "value": 0.4}`。时间以片段起点为基准，使用整数微秒或带单位的时间字符串，范围为 0 至片段时长。音量必须大于或等于 0，`1.0` 为原音量。同一片段的音量关键帧时间需唯一。

## 文本片段

- `text` 必填；可选 `font`。
- `style`: `size`、`bold`、`italic`、`underline`、`color`、`alpha`、`align`、`vertical`、`letter_spacing`、`line_spacing`、`auto_wrapping`、`max_line_width`。
- `border`、`background`、`shadow` 对应文本描边、背景和阴影。
- `bubble`: `{effect_id, resource_id}`；`flower_text_resource_id` 为资源 ID。
- `animations`: category 使用 `text_intro`、`text_outro`、`text_loop`。
- `keyframes` 支持位置、缩放、旋转和透明度。

## 贴纸、独立特效和独立滤镜

- 贴纸轨：`{resource_id, start, duration, clip?, keyframes?}`。
- effect 轨：`{name, start, duration, params?}`。
- filter 轨：`{name, start, duration, intensity?}`。

## SRT

顶层加入：

```json
{
  "srt_imports": [
    {
      "source": "<ABSOLUTE_SUBTITLE_PATH>",
      "track_name": "字幕",
      "time_offset": 0,
      "style": {"size": 5, "align": 1, "auto_wrapping": true},
      "clip": {"transform_y": -0.8}
    }
  ]
}
```

SRT 默认以 `mode: "text"` 导入普通文本轨。显式传 `mode: "native"` 可写入剪映原生字幕任务与轨道。可选传 `language`（如 `zh-CN` 或 `en-US`）、`style` 和 `clip`。原生模式接受绝对路径的 UTF-8 `.srt`，支持带符号的微秒数或时间字符串作为 `time_offset`，偏移后的字幕按顺序排列、互不重叠，并落在已有草稿时长内。字幕轨名称需唯一。请在目标剪映版本抽检原生字幕样式和统一编辑行为。

两个模式均支持可选的 `decoration`，可用 `font`、`bubble`、`flower_text_resource_id`、`border`、`background`、`shadow` 和 `animations`。字体和动画名称可以从 `list_jianying_presets` 查询。气泡使用 `{effect_id, resource_id}`，花字使用对应的资源 ID。

两个模式都可用 `highlights=[{"cue":1,"ranges":[[0,2]]}]` 高亮一条字幕的前两个字符。`cue` 按导入顺序从 1 开始，区间按去除 SRT 格式标签并解码后的正文计算，左闭右开。可用 `highlight_color` 传三个 0–1 数值覆盖暖黄色。

`compile_video_timeline` 支持 `editorial`、`documentary`、`broadcast` 和 `clean`，可通过 `list_video_style_presets` 挑选。`shots[].chapter` 声明章节入口，编译器把该风格的内置转场加在前一镜，并生成 MP4 对应效果。手选转场时，`shots[].transition` 写剪映资源，`shots[].portable_transition` 写同一切点的 MP4 效果，`frames` 为偶数且不超过相邻较短镜头。标题细调可在文字叠层填写 `portable_animation`，如 `{kind:"rise",duration_us:240000}`，与剪映 `animations` 同时保存在各自输出中。

### 剪映曲库与音效

音频轨片段可使用 `jianying_audio: {draft_path, material_id}` 代替 `source`。参数来自 `list_jianying_audio_assets`，片段仍按 `start`、`duration`、`source_start` 和 `source_duration` 排期。帧级时间线的 `bgm` 和 `audio[]` 也接受相同选择器。本地可读资源可混音。编辑器引用由剪映在打开草稿时解析。

## 参考草稿替换

素材替换项：

```json
{
  "track_type": "video",
  "track_name": "主画面",
  "segment_index": 0,
  "source": "<ABSOLUTE_REPLACEMENT_PATH>"
}
```

文字替换项：

```json
{"track_name": "标题", "segment_index": 0, "text": "新标题", "recalc_style": true}
```

不知道轨道名时可传同类型 `track_index`。先调用 `analyze_reference_draft` 获取轨道和片段下标。
