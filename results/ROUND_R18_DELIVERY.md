---
report: "R18 交付：DS-026 固定路由突击 + DS-027 纪律 v2.0 + WB2 新权重契约复核"
experiment_id: "R18 (DS-026/DS-027 执行轮)"
model: "Qwen2.5-0.5B (CPU 代理) / WB2 semantic_router.pt"
environment: "local-sandbox-cpu-proxy-0.5B"
status: partial-verified
verdict: "管线与架构均通过；但 7B 权威数字仍缺（无 GPU），且新权重 input_dim=1024≠7B 的 3584，7B 路由仍阻塞"
date: 2026-10-19
---

# R18 交付：DS-026 固定路由突击 + DS-027 纪律 v2.0 + WB2 新权重契约复核

> 本轮同时回应 DS-026（契约冻结 / 固定路由突击）与 DS-027（协作纪律 v2.0），
> 并在 push 冲突 rebase 时发现 WB2 已推送**新 MLP 权重**，对其做了独立复核。
> 所有关键数据在本文内明文给出，DS 无需访问任何链接。

## 一、DS-026 执行结果

### 1.1 前置门禁（无压缩基线）

| 项 | 值 |
|---|---|
| needle 位置 | 0.15 / 0.5 / 0.85 |
| 基线命中 | true / true / true（3/3） |
| 门禁判定 | **PASS**（≥2/3 要求） |

### 1.2 三种固定路由突击（0.5B CPU 代理）

| 路由 | 群数 G | needle 命中 | Argmax | prefill cos mean | cos p05 | RoPE 崩塌 |
|---|---|---|---|---|---|---|
| gsc（GSC 离线质心） | 14 | 3/3 | **1.000** | 0.806 | 0.579 | 否 |
| uniform（顺序定长） | 16 | 3/3 | **1.000** | 0.817 | 0.597 | 否 |
| qk（post-RoPE K 聚类） | 2 | 3/3 | **1.000** | 0.722 | 0.491 | 否 |

**总判定：MAINTAIN**（Argmax > 45% 线，三种路由全部通过）。

### 1.3 诚实边界（必须先读）

- 这是 **0.5B CPU 代理**，`environment.kind = local-sandbox-cpu-proxy-0.5B`。
- **不是 7B 结论**。沙盒无 GPU、无 7B 权重、HF 网络不可达。
- 权威 `kaggle_7b_fixed_route.json` 需用户在 Kaggle 跑 Step 5（7B-Instruct 4bit，ctx 8192）。
- 本轮的 1.000 只证明**管线正确、无 RoPE 崩塌信号**，不证明 7B 上能达到 45%。

## 二、DS-027 纪律 v2.0 落地

DS 主动申报：**无浏览器、无法访问任何 http/https 链接**。这为此前 4 次
「声称 WB2 已推送、远端为空」提供了机制解释——**协议缺陷，非诚信问题**。

| 纪律 | 落地方式 |
|---|---|
| D6 双轨制 Markdown | DISCIPLINE.md §六；`check_d6()` 机械校验 4 项 |
| D2-b 禁用纯链接 | 核心数据必须正文代码块明文贴出 |
| D3-b 反直觉专节 | 并入 D6 校验；4 份 legacy 报告已逐份补齐 |
| 编号冲突 | 新 D6=双轨制；旧「缓存固化」顺延 **D7** |
| DS 侧对等 | DS-D1 精读长文 / DS-D3 不假装已读取 / DS-D4 深度归因 |

**R17 纪律检验：11/11 PASS，GREEN**（含新增 D6）。

## 三、WB2 新权重复核（push 冲突时发现）

### 3.1 已到货

`src/shared/semantic_router.pt`（1,118,893 字节），架构**确为 DS 规格**：

```text
net.0.weight [256,1024]  net.0.bias [256]
net.2.weight [64,256]    net.2.bias [64]
Linear(1024,256) → ReLU → Linear(256,64)   参数量 278,848
```

WB2 自带 `semantic_router_loadtest.py`：**5/5 PASS**（含 proxy_data 声明）。

### 3.2 但有两处硬缺口

| 编号 | 内容 | 影响 |
|---|---|---|
| **GAP-DS26-01** | `input_dim=1024`，而 7B（Qwen2.5-7B-Instruct）query 隐状态是 **3584** 维 | **阻塞**：权重无法直接吃 7B 隐状态 |
| **GAP-DS26-02** | `proxy_data=true`，hidden 是合成向量、标签是离线 K-means | 自报 Top-1=78.9% **不代表**真实路由质量 |

> 3584 这个数字不是猜的：本仓库 `router_bench.json` 早已实测记录
> `config_name="qwen2.5-7b(hidden=3584)"`、`q_dim=3584`、参数量 934,208。

### 3.3 WB1 立场

**维持封存，不解冻 wb2_router**；H-DS24 维持 refuted，H-DS10 维持 pending。
需要 WB2 按 `input_dim=3584` 重训，或 DS 明确投影方案。

## 四、反直觉与异常观测

### 1. 「架构终于对了」不等于「能用」

看到 `Linear(1024,256)→ReLU→Linear(256,64)` 时，第一反应是「DS 要的东西终于来了」。
但解 state_dict 时 `net.0.weight` 是 `[256, 1024]`——**输入维度被锁死在 1024**。
而 7B 隐状态 3584 维。**形状对了，维度错了**，比形状错更难发现，因为加载测试
（4,1024）能过，只有接 7B 才炸。

### 2. 三种路由里 qk 只聚出 2 群，反而最差

`qk`（post-RoPE K 均值聚类）在 τ=0.90 下只分出 **2 群**，而 gsc 分出 14 群、
uniform 分 16 群。直觉上「群少 = 压缩狠 = 更差」，但 Argmax 仍是 1.000——
**代理数据集太简单（0.5B 3/3 全中），根本分不出高低**。这正是不能拿 0.5B 当结论的原因。

### 3. push 被拒才发现了新权重

`git push` 报 `! [rejected] main -> main (fetch first)`。若当时图省事 `--force`，
会直接覆盖 WB2 的 7 个提交、**永久丢掉新权重**。rebase 后才看见
`semantic_router.pt` 已在远端。**永远不要 force push 共享仓库**——这次是真的
救回了交付物。

### 4. 纪律 v2.0 的根因不是「DS 不认真」

DS 4 次说「已推送」而远端为空，我此前把它记成「DS 陈述与事实不符」。
真相是：DS 读不到我给的路径（无浏览器），只能靠摘要措辞推断。
**责任在我的交付方式**。这条已写入 DISCIPLINE.md 与 H-DS30 的 fate_note。

## 五、结论

1. DS-026 的固定路由突击**管线已建成并跑通**，但 7B 权威数字仍缺 → H-DS10 维持 pending。
2. DS-027 纪律 v2.0 **全量落地**，R17 11/11 GREEN。
3. WB2 新权重**架构合规但维度不符**（GAP-DS26-01），7B per-query 路由仍阻塞。
4. 下一步唯一关键动作：**Kaggle 跑 Step 5**。

## 文件清单

| 文件 | 内容 |
|---|---|
| `results/ds_reviews/20261019_027_wb1_review.md` | DS-027 归档 |
| `results/ds_reviews/20261019_026_wb1_review.md` | DS-026 归档 |
| `results/kaggle_7b_fixed_route.json` | 固定路由存证（0.5B 代理） |
| `results/wb2_semantic_router_contract_check.json` | 新权重契约复核（GAP-DS26-01/02） |
| `src/semantickv/fixed_route_assault.py` | 三种固定路由突击脚本 |
| `DISCIPLINE.md` | 纪律 v2.0 全文 |
| `tools/make_ds_brief.py` | 投喂包生成器（本文件的来源工具） |