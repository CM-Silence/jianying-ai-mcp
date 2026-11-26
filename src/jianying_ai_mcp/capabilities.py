from __future__ import annotations

from .config import Settings, TESTED_DRAFT_LIBRARY_VERSION, TESTED_JIANYING_VERSION
from .draft_crypto import crypto_status


CAPABILITIES = {
    "verified": [
        "create_draft",
        "multi_track",
        "local_video_audio_image",
        "trim_and_speed",
        "audio_volume_and_fade",
        "srt_import",
        "text_style_border20_background_shadow",
        "transition",
        "video_filter_and_effect",
        "independent_filter_effect_tracks",
        "chroma_key_rgba",
        "mix_mode_and_overlay",
        "video_text_sticker_keyframes",
        "image_overlay_and_sticker",
        "font_bubble_flower_text",
        "text_video_animation",
        "audio_scene_tone_speech_to_song",
        "read_plaintext_reference_draft",
        "copy_replace_and_batch_plaintext_template",
        "local_video_metadata_and_representative_frames",
        "sidecar_srt_vtt_transcript",
        "cached_video_observation_report",
    ],
    "local_tools": {
        "parse_media_info": "用 ffprobe 读取本地视频、音频和图片元数据",
        "analyze_media_batch": "对白优先判断素材主题，带时间码检索和紧凑视觉预览",
        "find_repeated_takes": "从已缓存对白列出重复口播候选和源时间码",
        "prepare_media_assets": "批量规范旋转、画幅、帧率和提取音轨",
        "production_tools": "图卡、浏览器动画、旁白、配乐、分屏构图、字幕词语高亮和文字进退场",
        "list_video_editing_modules": "按构图、字幕和文字标注查询可复用模块及时间线用法",
        "repair_caption_references": "在副本中移除用户指定的缺失文字片段引用",
        "list_jianying_presets": "查询本地草稿库预设；实际可用性受剪映版本、资源及账号影响",
        "list_video_style_presets": "查询并复用编辑感、暖调纪实、新闻包装和清爽极简的图卡与剪辑风格",
        "list_jianying_audio_assets": "读取指定剪映草稿已选的原生音乐与音效；可在编辑时间线中复用",
        "review_export": "用 FFmpeg 检查本地成片的抽样画面、黑场和响度",
        "master_video_audio": "双遍音频响度处理并复测，保留编码视频流",
        "openfilm_tools": "通过可选本地 OpenFilm 运行时提供预览、渲染和剪映草稿导出",
        "jianying_desktop_tools": "通过独立安装的 jianying-tool 0.5.0 探测剪映、诊断草稿和导出 MP4",
    },
    "experimental": {
        "mask": "依赖 pyJianYingDraft 0.3.1 蒙版结构；需在目标剪映版本抽检",
        "background_filling": "在底层视频轨写入纯色或模糊背景；需在目标剪映版本抽检",
        "volume_keyframes": "写入视频或音频片段的音量关键帧；需在目标剪映版本试听",
        "native_subtitles": "SRT mode=native 写入原生字幕轨及任务；需在目标剪映版本抽检",
    },
    "partial_or_disabled": {
        "mask": "默认禁用；使用 build_draft 的 allow_experimental_effects 参数试用",
        "blur_background": "默认禁用；使用 build_draft 的 allow_experimental_effects 参数试用",
        "audio_volume_keyframes": "默认禁用；使用 build_draft 的 allow_experimental_effects 参数试用",
        "encrypted_reference_draft": "需要本地 jy-draftc 兼容读取器和剪映 videoeditor.dll；仅处理临时副本",
        "video_semantic_understanding": "已提供代表帧与字幕上下文；准确度取决于所用多模态模型和采样密度",
        "automatic_caption_or_transcript": "支持旁挂或内嵌文本字幕、本地 Faster Whisper 和可选 Groq/OpenAI 语句级转写",
        "automatic_export": "可选桌面后端；先调用 probe_jianying_desktop 检查版本和本机配置",
    },
    "not_in_v1": {
        "jianying_builtin_music_and_sfx": "完整在线目录的浏览和资源下载由剪映管理；已选素材可从参考草稿复用",
        "jianying_tts": "需要剪映 UI 与云端服务",
        "beat_detection_and_auto_cut": "需要剪映 UI 或独立音频分析器",
    },
    "tested_versions": {
        "jianying": TESTED_JIANYING_VERSION,
        "pyJianYingDraft": TESTED_DRAFT_LIBRARY_VERSION,
    },
}


def capability_report(settings: Settings | None = None) -> dict[str, object]:
    report: dict[str, object] = {
        key: list(value) if isinstance(value, list) else dict(value)
        for key, value in CAPABILITIES.items()
    }
    if settings is not None:
        encrypted = crypto_status(settings)
        report["encrypted_draft_reader"] = encrypted
        if encrypted["available"]:
            verified = report["verified"]
            assert isinstance(verified, list)
            verified.extend(["read_encrypted_reference_draft", "copy_replace_and_batch_encrypted_template"])
    return report
