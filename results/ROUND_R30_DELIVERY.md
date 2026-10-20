---
title: "R30 交付：DS-052 D-65a export_sha256 唯一真值收敛 + 维持版本冻结待最终权重集成测试"
generated_at: "2026-10-20T09:50:00"
experiment_id: "R30 (DS-052 D-65a 唯一真值收敛轮)"
environment: "local-sandbox-cpu-3b"
report_verdict: "D-65a 收敛：export_sha256 唯一真值、与 sha256 恒等；冻结维持，待 WB2 2048 权重集成测试"
inline_json_count: 1
audience: DeepSeek(无浏览器-只能读明文)
---

> **致 DS**：本文所有关键数据已明文内联，你无需访问任何链接。文末文件路径仅供 CEO 溯源，你可忽略。

# R30 交付：DS-052 D-65a export_sha256 唯一真值收敛

> 本轮执行 DS-20261020-052-WB1 的 D-65a 裁定：把 `real_features_3b.json` 的 `export_sha256` 收敛为**唯一真值**，并维持版本冻结。所有关键数据在本文明文给出，DS 无需访问任何链接。

## 一、DS-052 指令与执行

DS-052 确认上一轮三项机制化验收通过，同时下达 D-65a：`export_sha256` 是唯一真值，WB2 的 `validate_wb2_input.py` 信任并读取它；后续任何重新生成的 npz 必须同步更新该 json。WB1 识别出**真实的命名冲突**——DS-051 要求字段名 `sha256`，DS-052 要求唯一真值 `export_sha256`，二者若不收敛会导致 WB2 校验器拦截。

| 动作 | 落点 | 结果 |
|---|---|---|
| 字段收敛 | `export_real_features.py` 哈希只算一次，镜像写 `export_sha256`(唯一真值)+`sha256`(别名)，`byte_size` 同理 | 四键恒等，杜绝两份真值漂移 |
| 校验器优先读真值 | `validate_wb1_export.py` 的 `read_expected_sha` 改优先 `export_sha256`；`--write` 同步镜像双写 | `--check` 以唯一真值为基准 |
| 同步现有交付件 | 对既有 npz 跑 `--write`（npz 未改，哈希不变） | `export_sha256`/`export_byte_size` 补写入 json |
| 文档固化 | `src/shared/README.md` 新增 §9.2 | 写明唯一真值 + 别名恒等 + 强制同步 |
| 维持冻结 | `version_freeze=true` | 仅做上述机制化修补 |

## 二、验收核验（DS-052 How）

| 核验项 | 结果 |
|---|---|
| `export_sha256` 根 + 内层 meta | 已写入（唯一真值） |
| `export_sha256` == `sha256` 四键恒等 | OK（自证断言通过） |
| 与实际 npz 哈希一致 | `84e37106e7e111e801294c45dc7d3910948d08d104561793a82acc7430e5b261` |
| `--check`（以 export_sha256 为基准） | ✔ SHA256 匹配（exit 0） |
| `--parts` 分卷校验 | ✔ 2 卷逐卷 + 拼整体 + json 交叉核对一致（exit 0） |
| 保真标记 | `raw_hidden_exported=true` / `l2_normalized=false` / `dim_check=PASS_DIM_CPU` |

```text
[write] 已写入 meta.export_sha256(唯一真值) / sha256 / byte_size / validated_at
[check] ✔ SHA256 匹配，文件完整，可安全加载。
[parts] ✔ 2 分卷逐卷校验通过，拼接完整 （sha256 匹配，8215605 bytes）
[parts] ✔ 与 json meta.sha256 交叉核对一致

字段恒等自证：
  export_sha256 == sha256 == meta.export_sha256 == meta.sha256
  = 84e37106e7e111e801294c45dc7d3910948d08d104561793a82acc7430e5b261
  export_byte_size == byte_size == 8215605
```

## 三、反直觉与异常观测（D3-b）

1. **DS-051 与 DS-052 字段名冲突是上游两次指令打架**：机械二选一会破坏另一方校验链。解法是「一次计算、双键镜像、值恒等」——`export_sha256`（唯一真值）与 `sha256`（DS-051 别名）由同一 `hashlib.sha256()` 一次计算后写入，从机制上杜绝两份哈希漂移。
2. **「唯一真值」≠「只准一个键」**：D-65a 的核心是「只有一个权威哈希值」，而非 JSON 只能有一个键。两键指向同一计算值仍是单一真值；反之让两键各自独立计算才真正违背唯一真值。
3. **集成测试仍缺最后拼图**：`git fetch` 确认远端 `origin/main` 仍停在 `5836772`，`src/shared/semantic_router.pt` 仍是旧版，WB2 尚未 push **input_dim=2048** 最终权重。故本轮不跑端到端集成测试，只把交付链路（导出 + 校验 + 分卷 + 唯一真值）彻底闭合待命。

## 四、结论

D-65a 命名冲突已收敛：`export_sha256` 为唯一真值、与 `sha256` 别名恒等、四键镜像；`validate --check` 改以 `export_sha256` 为基准；现有交付件已同步。`version_freeze=true` 维持。**端到端集成测试的前置条件（WB2 的 2048 版最终 Router 权重）尚未到位**，待 WB2 push 后即可按 DS-052 §三 跑一次「挂载最终权重 → 确认 Argmax 及格 → 正式归档」。

## 文件清单（交付物溯源）

| 文件 | 角色 |
|---|---|
| `src/semantickv/export_real_features.py` | 导出脚本（export_sha256 唯一真值 + 别名镜像） |
| `tools/validate_wb1_export.py` | 校验脚本（优先读 export_sha256） |
| `src/shared/real_features_3b.json` | 契约同伴（export_sha256/sha256/byte_size + 保真标记） |
| `src/shared/README.md` | 新增 §9.2 唯一真值说明 |
| `results/ds_reviews/20261020_052_wb1_review.md` | DS-052 评审归档 |
| `results/ROUND_R30_DELIVERY.md` | 本交付报告 |
