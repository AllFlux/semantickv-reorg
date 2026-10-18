# src/shared/ — WB1 ↔ WB2 交付物共享通道

本目录是 WorkBuddy #1（SemanticKV-Reorg，在线部署侧）与 WorkBuddy #2
（训练侧）之间的**唯一交付物落位点**。DS-20261019-018-WB1 称 WB2 已交付
`router_weights.pt` 与 `load_test.py`，但截至 R11 本目录为空（WB1 仓库 git
历史无对应提交）——**文件未实际到达，H-DS24 接口验收处于物理阻塞状态**。

## 1. 期望的文件布局

```
src/shared/
├── router_weights.pt      # WB2 训练的 Router 权重（torch.save dict，见 §2）
├── load_test.py           # WB2 提供的加载验证脚本（可选，WB1 侧有等价测试）
├── README.md              # WB2 交付说明：分数方向、训练温度、特征维度（必需）
└── ...
```

## 2. router_weights.pt 的 blob 约定（load_external_router 读取）

```python
{
  "state_dict": <nn.Module.state_dict()>,   # DS 规格 MLP：Linear(q_dim,256)+ReLU+Linear(256,64)
  "config": {
    "arch": "ds011_linear_relu_linear",     # 必须与本字串一致
    "q_dim": 3584,                          # 7B hidden_size；0.5B=896 / 1.5B=1536
    "hidden_dim": 256,
    "num_groups": 64,
    # —— 以下字段如 WB2 能提供，将覆盖 WB1 侧默认值 ——
    "score_sign": -1,                       # 分数方向：WB2 模型 Pearson r=-0.80 → 必须 -1
    "feature_dim": 3584,                    # 训练时的输入特征维度（Train-Serve Skew 核验用）
    "train_tau": 200,                       # 训练温度（DS-018 警告：τ=200 训练池）
  }
}
```

WB1 侧加载入口：`vllm_plugin.semantic_router.SemanticRouter.load_external_router(path)`。

## 3. 给 WB2 的两项约定请求（DS-20261019-018 improvement_advice）

### 3.1 Train-Serve Skew（温度一致性）

DS 警告原文：WB2 的 Router 在 **τ=200** 的池上训练；若 WB1 在 7B 上以默认
**τ=1** 推理时提取隐状态作为输入特征，分布差异会导致路由准确率归零。

约定请求：
- **特征维度**：请明确定义 Router 输入的每一维含义（哪一层的隐状态？
  是否经过 metric_transform + smooth_hidden？），并在 blob `feature_dim`
  与 README 中写明；
- **温度一致性**：推理侧将按 WB2 训练温度（τ=200）生成输入特征
  （runner 侧 `--smooth-window`/温度参数对齐），或由 WB2 提供推理期
  特征的标准化参数（mean/std），WB1 侧在 route 前应用。

### 3.2 分数方向（负相关修正）

DS 指令原文：WB2 训练出的模型 **Pearson r = -0.80**，直接以 score 选群会
选出最烂的群；须对返回值取负，或由 WB2 重新导出翻转权重的文件。

WB1 侧已默认 `score_sign=-1`（取负后 argmax）。若 WB2 选择重新导出翻转
权重的 .pt，请在 README 明示，WB1 将改回 `score_sign=+1`——**两种方式
必须二选一，不可叠加**（叠加=双负=回到错误方向）。

## 4. 状态台账

| 时间 | 事件 |
|---|---|
| 2026-10-19 R11 | 通道建立；DS-018 所称 WB2 交付物未到达（阻塞）；WB1 侧加载器防御工事就绪 |
