# 能力与兼容边界

上游桌面验证基线为剪映专业版 `10.0.5.13816` 与 pyJianYingDraft `0.3.0`。当前写入依赖为 pyJianYingDraft `0.3.1`。

## 已验证

- 新建草稿、画布、帧率、多视频/音频/文本/贴纸/滤镜/特效轨
- 本地视频、音频、图片；截取、整体变速、画中画、混合模式
- 音量、淡入淡出、场景音、音色、声音成曲
- SRT 导入、文本字体/颜色/背景/阴影、描边 20、气泡、花字
- 转场、视频/文本动画、滤镜、画面特效、色度抠图
- 视频/文本/贴纸位置和缩放关键帧
- 明文参考草稿分析、复制、按片段换料/换字和批量制作
- 配置本地读取器后，加密参考草稿分析、复制和批量制作（只读源文件）
- 本地视频元数据、代表帧、局部区间复查和分析缓存
- SRT/VTT 旁挂字幕解析、规范 SRT 输出

在线字体、贴纸、动画和效果可能受版本、账号、会员、网络与本地缓存影响。

## 查询工具

- `parse_media_info` 读取本地视频、音频或图片的媒体参数，需要 ffprobe。
- `list_jianying_presets` 查询草稿库内置预设。把返回的 `name` 填入相应清单字段。
- `list_video_style_presets` 提供可共用的包装风格，包括三份卖家口播参考稿的 `seller_reference`。剪映库存视频可由 list_jianying_stock_videos 盘点，确认画面后的区间可用 `index_broll_shots` 保存、`search_broll_shots` 搜索，选定后用 `broll` 写入全屏或分屏画面。
- `list_video_editing_modules` 提供可单独选用的构图、字幕和文字标注。传 `module_id` 可读取时间线参数和示例片段。
- `list_jianying_audio_assets` 查询参考草稿已有的剪映音乐与音效，返回素材标识与本地可用状态。
- `diagnose_jianying_draft` 通过可选桌面后端检查草稿的时间线、素材和引用，返回 `ok`、`errors` 和 `warnings`。

## 实验性或依赖环境

- 蒙版、底层视频轨的背景填充、视频或音频的音量关键帧需要在 `build_draft` 中显式启用 `allow_experimental_effects`。剪映实际效果需在目标版本抽检。
- `export_jianying_mp4` 需要隔离安装 `jianying-tool 0.5.0` 和可交互的剪映窗口。用 `probe_jianying_desktop` 查看版本和可用性。非基线版本通过基本探测后，可用 `allow_unverified_version=true` 试用。
- 已加密草稿：依赖 Windows、本机剪映 `videoeditor.dll` 和 `jy-draftc` 兼容读取器；版本导出符号变化时会明确失败。
- 视频语义：只观察抽取的代表帧，不是逐帧读取；快动作需要局部加密采样。
- 自动转写：可选本地 Faster Whisper 或明确启用的 Groq/OpenAI Whisper，输出语句级时间戳。
- 内嵌文本字幕可直接提取。Windows 本机旁白依赖已安装的 SAPI 语音。
- 本地生成图卡和帧级时间线可直接使用。HTML 动画和直接 MP4 渲染需要 OpenFilm。

## 尚未实现

- 剪映内置音乐/音效库自动搜索与下载
- 剪映内置 TTS、自动字幕、文稿识别（独立字幕/Whisper 方案不操作剪映内置功能）
- 剪映节拍检测、自动卡点
- 逐帧动作理解、精确口型同步和逐字字幕

上述功能需要剪映界面自动化、云服务或独立 ASR/视觉/音频模型。不要把代表帧观察描述成完整观看视频，也不要把草稿 JSON 生成能力描述成媒体理解能力。

## 版本兼容性

`get_jianying_capabilities` 返回本机安装和可用能力。剪映版本变化可能影响预设 ID、草稿字段、在线资源和视觉呈现，实际效果可通过目标版本的草稿或成片确认。
