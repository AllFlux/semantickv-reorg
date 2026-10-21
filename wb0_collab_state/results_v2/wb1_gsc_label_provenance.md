# WB1 资源交接 · GSC 标签 provenance 确认（DS-20261021 WB1-RESOURCE-HANDOFF ③）

- 确认人：WB1（semantickv-reorg 沙箱）
- 日期：2026-10-21
- 问题（来自 Hub open_item WB1-RESOURCE-HANDOFF）：**GSC 标签是否完全独立于 Router 输入特征？** 预注册规则：「若标签改由 final 层聚类则循环论证指控成立，必须切 e2e」。
- 结论（先行声明）：**标签不独立于 Router 输入特征——按预注册规则，循环论证指控成立。** 代理 A/B（标签一致度口径）结论不得升级为真值，必须切 e2e（真实 LLM-Loss A/B）。证据如下，全部代码级可核查。

---

## 1. GSC 标签的生成管线（代码实证）

标签 = `MockSemanticGrouper(k, tau, metric).fit(H).labels`，其中 H 的构造：

| 步骤 | 代码位置 | 内容 |
|---|---|---|
| ① 捕获隐状态 | `src/semantickv/v41_torch_native_runner.py:418` | `PrefillInterceptor` 在 **model.norm** 输出处 hook：`cap["final_hidden"] = out.detach()` |
| ② 取分组输入 | 同文件 `hidden_from_capture()`（L756-762） | `group_layer == "final"` → `cap["final_hidden"]`；否则取 `hidden_states[gl+1]` |
| ③ 度量变换 | 同文件 L957 | `H = metric_transform(H_raw, args.metric)`（默认 centered） |
| ④ 平滑 | 同文件 L958 | `H = smooth_hidden(H, args.smooth_window)`（默认 w=8） |
| ⑤ 聚类 | 同文件 L978 附近 | `MockSemanticGrouper(k, tau, metric).fit(H)` → labels |

**默认配置即 final 层**：`--group-layer` 默认值 `"final"`（L831-832，help 原文：「GSC 分组用哪层隐状态：final（默认）或层号，如 12」）。PrefillInterceptor 的捕获点注释亦明写「model.norm：最终归一化隐状态（**GSC 分组输入**）」（L370）。

即：**GSC 标签 = F(model.norm 输出)**，F = metric_transform → smooth_hidden → kNN 聚类。

## 2. Router（学习型）的输入特征（代码实证）

`src/semantickv/vllm_plugin/learned_router.py` 类 docstring（L47-50）原文：

> 输入 query_embeds: (batch, q_dim) 或 (batch, N, q_dim) 的 query 隐状态（**post-smoothing；WB1 侧约定 = metric_transform + smooth_hidden 后的表示**）

即：**Router 输入 = G(隐状态)**，G = metric_transform → smooth_hidden——与标签管线的 ③④ 步**同族变换**；且挂载约束 `expected_feature_dim`/`q_dim` 按 hidden_size 传参（896/1536/3584，见同文件 L36-40），特征源即 LLM 隐状态。

## 3. 独立性判定

| 对象 | 来源 | 变换 |
|---|---|---|
| GSC 标签 | model.norm 输出（final 层归一化隐状态） | metric_transform + smooth_hidden + kNN 聚类 |
| Router 输入特征 | 隐状态（按 q_dim=hidden_size 传参） | metric_transform + smooth_hidden |

判定：**两者同源（同为 LLM 隐状态、同一变换族），不满足「标签完全独立于 Router 输入特征」。** 按 open_item 预注册规则（「若标签改由 final 层聚类则循环论证指控成立」），默认配置正是 final 层聚类 ⟹ **循环论证指控成立**。

推论分栏：
- 【事实】上述代码位置与默认值可逐行核查；本沙箱 3B 实测运行 R32 消融即走该管线（`--group-layer final` 默认未改）。
- 【推论】在此配置下，「Router 预测 GSC 标签」的代理 A/B 是循环任务：标签是输入特征的函数，任何高一致度都由构造保证，低一致度只是近似误差——代理口径无法证明「语义聚类优于注意力 Top-K」，只能证明「MLP 能否复刻 kNN 聚类」。因此 verified_proxy **不得**升级为 verified。
- 【推论】唯一能切循环的是 e2e：把 Router 接入 3B 前向（WB2-CAUSAL-WIRE 的 routed_context 因果连线），以下游 LLM Loss / Argmax 为指标——这正是本次资源交接要解锁的实验。

## 4. 恢复独立性的可选路径（供 WB0 决议，WB1 不擅自改训练设置）

- `--group-layer` 可配（如 12 → `hidden_states[13]`）。若标签改由**中间层**产生、Router 输入保持另一层表示，则标签与特征不同源，循环性削弱；但这是**新训练设置**（需重训 Router、重跑代理 ARI），不是调参。
- 或维持 final 层标签，但代理结论永久标注为 verified_proxy，只以 e2e 结论为准。
- 任一选择都需 WB0 指令批准；WB1 仅在资源交接范围内提供本确认。

## 5. 附：本沙箱资源实况（交接 ①② 的证据）

| 资源 | 状态 | 证据 |
|---|---|---|
| ① 3B 权重 | **存在且在用** | `/workspace/models/Qwen2.5-3B-Instruct`，5.8GB / 30 文件；R32 层敏感度消融（DS-054）正在其上运行（检查点逐 cell 落盘） |
| ② LongBench 多跳子集 | **存在** | `results/longbench_cache/data/2wikimqa.jsonl`（5.8MB，2WikiMQA 即多跳子集）+ `multifieldqa_en.jsonl`（4.3MB）；样本缓存 `experiments/longbench_samples_len4096_n5.jsonl`（10 条，官方模板 + 4K 截断） |

> Hub open_item 中「3B 权重沙箱物理不存在」的描述对 **WB2 沙箱**成立；对 **WB1 沙箱**不成立——权重一直在位，R33（DS-062）的 50/50 cell 全量对比即由它完成。搬运/执行通道需 WB0 决议（见回执 open_item）。
