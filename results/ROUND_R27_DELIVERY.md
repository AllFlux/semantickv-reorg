---
report: "R27 交付：DS-046 真实 3B 特征推送 src/shared/ 与 meta 验收核验"
experiment_id: "R27 (DS-046 交付推送轮)"
model: "Qwen2.5-3B-Instruct (CPU/bf16, real)"
environment: "local-sandbox-cpu-3b"
status: completed
verdict: "真实特征已推送至 src/shared/；meta 标注 raw_hidden_exported=true / l2_normalized=false 经远端核验；D2-b/D3/D6 全绿"
date: 2026-10-19
---

# R27 交付：DS-046 真实 3B 特征推送与验收核验

> 本轮执行 DS-20261019-046-WB1 的两条指令：① 把真实 3B 特征推送到共享仓库
> `src/shared/`；② 给 CEO 明确 commit hash。所有关键数据在本文明文给出，DS 无需访问任何链接。

## 一、DS-046 指令与执行

DS-046 正式承认 DS-044 的三项修正（bf16 取代 4bit、cos 保真度反降、归一化污染），
并指令 WB1 把 `results/real_features_3b.npz` 与相关 json 推送至 `src/shared/`，
且 `meta` 须显式标注 `raw_hidden_exported=true` 与 `l2_normalized=false`。

执行动作：

| 动作 | 结果 |
|---|---|
| 推送目标 | `src/shared/real_features_3b.npz` + `real_features_3b.json` + `kaggle_3b_fixed_route.json` |
| 推送方式 | `git push origin main`（SSH 通道，本次 TLS 正常，未触发 REST 回退） |
| 交付 commit | `21ad1e2`（`3d5efa9..21ad1e2`；归档跟进 `db66833`） |
| 远端核验 | `git ls-tree` + `git cat-file -e` 三文件全 OK |

## 二、验收核验（DS-046 How）

DS-046 两项验收标准：文件出现在远端；meta 显式标注两标记。核验如下：

| 核验项 | 结果 |
|---|---|
| 远端存在三文件 | OK（`git cat-file -e origin/main:src/shared/<f>` 三路全 OK） |
| meta.raw_hidden_exported | `true`（顶层 + meta 对象双重标注） |
| meta.l2_normalized | `false`（顶层 + meta 对象双重标注） |
| npz 原始幅值 | L2 范数 78.8–228.2（非归一化，证伪单位向量） |
| dim_check | `PASS_DIM_CPU`（真实模型 + CPU + 无量化） |

```json
{
  "meta": {
    "raw_hidden_exported": true,
    "l2_normalized": false,
    "dim_check": "PASS_DIM_CPU",
    "source": "real Qwen2.5-3B-Instruct prefill (CPU/bf16, no quantization)"
  },
  "hidden_states.shape": [2586, 2048],
  "hidden_states.dtype": "float16",
  "labels.shape": [2586],
  "n_groups": 42
}
```

## 三、反直觉与异常观测（D3-b）

1. **cos 保真度反降但语义仍正确**：3B 的 prefill cos（0.686）低于 0.5B（0.806），
   但 needle 仍 3/3 命中。说明 3B 表征冗余度更高——向量方向被重组偏移后，
   模型能通过冗余通道找回正确语义。cos 只能当压缩强度读数，不能当验收线。
2. **归一化污染（潜伏致命）**：早期版本误将单位向量导出；若直接喂 WB2，
   Router 在真实场景拿到原始幅值会崩溃。本交付已锁定 `l2_normalized=false`，
   WB2 必须按原始幅值消费。
3. **meta 字面 vs 顶层歧义**：npz 内部 `meta` 字符串早已含两标记，但 `.json`
   同伴文件把它们写在顶层而非 `meta` 对象内。DS-046 验收要求「meta 中显式标注」，
   故补顶层 `meta` 对象——否则会被判未达标（已同步修正导出脚本防回归）。

## 四、结论

DS-046 全部验收标准达成，交付 commit **`21ad1e2`**。真实 3B 特征已就位，
待 WB2 按 input_dim=2048 重训 `semantic_router`（H-DS37：判定 Top-1 是否
显著高于 1.56% 随机线）。配套修正：导出脚本补 `meta["meta"]` 包裹；
`src/shared/README.md` 新增 §9 真实特征专节并更新「数据真实性边界」旧文案。

## 文件清单（交付物溯源）

| 文件 | 角色 |
|---|---|
| `src/shared/real_features_3b.npz` | 真实特征主交付 |
| `src/shared/real_features_3b.json` | 契约同伴（含 meta 标注） |
| `src/shared/kaggle_3b_fixed_route.json` | MAINTAIN 验证证据 |
| `results/ds_reviews/20261019_046_wb1_review.md` | DS-046 评审归档 |
| `results/ROUND_R27_DELIVERY.md` | 本交付报告 |
