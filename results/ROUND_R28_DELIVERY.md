---
experiment_id: "R28 (DS-048 导出校验机制化)"
environment: "local-sandbox-cpu-3b"
verdict: "DS-048 两项要求达成：导出自动同步 src/shared/ + validate 写 SHA256/byte_size 入 meta；版本冻结已记录"
status: delivered
---

# R28 交付：DS-048 导出校验机制化（SHA256 + byte_size + 自动同步 src/shared/）

> 本轮执行 DS-20261020-048-WB1 的两条机制化指令 + 版本冻结声明。所有关键数据在本文明文给出，DS 无需访问任何链接。

## 一、DS-048 指令与执行

DS 转达 WB2 反馈：下载 `real_features_3b.npz`（约 8.2MB）时因网络限速断点续传损坏，numpy 报 `Bad magic number`。DS 要求 WB1：

1. 在 `export_real_features.py` 中**默认输出改 `src/shared/` 或导出后自动 cp 一份到 `src/shared/`**；
2. 实现 `tools/validate_wb1_export.py`，生成后算 `SHA256` + 精确 `byte_size` 并写入 `.json` 的 `meta`。

执行动作：

| 动作 | 结果 |
|---|---|
| 导出脚本改造 | `export_real_features.py` 末尾自动 `shutil.copy` npz+json 到 `src/shared/`，并 subprocess 调用校验脚本 `--write` |
| 新建校验脚本 | `tools/validate_wb1_export.py`：魔数快检 + 加载校验 + SHA256 + 精确 byte_size + `--write`/`--check` |
| 现有交付物补校验 | 已对 `src/shared/real_features_3b.npz` 跑 `--write`，meta 写入三字段 |
| 消费侧模拟 | `--check` 重算 SHA256 与 meta 比对，匹配通过 |
| 版本冻结 | `version_freeze=true` 已记入评审归档；除本评审要求改造外暂停无关改动 |

## 二、验收核验（DS-048 How）

| 核验项 | 结果 |
|---|---|
| 文件稳定于 src/shared/ | OK（导出脚本已自动化 cp；历史 commit 21ad1e2 也含该目录） |
| meta.export_sha256 | `0d6db68aa653f437bc9473b2ea34bc2f8ce6b9389a96e60cfd3ae2056b955f40` |
| meta.export_byte_size | `8215353`（精确字节，7.83 MB） |
| meta.validated_at | `2026-10-19T16:43:56Z` |
| --check 比对 | ✔ SHA256 匹配，文件完整，可安全加载 |
| raw_hidden_exported / l2_normalized | `true` / `false`（DS-046 验收仍生效） |

```text
[validate --write]
[magic] real_features_3b.npz 头部=b'PK\x03\x04' -> OK (zip/npz)
[load] 成功 | keys=['labels', 'meta', 'hidden_states'] | hidden_states=(2586, 2048) float16
[sha256] 0d6db68aa653f437bc9473b2ea34bc2f8ce6b9389a96e60cfd3ae2056b955f40
[byte_size] 8215353 (7.83 MB)
[write] 已写入 meta.export_sha256 / export_byte_size / validated_at

[validate --check]（模拟 WB2 下载后）
[check] ✔ SHA256 匹配，文件完整，可安全加载。
```

## 三、反直觉与异常观测（D3-b）

1. **「8.2MB」实为 7.83MB**：DS-048 转述 WB2 称文件 8.2MB，但精确 `byte_size=8215353 bytes`（7.83 MB）。差异仅为四舍五入；**完整性必须以 SHA256 为基准，而非体积近似值**——体积吻合但字节被截断同样会 Bad magic number。
2. **断点续传损坏的根因是 zip 局部损坏**：npz 本质是 zip 归档（魔数 `PK\x03\x04`）。限速中断后文件尾部缺字节，zip 中央目录或局部头错位 → numpy 报 Bad magic number。仅靠「文件大小接近」无法发现，必须 SHA256 整文件哈希比对。
3. **校验脚本一度误报维度不一致**：初版读 `meta.hidden_size`（meta 对象内无该字段，它在 JSON 顶层），误判 0≠2048。修正为读 `j["hidden_size"]` 后 WARN 消除——提醒契约字段层级要核对（DS-046 要求 meta 对象内标注，但 `hidden_size` 这类结构字段仍在顶层）。
4. **版本冻结的边界**：DS-048 要求「暂停任何代码改动」，但本评审的改造本身就是在冻结生效前必须完成的收尾。二者不矛盾——冻结针对的是冻结时点之后的新功能，而非本评审已下达的待办。

## 四、结论

DS-048 全部要求达成，commit 待推送。真实 3B 特征交付链路现具备：
- **稳定交付目录**（`src/shared/`，导出自动同步）；
- **完整性指纹**（SHA256 + 精确 byte_size 入 meta），WB2 下载后 `--check` 即可识别损坏，杜绝 Bad magic number 误用。

`version_freeze=true` 已记录；待 WB2 回传最终权重（input_dim=2048 版 `semantic_router.pt`）并集成测试后，暂停其他代码改动。

## 文件清单（交付物溯源）

| 文件 | 角色 |
|---|---|
| `src/semantickv/export_real_features.py` | 导出脚本（新增自动 cp + 自动校验调用） |
| `tools/validate_wb1_export.py` | 新建：导出校验（SHA256 + byte_size + 魔数快检） |
| `src/shared/real_features_3b.npz` | 真实特征主交付（byte_size 8215353） |
| `src/shared/real_features_3b.json` | 契约同伴（meta 含 export_sha256/byte_size） |
| `results/ds_reviews/20261020_048_wb1_review.md` | DS-048 评审归档 |
| `results/ROUND_R28_DELIVERY.md` | 本交付报告 |
