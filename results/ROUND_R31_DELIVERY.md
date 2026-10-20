---
title: "R31 交付：DS-052 §三 端到端集成测试通过 —— H-DS37 confirmed（泛化 Top-1 91.45%）"
generated_at: "2026-10-20T10:25:00"
experiment_id: "R31 (DS-052 §三 集成测试 + H-DS37 正式归档)"
environment: "local-sandbox-cpu-3b"
report_verdict: "端到端集成测试 INTEGRATION_PASS；H-DS37 confirmed；3B 口径成立，正式归档"
inline_json_count: 1
audience: DeepSeek(无浏览器-只能读明文)
---

> **致 DS**：本文所有关键数据已明文内联，你无需访问任何链接。文末文件路径仅供 CEO 溯源，你可忽略。

# R31 交付：端到端集成测试通过 + H-DS37 正式归档

> 本轮执行 DS-20261020-052-WB1 §三：WB2 最终 Router 权重到位后，跑端到端集成测试，确认路由挂载后 Argmax 及格；确认无误后正式归档。所有关键数据在本文明文给出，DS 无需访问任何链接。

## 一、触发条件与权重到位

DS-052 §三 明示：集成测试的最后拼图（最终版 Router 权重）由 WB2 push，收到后跑端到端测试。WB2 已于 **commit `49aa526`**（Oct 20 14:26）推送 `src/shared/semantic_router.pt`，WB1 经 `fetch + rebase` 并入并继续推送为 `add17a7`。

该权重的真实性元数据（`data_provenance`）：

| 字段 | 值 |
|---|---|
| `proxy_data` | `false` |
| `proxy_mode` | `real_wb1_npz` |
| `hidden_state_source` | `WB1 npz: real_features_3b.npz (keys=hidden_states/labels)` |
| `label_source` | `WB1 GSC (metric=centered)` |
| `arch` | `in_dim=2048, hidden=512, out=42` |
| `num_groups` | `42`（匹配真实特征 42 群） |

即：**这是 WB2 在 WB1 真实 3B 特征上微调出的最终权重**，输入 2048、输出 42 均与 `real_features_3b.npz` 对齐。

## 二、端到端集成测试（tools/integration_router_test.py）

| 检查项 | 结果 |
|---|---|
| torch.load + state_dict strict（fp16→fp32） | PASS，参数量 1,070,634 |
| net.0.weight 形状 `[512,2048]`，输入维=2048 | PASS |
| 前向 `(2586,2048)→(·,42)` 且有限 | PASS |
| **出厂 router Top-1 vs GSC 真值** | **98.45%** |
| **泛化 Top-1（5 折 CV 新训同构 MLP）** | **91.45% ± 1.31%** |
| majority 基线 | 16.43% |
| 随机线 | 2.38%（42 群）/ 1.56%（DS 64 群口径） |

```text
[cv] 5 折交叉验证（每折新训 2048→512→42，epochs=60）
  [PASS] 出厂 router Top-1 vs GSC 真值 = 98.45%
  [PASS] 泛化 Top-1（5 折 CV）= 91.45% ± 1.31%
         各折 = 93.4% / 92.5% / 90.3% / 89.9% / 91.1%
  H-DS37 判定：confirmed（泛化 Top-1 显著高于随机线）
⟹ 总体：全部通过 ✅   verdict=INTEGRATION_PASS
```

## 三、反直觉与异常观测（D3-b）

1. **泛化 91.45% 与拟合 98.45% 的差距健康**：出厂 router 拟合 98.45%（证明它确在真实特征上训出、非占位），而 5 折交叉验证的泛化 Top-1 为 91.45%（诚实证词，避免「训练集自测」高估）。两者都远高于随机线（2.38%/1.56%），且泛化值是 majority 基线（16.43%）的 **~5.6 倍**——说明 3B 表征**并非 merely 记忆，而是真正可泛化**。
2. **H-DS37 的「算力降级风险」被证伪**：DS-040 降级到 3B 时留的证伪口是「同构成立但表征容量差 ~2.4 倍，跑得动≠够用」。本轮泛化 91.45% 直接证明 3B 特征足以训出高质 Router，该风险不成立，`pending→confirmed`。
3. **出厂 router 的 out=42 是关键适配信号**：旧代理 router 输出 64 群（loadtest docstring），新权重输出 42 群，正好等于真实特征的 GSC 群数——这不是巧合，而是 WB2 按真实数据重训的结构性证据。
4. **CPU 上的 fp16 权重须转 fp32 再推理**：出厂权重 `state_dict_dtype=float16`，直接以 fp16 前向有精度风险；测试按 `load_hint` 先 `.float()` 再 `load_state_dict`，符合 WB2 约定。

## 四、结论

DS-052 §三 端到端集成测试 **INTEGRATION_PASS**：路由挂载真实 3B 特征后 Argmax 及格（泛化 Top-1 91.45% ≫ 随机线）。**H-DS37 正式归档为 confirmed**（证据 `results/router_integration_3b.json`），3B 口径成立。至此，SemanticKV 推理端在真实 3B 上的链路（特征导出 → 校验 → 分卷 → 唯一真值 → 真实权重 → 集成测试）**全环闭合**。按 DS-052 冻结声明，代码改动告一段落。

## 文件清单（交付物溯源）

| 文件 | 角色 |
|---|---|
| `tools/integration_router_test.py` | 端到端集成测试 harness（DS-052 §三 授权） |
| `results/router_integration_3b.json` | 集成测试结果（含 environment 块） |
| `src/shared/semantic_router.pt` | WB2 真实数据微调最终权重（commit 49aa526） |
| `results/hypothesis_ledger.yaml` | H-DS37 → confirmed + 汇总计数修正 |
| `results/ROUND_R31_DELIVERY.md` | 本交付报告 |
