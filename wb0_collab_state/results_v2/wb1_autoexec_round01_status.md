# WB1 指令驱动自动执行器 · 首轮闭环状态（Round AUTOEXEC-01）

- 执行者：WB1 自动执行器（每 10 分钟轮询回路）
- 时间：2026-10-21 09:47 (+08:00)
- Hub：https://aa27d73086593e6b6.app.workbuddy.host （sharecode=V6GYQIUI7Z）
- 本轮处理指令：`1792546412-615e31`(WB1)、`1792536556-9b1e19`(ALL)、`1792516372-3aefaf`(ALL)
- 未触碰：`1792546413-22aebd`(WB2)、`1792537260-084d34`(WB2)、`1792537137-dcacb3`(WB2)、
  `1792542991-b248b7`(WB0)、`1792542344-3705a9`(WB0) —— 非 WB1/ALL，防自处理循环

---

## 1. 执行器部署状态：已上线

| 项 | 实测 |
|---|---|
| `GET /api/instructions` | HTTP 200，返回 8 条指令 |
| 筛选 target∈{WB1,ALL} | 命中 3 条 |
| 本轮自身 | 即该执行器的运行实例（部署成立的直接证据） |
| 复核命令 | `scheduler-api.sh list --keyword "WB1 指令驱动自动执行器"` 在自动化执行环境内被显式禁止（`CODEBUDDY_SESSION_TYPE=automation`），故改用「本轮成功轮询」作为部署证据 |

部署侧为幂等：setup_wb1_autoexec.sh 检测到同名任务已存在即跳过创建，重复执行无副作用。

## 2. RESOURCE-HANDOFF ①②：Hub 实物核验通过

| 声明产物 | Hub relpath | 实测大小 | 实测 sha256(前16) | 状态 |
|---|---|---|---|---|
| 3B 权重指纹 | `results_v2/wb1_3b_weights_fingerprint.json` | 1998 B | `0cea807f5b14b828` | 在位、可下载 |
| GSC provenance | `results_v2/wb1_gsc_label_provenance.md` | 5222 B | `2cb2b2240089cec2` | 在位、可下载 |

指纹内容自洽：9 个文件逐项 sha256、合计 6,183,451,098 B（5.8G）、
combined_sha256=`19ad70985ac10efbe013ffecb5d05c5d9f742d402ceb9590e24de2281c65adfe`，
`status=present_and_in_active_use`。
Provenance 文档含 5 处代码级引用（v41_torch_native_runner.py:418/756-762/957/958/978），
结论为「标签与 Router 输入同源 ⟹ 循环论证指控成立 ⟹ 代理 A/B 不得升级为真值，须切 e2e」。

> 上述为「Hub 上实物存在且内容自洽」的核验，**不等于**本执行器沙箱内存在该 5.8G 权重。

## 3. RESOURCE-HANDOFF ③：LongBench 多跳子集 —— Hub 上查无实物（不得标 PASS）

先前 WB1 的 open_item（`1792542991-b248b7`）声称已推 Hub：
`longbench/2wikimqa.jsonl`、`longbench/samples_len4096_n5.jsonl`。

**核验结果：`GET /api/files` 全量 17 条清单中，没有任何 relpath 以 `longbench/` 开头的条目。**
按纪律 DS-D4（实物未到不标 PASS），该声称当前不可核验，判为缺实物，已转 open_item。
WB1 不重推（本地无此文件，重推即伪造）。

## 4. 本执行器沙箱的资源实况（关键差异）

执行器所在沙箱 `/workspace` 为空（仅 `.codebuddy`），全盘递归检索结果：

| 检索目标 | 递归 glob `**/basename` | 结果 |
|---|---|---|
| 仓库 `semantickv-reorg` | `**/semantickv*` | 0 命中 |
| 3B 权重 `Qwen2.5-3B-Instruct` | `**/Qwen2.5*`、`**/*.safetensors` | 0 命中 |
| LongBench 子集 | `**/2wikimqa*`、`**/samples_len4096*` | 0 命中 |

**结论：自动执行器的沙箱 ≠ 产出指纹/provenance 的原 WB1 研究沙箱。**
因此本执行器当前**不具备**就地跑 e2e（Router 接入 3B 前向 + 下游 LLM Loss/Argmax）的条件。
此点直接影响 `1792542991-b248b7` 中待 WB0 决议的 A 项（e2e 执行方）与 B 项（5.8G 权重通道）。

> 已吸取 `1792542344-3705a9`（WB2 假阴性）教训：本轮对 relpath 一律做**全盘 + 树内递归**检索，
> 未在 cwd 平铺查找后即判缺失。上述 0 命中是递归检索后的真阴性，非假阴性。

## 5. 纪律合规

- 未抢跑任何未批准研究方向：本轮未启动 e2e、未启动新训练、未进入 LAR 骨架方向。
- 未编造任何数据：所有数值均来自 Hub 实测响应或本地文件系统实测。
- 未覆盖任何冻结件：`semantic_router.pt` 不在本沙箱，无从触碰；本轮新增文件均为新路径。
- 未写台账：`hub_state/done.json` 仅记录已处理指令 id。
- 长任务规范（`1792536556-9b1e19`）：本轮单次调用均为秒级，无小时级长跑，无需落地检查单。
