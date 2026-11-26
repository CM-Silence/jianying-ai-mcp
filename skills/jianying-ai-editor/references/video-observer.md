# 视频观察与剪辑规划

## 选择分析档位

| 任务 | 初次参数 | 复查策略 |
|---|---|---|
| 字幕整理、内容摘要 | `transcript` | 无画面问题时不抽帧 |
| 通用剪辑、长视频选段 | `efficient`, 24–40帧 | 对候选区间使用 `balanced` |
| 参考成片、镜头结构 | `balanced`, 40–80帧 | 对快速或细节片段局部 `dense` |
| 小字、界面操作 | 先512px | 仅相关区间升至1024px |

素材堆或主题未定时先用 `analyze_media_batch` 看带时间码的原声线索。已知候选范围后再局部抽帧。对白主题从实际语言证据概括，配合画面验证人物、动作和上下文。

## 工具输出

`analyze_video_source` 返回：

- `analysis_id`：缓存和保存观察结论的标识。
- `metadata`：时长、分辨率、编码、音轨状态。
- `frames`：`time_ms`、本地绝对路径和选择原因。
- `transcript`：语句级 `start_ms/end_ms/text`。
- `transcript_srt_path`：可直接导入剪映草稿的 SRT。
- `report_path`：完整本地 JSON 报告。

单素材初次默认返回最多 120 个字幕段，`transcript_limit` 可缩小到 0，全量存盘。已定位区间时先传 `start_ms/end_ms`；更长内容可通过 `get_video_analysis_context` 分页。批量结果默认只返回有上限的对白摘录和少量视觉预览，用 `get_media_batch_context` 按素材编号、关键词或时间范围查询。

本地素材必须使用绝对路径。URL 需要配置 `yt-dlp`。旁挂 `.srt/.vtt` 优先，然后尝试内嵌文字轨。`transcription="local"` 使用本地 Faster Whisper，`auto` 优先使用配置好的本地模型。ASR-only 或混音复核时使用 `prefer_subtitles=false`。`transcript_status` 为 `unavailable` 时保留证据缺口，`quality="review"` 的原话优先复听。

## 观察代表帧

打开需用于结论的代表帧或拼图。涉及细节时打开原帧，保存实际读到的内容：

```json
{
  "observations": [
    {
      "time_ms": 12500,
      "description": "中景，人物面向镜头讲话，背景保持不变",
      "tags": ["人物", "口播", "中景"],
      "confidence": 0.9
    }
  ],
  "edit_candidates": [
    {
      "start_ms": 11000,
      "end_ms": 18500,
      "reason": "句意完整且画面稳定，适合作为成片开场",
      "confidence": 0.82
    }
  ]
}
```

只写可见事实。代表帧之间的动作连续性属于推断，标低置信度；需要时局部重跑。

## 转成剪映清单

- 候选区间映射为视频片段的 `source_start/source_duration`。
- 未裁切整条源视频时可直接使用 `transcript_srt_path`。重排候选区间时必须重映射字幕时间；逐字高亮、口型级对齐需要额外强制对齐器。
- 场景变化可作为切点候选，不等同于最终艺术判断。
- 节拍卡点、波形同步和响度关键帧不由视频观察器完成。

## 隐私和模型兼容

- 视频和代表帧留在本机；无字幕且启用 Whisper API 时只上传压缩音频。
- 支持图片输入的 Codex/OpenCode 模型可直接读取帧。
- 纯文本 DeepSeek Harness 只能使用字幕和已有画面描述；应配置视觉模型旁路，保存观察后再交给文本模型规划。
- 不在 MCP 返回中重复嵌入所有图片；复用本地路径与缓存，控制 Token。
