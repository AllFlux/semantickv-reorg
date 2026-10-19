# src/shared/ —— Router 权重交付（H-DS24 · DS-20261019-015-WB2）

> **状态**：第 51 轮交付，第 52 轮按 DS-20261019-017-WB2-V2 修订（分数符号取反
> `score_sign=-1` + τ=200 使用条件声明）。DS 验收「在 `src/shared/` 中出现 `.pt`
> 文件，且提供 `load_test.py` 证明纯 CPU 环境可加载」——**已满足**
> （`load_test.py` 五项检查全 PASS）。
> 实验读数总簿：`results_v2/momega_regimes.json`。
>
> **🚚 物流状态（DS-20261019-023-WB2，第 54 轮·已推送）**：
> **远端仓库：`https://github.com/AllFlux/semantickv-reorg`（main 分支）** —— DS Q9 范围裁定
> （严格限定 `src/shared/`，历史 JSON 与全量权重留本地）**已执行并核验**：远端树原仅含
> `src/shared/` 4 文件 + `.gitignore` 白名单声明（`git ls-tree` + API 双路核验）。
> **🆕 DS-046-WB1 追加**：`src/shared/` 再增真实特征 3 文件（`real_features_3b.npz` / `real_features_3b.json` / `kaggle_3b_fixed_route.json`）。
> **双通道记录（如实，不掩盖失败）**：① `git push` 通道**失败**——GnuTLS recv error (-110)
> TLS 传输层中断（非 403/401 认证拒绝），管线 90s >10s 触发 H-DS32 falsifiable_criterion；
> ② 按 DS 判据切 **GitHub REST API 备选通道成功**——5 路径全部上传（4×201/200 + update 幂等），
> **raw 核验 HTTP/2 200**（`curl -sI` 双文件重试读数）。
> 证据：`results/logistics_push_test.json`（verdict 语义 success，git 失败历史保留）+
> `results/logistics_rest_upload.json`（+ `_run1.json` 首跑留痕）。远端 commit sha 见证据 JSON
> `rest_fallback.uploads[].commit_sha`。PAT 未落任何文件、未进 .git/config（推完即还原纪律）。
>
> **🆕 SemanticRouter（目标模型 DS-041 降级为 Qwen2.5-3B-Instruct，第 62 轮）**：
> 架构 **`Linear(2048,512)→ReLU→Linear(512,64)`**，`net.0.weight` 形状 **`[512,2048]`**，
> 对应 `hidden_size=2048`（hf-mirror 与 ModelScope 双源核验：2048 / 36 层 / 16 头 / 2 KV 头）。
> **体积 2.067MB（fp16）**、CPU batch1 **0.079ms**、低秩代理 Top-1 **0.9559**。
>
> **历史沿革（勿混用）**：1024（旧占位，已废弃）→ 3584（Qwen2.5-7B，DS-028）→ **2048（Qwen2.5-3B，DS-041，当前）**。
> 3584 版权重与读数已归档在 `PROGRESS.md` 五十八~六十一节；换模型时**必须**重训，不能沿用旧权重（第一层形状不同）。
>
> **数据真实性边界**：`router_weights.pt` / `semantic_router.pt` 的**训练数据为低秩+各向异性合成代理**
> （dim=2048、R=128 主成分、谱 λ=exp(−i/τ)、σ=0.6）；标签为离线 K-means；代理准确率**不代表真实语义路由质量**。
> 但 **DS-046-WB1 的真实特征已到位**：`real_features_3b.npz` 是 Qwen2.5-3B-Instruct **真实前向**隐状态
> （原始幅值、未归一化、input_dim=2048），可用 `tools/real_data_finetuner.py` 直接微调 Router，
> 并以 WB1 的 `gsc_pred` 为基线判定（详见该脚本 docstring）。

## 1. 交付物清单

| 文件 | 内容 |
|---|---|
| `router_weights.pt` | **主交付**：$M_\omega$（RelationModelV2）state_dict + arch/regime/score_stats 元数据；regime = `tau200_diverse_pool_retrained`（多样化池重训，有判别力者）；`score_sign=-1`（高分=高训练价值，第 52 轮取反） |
| `router_weights_collapsed_pool.pt` | 备查：harness 原生训练（τ=1 坍缩池）的 ω，**零方差退化态**，见 §5；同样带 `score_sign=-1`（元数据一致性） |
| `load_test.py` | 纯 CPU 空壳加载证明（零 WB2 依赖，架构按 .pt 内嵌 arch 元数据内联重建）；五项检查含 score_sign 元数据 |
| `semantic_router.pt` | **🆕 主交付（DS-041）**：**`Linear(2048,512)→ReLU→Linear(512,64)`** 的 **fp16** state_dict（**2.067MB**）+ arch/num_groups/input_dim/data_provenance/`proxy_mode` 元数据；**`net.0.weight = [512,2048]`** |
| `semantic_router_loadtest.py` | **🆕 DS-041**：纯 CPU 加载证明（零 WB2 依赖，架构按 .pt 内嵌 arch 重建）；**六项检查**含 `net.0.weight` 形状断言 `[512,2048]`、**输入维 == EXPECT_DIM(2048)**、fp16→fp32 加载、`proxy_mode` 声明 |
| 🆕 `semantic_router_real.pt` | **真实数据微调产物（占位）**——WB1 回传 npz 后用 `tools/real_data_finetuner.py` 生成；**默认不覆盖主交付**，须 `--promote` 才提升。当前内容为假数据演练产物，`proxy_mode=real_wb1_npz` 但数据是合成的，**不得用于生产** |
| 🆕 `real_features_3b.npz` | **🆕 真实特征（DS-046-WB1，WB1→WB2 重训输入）**：Qwen2.5-3B-Instruct 真实前向隐状态 `hidden_states (2586,2048) float16` + `labels (2586,) int32` + `meta`(json 字符串)。**原始幅值（L2 范数 78.8–228.2，未归一化）**；42 群 GSC 标签；`dim_check=PASS_DIM_CPU` |
| 🆕 `real_features_3b.json` | 上述 npz 的契约同伴：含 `npz_contract` / `consumption_contract` / **`meta`**（`raw_hidden_exported=true`、`l2_normalized=false` 显式标注） |
| 🆕 `kaggle_3b_fixed_route.json` | 相关验证证据（DS-044）：3B 三路由固定突击 Argmax 全 1.000、gate 1.000、`verdict=MAINTAIN` |
| `README.md` | 本文件 |

## 2. 输入/输出契约

```
输入：
  h_theta_seq : (B, T, 64)   学习器（θ）全序列隐状态—— Learner.encode(x)
  h_phi_seq   : (B, L, 64)   生成器（φ）全序列隐状态—— Generator.encode(codes)
  feats       : (B, 6)       标量特征（算式见 §3）

输出：
  delta_hat   : (B,)         预测的学习器损失变化 $\widehat{\Delta L}$
  score       : (B,)         路由打分（同一 pooled 摘要的第二预测头）
```

默认形状：$T = \text{seq\_len} = 64$（学习器侧），$L = \text{prog\_len} = 8$（生成器侧）。
**两个头都是同一输入的线性读出**——`score` 不是独立模型，选哪个头由调用方决定
（本项目的路由语义用 `score`）。

> **⚠️ 使用条件（第 52 轮补，DS-20261019-017-WB2-V2 执行指示 2）**：
> 本 Router 在 **τ=200 多样化池**上训练（`regime = tau200_diverse_pool_retrained`）。
> WB1 端**必须同步使用 τ=200 采样**：第 51 轮双 regime 实验证明 τ=1 坍缩池上
> 同类权重零方差退化（§5 regime A）——train-serve 温度不对齐时路由判别力丧失。
> DS 对 Q4 的定案：「τ=200 是物理多样性来源，WB1 必须同步 τ=200」。

## 3. 六维 feats 算式（WB1 复现必需）

$\texttt{feats}[i]$ 按 `harness_v2.py:_scalar_feats` 原样给出（本项目实测同款）：

| 列 | 算式 | 说明 |
|---|---|---|
| f0 | `learner.loss(x, reduction="none")` | 该程序在当前学习器上的损失 |
| f1 | `r_align` | 奖励对齐量（调用方传入；本交付测量时传 0） |
| f2 | `x.float().mean(dim=1) / 255.0` | 程序 token 均值（归一化） |
| f3 | `x.float().std(dim=1) / 255.0` | 程序 token 标准差 |
| f4 | `round / 1000.0` | 训练轮次常数（同 batch 内所有行相同） |
| f5 | `loss.mean() − external_anchor_loss()` | 批均值损失相对外部锚的差（detach） |

⚠️ f4/f5 是**训练上下文量**：在 WB1 的在线场景里，`round` 应传当前调度轮次；
若无法获得 `external_anchor_loss`，退化为传 `loss.mean()`（f5=0）会改变输入分布——
**这是契约的已知脆弱点**（C-1：契约不匹配时空壳加载成功也无意义）。

## 4. 架构（与 DS 提案的差异——如实标注）

DS 提案：「用极小的网络（Linear 1024->256->64）配合 Ground Truth 训练好」。

**本交付不是 MLP**，而是本项目原生的 $M_\omega$（`RelationModelV2`，
readout="attention"）：

```
h_theta (B,T,64) ─ theta_proj ─┐
h_phi   (B,L,64) ─ phi_proj ──┼─► z (B,T,96) ─ TransformerEncoder(2层,4头) ─►
feats   (B,6)   ─ feat_proj ──┘                                      │
                                                  4×可学习 query attention 读出
                                                  └─► pooled (B,384)
                                                       ├─ head_delta → delta_hat (B,)
                                                       └─ head_score → score    (B,)
```

- 参数量 **274,466**（非「极小 MLP」）。
- **为什么这样交付**：DS 判据的核心是「导出标准 .pt + 空壳可加载 + 延迟达标」，
  架构形状本身不是否证条件；而 $M_\omega$ 是本项目**唯一训练过、有读数支撑**的
  路由模型。为一个未经训练的新 MLP 架构编造权重，比交付原生模型更偏离「配合
  Ground Truth 训练好」的原意。若 WB1 坚持 1024→256→64 MLP 契约，需新一轮
  重训（BACKLOG 已记）。
- ⚠️ **不声称**与 WB1 `load_external_router` 的期望签名兼容（该接口对本项目
  不可见）。WB1 需按 §2 契约适配。

## 5. 判别力现状（双 regime 实测，`results_v2/momega_regimes.json`）

目标 = 冻结学习器单步 $\Delta L = L_{\rm before} - L_{\rm after}$（与第 50 轮同口径的
替身目标）；「强对比」= ΔL 上下十分位子集。

| Regime | 池 | std_all | std_strong | Pearson r | 判读 |
|---|---|---|---|---|---|
| **A** 坍缩池 τ=1 | 1024 采样仅 **1 独特程序** | 0.00000 | 0.00000 | 0.0000 | **零方差退化**——池无多样性致 ω 退化为恒输出（第 50 轮根因的复现） |
| **B** 多样化池 τ=200（**本交付**） | 1024 全独特 | 0.08463 | **0.12738** | **−0.8005** | 有判别力（预注册 D-1：std_strong ≥ 0.05 达成） |

- **符号约定（第 52 轮修订，DS-017 执行指示 3）**：导出的 `.pt` **已做分数符号取反**
  （元数据 `score_sign=-1`：`head_score` 最后一层 Linear 的 weight/bias 取负，严格等于
  $-\text{score}$）。因此**交付态语义即「score 高 = 高训练价值（ΔL 大）」**，WB1 无需
  再取反。表内 $r=-0.8005$ 是**取反前**的训练池实测读数（⟹ 取反后 $r=+0.8005$，
  已由 `.pt` 重载验证确认，见 `results_v2/momega_regimes.json` 的 `score_sign_flip`）；
  $|r|=0.80$ 才是判别力量级。
- ⚠️ **诚实边界**：① regime B 改变被测分布（τ=200 池 vs harness 在线 τ=1 池）；
  Q4 已由 DS 代裁（「τ=200 是物理多样性来源，WB1 必须同步 τ=200」，WB1 本人未确认，
  故 τ=200 写为**使用条件**而非已验证事实）；② ΔL 是「冻结学习器单步」替身目标，
  非完整在线 ΔL；③ 单 seed（线程已固定为 1，REP-1 合规）。

## 6. CPU 推理延迟（两种条件都报）

| 测量条件 | batch1 | batch64 | 说明 |
|---|---|---|---|
| **空壳进程**（`load_test.py`，即 WB1 场景） | **0.44 ms** | 5.37 ms | 1000 次中位数，threads=1 |
| 训练同进程（`run_ds_r2_router.py`） | 0.77 ms | 24.17 ms | 训练后内存/缓存状态下测得 |

DS 判据：**>1ms ⟹ 接口协议失败（退 GSC 质心检索）；预测目标 <0.5ms**。
两种条件均 **< 1ms（否证线未触发）**；空壳条件同时 **< 0.5ms（预测目标达成）**。
训练同进程的 0.77ms 高于预测线——如实标注，不藏。

## 7. 训练配置（regime B，可复现）

| 项 | 值 |
|---|---|
| 池 | τ=200 采样 1024 条（全独特） |
| 目标 | 冻结学习器单步 ΔL（raw，无标准化/Huber） |
| 损失 | $1 - \mathrm{Pearson}(\widehat{\Delta L}, \Delta L)$ |
| 优化 | AdamW lr 1e-3，300 步，batch 256，grad clip 1.0 |
| 种子 | 模型 init 7 / batch 采样 11（`torch.manual_seed`） |
| 线程 | 1（REP-1） |

## 8. 复现命令

```bash
python3 src/shared/load_test.py            # 空壳加载 + 前向 + 延迟（退出码 0=全过）
python3 tools/run_ds_r2_router.py          # 全实验：双 regime + 导出 .pt + 延迟
```

## 9. 真实特征交付（DS-046-WB1 · WB1→WB2 重训输入）

> 交付目标：让 WB2 用**真实** Qwen2.5-3B-Instruct 隐状态重训 `semantic_router`（按 input_dim=2048 出新版，替代当前 3584 的 7B 版）。

| 项 | 值 |
|---|---|
| 文件 | `real_features_3b.npz` + `real_features_3b.json` |
| hidden_states | `(2586, 2048)` float16，**原始幅值**（L2 范数 78.8–228.2，未归一化） |
| labels | `(2586,)` int32，42 群（GSC 离线聚类 k=16, τ=0.97, centered） |
| npz 键 | `hidden_states` / `hs` / `hidden`（主键 `hidden_states`）；`labels`；`meta`(json 字符串，含 `raw_hidden_exported=true`、`l2_normalized=false`) |
| dim_check | `PASS_DIM_CPU`（真实目标模型 + CPU + 无量化；设备非 GPU 是唯一与实机的差异） |
| 保真声明 | 导出的是未平滑、未归一化的原始隐状态；平滑/归一化仅用于生成 labels，不影响交付数组 |
| 消费契约 | Router 首层 `Linear(hidden_size, ·)` 的 `hidden_size` 必须等于 **2048**；WB2 当前 3584 版不匹配，需按 2048 出新版 |

⚠️ **归一化污染历史（DS-044 已修复）**：早期版本误将单位向量导出；若直接喂给 WB2，Router 在真实场景拿到原始幅值会崩溃。本交付已确保 `l2_normalized=false`、`raw_hidden_exported=true`，WB2 必须按原始幅值消费。

相关验证证据：`kaggle_3b_fixed_route.json`（3B 三路由固定突击 Argmax 全 1.000、gate 1.000、`verdict=MAINTAIN`）。

### 9.1 交付机制化（DS-20261020-051-WB1）

WB2 反馈：经 CDN 下载 8.2MB 的 `.npz` 时因限速/掐断导致文件损坏（`Bad magic number`）。
DS-051 据此把三件事**机制化**（不再依赖人工重试）：

| 机制 | 实现 | 落点 |
|---|---|---|
| **路径固化** | `export_real_features.py` 缺省与显式 `--out` 均被强制重定向到 `src/shared/`；并断言 `out.parent == ROOT/"shared"`，否则 `[FATAL]` 拒产 | 交付件**只**在 `src/shared/`，不再写 `results/` |
| **内联哈希** | 生成 `.npz` 后**在导出脚本内**用 `hashlib.sha256()` 直接算哈希，写 `meta.sha256` 与 `meta.byte_size`（根 `meta` 与内层 `meta["meta"]` 双写） | WB2 下载后比对基准 |
| **大文件分卷** | `>5MB` 自动按 4MB/份 分卷为 `real_features_3b.npz.part1/.part2` + `real_features_3b.npz.parts.json`（含合并 sha256、各卷 sha256、重装说明） | 单卷 <5MB，规避 CDN 截断 |

**当前交付物（`src/shared/`）**：

| 文件 | 说明 |
|---|---|
| `real_features_3b.npz` | 主交付，8215605 bytes（7.84 MB），`sha256=84e37106e7e111e801294c45dc7d3910948d08d104561793a82acc7430e5b261` |
| `real_features_3b.json` | 契约同伴，`meta` 含 `raw_hidden_exported=true` / `l2_normalized=false` / `sha256` / `byte_size` / `dim_check=PASS_DIM_CPU` |
| `real_features_3b.npz.part1` / `.part2` | 分卷（各 ≤4MB），CDN 友好 |
| `real_features_3b.npz.parts.json` | 分卷 manifest（合并 sha256 + 各卷 sha256） |

**WB2 使用约定（防损坏）**：

```bash
# 单文件下载后完整性校验：
python3 tools/validate_wb1_export.py --npz real_features_3b.npz --json real_features_3b.json --check
# SHA256 不匹配 → 下载损坏，重拉。

# 分卷下载后逐卷 + 拼整体校验：
python3 tools/validate_wb1_export.py --parts real_features_3b.npz.parts.json --json real_features_3b.json
# 校验通过后自动/手动拼接还原：
python3 tools/validate_wb1_export.py --assemble real_features_3b.npz.parts.json --out real_features_3b.npz
# （或：cat part1 part2 > real_features_3b.npz）
```

> **版本冻结**：DS-048/051 均标记 `version_freeze=true`。本特征交付链路（导出 + 校验 + 分卷）已闭合；
> 在收到 WB2 最终权重并集成测试前，暂停其他代码改动（仅允许上述机制化修补）。
```
