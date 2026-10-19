---
title: "R29 交付：DS-051 路径固化 + 内联哈希 + 大文件分卷机制化（version_freeze）"
round: R29
environment: local-sandbox-cpu-3b
verdict: "DS-051 三项强制机制全部落地：输出强制 src/shared/、导出内联 hashlib.sha256 写 meta、>5MB 自动分卷；交付链路闭合，version_freeze 维持"
date: "2026-10-20"
status: accepted
---

# R29 交付：DS-051 路径固化 + 内联哈希 + 大文件分卷机制化

> 本轮执行 DS-20261020-051-WB1 的三条强制机制化指令 + 延续版本冻结。所有关键数据在本文明文给出，DS 无需访问任何链接。

## 一、DS-051 指令与执行

DS 转达 WB2 的跨端传输事故：经 CDN 下载 8.2MB 的 `real_features_3b.npz` 时因限速/掐断损坏（`Bad magic number`）。DS 要求把"大文件传输可靠性"从人工重试升级为机器机制，并收紧 DS-048 的哈希要求。WB1 执行：

| 机制 | 实现落点 | 结果 |
|---|---|---|
| 路径固化 | `export_real_features.py` 缺省/显式 `--out` 均强制重定向 `src/shared/`；断言 `out.parent == ROOT/"shared"`，否则 `[FATAL]` 拒产 | 交付件只落 `src/shared/`；`results/` 重复件已 `git rm` |
| 内联哈希 | 生成 `.npz` 后**在导出脚本内**用 `hashlib.sha256()` 直接算，写 `meta.sha256` + `meta.byte_size`（根 meta 与内层 `meta["meta"]` 双写） | 不再依赖子进程；字段名按 DS-051 为 `sha256`/`byte_size` |
| 大文件分卷 | `>5MB` 自动按 4MB/份 分卷为 `.part1/.part2` + `.parts.json`（含合并与各卷 sha256、重装说明） | 单卷 <5MB 规避 CDN 截断 |

## 二、验收核验（DS-051 How）

重跑导出（本地 3B 模型、CPU/bf16、`--environment local-sandbox-cpu-3b`）后核验：

| 核验项 | 结果 |
|---|---|
| 输出落点 | `src/shared/real_features_3b.npz`（修复了早期 `ROOT` 解析错位导致的 `src/src/shared` 双目录 bug） |
| `dim_check` | `PASS_DIM_CPU`（可交付 WB2；修复了漏传 `--environment` 导致的 `PROXY_DIM_OK` 非交付级） |
| `meta.sha256` | `84e37106e7e111e801294c45dc7d3910948d08d104561793a82acc7430e5b261`（根 + 内层一致） |
| `meta.byte_size` | `8215605`（7.84 MB，精确字节） |
| 单文件 `--check` | ✔ SHA256 匹配，可安全加载（exit 0） |
| 分卷 `--parts` | ✔ 2 卷逐卷 + 拼整体校验通过，与 json meta 交叉核对一致（exit 0） |
| 还原 `--assemble` | ✔ 拼接还原文件 sha 与原文件**完全一致**（exit 0） |
| `raw_hidden_exported` / `l2_normalized` | `true` / `false`（DS-046 验收仍生效） |

```text
[done] src/shared/real_features_3b.npz  (7.8 MB)
       env=local-sandbox-cpu-3b   dim_check=PASS_DIM_CPU
       [sha256] 84e37106e7e111e801294c45dc7d3910948d08d104561793a82acc7430e5b261
       [byte_size] 8215605 (7.84 MB)
       ✔ 可交付 WB2：真实模型前向的真实隐状态 + 真实 GSC 标签。
[chunk] real_features_3b.npz > 5MB，已分卷为 2 份 + manifest：
         real_features_3b.npz.part1  4194304 bytes  sha256=...
         real_features_3b.npz.part2  4021301 bytes  sha256=...
         manifest: real_features_3b.npz.parts.json
```

## 三、反直觉与异常观测（D3-b）

1. **"8.2MB"实为 7.84MB**：DS 口述 8.2MB 是四舍五入；精确 `byte_size=8215605`（7.84 MB）。完整性必须以 **SHA256** 为基准，体积近似值不能替代——体积吻合但尾部截断同样会 `Bad magic number`。
2. **分卷反而更稳**：单文件 7.84MB 在 CDN 上被掐断即全损；分 2 卷（各 ≤4MB）后，即使某卷重传也只需补该卷，且每卷 sha256 独立可验，拼整体再验一次，损坏可定位到具体卷。
3. **两处导出期 bug 被本轮暴露并修复**：① `ROOT` 解析为 `repo/src`，导致 `ROOT/"src"/"shared"` 错成 `src/src/shared`（双目录）；改为 `ROOT/"shared"`。② 漏传 `--environment` 使 `dim_check=PROXY_DIM_OK`（脚本判"不可作 WB2 交付物"）；补 `--environment local-sandbox-cpu-3b` 后恢复 `PASS_DIM_CPU`。说明**重跑导出是必要的真实验证**，而非走过场。
4. **分卷与单文件并存**：`src/shared/` 同时保留原 npz（供 git 克隆者 intact 获取）与分卷（供 CDN 下载者）。manifest 提供合并 sha256，二者可互证。

## 四、结论

DS-051 全部强制要求达成，交付链路闭合：`export_real_features.py` 强制落 `src/shared/` + 内联 `hashlib.sha256` 写 `meta` + `>5MB` 自动分卷；`validate_wb1_export.py` 提供 `--check`/`--parts`/`--assemble` 三模式防损坏。`version_freeze=true` 维持，仅做上述机制修补。

待 WB2 用 `real_features_3b.npz`（input_dim=2048）重训并回传最终 `semantic_router.pt`，集成测试后按冻结声明暂停其他改动。

## 文件清单（交付物溯源）

| 文件 | 角色 |
|---|---|
| `src/semantickv/export_real_features.py` | 导出脚本（路径固化 + 内联哈希 + 自动分卷） |
| `tools/validate_wb1_export.py` | 校验脚本（SHA256 + 魔数快检 + 分卷校验/还原） |
| `src/shared/real_features_3b.npz` | 真实特征主交付（8215605 bytes） |
| `src/shared/real_features_3b.json` | 契约同伴（meta 含 sha256/byte_size/保真标记） |
| `src/shared/real_features_3b.npz.part1` / `.part2` | 分卷（各 ≤4MB，CDN 友好） |
| `src/shared/real_features_3b.npz.parts.json` | 分卷 manifest（合并 + 各卷 sha256） |
| `results/ds_reviews/20261020_051_wb1_review.md` | DS-051 评审归档 |
| `results/ROUND_R29_DELIVERY.md` | 本交付报告 |
