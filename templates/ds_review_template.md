---
review_id: DS-YYYYMMDD-NNN          # 唯一评审编号（日期+序号）
source_url: https://chat.deepseek.com/share/XXXX   # 原始对话链接（可核查）
date: 2026-10-18
reviewer: DeepSeek
focus: <一句话主题，如「V1.5 压缩策略评审」>
disciplines_proposed: []   # 若本评审提出纪律/规则，列条目名
sources: []                # 跨域/文献/论文断言的可核查来源（URL/论文/具体数据）
hypotheses:
  - id: H-DS1
    statement: "<DS 的原始预判/处方，逐字保留>"
    falsifiable_criterion: "在 <模型/上下文/指标> 下达到 <具体阈值/条件>，否则证伪"
    data_source: "results/xxx.json 或 experiments/E*.yaml（预期用于验证的 WB 产出）"
    status: pending          # pending | confirmed | refuted
    evidence_file: ""        # 状态非 pending 时必填，指向存在的 WB 结果文件
    ds_admitted_refutation: false   # 若 refuted，DS 是否在后续评审显式承认
    fate_note: ""
---

# 理论背景（Why）
<DS 给出该评审/处方的动机与理论依据：各向异性、低秩偏置、EMA 滞后等。>

# 具体步骤（What）
<DS 给 WorkBuddy 的具体修改指令，编号列出，可逐条执行。>

# 验收标准（How）
<每条假设对应的可证伪判定；WorkBuddy 跑出数据后据此更新 hypothesis_ledger.yaml 状态。>

<!--
使用说明（WorkBuddy intake 协议）：
1. 用户转发 DeepSeek 对话链接 → WorkBuddy WebFetch 抓取；
2. 按本模板落盘为 results/ds_reviews/<date>_<seq>_review.md；
3. 抽取 DS 的每条预判/处方为 hypotheses[] 条目，填写可证伪标准与数据来源；
4. 跑出对应实验后，更新 results/hypothesis_ledger.yaml 的 status/evidence_file；
5. tools/check_ds_review.py 校验本模板合规性与台账闭环。
-->
