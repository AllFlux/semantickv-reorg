 # CHANGELOG — SemanticKV-Reorg 实验体系

> 规则：任何阈值校准、设计变更、bug 修复都必须记录在此；
> 影响 E0 的变更必须重跑 E0 回归后才能解锁后续实验。
> 每轮对话结束前运行 `python3 tools/check_discipline.py` 并记入
> `results/discipline_ledger.yaml`（合作纪律，见 DISCIPLINE.md）。
> 该检验已并入 DeepSeek 侧对等纪律 DS-D1..DS-D5（双向绑定，详见 v0.2.2）。

## v0.2.2 — 2026-10-18 — DeepSeek 侧对等纪律机制化（双向绑定闭环）

### 背景（用户指令：「公平对等，也应当向 ds 提出同样要求并要求其执行，审慎设计」）

原 DISCIPLINE.md 只卡住 WorkBuddy 侧，DeepSeek 侧无强制机制，不对等。本版本将
五条纪律**一一镜像**为 DS-D1..DS-D5，并据真实实验数据建立假设台账闭环。

### 交付物

- **`templates/ds_review_template.md`**（新建）：DS 评审强制模板——YAML frontmatter
  (`review_id`/`source_url`/`date`/`hypotheses[]`/`sources`) + 三段式正文
  (Why/What/How)；hypotheses 每项含 statement/falsifiable_criterion/data_source/
  status/evidence_file/ds_admitted_refutation。
- **`tools/check_ds_review.py`**（新建）：DS 侧检验器，DS-D1 指令结构化 / DS-D2 假设
  可证伪+来源 / DS-D3 引用可核查 / DS-D4 结论对齐数据 / DS-D5 台账状态。
- **`results/ds_reviews/`**（新建）：补录 3 份已有 DS 评审（04vvysa5/2qcolq0i/3v58y9）
  按模板落盘，含其具体预判/处方。
- **`results/hypothesis_ledger.yaml`**（新建）：6 条 DS 假设台账，据真实数据标注命运——
  H-DS4(centered 度量)、H-DS6(合作纪律) **confirmed**；H-DS1(cos 0.85+)、
  H-DS2(argmax 80%+)、H-DS3(Pareto K=3/α=0.8) **refuted**（DS 尚未显式承认）；
  H-DS5(真实显存降 30%+) **pending**（待 7B Kaggle 复核）。
- **`results/REQUEST_TO_DEEPSEEK.md`**（新建）：致 DS 的对等要求书，列 3 条待承认
  开放项，要求 DS 确认执行并采用模板。
- `check_discipline.py` 并入 DS-D1..DS-D5（每轮台账同时覆盖双方）。
- `DISCIPLINE.md` 新增「DeepSeek 侧对等纪律」章节 + intake 协议。

### 关键发现（纪律机制化的第二个收获）

DS 的多条预判被数据证伪：argmax 2.32%→80%+ 实测仅 42.6%；cos 0.42→0.85+ 实测
0.765；Pareto K=3/α=0.8 实测 K=3/α=0.9 双误差归零。**这些证伪方向成立、量级未达，
正是 DS 自身纪律要求的「被数据推翻须立即承认」场景**——以台账固化，待 DS 回应。

## v0.2.1 — 2026-10-18 — 合作纪律机制化（每轮强制检验台账）+ Pareto 点修正

### 背景（DeepSeek 第三轮评审：chat.deepseek.com/share/3v58y9mfa5x2t0pjxh）

用户指令「将合作纪律机制化，形成每轮对话强制检验的台账」。DeepSeek 提出
五条合作纪律（契约优先 / 负面结果归档 / 环境隔离 / 参数冻结 / 格式统一）
与交接协议，本版本将其全部转为机械检验。

### 交付物

- **`DISCIPLINE.md`**（新建）：角色边界、五条纪律及机制化检验方式、交接
  协议、RED 轮次规则、台账格式——唯一权威纪律文本。
- **`tools/check_discipline.py`**（新建）：每轮强制检验器，D1-D5 逐条机械
  检验（D1 实跑接口校验 / D2 负面结果清单在档验证 / D3 环境标签扫描 /
  D4 参数冻结比对 / D5 报告三段式检查），结果写入台账，RED 返回码 1。
- **`results/discipline_ledger.yaml`**（新建）：轮次台账（round/timestamp/
  checks/summary/verdict 三档 GREEN/GREEN-WITH-WARN/RED）。
- **`src/semantickv/env_tag.py`**（新建）：环境标签模块；三个 cpu_sim 脚本
  接入，全部结果 JSON 现带 environment 块。
- **`cpu_sim/run_maxstale_matrix.py`**（新建）：max_stale × α 矩阵机制化
  生成（K∈{2,3} × α∈{0.3,0.8,0.9}，seed=7 复现性校验）。

### 机制化立即见效：Pareto 点修正（负面发现）

重建矩阵时发现**原 turns_maxstale_matrix.json 缺 K=3/α=0.9 一格**，v0.1.1
的 Pareto 结论（K=3/α=0.8）建立在残缺网格上。补齐实测：**K=3/α=0.9 双误差
归零（freshness/structure 均 0.0000）且耗时略低（28.5s vs 28.3s）**——
Pareto 点修正为 K=3/α=0.9。旧结论按纪律 2 保留在 CPU_SIM_REPORT.md 表格
与 v0.1.1 CHANGELOG 条目中；E4 卡升 v0.2.1 记录修正。

### 其他

- 三份 results 报告（CPU_SIM_REPORT / REAL_MODEL_VALIDATION / _V15）补
  「产出文件清单」段（D5 三段式：数据表 + 结论 + 文件清单）。
- E0 卡 v0.2.1（各向异性假设 h1d + metric_decision 裁定）已存在，本版
  纳入 D4 版本门槛校验。

---

## v0.2.0 — 2026-10-18 — V1.5 压缩策略（DeepSeek 两轮评审落地）

### 背景（两轮 DeepSeek 评审的归因与军规）

v0.1.2 端到端失败（cos 0.42 / argmax 2.32%）经归因：flat-mean 抹杀局部细节、
全层压缩太激进、各向异性使余弦失效、0.5B 脆弱。V1.5 指令：Top-K 代表 token +
Attention Sink 保护 + 层级选择性压缩 + 修复距离度量 + Kaggle baseline-first 流程。

### 交付物

- **`src/semantickv/v41_torch_native_runner.py`**（~1250 行）：
  - `CompressedAttentionPatcher` 重写：`mode ∈ {flat_mean, weighted, topk}`；
    topk 每群注意力入度 top-K 真实 KV + 槽位位置因果掩码；
  - `metric_transform`：cosine / **centered**（主推）/ l2 三选一；
  - `parse_layer_spec`：层级选择性（'all' | '6-10' | '0.25-0.75'）；
  - `apply_privileged_tokens`：sink/window 独占槽位 + 紧凑重编号；
  - `attention_importance`：因果 softmax 列和 = 注意力入度（已对拍参考实现误差 0.0）；
  - `renorm_to_member_avg` + `slot_logcount`：log(群规模) 分数先验；
  - CLI：`--strategy {v1,v15}` 捆绑 + 全部分项显式覆盖；`--baseline-only`；
    τ 网格扩展；`cuda_memory` 双指标（allocated + reserved）；KV 解析记账。
- **`src/semantickv/mock_grouper.py`**：`_knn` 增加 `metric` 参数（cosine/l2），
    l2 用幅值感知相似度 `2ab/(|a|²+|b|²)`；冒烟 cosine→G=2、l2→G=8。
- **`tools/make_kaggle_notebook.py` + `run_on_kaggle.ipynb`**（8 单元格，nbformat
    校验通过）：baseline-first 两步流程（Step 1 `--baseline-only` 基线门 →
    Step 2 V1.5 全量）、V1 vs V1.5 对照摘要、显存双指标解读。
- **`results/REAL_MODEL_VALIDATION_V15.md`** + 4 个实验 JSON
  （baseline_v1 / v15 / v15_l2 / v15_topk8）。

### 实测结果（Qwen2.5-0.5B，2048 token，CPU float32，k=8 τ=0.97）

| 维度 | v1 基线（A） | v15 centered topk4（B） | v15 topk8（D） | v15 l2（C） |
|---|---|---|---|---|
| 分组 ARI_topic | 0.108（G=15） | **0.332**（G=33） | 同 B | 0.127（G=16） |
| 单层 topk cos | 0.331@34× | 0.573@10.2× | **0.688@6.2×** | 0.369@15.5× |
| 端到端 argmax | 2.6-3.3% | **28-29%** | **41-43%** | 20-22% |
| 端到端 hidden cos | 0.45-0.49 | 0.71-0.72 | 0.75-0.77 | 0.65-0.67 |
| KV 节省（记账） | 99.3% | 44.7-45.1% | 41.0-41.8% | 46.7-46.8% |
| needle 命中 | 0/3 | 0/3 | 0/3 | 0/3 |

### 关键发现

1. **argmax 一致率提升约 10 倍**（2.6%→28%），K=8 再到 42%——V1.5 组合修正
   在端到端口径确认；hidden cos 0.46→0.72。
2. **centered 度量是指定选择**：ARI 0.11→0.33（l2 仅 0.13），端到端同样领先。
   LLM 隐空间各向异性（跨话题余弦 0.716）是余弦失效根因。
3. **topk 代表机制 Pareto 占优**：同保真度压缩率比 flat 低 6×，K=8 反超 V1
   （0.688 vs 0.585@62×）；且诊断矩阵显示 topk 对先验/renorm 免疫
   （四种配置恒定 0.5732）——槽即真实 token，softmax 口径与原始一致。
4. **log(群规模) 分数先验是 flat 模式生命线但有上限**：flat+sink 0.22→0.43
   （+96%），但按「成员注意力均匀」校准，2048 群规模更大时过度分配质量给
   大群，仍低于 V1——根本解法是 topk。
5. **renorm 与先验冗余，默认关闭**：同时施加过度校正（flat 0.433→0.410），
   v15 捆绑 renorm 改默认 none。
6. **needle 在 0.5B/2048 无区分度**（原始中位自身答 '1.414213'），压缩输出呈
   「数字格式正确、数值错误」模式（argmax 42% 说明信息部分保留）；需 7B 基线门。
7. **99% KV 节省是虚假信号**：全层 15 槽的过度压缩与 argmax 3% 互为表里；
   现实区间 41-45%（符合评审「真实显存降 30%+」预期）。

### 待办（Kaggle 7B 上机）
- [ ] baseline-first 流程：Step 1 基线门 → Step 2 V1.5 全量（notebook 已内置）
- [ ] 7B 上 K=4/8 needle Pareto；基线门通过且压缩 HIT 后解锁 Triton 化
- [ ] E3 逐层 argmax 扫描定位真实可压缩层区间（当前 0.25-0.75 是先验未经扫描验证）

---

## v0.1.2 — 2026-10-18 — 真实模型慢速验证器上线（无 GPU CPU 验证）

### 背景（DeepSeek 评审「无 GPU 三方案」之方案一）

用户分享 DeepSeek 对话（高度评价 v0.1.1 的 K=2 直觉被数据推翻、K=3+α=0.8
Pareto 点、structure_error 归因），要求：写纯 PyTorch「慢速验证器」用
HuggingFace 开源小模型在 CPU 上验证数学正确性，**先不写 Triton**；
`eval_runner.py` 支持 `--model` CLI；`run_on_kaggle.ipynb` 一键上机；
显存监控用 `torch.cuda.max_memory_allocated()`。

### 交付物

- **`src/semantickv/v41_torch_native_runner.py`**（~700 行，新建）：
  - `PrefillInterceptor`：monkey-patch `apply_rotary_pos_emb`（模块级函数，
    按调用计数定位层号）+ hook v_proj/self_attn/norm，无梯度只读捕获；
  - `CompressedAttentionPatcher`：把全部层 `self_attn.forward` 替换为
    「群均值 KV」注意力（prefill 算 scatter_mean、decode 用冻结群均值+新 token 缓冲）；
  - 8 话题跨模态语料（中/英/Python/JSON/数字/诗词，同质中文语料会导致 G=1 坍缩）；
  - 四阶段：V0 重排不变性 → (k,τ) 联合扫描 → V1/V2/V3 保真度 → needle 对比；
  - 无 sklearn 依赖（自带 ARI/mutual_kNN/窗口平滑）。
- **`eval_runner.py`**：新增 `--model/--ctx/--needle-positions/--skip-needle/--hf-endpoint`，
  提供后 importlib 转交 v41 runner；`check_interfaces` 同步校验（13/13）。
- **`tools/make_kaggle_notebook.py` + `run_on_kaggle.ipynb`**（nbformat 校验通过）：
  按显存自动选模型（≥40GB→7B bf16 / ≥14GB→1.5B / else 0.5B），
  含 GPU 检查、签名校验、核心验证、结果解读指南。
- **`results/real_model_validation.json`** + needle 三位置 JSON + **`results/REAL_MODEL_VALIDATION.md`**。

### 实测结果（Qwen2.5-0.5B，2048 token，CPU float32，111.3s，峰值 RSS 4.14GB）

| 阶段 | 结果 | 判定 |
|---|---|---|
| V0 重排不变性 | mean=0.9999999999999（std 2.2e-14） | ✅ **Triton 化的数学前提门槛已过** |
| (k,τ) 校准 | τ=0.97 硬边界（τ≤0.95 时 G=1 全坍缩）；k=8 ARI 0.108 > k=16 0.091 | τ 甜点 0.97，k=8 |
| 多尺度 | ARI_occ 0.060 < ARI_topic 0.108 | 话题×位置碎裂确认（与 mock 一致） |
| 谱系 | V1(136×)=0.5037 ≈ V2 > V3(34×)=0.4337 | 与 mock 一致复现 |
| 端到端 | final_hidden cos=0.4199，argmax 一致率=**2.32%** | ❌ 全层 flat-mean 136× 破坏性过大 |
| needle | 0.15/0.85 原始命中 '42'，压缩全失败；0.5 原始自身答错 | ❌（0.5B 中段基线不可靠，需 7B 复核） |

### 关键发现

1. **V0=1.0 是最重要正面结果**：KV gather/scatter + RoPE + GQA + 因果掩码 +
   o_proj 全管线在真实模型上数学正确（误差 float32 机器精度内）。
2. **真实隐空间话题结构远弱于 mock**：τ≤0.95 全坍缩（跨话题余弦 0.716），
   甜点被压到 0.97；E0 v0.2.0 预注册的 h0c（合成 τ 需重校准）证实且幅度超预期。
   w=8 因果平滑是分组前提（互通率 0.50→0.83）。
3. **单层余弦系统性高估端到端保真度**（0.50→0.42，残差流稀释）——mock 结论
   在真实模型上确认，方法论上必须看 logits/argmax 而非单层输出。
4. **朴素全层压缩不可行，但不是方向证伪**：升级路径按代价排序为
   （a）层级选择性压缩（E3 定位可压缩层）（b）提高每群代表数降低压缩率
   （c）混合策略（热 token 保留 + V2 稠密化）。
5. **0.5B needle 中段基线自身失败**（答 '1.414213'），needle 结论必须上
   Kaggle 7B 复核。

### 待办（Kaggle 上机）
- [ ] 7B 复跑本验证器，对照 0.5B 基线（τ 甜点是否下移、端到端 cos 是否回升）
- [ ] 压缩率-保真度曲线系统扫描（每群代表数 4→16→32）
- [ ] E2 显存监控：`torch.cuda.max_memory_allocated()` 前后对比写卡
- [ ] E0 通过后按 E1 → E3 → E4 → E2/E5/E6 顺序推进

---

## v0.1.1 — 2026-10-18 — 用户三项调整落地

### 调整一：E4 增加 Max Stale Turns 硬约束（`threshold(max_stale=K)`）

**动机**：v0.1.0 发现纯阈值触发的 partial 更新新鲜度误差峰值 0.2265，超 E4
通过线 0.05 → 需要周期性 full 兜底。用户提出 `max_stale=K` 硬约束：
距上次 full 超 K 轮则无论 drift 多小都强制 full。

**实现**：
- `mock_scheduler.py`：`MockReorgScheduler` 新增 `max_stale_turns` 参数与
  强制 full 分支；`_last_full_turn` 初值从 -1 改为 0（init fit 视为 full）；
  `note_full_cost(cost_s, turn)` 显式接收轮次（修 off-by-one）。
- `run_turns.py`：`run_mode` 新增 `max_stale_turns` / `ema_alpha` 参数；
  `parse_mode_spec` 支持 `threshold:max_stale=2:alpha=0.8` 规格串
  （含键名映射 `max_stale→max_stale_turns`、`alpha→ema_alpha`）。
- `mock_relation.py`：新增 `structure_error()`（边集 Jaccard），用于区分
  **权重滞后**（freshness 高、structure 低 → 调 alpha）与
  **结构陈旧**（两者都高 → 只能 full 修复）。

**CPU 模拟结果（10 轮，N0=20k，每轮 +2k，drift 爆发轮 4/8 为 40% 新话题）**：

| 臂 | α | K | full 次数 | 总耗时 s | max_freshness | max_structure | 判定 |
|---|---|---|---|---|---|---|---|
| per_turn_full | — | — | 10 | 74.74 | 0.0000 | 0.0000 | 基准 |
| threshold（无兜底） | 0.3 | — | 2 | 16.52 | 0.2265 | — | fail |
| periodic_k4 | 0.3 | 4 | 3 | 17.44 | 0.1902 | — | fail |
| threshold:max_stale=2 | 0.3 | 2 | 5 | 39.89 | **0.1902** | 0.0659 | **fail（用户预期未成立）** |
| threshold:max_stale=2 | 0.8 | 2 | 5 | 38.46 | 0.0819 | 0.0659 | inconclusive |
| threshold:max_stale=2 | 0.9 | 2 | 5 | 38.51 | 0.0639 | 0.0041 | inconclusive |
| threshold:max_stale=3 | 0.3 | 3 | 4 | 28.73 | 0.1830 | 0.0826 | fail |
| **threshold:max_stale=3** | **0.8** | **3** | **4** | **28.28** | **0.0442** | 0.0472 | **pass（Pareto）** |

**关键发现（修正用户预期，已如实写入 E4 卡 v0.2.0）**：
1. **「K=2 即可把误差压回 0.05」不成立**：α=0.3 时实测峰值 0.1902。
   根因：单次 partial 的 EMA 滞后就是 (1-α)·Δw ≈ 0.7·Δw，drift≈0.08 的
   常规轮单步误差即达 0.14–0.19；K 只限制 full 间隔，压不住单步滞后。
2. **max_stale 是必要非充分条件**，必须与 EMA 系数 α 联合校准：
   单步误差上界 ≈ (1-α)·Δw/|w|，要 ≤0.05 需 α≥0.8（Δw/|w|≈0.2 时）。
3. **Pareto 点为 K=3 + α=0.8**：max_freshness=0.0442 ≤ 0.05，总耗时 28.28s
   仅为 per_turn（74.74s）的 38%（2.64× 提速），且比 K=2 全系更快
   （K=3 允许更多 none 轮：4 vs 3）。有趣的是 K=3 比 K=2 既省时间又保新鲜度。
4. structure_error 全程 ≤0.083 且 full 后归零，证实误差以**权重滞后**为主，
   调 α 有效；结构陈旧由 max_stale 的 full 兜底控制。

### 调整二：提前准备真实环境 SemanticGrouper 接口

- 新增 `src/semantickv/anchor_grouper.py`：
  - `AnchorBasedSemanticGrouper(SemanticGrouper)` 空壳类，签名与接口
    骨架完全一致（`eval_runner.py --check-interfaces` 已注册校验，
    13/13 通过）；
  - `PrefillCapture` 数据类：隐状态 (N,d) + 锚点注意力行/列摘要 + 位置；
  - 明确有卡环境填充点：`register_prefill_hooks`（hook 注册）、
    `_anchors_from_capture`（锚点提取）、`fit/partial_fit`（GSC 流水线）；
  - 无卡环境可导入、可实例化、可校验签名；fit/partial_fit 调用时抛出
    带 TODO_ANCHOR 编号的 NotImplementedError（不静默失败）。
- 设计要点：真实环境输入不是原始 embedding，而是 prefill 的 Attention
  摘要与隐状态；token-锚点相似度 S=H·Aᵀ 与 GSC 的 token-质心相似度同构，
  同一套 GSC 流水线可复用。

### 调整三：合成分布 vs 真实分布的阈值校准（E0 准入第二职责）

- `experiments/E0_baseline_phase.yaml` 升级 v0.2.0：新增 `calibration` 段——
  E0 除测相位差外，必须提取真实数据 prefill 隐状态跑 kNN 分布分析
  （k_grid=[8,12,16,24,32] × tau_grid=[0.1..0.5]），输出
  `knn_sim_quantiles / mutual_knn_rate / fragmentation_rate / n_groups_at_tau`，
  按决策规则选定 (tau, k) 回填 E1。
- 新增假设 h0c/h1c：合成高斯混合上 ARI=1.0 的 τ=0.3 在真实多尺度流形
  分布上可能导致碎片化（合成基线互 kNN 互通率仅 0.40，真实分布预期更低）。
- 校准复用调整二的 `PrefillCapture`/hook 机制抽取隐状态。

### E4 卡同步升级
- `experiments/E4_reorg_schedule.yaml` v0.2.0：新增对照臂
  `threshold_max_stale2` / `threshold_max_stale3`、假设 H4b（max_stale 与
  α 联合校准）、`max_stale_arm` 判定阈值、mock 段写入全部 CPU 实测数据。

---

## v0.1.0 — 2026-10-17 — 脚手架 + CPU 模拟首版

### 交付
- E0–E6 预注册卡（`experiments/`）：假设 H0/H1、主指标、通过/证伪阈值、
  序贯停止规则、失败归因更新规则。
- `eval_runner.py`：接口骨架 + CLI + 95% CI + 序贯决策 + mock 冒烟模式。
- `REPORT_TEMPLATE.md`：结果报告模板。
- Mock 实现：GSC 分组器 / top-p 关系图（EMA+Delta+CRDT）/ 三模式重组调度器。
- CPU 模拟：机制微测试 14 项、规模基准（10k–500k）、10 轮对话模拟。

### 期间发现并修复的问题（负结果/坑位记录）

| # | 问题 | 根因 | 修复 | 影响面 |
|---|---|---|---|---|
| 1 | GSC 在合成数据上碎成 1 个群（ARI=0） | 话题内噪声用绝对 std=0.35，高维下噪声能量 σ²d（d=768 时≈95）淹没话题结构，话题内余弦仅 ≈0.01 | 噪声改为 `base_topic_std/√d`（默认 base=0.65，话题内余弦 ≈0.70） | `synth.py`、`run_turns.py` |
| 2 | `partial_fit` 把每个未匹配新 token 各成一个新群（1000 新 token → 1993 新群） | 新群编号按 token 逐个分配，未在未匹配子集内聚类 | 未匹配子集内做小型互 kNN + 连通分量 + 标签传播，同话题归一群 | `mock_grouper.py` |
| 3 | 互 kNN 后连通分量仍碎裂（5 个话题 → 138 片） | 互 kNN 互通率仅约 40%，碎片间无互边 | 标签传播合并碎片（与 `fit` 一致；`partial_fit` 微型聚类同样需要） | `mock_grouper.py` |
| 4 | kNN 吞吐极低（10k 耗时 15s，N² 外推 500k 需 10 小时） | pip 的 faiss-cpu 轮子在本机仅 6–25 GFLOPS；且 32 线程（98 GFLOPS）慢于 8 线程（240 GFLOPS）——容器内线程超订阅 | 默认后端切 torch 分块矩阵乘，`SEMANTICKV_THREADS=8`；faiss 保留为可选 | `mock_grouper.py`、`run_scaling.py` |
| 5 | 500k 运行被 OOM killer 斩杀（exit 137） | 容器 cgroup 上限 **8GB**（`free` 显示的 123GB 是宿主内存）；数据生成瞬态峰值 ≈4.6GB 叠加 kNN 稳态 ≈2.9GB | 合成分块生成（≈8MB/块）+ kNN 分块 512→256；500k 全维峰值降至 5.97GB | `synth.py`、`mock_grouper.py` |
| 6 | 10 轮模拟中 per_turn 模式图状态为空（edges=0、新鲜度=1.0） | `build_full` 是纯计算不装状态，full 分支从未把边灌入图 | 新增 `install_full()`（全量重建并替换状态），full 分支与初始化改用它 | `mock_relation.py`、`run_turns.py` |
| 7 | `run_turns.py` 汇总段 KeyError | `rows[0]`（init 行）没有 `"decision"` 键却被遍历 | 统计改用 `rows[1:]` | `run_turns.py` |

### 模拟产生的重要发现（非 bug）

- **纯增量关系更新有保鲜上限**：连续 3 轮只做 EMA+Delta 增量，边权与全量重算
  的相对误差达 0.226，**超过 E4 预注册的 0.05 通过线** → 需要周期性全量重组兜底
  （periodic 模式检查点保持 0.000）。已写入 E4 的后续改进建议。
- **重组经济性方向性证据**：threshold 模式 10 轮总耗时 17.5s，比 per_turn
  （73.4s）快 4.2×，调度器决策序列符合设计。
- **图操作成本可忽略**：10 轮全部 build+observe 合计 6–8ms（G≈60），
  成本几乎全部在分组 kNN（96–99%）。

### 阈值校准记录
- v0.1.1：CPU 模拟校准 `ema_alpha`（0.3→0.8）与 `max_stale_turns`（K=3），
  见 v0.1.1 调整一。真实分布的 τ/k 校准待有卡环境按 E0 v0.2.0 执行。

### 待办（有卡环境）
- [x] CPU 模拟验证核心逻辑（GSC / EMA+CRDT / 三模式调度 + max_stale 兜底）
- [x] 真实环境接口准备：`AnchorBasedSemanticGrouper` 空壳 + 签名校验
- [ ] 用真实 V4.1-Flash-1017 跑 E0（环境准入 + kNN 分布校准 τ/k）
- [ ] E0 通过后按 E1 → E3 → E4 → E2/E5/E6 顺序推进
- [ ] 有卡环境按 E4 H4b 重扫 (K, α) 网格，复核 Pareto 点
- [ ] 每次结论写入 `results/<ID>_report.md`，失败必须填归因决策树
