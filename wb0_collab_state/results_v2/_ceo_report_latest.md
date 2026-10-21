# WB0⇄WB1/WB2 协作进展汇报（致 CEO）
> 生成时间：2026-10-21 17:05 ｜ 数据来源：Agora Hub（唯一事实源 v0.21）

## 一、进度 Progress
- 台账版本 **v0.21**，假设 23 条（verified 3 / confirmed 3 / refuted 3 / pending 2 / resolved/open 若干）
- **H-WB2-E2E-01 由 verified_proxy 升级为 verified**（用户确认的 DEC-20261021-E2E-GATE 升级条件首次被真实数据满足）
- 回执机检：最新 `WB1-20261021-AB-v1` **PASS**（累计 10 条，当前无 FAIL 未决）

## 二、成果 Results（本轮：零中转全自动 E2E A/B 管线落地）
- **真实 A/B 三件套交付并全部上架 Hub**（results_v2/）：
  - `wb1_ab_real_stage1.json`（主交付物，sha256=c1e29058…）
  - `multihop_real.json`（60 实体/30 查询/58 边，构造修正六项披露）
  - `entity_embeddings.json/.npz`（真实 3B 隐状态 60×2048）+ `b_routers/`（18 个 B 侧权重）
- **核心结果**：A（冻结件标签拟合）recall@5=**0.1333**（4 活跃群）vs B（端到端检索 Reward 训练）recall@5=**0.4444**（K=64，10 活跃群）；配对 bootstrap **mean_diff=+0.3111，CI95=[+0.167, +0.478]，p_gt0=1.000**（n=30）。proxy 方向一致（proxy 4.25 倍 → 真实 3.33 倍），非伪。
- 「更少群数」子论断在 K=8 配置成立（3 活跃群 < A 的 4，且 recall 0.2444 > 0.1333）。
- **post-hoc 新发现（H-AB-INPUT-SCALE，confirmed）**：真实 LLM 隐状态大幅值（范数 ~165）会使 Router 初始 softmax 近均匀、群共现邻接退化为全连接，可微 reward 训练停滞于 chance；per-dim z-score 标准化后恢复（raw 口径 6 组结果全部保留作对照）。
- 环境保真：3B 权重 ModelScope 自取 9/9 sha256；R32 基线复现相对差 1.15%（env_fidelity=HIGH）；冻结件 sha256=4b4f1064… 未动。

## 三、问题 Issues（开放项）
- [🔴 高] `WB0-CLOUD-AGENT-FAILURE`（新增）：**云端 scheduled-agent 执行层整体故障**（三重实证：巡检触发零访问日志/20 分钟零外部连接/全新诊断探针两次准时触发零执行）。调度器派发正常，故障在执行层——零中转管线的「激活后自行抓取」在执行层暂不可用。缓解：探针 #13179921 保持武装作恢复探测器；长时任务 #13166335 幂等检查保留（回执 PASS 即退出）；**本轮 A/B 由 WB0 主会话依同一 runbook 代跑并全程披露**，科学交付物未中断。
- 其余 open_item 均已 resolved（WB0-EGRESS / WB1-RESOURCE-HANDOFF / WB2-CAUSAL-WIRE / VERIFY-REVIEW-FAIL / WB0-EGRESS-20261021）。

## 四、风险与下一步
- **云端执行层恢复监控**：探针任务恢复后首跑即产生 Hub 访问日志；恢复后长时任务幂等接棒，不会重复实验。
- **样本量**：30 查询（预算约束，非预注册 40）；best-B 为 in-sample 选择；K 不可与 A 的 42 配对——均已披露，建议后续扩查询集复核。
- **Stage 2 候选**：Router→LLM 因果接线已由 WB2 建模（RoutedLLMHarness），资源齐备后可做 LLM CE reward 的真实端到端对比。
- 离平台备份：GitHub `AllFlux/semantickv-reorg/wb0_collab_state` 持续维护。

---
*本汇报由 WB0 主会话生成；明细可查 Agora Hub 实时看板与 hypothesis_ledger.yaml v0.21。*
