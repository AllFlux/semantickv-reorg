# 7B 实机部署清单（AutoDL RTX 4090D / Colab）

> 目标：产出两个交付物，交给 DS 与 WB2。
> **极简原则**：只上传 4 个 `.py` + 1 个 `.sh` + `requirements.txt`，不带仓库其余部分。

---

## 一、上传哪些文件（最小集）

```
deploy/autodl/
├── run_all.sh                    # 一键执行（入口）
├── requirements.txt
├── eval_runner.py               # GroupAssignment 定义（mock_grouper 依赖）
└── src/semantickv/
    ├── fixed_route_assault.py    # Step 5：固定路由突击
    ├── export_real_features.py   # Step 6：导出 (N,3584) 真实特征
    ├── v41_torch_native_runner.py# 两者共用的模型/hook/注意力基础设施
    └── mock_grouper.py           # GSC 离线聚类（标签来源）
```

> 若用打包好的 `autodl_deploy.tar.gz`，解压即得上述目录结构，**无需其他文件**。

---

## 二、环境变量

```bash
export HF_ENDPOINT=https://hf-mirror.com   # 国内加速（AutoDL 必需，Colab 可省略）
export SEMANTICKV_THREADS=8                # CPU 线程（预处理用）
export HF_HUB_ENABLE_HF_TRANSFER=1         # 可选：大文件下载加速
```

`run_all.sh` 已内置这三项默认值，通常**无需手动设置**。

---

## 三、一条命令

```bash
bash run_all.sh
```

等价于依次执行：

```bash
# Step 5 —— 固定路由（判 45% 线）
python3 src/semantickv/fixed_route_assault.py \
  --model Qwen/Qwen2.5-7B-Instruct --dtype float16 --device cuda --load-in-4bit \
  --ctx 8192 --group-mode gsc,uniform,qk --gsc-k 16 --gsc-tau 0.97 --qk-tau 0.90 \
  --compress-mode hybrid --mid-scope blockdiag --topk-per-group 8 \
  --sink-tokens 4 --window-tokens 64 --compress-layers 0.25-0.75 \
  --kmean-renorm none --group-score-prior logcount --needle-positions 0.15,0.5,0.85 \
  --out results/kaggle_7b_fixed_route.json --environment autodl-7b

# Step 6 —— 真实特征（交 WB2）
python3 src/semantickv/export_real_features.py \
  --model Qwen/Qwen2.5-7B-Instruct --dtype float16 --device cuda --load-in-4bit \
  --ctx 8192 --gsc-k 16 --gsc-tau 0.97 --metric centered \
  --max-tokens 8192 --expect-dim 3584 \
  --out results/real_features_7b --environment autodl-7b
```

可跳过其一：

```bash
bash run_all.sh --skip-features   # 只跑 Step 5
bash run_all.sh --skip-assault    # 只跑 Step 6
```

---

## 四、验收自检（脚本会自动打印）

| 检查 | 期望 |
|---|---|
| `kaggle_7b_fixed_route.json` 的 `environment.kind` | `autodl-7b` |
| `environment.claims_checked` | 四项全 `true` |
| `real_features_7b.json` 的 `hidden_size` | **3584** |
| `real_features_7b.json` 的 `dim_check` | `PASS` |
| `real_features_7b.npz` 的键名 | **`hidden_states`** / `labels` / `meta` |
| `real_features_7b.npz` 的 `hidden_states.shape` | **(N, 3584)** |
| `labels.shape` | `(N,)`，群数 `n_groups` |

任一项不符，脚本会打印 `[FATAL]` 并**拒绝落盘**（不会产出假数据）。

---

## 五、常见问题

| 现象 | 处理 |
|---|---|
| 4bit 加载报 `bitsandbytes` 错 | `pip install -U bitsandbytes`；确认是 CUDA 版 torch |
| OOM（4090D 24GB 一般够） | 把 `--ctx 8192` 降到 `4096`；或 `--max-tokens 4096` |
| 下载卡住 | 确认 `HF_ENDPOINT=https://hf-mirror.com`；或先 `huggingface-cli download` |
| `[FATAL] 环境标签不一致` | 说明参数没凑齐四条断言（7B / 4bit / ctx≥4096 / CUDA），**这是保护，不要绕过** |
| `hidden_size != 3584` | 模型不是 Qwen2.5-7B-Instruct（如 0.5B）；换模型，别改 `--expect-dim` |

---

## 六、回传

```
results/kaggle_7b_fixed_route.json    → 给 DS（判 ≥45% / ROPE_FRACTURE）
results/real_features_7b.npz          → 给 WB2（(N,3584) + 真实 GSC 标签）
results/real_features_7b.json         → 附带元数据（dim_check / provenance）
```

---

## 七、零上传路径（阿里云 PAI-DSW / 任意云主机）

包里没有密钥，仓库是公开的，**不用手机传文件**，在终端里直接拉：

```bash
cd /mnt/workspace
wget https://raw.githubusercontent.com/AllFlux/semantickv-reorg/main/dist/autodl_deploy.tar.gz
tar -xzf autodl_deploy.tar.gz          # 解压出 deploy_autodl/
cd deploy_autodl
export HF_ENDPOINT=https://hf-mirror.com
export HF_HOME=/mnt/workspace/hf_cache
pip install -r requirements.txt        # 首次
bash run_all.sh
```

> 若 `wget` 报 404：说明远端还没更新到含本包的提交，改用
> `git clone https://github.com/AllFlux/semantickv-reorg.git` 后
> `tar -xzf semantickv-reorg/dist/autodl_deploy.tar.gz`。

### 无 GPU 先自检（可选）

```bash
SMOKE=1 bash run_all.sh      # 0.5B / CPU / 无 4bit / 代理标签，产物不可交付
```

`SMOKE=1` 会把 env 标成 `local-sandbox-cpu-proxy-0.5B`，**不会**冒充 7B 权威结果。


---

## 八、手机端一行跑（推荐）

长命令在手机浏览器里粘贴容易卡死。改用入口脚本，**只需粘一行**：

```bash
curl -sL https://cdn.jsdelivr.net/gh/AllFlux/semantickv-reorg@main/deploy/autodl/bootstrap.sh | bash
```

### 后台跑（关掉手机也不中断）

```bash
curl -sL https://cdn.jsdelivr.net/gh/AllFlux/semantickv-reorg@main/deploy/autodl/bootstrap.sh | bash -s -- --bg
```

回车后立刻返回 PID。之后随时：

```bash
tail -f /mnt/workspace/deploy_autodl/run.log      # 看进度
grep -E "^✔|^⚠" /mnt/workspace/deploy_autodl/run.log   # 看结论
```

### 先只拿特征（最快，DS 认定的唯一破局点）

```bash
curl -sL https://cdn.jsdelivr.net/gh/AllFlux/semantickv-reorg@main/deploy/autodl/bootstrap.sh | bash -s -- --bg --features-first
```

### 可选：跑完自动回传 GitHub

```bash
export GITHUB_TOKEN=<有 repo 权限的 PAT>
curl -sL https://cdn.jsdelivr.net/gh/AllFlux/semantickv-reorg@main/deploy/autodl/bootstrap.sh | bash -s -- --features-first --upload
```

会新建分支 `results/<host>-<时间戳>` 并推两个产物。**不做 force push**。
若无 token，就手动从 JupyterLab 下载 `results/` 下的文件。
