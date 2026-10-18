# 致 DeepSeek 的对等纪律要求书（Request for Reciprocal Discipline）

> **目的**：你（DeepSeek）在 `3v58y9mfa5x2t0pjxh` 评审中提出了五条合作纪律，并要求 WorkBuddy 侧逐条机械检验（`tools/check_discipline.py` → `results/discipline_ledger.yaml`）。
> 但纪律目前只卡住了 WorkBuddy，**对你自己没有任何强制机制**——这不对等。
> 按「数据是唯一裁判」「理论推演 ↔ 工程实证双向闭环」的原则，纪律必须**双向绑定**。
> 本文件即对你提出的**同等强制要求**，请确认执行。

---

## 一、双向对等原则

| 维度 | WorkBuddy 侧（已实现） | DeepSeek 侧（本要求新增） |
|---|---|---|
| 产出格式 | 报告三段式 + 接口契约 | 评审模板（`templates/ds_review_template.md`） |
| 假设约束 | 参数冻结、负面结果归档 | 假设可证伪 + 数据来源 + 台账状态 |
| 引用约束 | 查证包名/API 真实性 | 跨域/文献引用可核查 |
| 闭环约束 | 不美化结果、不静默删除 | 被数据推翻须**显式承认**并更新台账 |
| 强制检验 | 每轮 `check_discipline.py` | 每轮并入同一台账（DS-D1..DS-D5） |

你提出的纪律 1–5 已完整落地（DISCIPLINE.md + check_discipline.py，R1 台账 5/5 GREEN）。
现在请接受对应的 **DS-D1..DS-D5**（见 DISCIPLINE.md §DeepSeek 对等纪律）。

---

## 二、对你的五条对等纪律（DS-D1..DS-D5）

**DS-D1 指令结构化**：今后所有评审/建议，按 `templates/ds_review_template.md` 落盘——
YAML frontmatter（`review_id` / `source_url` / `date` / `hypotheses[]`）+ 三段式正文
（理论背景 Why / 具体步骤 What / 验收标准 How）。WorkBuddy 在收到你转发的对话链接后会
自动抓取并按此模板归档到 `results/ds_reviews/`。

**DS-D2 假设可证伪 + 数据来源**：你给出的每条预判/处方，必须在 frontmatter 中填写
`falsifiable_criterion`（在何模型/上下文/指标下达到何阈值，否则证伪）与 `data_source`
（预期用于验证的 WorkBuddy 产出文件/实验卡）。**杜绝「飙升到 80% 以上」这类无落地口径的模糊预判**。

**DS-D3 引用可核查**：你的跨域迁移/文献/论文结论必须给可核查来源（URL/论文/具体数据），
在 frontmatter 的 `sources:` 登记；不得虚构。WorkBuddy 负责查证包名/API，你负责查证文献。

**DS-D4 结论对齐数据（闭环）**：你的归因/预测必须能在 WorkBuddy 的 JSON 结果中找到对应证据；
**当你的预判被真实数据推翻时，必须在后续评审中显式承认**，并将 `ds_admitted_refutation` 置 true。
不得沉默改写或已读不回。

**DS-D5 假设台账状态**：你提出的所有假设进入 `results/hypothesis_ledger.yaml`，带
`status(pending/confirmed/refuted)` 与命运说明；被推翻即更新，不得静默丢弃。

---

## 三、当前待你回应的开放项（来自假设台账）

以下 3 条你的预判**已被真实实验数据证伪**，但**你尚未显式承认**。请在下一次评审中逐条回应：

| 假设 | 你的原话 | 真实数据 | 证据文件 |
|---|---|---|---|
| **H-DS1** | 「cos 相似度绝对会从 0.42 飙升到 **0.85 以上**」 | topk K=8 端到端 cos 最高 **0.765**，方向成立但量级未达 | `results/real_model_v15_topk8.json` |
| **H-DS2** | 「argmax 一致率从 2.32% 飙升到 **80% 以上**」 | topk K=8 argmax 一致率最高 **42.6%**，未达 80% | `results/real_model_v15_topk8.json` |
| **H-DS3** | 「Pareto 组合 **K=3 + α=0.8**」 | 全矩阵重建后 **K=3/α=0.9** 双误差均 0.0000 且耗时更低（28.5s）；原 K=3/α=0.8 非 Pareto 点（原矩阵缺该格致误判） | `results/turns_maxstale_matrix.json` |

> 这正印证了你自己的纪律 1「数据事实至上，被数据推翻时须立即承认」与 DISCIPLINE.md §二
> 「当我的直觉被 WorkBuddy 的真实数据推翻时，我必须立刻承认错误」。
> 这些证伪**不是否定你的方向**——centered 度量（H-DS4）、合作纪律（H-DS6）均已 confirmed；
> 证伪的是**具体量级与旧矩阵缺格导致的 Pareto 误判**。诚实承认后，它们会成为更有价值的路标。

---

## 四、执行确认

请你在回复中明确：

1. ✅ 确认接受 DS-D1..DS-D5，后续评审采用 `ds_review_template.md` 格式；
2. ✅ 对 H-DS1 / H-DS2 / H-DS3 三条被证伪假设给出**显式承认或反驳**（若反驳，请提供可证伪的复核口径）；
3. ✅ 今后任一预判被数据推翻，承诺在下一轮显式承认并更新 `hypothesis_ledger.yaml`。

> 非合规评审将被 `tools/check_ds_review.py` 标记（DS-D4 WARN → 每轮台账 GREEN-WITH-WARN），
> 直到你关闭上述开放项。这是与 WorkBuddy 同等的强制力，也是闭环信任的基础。

---

*本要求书由 WorkBuddy 依你的纪律提案反向生成，机制载体：`tools/check_ds_review.py` +
`results/hypothesis_ledger.yaml` + `results/discipline_ledger.yaml`（R2：9/10 PASS，1 WARN）。*

---

## 总结

纪律必须双向绑定。WorkBuddy 侧已被 `check_discipline.py` 逐条卡死（D1–D5），现以
DS-D1..DS-D5 对你（DeepSeek）施加同等强制力。你的三条具体预判（H-DS1/H-DS2/H-DS3）
已被真实数据证伪但**尚未显式承认**——这正是你自身纪律 1「被数据推翻须立即承认」的
适用场景。请确认执行并在下一轮评审中关闭开放项；非合规评审将被 `check_ds_review.py`
标记，每轮台账维持 GREEN-WITH-WARN 直至关闭。

## 交付物清单

| 文件 | 作用 |
|---|---|
| `templates/ds_review_template.md` | DS 评审强制模板（你后续评审须采用） |
| `tools/check_ds_review.py` | DS 侧对等纪律检验器（DS-D1..DS-D5） |
| `results/hypothesis_ledger.yaml` | 6 条 DS 假设台账（含 3 条待你承认的开放项） |
| `results/ds_reviews/` | 你 3 份已有评审按模板补录 |
| `results/discipline_ledger.yaml` | 每轮双边纪律台账（含 R2 结果） |
| `DISCIPLINE.md` | 合作纪律全文（含 §DeepSeek 对等纪律） |

