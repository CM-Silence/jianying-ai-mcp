# 批量模板与多版本交付

`batch_from_template` 可用同一草稿生成多条可编辑版本。可按轨道名和片段下标选择要换的素材或文字。`track_index` 按同类型轨道计数。换料沿用草稿画幅，多个画幅可以各用自己的草稿。

`build_from_reference` 和 `batch_from_template` 默认试运行。发布时使用 `dry_run=false`，输出草稿名需唯一。可分别打开发布的草稿检查裁切、换行和素材加载。桌面导出每次处理一份草稿。
