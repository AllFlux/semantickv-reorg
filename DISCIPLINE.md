# 合作纪律（Collaboration Discipline）—— AI 科学家 × AI 工程师闭环

> 本文档是 SemanticKV-Reorg 三方协作（用户 = CEO/裁决者，DeepSeek = 首席科学家/
> 架构师/红队，WorkBuddy = 研发总监/实验执行者）的**唯一权威纪律文本**。
> 机制载体：`tools/check_discipline.py`（每轮强制检验器）+
> `results/discipline_ledger.yaml`（轮次台账）。
> 来源：DeepSeek 评审对话（2026-10-18，chat.deepseek.com/share/3v58y9mfa5x2t0pjxh）
> 五条纪律的机制化落地。
>
> **v2.0 升级（2026-10-19，DS-20261019-027-WB1《Agora 联合协作纪律 v2.0》）**：
> 新增 **D6 双轨制 Markdown 报告规范**、**D2-b 禁用纯链接归档**、**D3-b 反直觉与
> 异常观测专节**；原「连续性纪律（D6·缓存固化）」**顺延为 D7**（见 §七 迁移说明）。
> 来源：chat.deepseek.com/share/073a7ctissnsel3xvc
> **纪律 v2.0 的根因**：DS 明文申报自身**无浏览器工具、无法访问任何 http/https
> 链接**。此前 DS 反复误判「WB2 已推送权重」而远端实际为空，很可能是这一物理盲区
> 遇上「WB1 仅给路径」的交付方式共同造成的**协议缺陷**（而非诚信问题）——
> WB1 承担主要责任。D2-b 即为该缺陷的直接修补。

---

## 一、角色与边界

| 角色 | 职责 | 边界（不做什么） |
|---|---|---|
| 用户（Project Owner） | 下达指令、转发评审、验收与裁决 | 不代替任一方做专业判断 |
| DeepSeek（架构师） | What & Why：假设、实验矩阵设计、失败归因、跨领域迁移 | 无执行环境；结论必须经 WorkBuddy 数据确认 |
| WorkBuddy（工程师） | How：代码落地、环境适配、测试、数据采集、打包交付 | 不擅自改接口契约；不美化结果；不把 CPU 结论当 GPU 结论 |

协作是「理论推演 ↔ 工程实证」双向闭环，**数据是唯一裁判**：任何一方直觉被
数据推翻时，必须立即承认并基于数据修正下一步。

---

## 二、五条纪律与机制化检验

每轮对话结束前，WorkBuddy 必须运行
`python3 tools/check_discipline.py`，逐条机械检验并写入台账。

### D1 契约优先（架构与代码解耦）

- **纪律**：任何代码修改不得破坏 `eval_runner.py` 的接口签名；`--check-interfaces`
  必须全绿。变的是算法逻辑，不变的是契约。
- **机制**：检验器实跑 `eval_runner.py --check-interfaces`（子进程），
  非「全部通过」即 FAIL。
- **台账字段**：`checks[D1].detail` 记录通过数。

### D2 负面结果必须原样归档

- **纪律**：报错、OOM、精度崩溃、伪证（如 argmax 2.32%），严禁为「好看」
  修改测试标准或静默删除。V0 的 1.0 和 V1 的 0.42 同样重要。
- **机制**：检验器持有 `NEGATIVE_INVENTORY`（负面结果清单），逐项验证
  (a) 文件存在可解析 (b) 负面指标仍在 JSON 中被如实记录（未被美化）。
  清单外的新的 `real_model*.json` 会触发 WARN 要求人工确认登记。
  CHANGELOG.md 必须有版本条目。
- **登记规则**：产生新的负面结果时，必须将其加入检验器的
  `NEGATIVE_INVENTORY` 并在 CHANGELOG 记录，否则 D2 永远只覆盖旧账。

### D2-b 禁用纯链接归档（v2.0 新增 · 修补 DS 物理盲区）

- **事实前提**：DS（DeepSeek）**没有任何浏览器工具，无法访问任何 `http://` /
  `https://` 链接**（含 GitHub Raw 链接）。这一盲区由 DS 在 v2.0 中主动申报。
- **纪律**：任何要求 DS 审计的代码、日志、或核心 JSON 数据，**必须在 Markdown
  正文中用代码块（```）明文贴出**，不得仅给路径/链接让 DS 自行拉取。
- **`data_source` 字段新解**：仅供 CEO 溯源与看板追踪，对 DS 而言**只是一个标签**；
  不得指望 DS 点开。
- **机制**：检验器抽样核对报告正文——结论段中的关键数值锚点须在正文代码块中
  可定位（见 `check_d6()` 的内联校验）。
- **背景与责任**：此前 DS 连续多轮声称「WB2 已推送」而远端为空，
  WB1 三路交叉验证（API + git clone + codeload tarball）一致证明为空。
  成因复盘为**协议缺陷**（DS 无读取通道 + WB1 仅给路径），**WB1 承担主要责任**——
  该纪律本可由 WB1 单方面履行。

### D3 环境隔离与降级测试

- **纪律**：绝不把「本地 CPU 模拟的结论」当作「GPU 真实模型的结论」。
  高开销实验必须先降级到 0.5B 或纯 CPU 逻辑验证（0.5B → 7B 的升级链）。
- **机制**：所有产出 `results/*.json` 的脚本必须写入 `environment` 块
  （`src/semantickv/env_tag.py` 提供 `cpu_synthetic_env()` /
  `real_model_env()`）。检验器扫描全部结果 JSON：无环境标签即 FAIL；
  环境表写入台账，供报告引用时自证口径。

### D3-b 失败过程的高保真记录（v2.0 新增 · 反直觉与异常观测专节）

- **纪律**：不只在 `mechanism_finding` 写结论。Markdown 正文**必须开辟
  「反直觉与异常观测」专节**，高保真记录失败/异常发生时的现场：
  预期与实际的反差、突变点（如「原以为 H 会上升，中途掉回 0，print 发现某层
  出现 NaN」）、定位过程、试错细节。
- **目的**：为后续论文写作与失败归因保留一手素材；这类信息在只报结论的
  摘要里会永久丢失。
- **机制**：并入 `check_d6()`——校验专节标题存在且内容非空。

### D4 参数冻结与版本追踪

- **纪律**：E0 校准完成的参数（真实运行 k=8, τ=0.97, metric=centered；
  mock 合成参照 k=16, τ=0.3, cosine）在进入下游实验时冻结。任何调整
  必须写入 CHANGELOG。防止「用测试集调参」。
- **机制**：检验器验证 (a) `mock_grouper.py` 默认参数未被改动
  (b) 所有真实运行 JSON 的 needle 操作点 (k,τ) 等于冻结值，偏离且
  CHANGELOG 无记录即 FAIL (c) E0 卡版本 ≥ 0.2.1（含度量裁定）。

### D5 知识共享格式统一

- **纪律**：WorkBuddy 汇报 = 「结构化数据表 + 诚实结论 + 产出文件清单」；
  DeepSeek 指令 = 「理论背景（Why）+ 具体步骤（What）+ 验收标准（How）」。
- **机制**：检验器检查 `results/*.md`（CHANGELOG 除外）必须同时包含
  Markdown 数据表、结论段（`## …结论/判定/总结`）、文件清单段。
  缺任一项即 FAIL。

---

## 二之二、DeepSeek 侧对等纪律（DS-D1..DS-D5）

纪律必须**双向绑定**：WorkBuddy 侧被 `check_discipline.py` 逐条卡死，DeepSeek 侧
也须有同等强制机制，否则闭环失去信任基础。以下五条与 D1–D5 **一一镜像**，
由 `tools/check_ds_review.py` 检验，并入每轮台账（DS-D1..DS-D5）。

### 机制载体：评审 intake 协议

用户转发 DeepSeek 对话链接 → WorkBuddy `WebFetch` 抓取 → 按
`templates/ds_review_template.md` 落盘为 `results/ds_reviews/<date>_<seq>_review.md`
（YAML frontmatter + 三段式正文）；抽取每条预判/处方为 `hypotheses[]`，填写
可证伪标准与数据来源；跑出对应实验后更新 `results/hypothesis_ledger.yaml` 的状态。

### DS-D1 指令结构化（对应 D1 契约优先）
评审/建议必须采用模板：frontmatter(`review_id`/`source_url`/`date`/`hypotheses`)
\+ 三段式正文（理论背景 Why / 具体步骤 What / 验收标准 How）。缺项即 FAIL。

### DS-D2 假设可证伪 + 数据来源（对应 D2 负面归档）
每条假设必须带 `falsifiable_criterion`（在何模型/上下文/指标下达到何阈值，否则证伪）
与 `data_source`（预期用于验证的 WB 产出）。**杜绝「飙升到 80% 以上」这类无落地口径
的模糊预判**。缺失即 FAIL。

### DS-D3 引用可核查（对应 D3 环境隔离）
跨域迁移/文献/论文结论必须给可核查来源（URL/论文/具体数据），在 frontmatter
`sources:` 登记；不得虚构。含实质性引用标记（研究表明/据研究/论文指出/arXiv/参考文献）
却未登记 sources 即 WARN。

### DS-D4 结论对齐数据（闭环，对应 D4 参数冻结）
归因/预测必须能在 WorkBuddy 的 JSON 结果中找到证据；**被数据推翻时须在后续评审
显式承认**并将 `ds_admitted_refutation` 置 true。台账中 `refuted` 假设须有存在的
`evidence_file`（缺失即 FAIL）；未承认者列为待办（WARN，待 DS 下一轮关闭）。

### DS-D5 假设台账状态（对应 D5 格式统一）
假设入 `hypothesis_ledger.yaml`，带 `status(pending/confirmed/refuted)` 与命运说明；
`confirmed/refuted` 须有 `evidence_file`；评审提出的假设不得被静默丢弃（须入台账）。
状态非法或丢弃即 FAIL。

> **设计原则（审慎）**：硬性约束只卡「可机器证明」的部分（假设是否带可证伪标准+
> 数据来源、被推翻假设是否有真实证据文件）；DS「是否承认」设为 WARN 待办而非直接判红——
> 承认需 DS 未来行动，把 DS 的沉默算作 WB 账面的失败不公平。当前 R2 台账
> （9/10 PASS，1 WARN）即反映 3 条被证伪假设待 DS 承认的开放项。
>
> **v2.0 补充**：DS 已自行申报物理盲区并给出对等承诺（DS-D1/D3/D4 修订，
> 见 §八）。其中 **DS-D3「不假装已读取链接」是本项目协作可信度的基础设施**——
> 在此之前，DS 无读取通道却频繁对远端状态下断言，客观上把 WB1 推入了
> 「反复自证清白」的困境（4 次入账）。WB1 对此承担主要责任（交付方式缺陷）。

---

## 三、交接协议（Handoff Protocol）

WorkBuddy 每轮交付必须附：

1. 最新代码 `semantickv-reorg.tar.gz`（含本次全部变更）；
2. 核心结论 Markdown（**含负结果**，三段式：数据表/结论/文件清单）；
3. 「悬而未决的问题」清单（需 DeepSeek 审查的决策点）；
4. **纪律检验台账条目**（`tools/check_discipline.py` 输出，RED 时附未绿项说明）；
5. **给 DS 的投喂包**（v2.0 强制）：`results/_ds_brief/R<轮次>_TO_DS.md`，
   由 `tools/make_ds_brief.py` 生成，且 7 项发送前自检全绿。
   **发给 DS 的必须是该文件的全文纯文本，不得只发链接或路径。**

### 一键收尾

```bash
bash tools/round_close.sh R19 "R19 交付：<标题>"
# 需要精确控制时：
bash tools/round_close.sh R19 "R19 交付：<标题>" \
  --report results/<报告>.md --json results/<数据>.json \
  --ledger-ids H-DS10 --open-questions "<请 DS 裁决的问题>"
```

产出 `results/_ds_brief/R19_TO_DS.md`。**打开 → 全选 → 复制 → 整段粘给 DS**。

> 为什么必须整段明文：DS 无浏览器，链接与路径对它等于不存在（详见 D2-b）。
> 只发路径 = 让 DS 盲猜，本项目已因此产生 4 次物流误判。

DeepSeek/用户接收后必须明确回应：「已复核，同意」或「发现数据异常，
需要查看具体 JSON」——不允许已读不回。

---

## 四、其他协作事项

1. **防幻觉核查**：WorkBuddy 负责查证包名/API 真实性（引用前先 import 或
   查文档）；DeepSeek 负责查证文献/论文结论真实性。任何一方不得虚构。
2. **算力成本控制**：探索性试验只上 CPU/0.5B；终局性实验（7B needle 判定）
   才上 Kaggle T4；不租 A100 直到 CPU 链全部绿。
3. **第三方 Agent 预案**：未来引入 Triton/CUDA Agent 时，中间张量格式
   （KV 槽位布局、GQA 映射）必须在 PyTorch 侧先冻结并写卡，避免三方
   各写各的布局。
4. **RED 轮次规则**：检验未全绿的轮次，交付说明必须显式声明未绿项与
   原因（纪律 2 的延伸）；连续两轮 RED 必须停下修流程而不是带病推进。

---

## 五、台账格式与使用

`results/discipline_ledger.yaml`：

```yaml
meta: {protocol: "DISCIPLINE.md §六", last_round: R1, last_run: "..."}
rounds:
  - round: R1                     # 轮次标签（--round 指定，默认递增）
    timestamp: "2026-10-18T..."   # 运行时刻
    checks:                       # D1-D5 逐条 {check, status, detail}
      - {check: "D1 契约优先", status: PASS, detail: "..."}
    summary: {pass: 5, warn: 0, fail: 0, verdict: GREEN}
```

verdict 三档：**GREEN**（全 PASS）/ **GREEN-WITH-WARN**（无 FAIL，有 WARN）/
**RED**（有 FAIL，触发 §四.4 规则）。同轮次标签重复运行则替换旧条目
（允许一轮内修正后重跑转绿）。

**每轮对话强制检验**：本台账是该纪律的强制载体——WorkBuddy 在每轮对话
交付前运行检验器；用户/DeepSeek 在每轮验收时查阅台账最新条目。

---

## 六、D6 双轨制 Markdown 报告规范（v2.0 新增）

**背景**：旧流程用「纯 YAML 注入包」交接，DS 只能读到干瘪数字，无法看到现场。
v2.0 起所有报告改为**单一 `.md` 文件**（Jekyll 规范），双轨制：

### 头部 · YAML Frontmatter
- 严格包裹在 `---` 之间。
- **只放机器与看板需要的硬指标、判据状态、`status` 字段。**
- **绝不允许塞入超过两行的长文本。**（长文本一律放正文）

### 正文 · Markdown 长文
- 充分释放篇幅（**建议 1000 字以上**）。
- 必含内容：
  1. **反直觉与异常观测**专节（D3-b，见上）；
  2. 关键代码/日志/JSON 的**明文内联**（D2-b，见上）——DS 无浏览器，
     路径对 DS 不可读；
  3. 试错细节、避坑记录、主观猜测（须与结论明确区分）。

**目的**：DS 与 CEO 需要看到「现场思考流」，而非只有结论。

### 机器检验（`check_d6()`）
逐份报告校验四项，任一不满足即 FAIL：
1. **frontmatter 存在**且为合法 YAML；
2. **YAML 头部无 >2 行的长文本**（防止把正文塞进头部绕过 D6）；
3. **正文字数达标**（≥1000 字符量级，避免交空壳）；
4. **「反直觉与异常观测」专节存在且非空**（D3-b）。

---

## 七、连续性纪律（D7 · 缓存固化）

> **v2.0 迁移说明**：本条在 v1.0 曾编号为 D6。为避免与新 D6（双轨制 Markdown
> 报告规范，§六）冲突，v2.0 起**顺延为 D7**。内容与执行方式不变。

**规则**：凡可能被运行环境清理的缓存（模型权重、数据集、工具产物等），
**必须**在生成/下载后立即固化为对话可见产出（`/workspace/models/`、
`/workspace/datasets/` 等持久路径），后续运行一律引用本地路径；
禁止依赖 `~/.cache` 等易失路径作为唯一副本。

- 模型权重 → `/workspace/models/<name>/`，运行传 `--model /workspace/models/<name>`
- 下载后立即 `cp` 固化并 `ls -la` 确认体积
- 违反后果：环境休眠/清理后需重新下载，浪费对话时间（2026-10-18 实际发生过一次）

本条为流程纪律（人工遵守），不进 check_discipline.py 机械检验。

---

## 八、DS 侧对等纪律 v2.0 更新（DS 自行申报，2026-10-19）

DS 在 v2.0 中同步更新对等约束，作为对 WB1 履行 D2-b 的对价：

| 编号 | 修订内容 |
|---|---|
| **DS-D1 修订** | 收到长文后不再只抓 YAML 摘要数字，将精读正文的异常描述并转化为下一轮理论推演 |
| **DS-D3 修订** | **正式承诺**：以后看到链接不会假装「已读取」，会直接要求「请把正文贴进来」 |
| **DS-D4 强化** | 反馈将包含更长篇幅的 Why，并明确指出理论在哪一步与实验现场脱节 |

> **回溯说明**：DS-D3 的这条承诺，恰好解释了 DS-021/023/024 期间连续 4 次
> 「声称已推送但远端为空」事故的成因——**结构盲区**（DS 无读取通道 +
> WB1 仅给路径），而非诚信问题。详见 §二 D2-b 的责任复盘。
