# 分镜示例

按题材选分镜：

- `packaged-talk-template.json`：**骨架模板**。把 `media/` 换成自己的素材目录，把 `text` 和 `data` 换成新题材，就能产出同一包装风格的另一期。结构、字幕、分屏、转场、总结都已经按 `references/packaging-rules.md` 写好，不要删字段。
- `meigong-outsource-vs-inhouse.json`：**成片级示例**（23 拍，约 194 秒）。看真实项目里小节怎么切、要点怎么分布、总结卡怎么配原声。

## 跑一遍模板

卖家分镜样例约 32 秒。把本期口播放入 `media/seller-talk.mp4`，按新原话更新字幕和入出点。用 `search_broll_shots` 搜剪映库中至少 6 秒的装箱和扫码画面，将两条真实的 `shot_id` 填入样例。第 18–26 秒在同一幅分屏里逐项显示三项核对内容。

```powershell
jianying-ai-mcp compile-episode examples/episodes/seller-project-review.json --mixdown
```

```bash
# 1. 素材放进 media/：talk.mp4（口播）、broll.mp4（空镜）
# 2. 编译成分镜清单
jianying-ai-mcp compile-episode examples/episodes/packaged-talk-template.json \
  --media-root examples/episodes --mixdown --openfilm
# 3. 发布草稿
#    build_draft(manifest_path=..., dry_run=false)
```

写成 JSON 后也可以直接交给 `compile_episode(spec_path=..., media_root=...)`。

## 模板里每条规则对应哪一拍

| 规则 | 对应的拍 | 关键字段 |
| --- | --- | --- |
| 片头只放一个问题卡 | 第 1 拍 | `recipe: "hook"`，`seconds: 3.0` |
| 小节作为剪点标记 | 第 2、8 拍 | `chapter: "01 …"` |
| 数量、规模用数字卡分屏 | 第 2 拍 | `layout.with.graphic.recipe: "number"` |
| 两个递进分句上下呈现 | 第 3 拍 | `caption: {"mode": "stack"}` |
| 要点清单随讲随出 | 第 4、8 拍 | `layout.with.graphic.recipe: "list"` + `items[].at` |
| 换画面避免整片同一构图 | 第 5 拍、第 6 拍 `main: "top"` | `media` beat，`layout.main` |
| 长句分块出现 | 第 6 拍 | `caption: {"mode": "blocks"}` |
| 整句结论用 `single` 并压到下方 | 第 7 拍 | `caption: {"mode": "single", "zone": "lower"}` |
| 总结卡片配原声 | 第 9 拍 | `role: "summary"` + `voice` |
| 片尾署名 | 第 10 拍 | `recipe: "end"` |

## 常见错误

| 写法 | 为什么不对 | 改成 |
| --- | --- | --- |
| 每个小节前插一张全屏 `chapter` 卡 | 只是把主讲的第一句话再说一遍，节奏被打断 | 在口播 beat 上写 `chapter` 字段 |
| 小节中途插全屏 `list` 卡 | 复述刚讲过的内容；编译会报错 | 放进口播分屏 `layout.with.graphic` |
| 字幕缩小后难读 | 手机屏上分辨不清 | 保持字号，回听分句停顿 |
| 字幕固定 `transform_y: -0.43` | 压住主讲人的脸 | `caption_zone: "auto"` 或按拍指定 `zone` |
| 每两个切点都加转场 | 连续口播被转场打断 | `transitions: "auto"`，同机位硬切 |
| 无停顿的单句强行堆叠 | 观众会读错句意 | 保留 `single` |
| 总结写一段新口播 | 打断「卡片+原声」的收束感 | 总结卡 `voice` 用原素材人声 |
| `items[].at` 全填 0 | 要点一次性全出，和口播脱节 | 按 `时长 × 关键词前字数 ÷ 全句字数` 估 |
