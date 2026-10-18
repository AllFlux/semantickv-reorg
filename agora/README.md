# Agora · DS–WB 协作平台

> **雅典学院** —— 把 WorkBuddy（WB）与 DeepSeek（DS）之间「聊天框里的散漫对话」，固化为
> **有版本控制 / 有审计日志 / 有看板展示**的现代软件工程协作。

Agora 是 SemanticKV Reorg（KV 缓存压缩）项目的协作看板，把 `results/` 下的三套核心产物
可视化，并对齐 DS 在对话中提出的「双向纪律」协作模式。

---

## 为什么需要它

DS **没有内置浏览器**，无法直接登录带鉴权的网页。要让 DS「访问文件」，平台必须采用
**无状态 + GitOps + 纯文本暴露**架构：WB 负责 `git push` 与渲染，DS 通过 **Raw 链接**
读取纯文本，用户作为「人肉 API Gateway」搬运链接/文本，形成闭环。

## 架构

```
semantickv-reorg/results/        ← 单一事实来源（YAML / MD / JSON）
        │  build_bundle.py（阶段一：自动同步）
        ▼
agora/data/bundle.json           ← 前端可读的规范化产物
        │
        ▼
agora/ (index.html + css + js)   ← 静态看板（可直接 GitHub Pages 部署）
        │  sync_to_agora.py --push
        ▼
GitHub 仓库 + GitHub Pages       ← 对外站点，每个文件提供 Raw 链接
```

## 看板模块

| 模块 | 数据源 | 说明 |
|---|---|---|
| **概览** | `hypothesis_ledger.yaml` + `discipline_ledger.yaml` | 假设统计、开放项、纪律判定状态灯 |
| **假设台账** | `results/hypothesis_ledger.yaml` | H-DS1..H-DS6，含 status / 可证伪标准 / 证据文件 / DS 承认状态 |
| **纪律检查** | `results/discipline_ledger.yaml` | R1/R2 双边纪律（D1–D5 + DS-D1..DS-D5）逐轮检验明细 |
| **评审画廊** | `results/ds_reviews/*.md` | DS 按 `ds_review_template.md` 落盘的评审档案 |
| **原始文件** | `config.json` 中登记 | 每个文件一个「复制 Raw 链接」按钮 |
| **DS 上下文注入包** | 动态生成 | 一键复制，粘贴给 DS 完成闭环（含开放项清单） |

## 本地运行

```bash
cd agora
python3 build_bundle.py          # 从 results/ 重建 data/bundle.json
python3 -m http.server 8080      # 启动本地静态服务
# 浏览器打开 http://localhost:8080
```

> 必须通过 HTTP 访问（`fetch` 在 `file://` 下会被 CORS 拦截），不能双击打开 index.html。

## 部署到 GitHub Pages

1. 将整个 `agora/` 推到 GitHub 仓库（任意分支，如 `main`）。
2. 仓库 Settings → Pages → Source 选择包含 `agora/` 的分支与目录。
3. 在 `config.json` 中把 `raw_base` 改成你的真实 Raw 基址，例如：
   ```json
   "raw_base": "https://raw.githubusercontent.com/<OWNER>/<REPO>/main/agora/"
   ```
4. 每次 WB 跑完实验、更新 `results/` 后：
   ```bash
   python3 sync_to_agora.py --push --msg "R2: GREEN-WITH-WARN"
   ```
   Pages 会自动更新；把「DS 上下文注入包」复制给 DeepSeek 即可闭环。

## 关键文件

| 文件 | 作用 |
|---|---|
| `build_bundle.py` | 从 `results/` 解析 YAML/MD → `data/bundle.json` |
| `sync_to_agora.py` | 重建 +（可选）git 提交推送，GitOps 同步 |
| `config.json` | 站点标题、Raw 基址、登记文件清单 |
| `index.html` / `css/style.css` / `js/app.js` / `js/markdown.js` | 看板前端（纯静态，无构建步骤） |
| `data/bundle.json` | 由脚本生成，前端实际消费的数据 |

## 闭环工作流

1. WB 跑实验 → 更新 `results/*.json`、改写 `hypothesis_ledger.yaml` 状态。
2. `python3 build_bundle.py` 重建看板数据。
3. 用户打开 Agora 看板，一眼看清进度与待处理开放项。
4. 点击「复制注入包」→ 粘贴给 DeepSeek。
5. DS 按 `ds_review_template.md` 回复（含显式承认被证伪的假设）。
6. WB 把 DS 回复落盘为 `ds_reviews/*.md`，更新 `ds_admitted_refutation`，重跑检查器。
7. `sync_to_agora.py --push` 推送，看板刷新。
