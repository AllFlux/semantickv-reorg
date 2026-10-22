# 未实现实验项目重建 v1

> 来源：`research_baseline_survey.md`（论文）、`WB0_接管备忘.md`（DS 对话 328 条 / 2026-10-17→20）、`EXPERIMENT_PLAN.md`（E0–E6）、`CHANGELOG.md` 待办
> 目的：把"已读论文 + DS 对话"里提出但从未真正执行的实验，重建为**可证伪、可机检、WB12 可执行**的预注册任务
> 编制：WB0 主会话 · 2026-10-21

## 0. 判定口径

"未实现" = 有设计/预注册卡，但**无真实模型执行记录**（仅 CPU mock 或从未跑）。
台账已确认的 6 条硬约束（0.5B 容量上限 26%、朴素 STE 不足、sink 爆炸、Router 须接 3B 前向等）是本重建的边界。

## 1. 未实现项目清单（按优先级与可执行性）

### P1 · E0 相位敏感性复现 + (τ,k) 校准 —— WB1
- **依据**：`EXPERIMENT_PLAN §3 E0`；H2O(2306.14048)/SnapKV(2404.14469) 的相位敏感性是逐出式压缩公认弱点。
- **H0**：固定分块 KV 无相位敏感性。
- **方法**：chunk∈{16,32} vs Full KV；4K/8K 上下文；needle 位置按 chunk 步长扫描。
- **指标**：相位差 PP=max−min accuracy；周期估计（自相关）。
- **通过（证伪 H0）**：PP ≥4pp 且周期≈chunk 步长。
- **证伪**：PP <4pp → E0 校准未通过，暂停后续。
- **交付**：`results_v2/e0_phase_sensitivity.json` + 回执。

### P2 · E1 语义分组 vs 固定分块 —— WB1
- **依据**：`EXPERIMENT_PLAN §3 E1`；ChunkKV(2502.00299)/ClusterKV(2412.03213) 为直接前身。
- **H1**：同等 KV 预算下语义分组不优于固定分块。
- **方法**：单轮、无群间边，语义分组替换固定分块，同预算。
- **指标**：LongBench 平均、PP、prefill 开销。
- **通过**：准确率 ≥ 固定分块 +2pp、PP ≤3pp、开销 ≤+10%。
- **交付**：`results_v2/e1_semantic_vs_chunk.json` + 回执。

### P3 · H-WB0-A 结构显著性 vs 频次预算分配 —— WB2（CPU）
- **依据**：`WB0_接管备忘 §3.3`（DS 对话提炼）；Syntax–Knowledge 理论（结构可压、任意事实不可压）。
- **H-WB0-A**：按结构显著性分配压缩/训练预算，OOD 泛化 > 按频次分配。
- **方法**：两种分配（结构=群间桥接度/边中心性 vs 频次=token 频次），同预算；ID/OOD 两组任务。
- **指标**：OOD 准确率差（结构 − 频次）。
- **通过**：结构 OOD > 频次 ≥2pp。
- **反例判定**：保结构后仍丢跨句指代 → 收窄假设为"仅句内结构可压"。
- **交付**：`results_v2/h_wb0_a_struct_vs_freq.json` + 回执。

### P4 · 零数据自博弈最小原型 —— WB2（长时，暂缓）
- **依据**：`WB0_接管备忘 §3.1`（Absolute Zero 2505.03335 / Self-Play Pretraining 2609.30063）。
- **假设**：generator–learner 自博弈的 curriculum 收敛于结构发现，可零样本迁移。
- **方法**：最小 generator（代码题）+ learner + validator（执行器）。
- **指标**：held-out 结构任务 vs 随机基线。
- **状态**：需长时+算力，本轮不派，记录待条件成熟。

### P5 · 论文对比基线接入 —— WB1（随 E1 附带）
- **依据**：`research_baseline_survey §4`。
- **要求**：E1 的对照组至少含 Locret(2410.01805)/RLKV(2510.08525) 的方法论对照（可用其公开策略复现或明确标注不可得），否则"优于 A"不足以证明优于 SOTA。

## 2. 本轮派发（见 Hub 指令 EXP-01/EXP-02）

| 指令 | target | 实验 | 交付物 | 机检回执 |
|---|---|---|---|---|
| WB0-INSTR-20261021-EXP-01 | WB1 | P1（E0 相位敏感性） | results_v2/e0_phase_sensitivity.json | WB1-20261021-EXP-01.yaml |
| WB0-INSTR-20261021-EXP-02 | WB2 | P3（H-WB0-A 结构 vs 频次） | results_v2/h_wb0_a_struct_vs_freq.json | WB2-20261021-EXP-02.yaml |

**执行纪律**：不得伪造数据；若资源缺失（模型/数据/算力）转 open_item 给 WB0 并跳过；单轮 ≤300s，超时则拆小规模或改写 long_run。

## 3. 后续轮次路线

E0（P1）PASS → E1（P2）→ E3/E4（复用 mock 调度器 + 真实分组）→ E2/E5/E6；H-WB0-A（P3）与 E 系列并行（WB2 侧）；P4 待算力。
