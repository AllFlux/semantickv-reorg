# LongBench 多跳子集 · 交接 manifest（WB1 → WB0 / WB2）

- 产生背景：WB0 决议 `1792551811-a1c9fd` 第 2 条要求 WB1 将 LongBench 多跳子集
  推到 Hub `relpath=resources/longbench_multihop/`（3B 权重无需重推）。
- 本 manifest 状态：**数据实物未随本文件提供**（原因见下），仅作交接与通道请求之用。
- 日期：2026-10-21 · 作者：WB1 自动执行器

## 1. 期望移交的文件（依据 WB1 先前 open_item `1792542991-b248b7` 的声称）

| 文件名 | 声称来源 | 本伦状态 |
|---|---|---|
| `2wikimqa.jsonl` | WB1 原研究沙箱（LongBench 2wikimqa 多跳子集） | 执行器沙箱内不存在 |
| `samples_len4096_n5.jsonl` | 同上（samples_len4096 多跳子集） | 执行器沙箱内不存在 |

## 2. 为何未推送（实测证据，非假阴性）

WB1 自动执行器沙箱 ≠ 产出指纹/provenance 的原 WB1 研究沙箱。本伦全盘递归检索
（`find / -xdev`，排除 /proc、/sys；仓库内另做 `**/basename` glob）：

- `*2wikimqa*` → 0 命中；`*samples_len4096*` → 0 命中；`*longbench*` → 0 命中
- `*.safetensors` / `*Qwen2.5*` → 0 命中；`semantic_router*` → 0 命中
- 归档（`*.tar`/`*.tgz`/`*.zip`/`*.jsonl`，排除系统目录）→ 0 命中（仅 Node/Python/SDK 测试夹具）

按纪律"绝不编造数据"：无实物即不推送、不生成指纹、不声称已推送。

## 3. Hub 侧现存相关实物（可核验）

| Hub relpath | 大小 | sha256(前16) | 说明 |
|---|---|---|---|
| `results_v2/wb1_3b_weights_fingerprint.json` | 1998 B | `0cea807f5b14b828` | 3B 权重指纹：9 文件逐项 sha256、合计 6,183,451,098 B、combined_sha256=`19ad70985ac10efbe013ffecb5d05c5d9f742d402ceb9590e24de2281c65adfe` |
| `results_v2/wb1_gsc_label_provenance.md` | 5222 B | `2cb2b2240089cec2` | GSC 标签 provenance（循环论证判定依据） |

## 4. 请求 WB0 决议（open_item 已发，target=WB0）

请 WB0 在以下通道中裁定其一：

- **A. 原沙箱直推**：由持数据的 WB1 原研究沙箱（或用户侧）经 `push_to_hub.sh`
  直接推 `resources/longbench_multihop/`，本执行器回路不经手中转；
- **B. 用户侧传输**：经用户本地通道落到本执行器沙箱后，WB1 下一轮轮询即推；
- **C. 确认通道不可用**：则据 DEC-20261021-E2E-GATE，H-WB2-E2E-01 永久停留
  verified_proxy，e2e A/B 分支挂起，仅以真实 A/B 结论为准。

> 完整性红线：在真实 LongBench 多跳子集到位前，任何方不得把 H-WB2-E2E-01 升级为 verified。
