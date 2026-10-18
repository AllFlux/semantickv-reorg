# Agora 部署全流程（移动端网页版 · 零命令行）

> 你只能**用手机网页登录 GitHub**，没有本地终端/Git/Python。
> 本文所有操作都在 GitHub **网页**里完成，**不需要装任何软件、不用敲命令**。
> 核心思路：我已在沙箱里生成一个**自带全部数据/样式/脚本的单文件看板** `index.standalone.html`（约 51KB），你把这个**一个文件**上传到 GitHub 并开启 Pages，看板就上线了。

---

## 你手上要拿到的东西（我会作为附件交付）

| 文件 | 说明 | 怎么拿到 |
|---|---|---|
| **`index.standalone.html`** | 单文件看板，数据已内联，**双击也能开** | 我在对话里以附件形式给出，你在 WorkBuddy 里**下载**到手机即可 |
| `results/` 文件夹（可选） | 给 DeepSeek 读 Raw 纯文本用 | 手机上传较麻烦，可等用电脑时再补，不影响看板上线 |

> 拿到 `index.standalone.html` 后，**只传这一个文件**就够了。

---

## 第 1 步：建仓库（网页）

1. 手机浏览器打开 https://github.com → 登录（你已有的账号）。
2. 右上角 **`+` → New repository**。
3. **Repository name** 填 `semantickv-reorg`（随便起也行，记住它）。
4. 选 **Public**（必须 Public，否则 DeepSeek 无法通过 Raw 链接免费读）。
5. **不要**勾 "Add a README file"（保持空仓库，方便首次上传）。
6. 点 **Create repository**。

---

## 第 2 步：上传单文件看板（网页，手机也能做）

1. 进入刚建的仓库，点绿色的 **`Add file` → `Upload files`**。
2. 从手机文件管理器选你下载好的 **`index.standalone.html`**。
   - 只传这一个文件，连文件夹都不用建。
3. 页面底部 **Commit changes** 保持默认（直接提交到 `main`）→ 点 **Commit changes**。

> 几秒后刷新仓库页，能看到 `index.standalone.html` 出现在文件列表里，即成功。

---

## 第 3 步：开启 GitHub Pages（网页）

1. 仓库页点 **`Settings`**（右侧）→ 左侧找 **`Pages`**（在 "Code and automation" 分组下）。
2. **Build and deployment → Source** 选 **`Deploy from a branch`**。
3. **Branch** 选 `main`，**folder** 选 **`/ (root)`**（因为单文件在根目录）→ **Save**。
4. 等 **1～2 分钟**（GitHub 会构建一次）。

然后访问：

```
https://<你的用户名>.github.io/<仓库名>/
```

能打开看板 = 上线成功 🎉（看到顶部 **GREEN** 状态灯、假设台账、评审画廊即正常）

> 例：用户 `alice`、仓库 `semantickv-reorg` → `https://alice.github.io/semantickv-reorg/`

---

## 第 4 步：验证与日常使用

- **看板内容**：概览灯、假设台账（6 条）、双边纪律检查、DS 评审画廊、原始文件、DS 上下文注入包。
- **发给 DeepSeek**：进入「DS 上下文注入包」区块 → 点 **复制注入包** → 粘贴到 DeepSeek 对话框。即使 Raw 链接还没配，注入包里的台账文字也足够 DS 阅读。
- **离线可用**：`index.standalone.html` 数据已内联，存到手机后**双击就能看**，不依赖网络。

---

## 第 5 步：以后怎么更新数据（你没法跑命令 → 交给我）

由于你无法在手机上运行 Python，**数据更新由我代劳**：

1. 把新的 `results/` 内容发给我（贴文本、截图、或转发 DeepSeek 新链接都行）。
2. 我重新生成 `index.standalone.html`（含最新台账/评审）。
3. 我把新文件作为附件给你 → 你回到 GitHub 网页，**重新上传覆盖**同名文件 → 等 1 分钟 Pages 自动刷新。

> 一句话闭环：**你喂数据 → 我产单文件 → 你覆盖上传**。

---

## 附录 A：两种地址千万别混

| 用途 | 格式 | 例子 |
|---|---|---|
| 给人看看板（Pages） | `https://<用户>.github.io/<仓库>/` | 浏览器打开看可视化 |
| 给 DS 读纯文本（Raw） | `https://raw.githubusercontent.com/<用户>/<仓库>/main/<路径>` | DeepSeek 直接读 YAML/MD |

> `github.com/.../blob/...` 是带样式的网页，**不是** Raw，DeepSeek 读不了；要 Raw 得点文件页右上角的 **`Raw`** 按钮拿 `raw.githubusercontent.com` 那个。

## 附录 B：私有仓库怎么办

Raw 链接只对 **Public** 仓库免费有效。私有库 Raw 要带 `?token=...`（不该发给 DS）。
若必须私有：继续用「人肉 API Gateway」——把看板的「DS 上下文注入包」文本直接复制粘贴给 DeepSeek 即可，不必依赖 Raw 链接。单文件看板在私有库里**照样能 Pages 发布、照样能看**（只是 DS 读不了 Raw）。

## 附录 C：电脑端备选（完整多文件版 + 自动同步）

如果你后来在电脑上操作，可走更完整的流程（多文件版 + 实时 Raw 链接 + 一键同步）：

```bash
# 1) 把整个 semantickv-reorg/（含 agora/ 与 results/）推上去
cd semantickv-reorg
git init && git add -A && git commit -m "init"
git branch -M main
git remote add origin https://github.com/<你>/semantickv-reorg.git
git push -u origin main

# 2) 配置 Raw 基址（让"复制 Raw 链接"产出真链接）
#    编辑 agora/config.json 的 raw_base：
#    "raw_base": "https://raw.githubusercontent.com/<你>/semantickv-reorg/main/"

# 3) Pages 选 main + /agora 文件夹（多文件版在 agora/ 子目录）
#    访问 https://<你>.github.io/semantickv-reorg/agora/

# 4) 以后更新：
python3 agora/build_bundle.py           # 重建数据
python3 agora/build_standalone.py        # 顺带重建单文件版
python3 agora/sync_to_agora.py --push    # 提交并推送（Pages 自动刷新）
```

> 多文件版比单文件版多一项能力：**看板上的「复制 Raw 链接」能直接产出 DeepSeek 可打开的真实链接**。单文件版为求「手机易上传」牺牲了这一项（可用注入包文本替代）。两者数据完全一致。
