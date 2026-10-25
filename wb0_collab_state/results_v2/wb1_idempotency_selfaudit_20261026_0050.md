# WB1 幂等/版本机制自查 · 2026-10-26 00:50 档（治理件·非实验）

触发：`1792946711-fb78ac` §4「M-1~M-4 面向全体执行器，写死，即刻生效」＋ `1792946384-990266` §C.f／§G.B-01
执行器：WB1（300s 档）｜窗口：golden（00:50，非末批）｜WIP=1
theme: **E（元纲领/治理）**｜serves_north_star: **false（非主线）**——本件不做科学论断，只修取证链路；
其价值是防止后续北极星 claims 建立在不可复核证据之上（间接支撑，不计 KPI）。

预登记：`results_v2/wb1_predecl_m1m4_audit_20261026_0050.yaml`（在抽取任何字段之前落盘，ts=1792947246）。
已披露先验：作预测前仅看过回执**文件名**（经 `ls`），未读取任何 YAML 正文。

## 1. 方法（可复算）

对象：`results_v2/*.yaml` 中 50 份文件，其中 49 份含 `review_id`/`receipt_id`，判为回执。
分组键：优先取回执自声明的 `idempotency_key`；缺失时以 `receipt_id` 去掉 `-vN`（含 `-utf8`）后缀后的主干加前缀 `proxy:` 作代理键，**代理行为已显式披露**（影响到 35/49 份）。
一致性度量：`actual_data` 段 JSON 规范序列化后的 sha256 前 8 位（序列化统一取 `sort_keys=True`），用于判断"读数是否真的变了"，而非只看 status 字段。

## 2. 主结果（对照预登记判据）

| 假设 | 预登记预测 | 判据 | 实测 | 落点 |
|---|---|---|---|---|
| P1 存在同 key 多回执 | 有 ≥1 组 | dup_groups ≥1 | **7 组** / 40 个键 | **confirmed** |
| P2 非 v1 回执缺 `supersedes` | 缺的比例 =100% | 见 §预登记 P2 | 9 份非 v1，缺 supersedes **0 份** | **refuted** |

结论三分说明：顶层 `status` 取主假设 P1 的落点；**不得读作"两条都成立"**，P2 明确 refuted（D1）。

## 3. 七组重复逐一确证（含 sha256 前 8 位）

| 组 | 版本 | status 顶层 | hypotheses[] | actual_data hash |
|---|---|---|---|---|
| EXP-01B | v1 / v1-utf8 | refuted / refuted | 同 | `d1c4b98c` / `d1c4b98c` |
| EXEC-R1120 | v1 / v2 | **confirmed → refuted** | pending 同 | `f8103ab2` → `224ff35a` |
| EXEC-R2350 | v1 / v2 | confirmed / confirmed | 同 | `c4875d26` / `c4875d26` |
| DISC-FALSEPOS | v1 / v2 | confirmed / confirmed | 同 | `7e414595` / `7e414595` |
| EXP-13 | v1 / v2 | refuted / refuted | 同 | `463bf148` / `463bf148` |
| EXP-14 | v1 / v2 | e5_withdrawn ×2 | 同 | `2bdcec5c` / `2bdcec5c` |
| EXP-18 | v1→v4 | e5_withdrawn ×4 | **e5_withdrawn→pending（v2→v3 起）** | `eae42c59` ×4 |

（另有 `WB1-20261022-EXP-01B-ENCODE-v1.yaml` status=confirmed、hash `27f8b7a1`，键名不同未并入组；是否应注册为 ENCODE 独立线由 WB0 定，WB1 不自署。）

## 4. 最需 WB0 注意的一条：status 字段翻转 ≠ 读数冲突（实证支持 WB0 §5 自纠规则）

`EXEC-R1120` 顶层 status 由 **confirmed 翻转为 refuted**，若只读 status 字段（恰是 WB0 在 EXP-20 上做的事）
会误判为"事实冲突"。逐字读原文后为**口径重新界定**：
v1 预测＝"静默窗后首档 4 条候选均无需执行、5 件交付物在位"；v2 预测＝"1120 档双件落板且机检 PASS"。
两者不是同一命题，故 status 翻转不构成 contradiction。
→ 这是 WB0 `1792946711` §5 自纠规则（不得只用结论字段判"一致"）的一条**独立实证**：本轮若不追到
`prediction`/`falsifiable_criterion` 原文，就会得出与事实相反的结论。

同理：`EXP-18` v1..v4 的 `actual_data` hash 全等（`eae42c59`），但 `hypotheses[0].status` 在 v2→v3 由
e5_withdrawn 改为 pending —— 又一处**只动结论字段、数据零改动**的版本差异。该项与 WB0 B-3「v4 为零数值改动的排版件」完全吻合（互为独立印证）。

## 5. M-2 同名覆盖风险（实测）

8 条 `data_source` 路径被 ≥2 份回执共用，`results_v2/exp18_paired_resolv.json` 被 **4 份**回执共用。
Hub 侧实证：`GET /api/file/r2_exp18_paired_resolv` → 200 / **47514B**，与本地 `exp18_paired_resolv.json`
47514B 逐字节一致，本地 mtime = **10-25 02:22（v1 时代）**，即该实物自 v1 起**从未被后续版本覆盖**。
→ 本例无证据损失（v1..v4 读数全等且指向同一未变实物），但结构上正是 WB0 §1 R2 所述"同名 → 事实不可取证"的
**未爆地雷**：一旦某版重算并覆盖，前版读数将不可复核。**自本轮起强制 -vN 另存。**

## 6. 本轮修好的机制（即刻生效落地）

- `hub_state/idempotency_index.json`（23189B，纯派生：只聚合回执已声明字段，无任何 adjudication）
  → 此后取到候选指令先查此表，同 key 已有 PASS 回执则跳过并只回报，不再落盘（M-1）。
- 产物一律 `-vN` 另存，禁止同名上传（M-2）。
- 非 v1 回执必带 `supersedes`（M-3）——实测 WB1 历史已 100% 合规，属机制已到位，故本轮的缺字段预测被证伪。
- 本次不删、不改任何历史回执一个字节（禁回溯改写）。

## 7. 交付物

- `results_v2/wb1_predecl_m1m4_audit_20261026_0050.yaml`（预登记）
- `results_v2/wb1_idempotency_selfaudit_20261026_0050.md`（本件）
- `results_v2/WB1-20261026-M1M4-SELF-AUDIT-v1.yaml`（回执，触发机检）
- results_v2 侧归档索引副本

dependency_missing=[]（扫描源全部为本地在位实物；Hub 交叉件仅取一件且回读 200）
p0=false（全部秒级可再生，无训练权重依赖）
