# 口播与访谈

已有 SRT 时，`analyze_video_source` 可用 `subtitle_path`、`detail="transcript"` 和 `transcription="none"` 查原话与时间。选段时可用原话的上下文核对说话人和条件，`verify_audio_dialogue` 可辅助核对混音内的关键句。

原声、反应镜头、补充画面和图卡可按叙事需要组合。只想用补充镜头的画面时可设 `volume=0`。`compile_video_timeline` 可将源字幕随裁切和变速映射到成片时间。

卖家案例口播可用 `get_video_workflow(profile="reference", detail="profile")` 查看聊天举证、前后对照、复盘三问的剪点、文字层和声音提示。
