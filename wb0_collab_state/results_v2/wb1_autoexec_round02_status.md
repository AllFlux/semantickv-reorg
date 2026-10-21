# WB1 指令驱动自动执行器 · Round AUTOEXEC-02 状态（2026-10-21）

- 执行者：WB1 自动执行器（每 10 分钟轮询回路）
- Hub：https://aa27d73086593e6b6.app.workbuddy.host （sharecode=V6GYQIUI7Z）
- 本轮处理指令：`1792551811-a1c9fd`(WB0 决议·ALL)、`1792552522-bd26f4`(E2E-GATE·ALL)
- 未触碰：`1792550341-89f733`(WB0)、`1792549615-5f9aca`(WB0)、`1792542991-b248b7`(WB0)、
  `1792542344-3705a9`(WB0)、`1792549481-940e4c`(WB2)、`1792547352-bac606`(WB2)、
  `1792546413-22aebd`(WB2)、`1792537137-dcacb3`(WB2) —— 非 WB1/ALL，防自处理循环
  （含 WB1 自己发出的 open_item，一律不回收）

---

## 1. DEC-20261021-E2E-GATE 确认生效（指令 1792552522-bd26f4）

已确认收悉并作为不可绕过硬约束遵守：

1. **H-WB2-E2E-01 维持 verified_proxy**，WB1 不以任何理由（包括"已有 PASS 回执"）将其标 verified。
2. 升级唯一通道：WB2 用 WB1 侧提供的 LongBench 多跳子集跑出**真实 LLM-LongBench A/B**
   （60 实体/40 查询，PR@5 + 活跃群数对比）后方可改判。
3. 科学依据对齐台账 H-DS-CIRC-01 精确裁定：纯循环指控不成立（target_layer=12 跨层映射），
   但"端到端任务 Loss 才是正确方向"成立 ⟹ proxy ≠ 真值。

## 2. WB0 决议执行（指令 1792551811-a1c9fd）

### 2.1 LongBench 多跳子集补推（决议第 2 条）→ 受阻，按第 6 条转 open_item

决议要求 WB1 将 LongBench 多跳子集推到 Hub `relpath=resources/longbench_multihop/`。
**实测：本执行器沙箱内不存在该数据，无法推送，亦拒绝伪造。**

全盘递归检索证据（`find / -xdev`，排除 /proc /sys）：

| 检索目标 | 模式 | 命中 |
|---|---|---|
| 2wikimqa 子集 | `-iname "*2wikimqa*"` | 0 |
| samples_len4096 子集 | `-iname "*samples_len4096*"` | 0 |
| 任意 longbench 路径 | `-iname "*longbench*"` | 0 |
| 归档中可能内嵌数据 | `*.tar/*.tgz/*.zip/*.jsonl`（排除系统目录） | 0（仅 Node/Python/SDK 测试夹具） |
| 3B 权重 | `*.safetensors` / `*Qwen2.5*` | 0 |
| Router 冻结件 | `semantic_router*` | 0 |

Hub 侧 `GET /api/files` 全量清单（23 条）亦无任何 `longbench` / `resources/longbench_multihop` 条目；
`resources/3b_weights/` 同样不在清单中（仅有指纹文件 `results_v2/wb1_3b_weights_fingerprint.json`）。

> 注：上轮（AUTOEXEC-01）已得出同一结论；本伦复测确认，非假阴性（检索为全盘递归，非 cwd 平铺）。

**处置**（决议第 6 条）：不伪造、不绕过完整性；改为
① 推送交接 manifest（`results_v2/wb1_longbench_multihop_manifest.md`，不含任何编造指纹）；
② 经 `/api/instruction` 发 open_item 给 WB0（target=WB0），请 WB0 决议传输通道。

### 2.2 AUTOEXEC01-v1/v2 清理（决议第 4 条）→ 本地已完成，Hub 侧需 WB0 协助

- 本地：`results_v2/WB1-20261021-AUTOEXEC01-v1.yaml`、`-v2.yaml` 已删除，`-v3.yaml` 保留。
- Hub 侧：v1/v2 的回执记录存于 Hub 机检库，WB1 无删除能力
  （`DELETE /api/review` → HTTP 405 Method Not Allowed；MVP 无删除端点）。
  已在 open_item 中提请 WB0 侧移除该两条 FAIL 记录，以解除监督器 DEGRADED / VERIFY-REVIEW-FAIL。

### 2.3 e2e 执行方 = WB2（决议第 1 条）→ WB1 不抢跑

WB1 执行器沙盒不具备就地跑 e2e 的条件（见 2.1 检索表），本伦不启动任何 e2e / 新训练 /
LAR 骨架方向，等 WB2 从 Hub 拉齐输入后执行。

## 3. 纪律合规

- 未抢跑未批准研究方向（未启动 e2e / 新训练 / LAR 骨架方向）。
- 未编造数据（所有数值来自 Hub 实测响应与本地文件系统实测；manifest 不含无实物对应的指纹）。
- 未覆盖冻结件（semantic_router.pt 不在本沙箱；删除的 v1/v2 为 WB1 自己的测试回执，非冻结件；
  其余新增文件均为新路径）。
- 未写台账（hub_state/done.json 仅记录已处理指令 id）。
- 未触碰 target=WB0/WB2 指令（含 WB1 自产 open_item），防自处理循环。
