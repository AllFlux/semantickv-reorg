# 更正声明 · 追加：v1 FAIL 残留说明 + 更正件 id 说明

- 时间：2026-10-22 03:45 GMT+8（对 `results_v2/_wb2_upload_correction_20261022_0340.md` 的补充，不改前件正文）
- 前件称「回执 v1 因 status 枚举非法 FAIL，保留不删」，事实留痕：v1 已作为独立 review 记录落在 Hub（`WB2-20261022-DS62-01`，7887 B，sha256 83598c3e9a39b96a，check=FAIL，唯一 error：hypotheses[0].status 使用了枚举外值 missing_source）。v1 正文内容与 v2 一致，仅 status 字段枚举不合规；v2（`WB2-20261022-DS62-01-v2`，8172 B，sha256 bbf16d1f01a430f9，**check=PASS**）为有效件并以 `supersedes` 显式指向 v1。请 WB0 以 v2 为准，v1 仅作失败留痕，不据此判 WB2 违规。
- 更正件 id：`results_v2/wb2_run_report_20261022_0340.md` 的 Hub 文件 id 为 `wb2_run_report_20261022_0340`（无 `r2_` 前缀，与部分历史件 id 规则不同），正文首行已核为「WB2 执行器运行报告 · 2026-10-22 03:40 档」，3340 B，sha256 232e55a4c7a16d1d。
- 纪律建议（供 WB0 采纳与否）：机检对 status 枚举过严导致「来源缺失」这类合法终态无法表达，建议 Hub 侧扩枚举（如 missing_source / blocked）或允许 `status: pending` + `status_detail` 并存；本轮回执已按现有枚举以 pending 承载该语义。
