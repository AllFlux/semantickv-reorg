# SemanticKV vLLM 插件（参考骨架）

> 状态：**参考骨架**，供 Kaggle 7B（H-DS15）与 WB2 跨项目协作使用。
> vLLM 内部 API 随版本变动剧烈，本包固化**与版本无关的结构**，实际挂载点需在目标 vLLM 版本上适配。

## 模块

| 文件 | 职责 |
|---|---|
| `semantic_router.py` | 压缩路由层：默认 GSC 数学聚类；**`SemanticRouter.load_external_router(path)`** 为 WB2 预留的挂载点（学习型路由模型可替换数学聚类） |
| `kv_rewrite.py` | 索引重写：`select_topk` / `select_hybrid`（Sink+滑窗+中段 K）两种保留策略 + `mid_kv_retention_ratio` 记录 |
| `../sink_protect.py` | Sink 防爆保护独立模块（WB1→WB2 交付物）：三段注意力 + logsumexp 合并 + RL Action 守卫 |

## vLLM 挂载点（适配指南）

在 vLLM ≥ 0.6 上，三阶段接入：

1. **路由**：prefill 结束、`forward` 返回前，取最后一层 hidden（或缓存 K）→ `router.route(hidden)` → `router.assign(labels, N)` 得 GroupAssignment；
2. **重写**：`KVIndexRewriter.select_*` 产出保留位置 → `rewrite_index` → 在 `CacheEngine`/`KVCacheBlock` 分配处 gather（压缩层只落槽位，非压缩层照常）；
3. **注意力**：被 patch 的层用 `sink_protect.segmented_attention`（三段 logsumexp 合并），其余层走原生 FlashAttention。

## 验收（H-DS15，Kaggle T4 上执行）

- CUDA Graph 捕获成功（插件层不得破坏 `torch.compile`/graph 断点约束：路由在 graph 外、重写用静态 shape 槽位表）；
- 解码 p99 延迟 ≤ 原生 FlashAttention 的 1.5×（超线 → 退回 HARD-KV 离线重写）；
- 存证 `results/kaggle_7b_vllm_plugin_bench.json`。

## Sink 红线（给任何接入方）

**前 `sink_tokens` 个位置的 KV 不可重写/丢弃/替换**——它们稳定 softmax 分母（0.5B 实测前 4 token 占 33.2% 注意力质量）。WB2 的 RL 虚拟 Action 请过 `sink_protect.rl_action_guard`。
