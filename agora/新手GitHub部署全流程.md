# Agora 新手部署全流程（GitHub 零基础版）

> 目标：从「我连 GitHub 账号都没有」，到「看板在线 + DeepSeek 能读 Raw 链接」。
> 全程复制粘贴命令即可。把下面所有 `<你的用户名>` 换成你自己的 GitHub 用户名。

---

## 第 0 步：准备工具（一次性）

### 1) 注册 GitHub 账号
打开 https://github.com → 点 **Sign up** → 用邮箱注册 → 验证邮箱。

### 2) 安装 Git
- **Windows**：下载 https://git-scm.com 安装（一路 Next，默认选项即可）。
- **macOS**：终端执行 `brew install git`（或装 Xcode 命令行工具）。
- **Linux**：`sudo apt install git`。
安装后，终端执行 `git --version` 能看到版本号即成功。

### 3) 配置 Git 身份（只做一次）
```bash
git config --global user.name  "你的名字"
git config --global user.email "你注册 GitHub 用的邮箱"
```

### 4) 登录方式（推荐用 GitHub CLI，最省事）
- 安装 `gh`：https://cli.github.com （Windows 用安装包；Mac `brew install gh`；Linux 见官网）
- 登录：终端执行 `gh auth login` → 选 **GitHub.com** → 选 **HTTPS** → 选 **Login with a web browser** → 按提示在浏览器里授权。
  > 不想装 `gh` 也行，后面用「个人访问令牌(PAT)」代替密码，见文末附录。

---

## 第 1 步：在 GitHub 上建仓库

1. 登录 github.com，右上角 **+ → New repository**。
2. **Repository name** 填 `semantickv-reorg`（和本地文件夹同名，最省心）。
3. 选 **Public**（必须 Public，否则 DeepSeek 无法通过 Raw 链接免费读取）。
4. **不要**勾选 "Add a README file"（保持空仓库，方便首次推送）。
5. 点 **Create repository**。
6. 创建后会看到一个快速上手页面——先别关，等下要用里面的地址。

---

## 第 2 步：把本地项目推上去

在你**本机**上，进入项目文件夹（里面有 `agora/` 和 `results/`）：

```bash
cd 路径/到/semantickv-reorg      # 换成你实际的目录

git init                         # 初始化本地仓库
git add -A                       # 把所有文件加入暂存
git commit -m "init: 项目 + Agora 看板"
git branch -M main               # 主分支命名为 main
git remote add origin https://github.com/<你的用户名>/semantickv-reorg.git
git push -u origin main          # 推送到 GitHub
```

> 用 `gh` 登录过的话，`git push` 会直接成功。
> 没用 `gh`、走 HTTPS 的话，弹窗要密码——**密码处粘贴你的 PAT**（不是账号密码），见附录。

推送完刷新 github.com 页面，能看到 `agora/`、`results/` 等文件和文件夹，即成功。

---

## 第 3 步：打开「复制 Raw 链接」（配置 raw_base）

看板上的「复制 Raw 链接」按钮，靠 `agora/config.json` 里的 `raw_base` 生成真实链接。
把它改成你的真实地址：

```json
"raw_base": "https://raw.githubusercontent.com/<你的用户名>/semantickv-reorg/main/"
```

改完保存，然后提交推送：
```bash
git add agora/config.json
git commit -m "chore: 设置 raw_base"
git push
```

> 之后每次看板的「复制 Raw 链接」「DS 上下文注入包」产出的就是 DeepSeek 能直接打开的链接。

---

## 第 4 步：发布看板（GitHub Pages）

1. 在 github.com 打开你的仓库 `semantickv-reorg`。
2. 点 **Settings → Pages**（左侧边栏，可能在 "Code and automation" 下）。
3. **Build and deployment → Source** 选 **Deploy from a branch**。
4. **Branch** 选 `main`，**folder** 选 **`/agora`**（因为我们把看板放在 agora 子目录）。
5. 点 **Save**。
6. 等 **1～2 分钟**，然后访问：

```
https://<你的用户名>.github.io/semantickv-reorg/agora/
```

能打开看板 = 发布成功 🎉

> 如果你在第 1 步把看板放在仓库根目录（而不是 agora/ 子目录），folder 就选 `/ (root)`，
> 访问地址则是 `https://<你的用户名>.github.io/semantickv-reorg/`。

---

## 第 5 步：拿到一个 Raw 链接（给 DeepSeek 用）

方法 A（网页，最稳）：
1. 在 github.com 打开 `results/hypothesis_ledger.yaml`；
2. 点文件右上角的 **`Raw`** 按钮 → 浏览器跳到纯文本页；
3. 复制地址栏，形如：
   ```
   https://raw.githubusercontent.com/<你的用户名>/semantickv-reorg/main/results/hypothesis_ledger.yaml
   ```
   把这个链接发给 DeepSeek，它就能直接读。

方法 B（看板，更方便）：打开你的看板页面 → 「原始文件」区块 → 点对应文件的 **复制 Raw 链接**。

---

## 第 6 步：以后每次的更新流程（GitOps 闭环）

WB 跑完新实验、更新了 `results/` 后，只需：

```bash
cd 路径/到/semantickv-reorg
python3 agora/build_bundle.py          # 重建看板数据
python3 agora/sync_to_agora.py --push  # 提交并推送（自动发布到 Pages）
```

> 没装 Python 或想手动也行：
> ```bash
> git add -A
> git commit -m "data: 实验 R3 更新"
> git push
> ```
> GitHub Pages 会自动重新构建，刷新页面即可看到新数据。

把看板的「DS 上下文注入包」复制给 DeepSeek → 它按模板回复 → 你落盘新评审 → 重跑 `check_discipline.py` → 推送。闭环完成。

---

## 附录 A：HTTPS 推送要「个人访问令牌(PAT)」而不是密码

GitHub 自 2021 年起不支持账号密码推代码。两种登录方式：
- **推荐**：装 `gh` 并 `gh auth login`（第 0 步第 4 点），免令牌。
- **或** 用 PAT：
  1. github.com → 右上角头像 → **Settings → Developer settings → Personal access tokens → Tokens (classic)**；
  2. **Generate new token (classic)**，勾选 `repo` 权限，设个过期时间；
  3. 生成后**复制**那串令牌（只显示一次！）；
  4. 推代码要密码时，粘贴这串令牌。

## 附录 B：两种地址千万别混

| 用途 | 格式 | 例子 |
|---|---|---|
| 给人看看板（Pages） | `https://<用户>.github.io/<仓库>/agora/` | 浏览器打开看可视化 |
| 给 DS 读纯文本（Raw） | `https://raw.githubusercontent.com/<用户>/<仓库>/main/<路径>` | DeepSeek 直接读 YAML/MD |

> `github.com/.../blob/...` 是带样式的网页，**不是** Raw，DeepSeek 读不了；要点 `Raw` 按钮拿 `raw.githubusercontent.com` 那个。

## 附录 C：私有仓库怎么办

Raw 链接只对 **Public** 仓库免费有效。私有库 Raw 需要带 `?token=...`（不该发给 DS）。
若必须私有：继续用「人肉 API Gateway」——把看板的「DS 上下文注入包」文本直接复制粘贴给 DeepSeek 即可，不必依赖 Raw 链接。
