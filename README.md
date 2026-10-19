# SemanticKV-Reorg 实验脚手架 + CPU 模拟验证

在 V4.1-Flash-1017 固定分块 KV 压缩之上验证 **SemanticKV-Reorg** 架构
（语义分组 + 细粒度群间关系 + 每轮/周期性重组）的实验体系。

本仓库两部分：

| 目录 | 内容 | 状态 |
|---|---|---|
| `experiments/` | E0–E6 预注册卡（假设 H0/H1、阈值、停止规则、更新规则） | ✅ 已交付 |
| `eval_runner.py` | 实验运行器：接口骨架 + CLI + 序贯决策 | ✅ 已交付 |
| `REPORT_TEMPLATE.md` | 结果报告模板（含 95% CI 计算逻辑、失败归因填空项） | ✅ 已交付 |
| `src/semantickv/` | 三个核心组件的 Mock 实现 + 真实环境空壳 + 统计/合成数据工具 | ✅ 已交付 |
| `cpu_sim/` | 纯 CPU 模拟：规模基准、10 轮对话、机制微测试 | ✅ 已运行 |
| `results/` | 全部运行结果（JSON + 报告） | ✅ 已交付 |
| `src/semantickv/v41_torch_native_runner.py` | 真实模型慢速验证器（纯 PyTorch，CPU 可跑；V1.5 压缩家族） | ✅ 已验证 |
| `run_on_kaggle.ipynb` | Kaggle 一键上机脚本（baseline-first 两步流程 + V1.5 参数） | ✅ 已交付 |
| `DISCIPLINE.md` | 合作纪律（WB 侧 D1-D5 + **DeepSeek 侧对等 DS-D1..DS-D5** + 交接协议） | ✅ 已交付 |
| `tools/check_discipline.py` | 每轮对话强制检验器（D1-D5 + DS-D1..DS-D5 双边 → 台账） | ✅ 已交付 |
| `tools/check_ds_review.py` | DeepSeek 侧对等纪律检验器（DS-D1..DS-D5 独立运行） | ✅ 已交付 |
| `templates/ds_review_template.md` | DeepSeek 评审强制模板（frontmatter + 三段式 + hypotheses） | ✅ 已交付 |
| `results/REQUEST_TO_DEEPSEEK.md` | 致 DeepSeek 的对等要求书（3 条待承认开放项） | ✅ 已交付 |

**重要边界**：本仓库的 CPU 模拟只验证**流水线机械正确性与算法开销**，
不构成对 E0–E6 任何假设的证据。真实假设检验需要 GPU + V4.1-Flash-1017，
用 `eval_runner.py --card experiments/Ex_*.yaml`（不加 `--mock`）运行。

---

## 1. 快速开始

```bash
# 接口签名校验（换有卡实现后先跑这个）
python3 eval_runner.py --check-interfaces

# 合作纪律检验（每轮对话交付前强制运行；D1-D5 + DS-D1..DS-D5 双边 → results/discipline_ledger.yaml）
python3 tools/check_discipline.py

# DeepSeek 侧对等纪律独立检验（DS-D1..DS-D5）
python3 tools/check_ds_review.py

# CPU 冒烟：用合成数据跑通整条流水线（秒级）
python3 eval_runner.py --card experiments/E1_semantic_grouping.yaml --mock --seeds 3

# 全部 CPU 模拟（微测试 + 规模基准 + 10 轮对话）
python3 cpu_sim/run_all.py

# 真实模型慢速验证（CPU 可跑，v0.1.2 新增；先 PyTorch 后 Triton）
python3 src/semantickv/v41_torch_native_runner.py --model Qwen/Qwen2.5-0.5B-Instruct --device cpu --ctx 2048

# V1.5 压缩策略验证（v0.2.0：centered 度量 + Top-K 代表 + 中层压缩 + sink 保护）
# 第一步：原始 needle 基线门（无压缩，快）
python3 src/semantickv/v41_torch_native_runner.py --model Qwen/Qwen2.5-0.5B-Instruct --ctx 2048 --baseline-only
# 第二步：基线门通过后跑 V1.5 全量（K=8 为推荐档位，见 results/REAL_MODEL_VALIDATION_V15.md）
python3 src/semantickv/v41_torch_native_runner.py --model Qwen/Qwen2.5-0.5B-Instruct --ctx 2048 \
    --strategy v15 --metric centered --compress-mode topk --topk-per-group 8 \
    --sink-tokens 4 --window-tokens 64 --compress-layers 0.25-0.75 \
    --needle-positions 0.15,0.5,0.85

# 或经 eval_runner 转交（提供 --model 即切换真实模式）
python3 eval_runner.py --model Qwen/Qwen2.5-0.5B-Instruct --ctx 2048

# 真实实验（需 GPU；当前环境会明确报错而不是静默失败）
python3 eval_runner.py --card experiments/E1_semantic_grouping.yaml
```

环境：Python ≥3.11，numpy / scipy / pyyaml / torch（CPU 即可）。
可选：faiss-cpu（本机上比 torch 慢 5–14 倍，默认不使用）。

## 2. 接口骨架（换有卡环境时唯一契约）

`eval_runner.py` §1 定义三个组件的签名（`--check-interfaces` 可校验一致性）：

| 组件 | 方法 | 输入 → 输出 |
|---|---|---|
| `SemanticGrouper` | `fit` | embeddings (N,d) → `GroupAssignment` |
| | `partial_fit` | new_embeddings + prev → 覆盖全量的 `GroupAssignment` |
| | `centroids` | → (G,d) 质心 |
| `RelationGraph` | `build_full` | centroids + turn → `EdgeSet` |
| | `observe` | observed + turn → `Delta`（EMA + 增量） |
| | `merge` | Delta → None（CRDT LWW，幂等可乱序） |
| | `freshness_error` | reference EdgeSet → 相对 L2 误差 |
| `ReorgScheduler` | `should_reorg` | turn, drift, stats → `ReorgDecision` |
| | `on_turn_boundary` | turn → None |

替换实现时保持签名不变，放进 `src/semantickv/` 并在 `check_interfaces()`
的 `candidates` 里换类名即可。

**真实环境接口已预备**（v0.1.1）：`src/semantickv/anchor_grouper.py` 提供
`AnchorBasedSemanticGrouper(SemanticGrouper)` 空壳 + `PrefillCapture`
（prefill 隐状态/注意力摘要）。有卡环境只需填充 TODO_ANCHOR-1..4
（hook 注册 / 锚点提取 / GSC 入口 / partial_fit），签名已由
`--check-interfaces` 注册校验（13/13 通过）。

## 3. Mock 实现要点

- **SemanticGrouper（GSC）**：kNN 图 → 互 kNN + 相似度阈值剪枝 → 连通分量 →
  标签传播 → 小群合并。kNN 用 torch 分块矩阵乘（实测本机 8 线程 240 GFLOPS，
  比 pip 的 faiss-cpu 快 5–14 倍；线程数用 `SEMANTICKV_THREADS` 调）。
- **RelationGraph**：top-p 稀疏边，边权 `1/(1+质心距离)`；EMA 更新；
  Delta = added/removed/updated；合并为 LWW-Register（turn 为逻辑时钟），
  幂等、可乱序；`active_groups` 限定衰减范围；`structure_error`（Jaccard）
  区分权重滞后与结构陈旧。
- **ReorgScheduler**：per_turn / periodic(k) / threshold(drift) 三种模式，
  外加 `max_stale_turns` 硬约束（距上次 full 超 K 轮强制 full，
  v0.1.1 新增，见 E4 卡 v0.2.0）。

## 4. 已知实现注意点（踩坑记录）

1. **合成数据的噪声必须按 1/√d 缩放**：高维下用绝对 std 会被噪声能量 σ²d
   淹没话题结构（d=768, σ=0.35 时话题内余弦仅 ≈0.01）。
2. **互 kNN 互通率约 40%**：仅靠连通分量会把一个话题碎成上百片，必须跟
   标签传播才能合并（`fit` 和 `partial_fit` 的微型聚类都适用）。
3. **CPU 线程不是越多越快**：本容器 32 可见核，torch 8 线程 240 GFLOPS >
   16 线程 152 > 32 线程 98（超订阅反噬）。
4. **绝对不要 materialize N×N 距离矩阵**：500k 时是 1TB；分块后峰值 ~3GB。

## 5. WB2 共享交付物拉取（DS-20261019-021 基建）

WB2 的训练产物（`router_weights.pt` / `load_test.py` 等）以本仓库的
`src/shared/` 为数据总线。`auto_pull_shared.py` 负责在 Kaggle Notebook
启动时自动拉取，取代"等本地同步"。

```bash
# Kaggle: Add-ons → Secrets 添加 GITHUB_PAT（DS 稍后配置），然后：
python3 auto_pull_shared.py                      # PAT 优先，无 PAT 时公共仓库匿名拉取
python3 auto_pull_shared.py --verify-torch-load  # 额外对 .pt 做 torch.load 冒烟
```

- 主路径：`git clone --depth 1 https://{PAT}@github.com/AllFlux/semantickv-reorg.git`
  → 复制 `src/shared/` → 计算 SHA256 并与最后修改 commit 双比对 → 计时
  （H-DS30 判据 ≤10s）。
- 降级路径（H-DS30 falsifiable 预置退路）：git 失败/超时 → GitHub Raw
  直链逐文件下载。
- PAT 全链路脱敏（日志/报错/存证均不泄露）。
- 存证：`results/logistics_pull_test.json`（方法、耗时、每文件 SHA256 与
  commit、`key_files_present` 反映 WB2 交付物是否已推送）。
- notebook 已挂载：`run_on_kaggle.ipynb` Step 2.5 cell 自动执行本脚本并
  确认关键文件可见。
- blob 字段规范与 Train-Serve Skew 约定请求见 `src/shared/README.md`。

## 6. 结果索引

| 文件 | 内容 |
|---|---|
| `results/relation_graph_tests.json` | 14 项机制微测试（全过） |
| `results/scaling_results.json` | GSC 规模基准（10k–500k，时间/内存/ARI） |
| `results/turns_results.json` | 10 轮对话：三种重组模式对比（v0.1.0） |
| `results/turns_maxstale_matrix.json` | 10 轮对话：max_stale × α 参数矩阵（v0.2.1 重建全矩阵，Pareto 点修正为 K=3/α=0.9） |
| `results/real_model_validation.json` | 真实模型四阶段验证：V0 不变性 + (k,τ) 扫描 + 保真度谱系 + needle（v0.1.2） |
| `results/real_model_needle_0.15/0.85.json` | needle 三位置扫描另两个位置（0.5 在主 JSON，v0.1.2） |
| `results/REAL_MODEL_VALIDATION.md` | 《真实模型慢速验证报告》（V0=1.0；τ=0.97 硬边界；端到端 cos 0.42 → 升级路径） |
| `results/real_model_v15_baseline_v1.json` | V1 基线对照：cosine + flat_mean + 全层（v0.2.0 A 组） |
| `results/real_model_v15.json` | V1.5 主实验：centered + topk K=4 + 中层 + sink/window（v0.2.0 B 组） |
| `results/real_model_v15_l2.json` | 度量消融：v15 + l2 幅值感知度量（v0.2.0 C 组） |
| `results/real_model_v15_topk8.json` | K 敏感性：v15 + topk K=8 Pareto 上探（v0.2.0 D 组） |
| `results/REAL_MODEL_VALIDATION_V15.md` | 《V1.5 压缩策略验证报告》（argmax 2.6%→28%/42%；centered 度量裁定；topk Pareto 占优） |
| `results/discipline_ledger.yaml` | 合作纪律台账：每轮 D1-D5 + DS-D1..DS-D5 双边检验记录（GREEN/WARN/RED） |
| `DISCIPLINE.md` | 合作纪律文本（WB 侧 D1-D5 + DeepSeek 侧对等 DS-D1..DS-D5 + 交接协议） |
| `results/ds_reviews/` | DeepSeek 评审按模板补录（intake 协议落盘，含 hypotheses 抽取） |
| `results/hypothesis_ledger.yaml` | DeepSeek 假设台账：6 条预判的命运（confirmed/refuted/pending）+ 待承认开放项 |
| `results/REQUEST_TO_DEEPSEEK.md` | 致 DeepSeek 的对等要求书（DS 侧纪律 + 3 条待承认开放项） |
| `results/CPU_SIM_REPORT.md` | 《CPU 模拟实验报告》（结论 + 外推） |
| `results/CHANGELOG.md` | 阈值校准与设计变更记录 |
