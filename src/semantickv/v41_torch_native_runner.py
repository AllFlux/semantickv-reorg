#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v41_torch_native_runner.py — 真实模型慢速验证器（纯 PyTorch，无自定义 Kernel）。

=====================================================================
定位（对应「无 GPU 环境的三方案」之方案一）
=====================================================================
这是上 GPU / Triton 之前的**数学正确性验证器**：用开源小模型
（默认 Qwen2.5-0.5B，任何 Qwen2/Llama 系稍改即可）在 CPU 上验证
SemanticKV-Reorg 架构的三个核心机制：

  A. 真实隐状态上的 GSC 分组：构造「多话题 × 多次重复」的 2K 上下文，
     用 prefill 隐状态跑 GSC，对照 ground truth 算 ARI，并扫描 tau/k
     —— 这是 E0「真实分布阈值校准」的第一次真实数据演练；
     （v0.2.0 起支持 cosine / centered / l2 三种度量，修正各向异性）
  B. KV 重组的张量机制：
       V0 重排不变性  —— gather/scatter 重排群序，注意力输出应与原始
                         逐位余弦 ≈1（验证 RoPE 捕获 / GQA 展开 / 掩码正确）；
       V1 群均值压缩  —— 每群 KV 合并为均值槽位（KV 预算 N → G），
                         测输出余弦相似度（信息损失下界）；
       V2 关系图掩码  —— 只允许「同群 + top-p 关系边」访问，测更激进的稀疏化；
       V1.5 保护性压缩（v0.2.0，DeepSeek 评审）——
         topk     ：每群保留注意力入度 top-K 个**真实** KV（非均值）；
         weighted ：注意力入度加权的群均值；
         sink     ：前 sink + 后 window 个 token 独占槽位（精确保留）；
         层级选择性：只压缩中层（--compress-layers 0.25-0.75）；
  C. 全模型压缩推理 + needle 检索：对比原始模型与压缩模型的 needle
     回答正确率（E0 相位敏感性的玩具版）；--baseline-only 可先单独
     验证原始基线（Kaggle 上机流程第一步）。

边界声明：
  - 本脚本验证的是**机制与数学**，不是最终性能；慢是预期内的。
  - 群均值 KV 混合了群内不同位置的 RoPE 后 K——位置合并方案是 E1 的
    开放设计问题，此处用「后 RoPE 均值」作为可测的近似，报告中已标注。
  - 小模型（0.5B）的绝对准确率低；只看**原始 vs 压缩的相对差异**。

用法：
    python3 src/semantickv/v41_torch_native_runner.py \
        --model /path/to/Qwen2.5-0.5B --ctx 2048 --paragraphs 8 --repeats 4
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "semantickv"))

# ---------------------------------------------------------------------
# 话题语料：8 个话题，刻意跨语言/跨模态（中/英/代码/JSON/数字/诗歌），
# 保证语义结构在隐空间里真正可分。
# 设计教训（首轮实测）：全是同质中文散文时，Qwen2.5-0.5B 的末层隐状态
# 跨话题余弦仍高达 0.6+，GSC 会坍缩成一两个巨群——这正是 E0 校准
# 假设 H1c 的现场证据：真实数据的「可分性」取决于语料多样性。
# ---------------------------------------------------------------------
TOPIC_TEXTS = [
    # T0 中文科技散文
    "量子计算利用量子比特的叠加与纠缠特性，在特定问题上相较经典计算机具有指数级加速潜力。"
    "Shor 算法可以在多项式时间内分解大整数，这对现有公钥密码体系构成潜在威胁。",
    # T1 中文生物
    "光合作用将光能转化为化学能，叶绿体中的类囊体膜是光反应的主要场所，"
    "而卡尔文循环在叶绿体基质中固定二氧化碳并合成糖类。",
    # T2 英文新闻体
    "The rapid advancement of renewable energy technologies has reshaped global "
    "investment patterns, as solar and wind capacity continues to outpace fossil "
    "fuel additions worldwide.",
    # T3 Python 代码
    "def quicksort(arr):\n    if len(arr) <= 1:\n        return arr\n"
    "    pivot = arr[len(arr) // 2]\n    left = [x for x in arr if x < pivot]\n"
    "    right = [x for x in arr if x > pivot]\n"
    "    return quicksort(left) + [pivot] + quicksort(right)",
    # T4 JSON 数据
    '{"users": [{"id": 1001, "name": "alice", "roles": ["admin", "editor"]}, '
    '{"id": 1002, "name": "bob", "roles": ["viewer"]}], "total": 2}',
    # T5 数字序列
    "3.14159265358979 2.71828182845905 1.41421356237310 0.57721566490153 "
    "1.61803398874989 0.69314718055995 2.30258509299405",
    # T6 中国古典诗词
    "大江东去，浪淘尽，千古风流人物。故垒西边，人道是，三国周郎赤壁。"
    "乱石穿空，惊涛拍岸，卷起千堆雪。",
    # T7 英文技术
    "The transformer architecture relies on self-attention mechanisms to model "
    "sequence dependencies, enabling parallel training across unprecedented "
    "scales of data.",
]


def build_structured_input(tokenizer, n_paragraphs: int, repeats: int, target_tokens: int,
                           needle: Optional[str] = None, needle_frac: float = 0.5,
                           question: Optional[str] = None, seed: int = 7):
    """构造「多话题 × 多次重复」的交错长上下文。

    返回 (input_ids (1,N), truth_topic (N,), truth_occ (N,), needle_pos, question_start)
    truth_topic[i] = 话题编号；truth_occ[i] = 话题编号 × repeats + 第几次出现
    （用于诊断真实隐空间的多尺度结构：同话题不同出现位置是否各自成簇）；
    needle/question 部分标记为 -1。
    """
    rng = np.random.default_rng(seed)
    topics = TOPIC_TEXTS[:n_paragraphs]
    # 每个话题重复 repeats 次，轮次内顺序打乱 -> 同一话题的 token 散布全文
    blocks = []          # [(topic_id, round_id, token_ids)]
    for r in range(repeats):
        order = rng.permutation(len(topics))
        for t in order:
            ids = tokenizer(topics[t], return_tensors="pt", add_special_tokens=False).input_ids[0]
            blocks.append((int(t), r, ids))
    # 组装到 target_tokens 附近
    seq, lab, lab_occ = [], [], []
    for t, r, ids in blocks:
        seq.extend(ids.tolist())
        lab.extend([t] * len(ids))
        lab_occ.extend([t * repeats + r] * len(ids))
        if len(seq) >= target_tokens:
            break
    seq, lab, lab_occ = seq[:target_tokens], lab[:target_tokens], lab_occ[:target_tokens]

    needle_pos = None
    if needle is not None:
        needle_pos = int(len(seq) * needle_frac)
        nids = tokenizer(needle, return_tensors="pt", add_special_tokens=False).input_ids[0]
        seq[needle_pos:needle_pos] = nids.tolist()
        lab[needle_pos:needle_pos] = [-1] * len(nids)
        lab_occ[needle_pos:needle_pos] = [-1] * len(nids)
    q_start = len(seq)
    if question is not None:
        qids = tokenizer(question, return_tensors="pt", add_special_tokens=False).input_ids[0]
        seq.extend(qids.tolist())
        lab.extend([-1] * len(qids))
        lab_occ.extend([-1] * len(qids))

    input_ids = torch.tensor([seq], dtype=torch.long)
    return input_ids, np.array(lab, dtype=np.int64), np.array(lab_occ, dtype=np.int64), needle_pos, q_start


# ---------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------

def repeat_kv(hidden_states: torch.Tensor, n_rep: int) -> torch.Tensor:
    """与 transformers 的 repeat_kv 语义一致：Q 头 h 对应 KV 头 h // n_rep。"""
    batch, num_kv_heads, slen, head_dim = hidden_states.shape
    if n_rep == 1:
        return hidden_states
    hidden_states = hidden_states[:, :, None, :, :].expand(batch, num_kv_heads, n_rep, slen, head_dim)
    return hidden_states.reshape(batch, num_kv_heads * n_rep, slen, head_dim)


def scatter_mean(x: torch.Tensor, labels: np.ndarray, G: int) -> torch.Tensor:
    """按 labels 对 (1,H,N,D) 在第 2 维求群均值 -> (1,H,G,D)。"""
    idx = torch.as_tensor(labels, dtype=torch.long, device=x.device)
    B, H, N, D = x.shape
    out = torch.zeros(B, H, G, D, dtype=x.dtype, device=x.device)
    cnt = torch.zeros(G, dtype=x.dtype, device=x.device)
    out.index_add_(2, idx, x)
    cnt.index_add_(0, idx, torch.ones(N, dtype=x.dtype, device=x.device))
    return out / cnt.clamp(min=1.0).view(1, 1, G, 1)


def scatter_wmean(x: torch.Tensor, w: torch.Tensor, labels: np.ndarray,
                  G: int) -> torch.Tensor:
    """按 labels 对 (B,H,N,D) 加权求群均值 -> (B,H,G,D)。w: (B,H,N)，群内已归一化。"""
    idx = torch.as_tensor(labels, dtype=torch.long, device=x.device)
    B, H, N, D = x.shape
    out = torch.zeros(B, H, G, D, dtype=x.dtype, device=x.device)
    out.index_add_(2, idx, x * w.unsqueeze(-1))
    return out


def renorm_to_member_avg(x_mean: torch.Tensor, x: torch.Tensor, labels: np.ndarray,
                         G: int) -> torch.Tensor:
    """把群均值槽的范数重标定到「成员平均范数」（只用于 K）。

    动机（v0.2.0 实测）：Qwen2.5-0.5B 第 12 层 post-RoPE K 范数 mean≈17.2，
    而群均值范数仅 ≈14.9（0.865×）——话题内 K 方向相干使均值保住了大部分
    范数，但残余的 14% 亏缺在 score 量级 ~20 下造成 e^2.8≈16× 的 softmax
    系统性偏置：精确槽（sink/window/topk 选出的真实 token）会吸走本应属于
    群均值槽的注意力质量。这解释了「flat+sink 反而比纯 V1 差」的异常。
    重标定后群均值槽与精确槽的范数可比，偏置消除。
    V 不重标定（幅值承载语义信息）。
    """
    idx = torch.as_tensor(labels, dtype=torch.long, device=x.device)
    B, H, N, D = x.shape
    cnt = torch.zeros(G, dtype=x.dtype, device=x.device)
    cnt.index_add_(0, idx, torch.ones(N, dtype=x.dtype, device=x.device))
    xn = x.norm(dim=-1)                                        # (B,H,N)
    nsum = torch.zeros(B, H, G, dtype=x.dtype, device=x.device)
    nsum.index_add_(2, idx, xn)
    avg_n = nsum / cnt.clamp(min=1.0).view(1, 1, G)            # (B,H,G)
    mn = x_mean.norm(dim=-1).clamp(min=1e-9)                   # (B,H,G)
    return x_mean * (avg_n / mn).unsqueeze(-1)


def attention_importance(q: torch.Tensor, k: torch.Tensor, scaling: float,
                         n_rep: int, chunk: int = 512) -> torch.Tensor:
    """每 KV 头的「注意力入度」：因果 softmax 后按 key 列求和（该 key 被
    所有 query 关注的总量），用于 V1.5 的代表 token 选择与加权群均值。

    q: (B,Hq,N,hd) post-RoPE；k: (B,Hkv,N,hd) post-RoPE（GQA，函数内 repeat）
    返回 imp: (B,Hkv,N)，每头列和 ≈ 1（占总注意力量的比例；排序用时尺度无关）。
    """
    B, Hq, N, hd = q.shape
    Hkv = k.shape[1]
    k_e = repeat_kv(k, n_rep)                          # (B,Hq,N,hd)
    imp = torch.zeros(B, Hkv, N, dtype=q.dtype, device=q.device)
    pos_k = torch.arange(N, device=q.device)
    for s in range(0, N, chunk):
        e = min(s + chunk, N)
        sc = torch.matmul(q[:, :, s:e, :], k_e.transpose(-1, -2)) * scaling  # (B,Hq,c,N)
        causal = pos_k[None, None, None, :] > torch.arange(s, e, device=q.device)[None, None, :, None]
        sc = sc.masked_fill(causal, float("-inf"))
        p = torch.softmax(sc, dim=-1)
        # Q 头 h 对应 KV 头 h // n_rep（repeat_kv 约定）
        p = p.view(B, Hkv, n_rep, e - s, N).sum(dim=(2, 3))
        imp += p
    # 每个 (Q头, query) 的 softmax 行和 = 1，总注意力量 = n_rep·N
    return imp / (N * n_rep)


def select_topk_per_group(imp_head: np.ndarray, labels: np.ndarray, G: int,
                          K: int) -> Tuple[np.ndarray, np.ndarray]:
    """单 KV 头：每群按注意力入度选 top-K token（DeepSeek V1.5-a）。

    imp_head: (N,)；labels: (N,)
    返回 (slot_group (S,), slot_token (S,))。群规模 ≤ K 时全选。
    """
    sel_g: List[np.ndarray] = []
    sel_t: List[np.ndarray] = []
    for g in range(G):
        members = np.flatnonzero(labels == g)
        if members.size <= K:
            sel_g.append(np.full(members.size, g, dtype=np.int64))
            sel_t.append(members.astype(np.int64))
        else:
            top = members[np.argsort(-imp_head[members])[:K]]
            sel_g.append(np.full(top.size, g, dtype=np.int64))
            sel_t.append(top.astype(np.int64))
    return np.concatenate(sel_g), np.concatenate(sel_t)


def apply_privileged_tokens(labels: np.ndarray, sink: int, window: int,
                            force_bos: bool = False) -> np.ndarray:
    """Attention Sink 保护（DeepSeek V1.5-c）：前 sink 个 token（BOS/Sink）
    与后 window 个 token（近期局部窗口）各自独占槽位——精确保留原始 KV，
    不参与群均值混合。force_bos=True 时至少保留 token 0（防 topk 模式
    prefill 首行 softmax 全 -inf）。

    返回紧凑重编号的新 labels。
    """
    N = len(labels)
    lab = np.asarray(labels).copy()
    priv = np.zeros(N, dtype=bool)
    if force_bos:
        priv[0] = True
    if sink > 0:
        priv[:sink] = True
    if window > 0:
        priv[max(0, N - window):] = True
    nxt = int(lab.max()) + 1
    for i in np.flatnonzero(priv):
        lab[i] = nxt
        nxt += 1
    uniq, inv = np.unique(lab, return_inverse=True)
    return inv.astype(np.int64)


def metric_transform(H: np.ndarray, metric: str) -> np.ndarray:
    """分组前隐状态变换（v0.2.0 各向异性修正，DeepSeek 指令 3）。

    cosine  : L2 归一化（v0.1.x 行为；真实数据 τ 甜点被各向异性压到 0.97）
    centered: 先减全局均值（去掉各向异性公共分量）再归一化——主推修复
    l2      : 保持原始幅值；kNN 侧用幅值感知相似度（见 mock_grouper._knn）
    """
    if metric == "cosine":
        return H / np.maximum(np.linalg.norm(H, axis=1, keepdims=True), 1e-12)
    if metric == "centered":
        Hc = H - H.mean(axis=0, keepdims=True)
        return Hc / np.maximum(np.linalg.norm(Hc, axis=1, keepdims=True), 1e-12)
    if metric == "l2":
        return H.astype(np.float32)
    raise ValueError(f"unknown metric: {metric}")


def parse_layer_spec(spec: str, n_layers: int) -> List[int]:
    """解析 --compress-layers：'all' | '6-10'（1-indexed 含端点）| '0.25-0.75'（比例）。"""
    spec = spec.strip().lower()
    if spec in ("all", "*", ""):
        return list(range(n_layers))
    if "-" not in spec:
        i = int(spec)
        return [max(0, min(n_layers - 1, i - 1))]
    a, b = spec.split("-", 1)
    if "." in a or "." in b:                       # 比例
        fa, fb = float(a), float(b)
        lo = int(round(fa * n_layers))
        hi = int(round(fb * n_layers)) - 1
    else:                                          # 绝对层号（1-indexed）
        lo, hi = int(a) - 1, int(b) - 1
    lo = max(0, min(n_layers - 1, lo))
    hi = max(0, min(n_layers - 1, hi))
    return list(range(lo, hi + 1))


def cosine_per_position(a: torch.Tensor, b: torch.Tensor) -> Dict[str, float]:
    """逐位置余弦相似度统计。a/b: (1,N,D) 或 (N,D)。"""
    a = a.reshape(-1, a.shape[-1]).double()
    b = b.reshape(-1, b.shape[-1]).double()
    cos = torch.nn.functional.cosine_similarity(a, b, dim=-1)
    c = cos.numpy()
    return {"mean": float(c.mean()), "p05": float(np.percentile(c, 5)),
            "min": float(c.min()), "std": float(c.std())}


def adjusted_rand_index(labels_true: np.ndarray, labels_pred: np.ndarray) -> float:
    """ARI（不依赖 sklearn）。"""
    lt = np.asarray(labels_true); lp = np.asarray(labels_pred)
    classes = np.unique(lt); clusters = np.unique(lp)
    if len(classes) < 2 or len(clusters) < 2:
        return 1.0 if len(classes) == len(clusters) else 0.0
    # 列联表
    cont = np.zeros((len(classes), len(clusters)), dtype=np.int64)
    ci = {c: i for i, c in enumerate(classes)}
    cj = {c: i for i, c in enumerate(clusters)}
    for a, b in zip(lt, lp):
        cont[ci[a], cj[b]] += 1
    def comb2(x):
        return x.astype(np.float64) * (x - 1) / 2.0
    sum_ij = comb2(cont).sum()
    sum_a = comb2(cont.sum(axis=1)).sum()
    sum_b = comb2(cont.sum(axis=0)).sum()
    n = len(lt)
    total = comb2(np.array([n]))[0]
    expected = sum_a * sum_b / total
    maximum = (sum_a + sum_b) / 2.0
    if maximum == expected:
        return 1.0
    return float((sum_ij - expected) / (maximum - expected))


def mutual_knn_rate(idx: np.ndarray) -> float:
    """互 kNN 互通率：互为 top-k 邻居的有向边占比。"""
    N, k = idx.shape
    M = np.zeros((N, N), dtype=bool)
    rows = np.arange(N)[:, None]
    M[rows, idx] = True
    return float((M & M.T).sum() / (N * k))


# ---------------------------------------------------------------------
# Prefill 拦截器：捕获目标层的 Q/K/V（RoPE 后）/ 注意力输出 / 最终隐状态
# ---------------------------------------------------------------------

class PrefillInterceptor:
    """通过 monkey-patch + hook 捕获 Qwen2 prefill 的内部张量。

    捕获点（全部只读、无梯度）：
      - apply_rotary_pos_emb：RoPE **后**的 Q/K（按层调用计数区分层号）；
      - 目标层 v_proj：V（未过 RoPE）；
      - 目标层 self_attn：原始注意力输出（过 o_proj，作为参照）；
      - model.norm：最终归一化隐状态（GSC 分组输入）。
    """

    def __init__(self, model, target_layer: int):
        self.model = model
        self.target_layer = target_layer
        self.n_layers = model.config.num_hidden_layers
        self.cap: Dict[str, torch.Tensor] = {}
        self._rope_calls = 0
        self._orig_rope = None
        self._handles = []
        self._mod = None

    def install(self):
        import transformers.models.qwen2.modeling_qwen2 as mq
        self._mod = mq
        self._orig_rope = mq.apply_rotary_pos_emb
        target = self.target_layer
        n_layers = self.n_layers
        orig = self._orig_rope
        state = {"calls": 0}

        def rope_capture(q, k, cos, sin, unsqueeze_dim=1):
            qe, ke = orig(q, k, cos, sin, unsqueeze_dim)
            layer = state["calls"] % n_layers
            state["calls"] += 1
            if layer == target:
                self.cap["q_post"] = qe.detach()
                self.cap["k_post"] = ke.detach()
                self.cap["cos"] = cos.detach()
                self.cap["sin"] = sin.detach()
            return qe, ke

        mq.apply_rotary_pos_emb = rope_capture
        self._rope_state = state

        attn = self.model.model.layers[target].self_attn

        def v_hook(_m, _inp, out):
            self.cap["v_raw"] = out.detach()          # (1, N, kv_heads*head_dim)

        def attn_hook(_m, _inp, out):
            self.cap["attn_out"] = out[0].detach()    # (1, N, hidden) 过 o_proj

        self._handles.append(attn.v_proj.register_forward_hook(v_hook))
        self._handles.append(attn.register_forward_hook(attn_hook))

        def norm_hook(_m, _inp, out):
            self.cap["final_hidden"] = out.detach()   # (1, N, hidden)

        self._handles.append(self.model.model.norm.register_forward_hook(norm_hook))

    def uninstall(self):
        if self._orig_rope is not None:
            self._mod.apply_rotary_pos_emb = self._orig_rope
        for h in self._handles:
            h.remove()
        self._handles = []

    def run_prefill(self, input_ids: torch.Tensor, use_cache: bool = False,
                    need_hidden_states: bool = True):
        self._rope_state["calls"] = 0
        with torch.no_grad():
            out = self.model(input_ids, use_cache=use_cache,
                             output_hidden_states=need_hidden_states)
        if need_hidden_states:
            self.cap["hidden_states"] = tuple(h.detach() for h in out.hidden_states)
        self.cap["logits"] = out.logits.detach()
        return out


# ---------------------------------------------------------------------
# 全模型压缩注意力：把 self_attn.forward 换成「压缩 KV」版
# ---------------------------------------------------------------------

def count_topk_slots(labels: np.ndarray, G: int, K: int) -> int:
    """topk 模式的槽位数：Σ_g min(K, |g|)（与 KV 头无关，仅由群规模决定）。"""
    return int(sum(min(K, int((labels == g).sum())) for g in range(G)))


def safe_softmax(scores: torch.Tensor) -> torch.Tensor:
    """行全 -inf 时返回全 0（而非 NaN）的 softmax——分段注意力中
    「该段对当前 query 全部被因果/块对角掩码」是正常情形（如早期 query
    看不到局部窗段），须产出零权重而非 NaN。"""
    row_max = scores.max(dim=-1, keepdim=True).values
    bad = torch.isinf(row_max)
    z = torch.exp(scores - row_max.clamp(min=-1e30))
    denom = z.sum(dim=-1, keepdim=True).clamp(min=1e-30)
    return (z / denom).masked_fill(bad, 0.0)


class CompressedAttentionPatcher:
    """慢速验证器的压缩推理路径（对应有卡环境 Triton Kernel 的 PyTorch 版）。

    v0.2.0（DeepSeek 评审 V1.5）新增保护性压缩模式与层级选择性压缩：

      mode="flat_mean" —— v0.1.x 行为：每层用「该层 K/V 的群均值」作为 KV 集；
      mode="weighted"  —— 群均值按「注意力入度」加权（重要 token 权重高），
                          槽位数与 flat_mean 相同（G 个）；
      mode="topk"      —— 每群保留注意力入度 top-K 个**真实** KV（非均值），
                          prefill 期按槽位真实位置做因果掩码（不泄未来）；

    三种模式共享：
      - sink 保护：前 sink 个 token（BOS/Sink）与后 window 个 token
        （近期局部窗口）各自独占槽位，精确保留原始 KV（DeepSeek V1.5-c）；
      - 层级选择性：只 patch compress_layers 中的层，其余层走原始注意力。

    prefill：按模式构造压缩 KV 集（G 或 S 个槽位）；
    decode ：KV 集 = 冻结的压缩槽位 + 新 token 的 K/V 缓冲。
    """

    def __init__(self, model, mode: str = "flat_mean", topk_per_group: int = 4,
                 sink_tokens: int = 0, window_tokens: int = 0,
                 compress_layers: Optional[List[int]] = None,
                 kmean_renorm: str = "member",
                 group_score_prior: str = "logcount",
                 mid_scope: str = "blockdiag"):
        assert mode in ("flat_mean", "weighted", "topk", "hybrid")
        assert kmean_renorm in ("none", "member")
        assert group_score_prior in ("none", "logcount")
        assert mid_scope in ("blockdiag", "global")
        self.model = model
        self.mode = mode
        self.mid_scope = mid_scope                 # hybrid：中段 query 视野
        self.topk_per_group = max(1, int(topk_per_group))
        self.sink_tokens = int(sink_tokens)
        self.window_tokens = int(window_tokens)
        self.compress_layers = compress_layers      # None = 全部层
        self.kmean_renorm = kmean_renorm            # 群均值 K 范数重标定
        self.group_score_prior = group_score_prior  # 群规模分数先验 log(m)
        self._orig_forwards = {}
        self.labels = None
        self.G = 0
        self.n_priv = 0
        self.kmean: Dict[int, torch.Tensor] = {}   # 冻结的压缩 KV 槽位
        self.vmean: Dict[int, torch.Tensor] = {}
        self.slot_pos: Dict[int, torch.Tensor] = {}  # topk：槽位真实 token 位置 (B,Hkv,S)
        self.buf_k: Dict[int, torch.Tensor] = {}
        self.buf_v: Dict[int, torch.Tensor] = {}

    def set_groups(self, labels: np.ndarray, n_tokens: int):
        """装入分组标签；内部应用 sink/window 特权扩展（独占槽位）。"""
        base = np.asarray(labels)
        force_bos = (self.mode == "topk")   # topk 防 prefill 首行 softmax 全 -inf
        lab = apply_privileged_tokens(base, self.sink_tokens, self.window_tokens,
                                      force_bos=force_bos)
        self.labels = lab
        self.G = int(lab.max()) + 1
        n_priv = min(self.sink_tokens, n_tokens) + min(self.window_tokens, n_tokens)
        if force_bos:
            n_priv = max(n_priv, 1)
        self.n_priv = int(min(n_priv, n_tokens))
        # 槽位规模（群内 token 数；特权独占槽为 1）→ log 先验
        self.slot_counts = np.bincount(lab, minlength=self.G).astype(np.float32)
        self.slot_logcount = np.log(np.maximum(self.slot_counts, 1.0)).astype(np.float32)
        # hybrid 分段边界（DS-20261018-006）：[0, n_sink) sink 段；
        # [N-n_window, N) 局部窗段；其余为中段。特权群在 labels 尾部（位置序）。
        self.n_tokens = int(n_tokens)
        self.n_sink = min(self.sink_tokens, n_tokens)
        self.n_window = min(self.window_tokens, n_tokens)
        if self.mode == "hybrid":
            assert self.n_sink >= 1, "hybrid 模式要求 sink_tokens >= 1（数值稳定：保证任意 query 至少可见 sink 槽 0）"
        self.G_base = self.G - self.n_priv    # 语义（中段）群数

    def kv_slots(self) -> int:
        """每压缩层的冻结 KV 槽位数。"""
        if self.labels is None:
            return 0
        if self.mode in ("topk", "hybrid"):
            return count_topk_slots(self.labels, self.G, self.topk_per_group)
        return self.G

    def mid_slots(self) -> int:
        """hybrid：中段（非特权群）保留的槽位数。"""
        if self.labels is None or self.mode != "hybrid":
            return 0
        return int(sum(min(self.topk_per_group, int((self.labels == g).sum()))
                       for g in range(self.G_base)))

    def _hybrid_attention(self, layer_idx: int, q: torch.Tensor, k: torch.Tensor,
                          v: torch.Tensor, idx_sel: torch.Tensor,
                          grp_sel: torch.Tensor, n_rep: int,
                          scaling: float) -> torch.Tensor:
        """E1.5 混合保护分段注意力（DS-20261018-006 规格）。

        三段独立计算、logsumexp 数值稳定合并（数学上等价于三段拼接的
        联合 softmax，但显式分段可控且避免大 Mask 拼接）：
          sink 段  —— 前 n_sink 个 token 的真实 KV；
          local 段 —— 最近 n_window 个 token 的真实 KV；
          mid   段 —— 各语义群 top-K 代表（块对角 Mask：中段 query 只见
                      本群代表，特权 query（sink/窗，即读出位）豁免、
                      可见全部中段代表——保证 needle 可达）。
        decode 不走本函数（新 token 不属于任何群，走联合 softmax）。
        返回 o: (B,Hq,N,hd)。
        """
        B, Hq, N, hd = q.shape
        Hkv = k.shape[1]
        device = q.device
        lab_t = torch.as_tensor(self.labels, device=device)
        is_priv_q = lab_t >= self.G_base                       # (N,)
        qpos = torch.arange(N, device=device)
        out = torch.empty_like(q)
        for h in range(Hkv):
            sp = idx_sel[0, h]                                 # (S,) 槽位真实位置
            sg = grp_sel[0, h]                                 # (S,) 槽位群 id
            qs = q[0, h * n_rep:(h + 1) * n_rep]               # (n_rep,N,hd)
            kh, vh = k[0, h], v[0, h]                          # (N,hd)
            sink_m = sp < self.n_sink
            win_m = sp >= (N - self.n_window)
            mid_m = ~(sink_m | win_m)
            seg_o, seg_lse = [], []
            for m in (sink_m, win_m, mid_m):
                if not bool(m.any()):
                    seg_o.append(None)
                    seg_lse.append(None)
                    continue
                idxs = sp[m]                                   # (s,)
                ks = kh.index_select(0, idxs).unsqueeze(0)     # (1,s,hd)
                vs_ = vh.index_select(0, idxs).unsqueeze(0)    # (1,s,hd)
                sc = torch.matmul(qs.unsqueeze(0),
                                  ks.transpose(-1, -2)) * scaling   # (1,n_rep,N,s)
                causal = idxs[None, None, :] > qpos[None, :, None]
                sc = sc.masked_fill(causal, float("-inf"))
                if m is mid_m and self.mid_scope == "blockdiag":
                    # 块对角：中段 query 只见本群代表；特权 query 豁免
                    sg_m = sg[m]
                    bd = (lab_t[None, :, None] != sg_m[None, None, :]) \
                        & (~is_priv_q)[None, :, None]
                    sc = sc.masked_fill(bd.unsqueeze(0), float("-inf"))
                seg_o.append(torch.matmul(safe_softmax(sc), vs_).squeeze(0))
                seg_lse.append(torch.logsumexp(sc, dim=-1))         # (1,n_rep,N)
            lse_stack = torch.stack([
                (x if x is not None
                 else torch.full_like(seg_lse[0], float("-inf")))
                for x in seg_lse], dim=0)
            m_tot = torch.logsumexp(lse_stack, dim=0)               # (1,n_rep,N)
            o_h = torch.zeros_like(qs)
            for o_seg, l in zip(seg_o, seg_lse):
                if o_seg is None:
                    continue
                w = torch.exp(l - m_tot).unsqueeze(-1)              # (1,n_rep,N,1)
                o_h = o_h + w * o_seg
            out[0, h * n_rep:(h + 1) * n_rep] = o_h
        return out

    def _make_forward(self, layer_idx: int, attn):
        import transformers.models.qwen2.modeling_qwen2 as mq
        orig_rope = mq.apply_rotary_pos_emb
        patcher = self

        def fwd(hidden_states, position_embeddings=None, attention_mask=None,
                past_key_values=None, cache_position=None, **kwargs):
            input_shape = hidden_states.shape[:-1]
            hidden_shape = (*input_shape, -1, attn.head_dim)
            q = attn.q_proj(hidden_states).view(hidden_shape).transpose(1, 2)
            k = attn.k_proj(hidden_states).view(hidden_shape).transpose(1, 2)
            v = attn.v_proj(hidden_states).view(hidden_shape).transpose(1, 2)
            cos, sin = position_embeddings
            q, k = orig_rope(q, k, cos, sin)
            if past_key_values is not None:
                k, v = past_key_values.update(k, v, layer_idx,
                                              {"sin": sin, "cos": cos, "cache_position": cache_position})
            n_rep = q.shape[1] // k.shape[1]   # GQA：Q 头 / KV 头
            N = q.shape[-2]
            causal_mask = None
            if N > 1:      # prefill：构造压缩 KV 集
                if patcher.mode in ("topk", "hybrid"):
                    imp = attention_importance(q, k, attn.scaling, n_rep)  # (B,Hkv,N)
                    B, Hkv, _, hd = k.shape
                    sel_list, grp_list = [], []
                    for h in range(Hkv):
                        sg, st = select_topk_per_group(
                            imp[0, h].detach().cpu().numpy(), patcher.labels,
                            patcher.G, patcher.topk_per_group)
                        grp_list.append(torch.as_tensor(sg, device=k.device))
                        sel_list.append(torch.as_tensor(st, device=k.device))
                    idx_sel = torch.stack(sel_list, dim=0).unsqueeze(0)     # (B,Hkv,S)
                    grp_sel = torch.stack(grp_list, dim=0).unsqueeze(0)     # (B,Hkv,S)
                    patcher.slot_pos[layer_idx] = idx_sel
                    if patcher.mode == "hybrid":
                        # E1.5 分段注意力（sink/local/mid 独立计算 + logsumexp 合并）
                        o = patcher._hybrid_attention(
                            layer_idx, q, k, v, idx_sel, grp_sel, n_rep, attn.scaling)
                        km = torch.gather(k, 2, idx_sel.unsqueeze(-1).expand(-1, -1, -1, hd))
                        vm = torch.gather(v, 2, idx_sel.unsqueeze(-1).expand(-1, -1, -1, hd))
                        patcher.kmean[layer_idx] = km   # decode 用（联合 softmax，新 token 不属任何群）
                        patcher.vmean[layer_idx] = vm
                        patcher.buf_k[layer_idx] = k[:, :, :0]
                        patcher.buf_v[layer_idx] = v[:, :, :0]
                        o = o.transpose(1, 2).reshape(*input_shape, -1).contiguous()
                        o = attn.o_proj(o)
                        return o, None
                    km = torch.gather(k, 2, idx_sel.unsqueeze(-1).expand(-1, -1, -1, hd))
                    vm = torch.gather(v, 2, idx_sel.unsqueeze(-1).expand(-1, -1, -1, hd))
                    # prefill 因果掩码：query i 只能访问槽位真实位置 ≤ i 的 token
                    spos = idx_sel.repeat_interleave(n_rep, dim=1)[0]      # (Hq,S)
                    qpos = torch.arange(N, device=q.device)
                    causal_mask = spos[None, :, None, :] > qpos[None, None, :, None]
                elif patcher.mode == "weighted":
                    imp = attention_importance(q, k, attn.scaling, n_rep)  # (B,Hkv,N)
                    G = patcher.G
                    idx = torch.as_tensor(patcher.labels, device=k.device)
                    wsum = torch.zeros(*imp.shape[:-1], G, dtype=imp.dtype, device=imp.device)
                    wsum.index_add_(2, idx, imp)                            # (B,Hkv,G)
                    w = imp / wsum[:, :, idx].clamp(min=1e-9)               # 群内归一化
                    km = scatter_wmean(k, w, patcher.labels, G)
                    vm = scatter_wmean(v, w, patcher.labels, G)
                    if patcher.kmean_renorm == "member":
                        km = renorm_to_member_avg(km, k, patcher.labels, G)
                else:  # flat_mean
                    km = scatter_mean(k, patcher.labels, patcher.G)
                    vm = scatter_mean(v, patcher.labels, patcher.G)
                    if patcher.kmean_renorm == "member":
                        km = renorm_to_member_avg(km, k, patcher.labels, patcher.G)
                patcher.kmean[layer_idx] = km
                patcher.vmean[layer_idx] = vm
                patcher.buf_k[layer_idx] = k[:, :, :0]
                patcher.buf_v[layer_idx] = v[:, :, :0]
            else:                    # decode：压缩槽位 + 新 token 缓冲
                patcher.buf_k[layer_idx] = torch.cat([patcher.buf_k[layer_idx], k], dim=2)
                patcher.buf_v[layer_idx] = torch.cat([patcher.buf_v[layer_idx], v], dim=2)
                km = torch.cat([patcher.kmean[layer_idx], patcher.buf_k[layer_idx]], dim=2)
                vm = torch.cat([patcher.vmean[layer_idx], patcher.buf_v[layer_idx]], dim=2)
            km = repeat_kv(km, n_rep)
            vm = repeat_kv(vm, n_rep)
            scores = torch.matmul(q, km.transpose(-1, -2)) * attn.scaling
            if causal_mask is not None:
                scores = scores.masked_fill(causal_mask, float("-inf"))
            if patcher.group_score_prior == "logcount" and patcher.mode not in ("topk", "hybrid"):
                # 群规模先验：群均值槽加 log(m)，使其概率 ≈ 成员概率之和
                # （v0.2.0 实测：无此修正时 15 槽 softmax 温度剧变，群均值槽
                #  质量被精确槽系统性挤占，flat+sink cos 仅 0.26；修正后 0.47）
                lc = torch.as_tensor(patcher.slot_logcount, device=q.device)
                if km.shape[-2] > lc.shape[0]:    # decode：尾部新 token 计数为 1
                    pad = km.shape[-2] - lc.shape[0]
                    lc = torch.cat([lc, torch.zeros(pad, device=q.device)])
                scores = scores + lc[None, None, None, :]
            attn_w = torch.softmax(scores, dim=-1)
            o = torch.matmul(attn_w, vm)
            o = o.transpose(1, 2).reshape(*input_shape, -1).contiguous()
            o = attn.o_proj(o)
            return o, None
        return fwd

    def install(self):
        n_layers = self.model.config.num_hidden_layers
        layers = self.compress_layers if self.compress_layers is not None \
            else list(range(n_layers))
        for i in layers:
            attn = self.model.model.layers[i].self_attn
            self._orig_forwards[i] = attn.forward
            attn.forward = self._make_forward(i, attn)

    def uninstall(self):
        for i, fwd in self._orig_forwards.items():
            self.model.model.layers[i].self_attn.forward = fwd
        self._orig_forwards = {}


# ---------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------

def hidden_from_capture(cap: Dict[str, torch.Tensor], group_layer: str) -> np.ndarray:
    """从捕获中取 GSC 分组用的隐状态（**原始幅值** numpy；归一化由
    metric_transform 按度量决定，v0.2.0 起不再强制 L2 归一化）。"""
    if group_layer == "final":
        H = cap["final_hidden"][0].numpy().astype(np.float32)
    else:
        gl = int(group_layer)
        H = cap["hidden_states"][gl + 1][0].numpy().astype(np.float32)
    return H


def smooth_hidden(H: np.ndarray, window: int, renormalize: bool = True) -> np.ndarray:
    """因果滑动窗口平滑（抑制位置特异性噪声）。

    动机：首轮实测发现真实隐空间呈「话题 × 位置」多尺度结构——同一段落
    第 1 次与第 5 次出现的隐状态各自成簇。对分组决策用的隐状态做局部
    平均，可在不改变 KV 内容的前提下压低位置噪声，让「同语义」token
    的相似度上升。window=1 即不平滑。
    renormalize=False 时保留平滑后的原始幅值（metric="l2" 需要）。
    """
    if window <= 1:
        return H if renormalize else H
    N, d = H.shape
    w = min(window, N)
    csum = np.cumsum(H, axis=0)
    out = np.empty_like(H)
    for i in range(N):
        lo = max(0, i - w + 1)
        seg = csum[i] - (csum[lo - 1] if lo > 0 else 0.0)
        out[i] = seg / (i - lo + 1)
    if not renormalize:
        return out
    return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-12)


def load_model(model_path: str, dtype: str = "float32"):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    dt = {"float32": torch.float32, "bfloat16": torch.bfloat16, "float16": torch.float16}[dtype]
    tok = AutoTokenizer.from_pretrained(model_path)
    model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=dt)
    model.eval()
    return tok, model


def main() -> int:
    ap = argparse.ArgumentParser(description="SemanticKV-Reorg 真实模型慢速验证器")
    ap.add_argument("--model", default="Qwen/Qwen2.5-0.5B",
                    help="HF 模型 id 或本地路径")
    ap.add_argument("--hf-endpoint", default=os.environ.get("HF_ENDPOINT", ""),
                    help="如 https://hf-mirror.com（国内镜像）")
    ap.add_argument("--dtype", default="float32", choices=["float32", "bfloat16", "float16"])
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--ctx", type=int, default=2048)
    ap.add_argument("--paragraphs", type=int, default=8)
    ap.add_argument("--repeats", type=int, default=6)
    ap.add_argument("--target-layer", type=int, default=12)
    ap.add_argument("--group-layer", default="final",
                    help="GSC 分组用哪层隐状态：final（默认）或层号，如 12")
    ap.add_argument("--smooth-window", type=int, default=8,
                    help="分组用隐状态的因果滑动窗口平滑宽度（1=不平滑；"
                         "实测 w=8 可把互 kNN 互通率从 0.50 提到 0.85）")
    ap.add_argument("--reps-per-group", type=int, default=4,
                    help="V3 温和压缩：每群保留的代表 token 数")
    ap.add_argument("--strategy", default="v15", choices=["v1", "v15"],
                    help="v1 = v0.1.x 基线（flat_mean/全层/cosine/无 sink，用于复现对比）；"
                         "v15 = DeepSeek 保护性策略（topk+centered+中层压缩+sink/window 保护）。"
                         "下方各分项参数显式传入时优先于策略默认值")
    ap.add_argument("--compress-mode", default=None,
                    choices=["flat_mean", "weighted", "topk", "hybrid"],
                    help="V1.5 压缩模式：flat_mean（v0.1.x 群均值）| weighted（注意力入度 "
                         "加权群均值）| topk（每群保留入度 top-K 真实 KV）")
    ap.add_argument("--topk-per-group", type=int, default=None,
                    help="topk 模式每群保留的真实 KV 数（DeepSeek 建议 K=2 或 4）")
    ap.add_argument("--sink-tokens", type=int, default=None,
                    help="Attention Sink 保护：前 N 个 token 独占槽位不压缩（DeepSeek 建议 4）")
    ap.add_argument("--window-tokens", type=int, default=None,
                    help="近期局部窗口保护：后 N 个 token 独占槽位不压缩（DeepSeek 建议 64）")
    ap.add_argument("--mid-scope", default="blockdiag", choices=["blockdiag", "global"],
                    help="hybrid 模式中段 query 视野（DS-20261018-006）："
                         "blockdiag=中段 query 只见本群代表（特权/读出位豁免）；"
                         "global=所有 query 见全部中段代表（即 topk 语义，消融对照）")
    ap.add_argument("--compress-layers", default=None,
                    help="层级选择性压缩：'all' | '6-10'（1-indexed 含端点）| "
                         "'0.25-0.75'（层数比例，DeepSeek 建议只压中层）")
    ap.add_argument("--metric", default=None, choices=["cosine", "centered", "l2"],
                    help="分组相似度度量（DeepSeek 指令 3：各向异性修正）："
                         "cosine（v0.1.x，τ 甜点被压到 0.97）| "
                         "centered（减全局均值再归一化，主推）| "
                         "l2（幅值感知 2ab/(|a|²+|b|²)）")
    ap.add_argument("--kmean-renorm", default=None, choices=["none", "member"],
                    help="群均值 K 范数重标定到成员平均范数：修正「精确槽 vs 群均值槽」"
                         "的 softmax 系统性偏置（v0.2.0 实测群均值范数仅 0.865×，"
                         "score~20 时偏置 e^2.8≈16×）；V 不重标定")
    ap.add_argument("--group-score-prior", default=None, choices=["none", "logcount"],
                    help="群均值槽的 log(群规模) 分数先验：使群概率 ≈ 成员概率之和"
                         "（v0.2.0 实测：无修正时少槽 softmax 温度剧变，群均值槽质量"
                         "被精确槽挤占，flat+sink cos 0.26 -> 修正后 0.47）")
    ap.add_argument("--k", type=int, default=16)
    ap.add_argument("--k-grid", default="8,16",
                    help="k 网格（与 tau 网格交叉扫描；E0 校准的 (k,tau) 联合寻优）")
    ap.add_argument("--tau-grid", default="0.1,0.2,0.3,0.5,0.7,0.9,0.93,0.95,0.97",
                    help="tau 网格；centered 度量下相似度分布下移，需覆盖低 tau 区")
    ap.add_argument("--top-p", type=int, default=10)
    ap.add_argument("--needle", default="请注意，一个重要事实：小王最喜欢的数字是 42。")
    ap.add_argument("--needle-positions", default="0.5",
                    help="逗号分隔的 needle 相对位置，如 0.25,0.5,0.75")
    ap.add_argument("--question", default="小王最喜欢的数字是多少？请只回答数字：")
    ap.add_argument("--max-new-tokens", type=int, default=8)
    ap.add_argument("--out", default=str(ROOT / "results" / "real_model_validation.json"))
    ap.add_argument("--skip-needle", action="store_true")
    ap.add_argument("--baseline-only", action="store_true",
                    help="只跑原始模型 needle 基线（Kaggle 上机流程第一步：先确认 7B "
                         "无压缩命中率正常，再挂载压缩策略）")
    args = ap.parse_args()

    # ---- 策略捆绑：分项参数未显式传入时用策略默认值 ----
    if args.strategy == "v1":
        strat = {"compress_mode": "flat_mean", "metric": "cosine",
                 "compress_layers": "all", "sink_tokens": 0, "window_tokens": 0,
                 "topk_per_group": 4, "kmean_renorm": "none",
                 "group_score_prior": "none"}
    else:  # v15：DeepSeek 保护性策略
        # renorm=none：2048 诊断显示先验开启时 renorm=member 过度校正
        # （flat+sink 0.4333→0.4103、weighted 0.4866→0.4612）；先验与 renorm
        # 是对同一 softmax 温度问题的两种补偿，叠加反而过冲。topk 模式本就不
        # 使用 renorm（槽即真实 token），故默认关闭。
        strat = {"compress_mode": "topk", "metric": "centered",
                 "compress_layers": "0.25-0.75", "sink_tokens": 4,
                 "window_tokens": 64, "topk_per_group": 4,
                 "kmean_renorm": "none", "group_score_prior": "logcount"}
    for key, val in strat.items():
        if getattr(args, key) is None:
            setattr(args, key, val)

    if args.hf_endpoint:
        os.environ["HF_ENDPOINT"] = args.hf_endpoint
    import torch
    torch.set_num_threads(int(os.environ.get("SEMANTICKV_THREADS", "8")))

    from mock_grouper import MemTracker, MockSemanticGrouper, _knn
    from mock_relation import MockRelationGraph
    from eval_runner import EdgeSet

    report: Dict = {"config": vars(args), "stages": {}}
    t_start = time.time()

    # ---- 0. 加载模型 ----
    print(f"[load] {args.model} dtype={args.dtype}", flush=True)
    tok, model = load_model(args.model, args.dtype)
    model.to(args.device)
    n_params = sum(p.numel() for p in model.parameters())
    print(f"[load] 参数量 {n_params/1e6:.1f}M，层数 {model.config.num_hidden_layers}，"
          f"hidden {model.config.hidden_size}", flush=True)
    report["model"] = {"path": args.model, "params_m": round(n_params / 1e6, 1),
                       "n_layers": model.config.num_hidden_layers,
                       "hidden": model.config.hidden_size}

    # ---- 1. 结构化输入 + 标准 prefill（捕获） ----
    print(f"[prefill] 构造 {args.ctx} token 结构化上下文 "
          f"({args.paragraphs} 话题 × {args.repeats} 次重复)", flush=True)
    input_ids, truth, truth_occ, _, _ = build_structured_input(
        tok, args.paragraphs, args.repeats, args.ctx, seed=7)
    N = input_ids.shape[1]
    print(f"[prefill] 实际 N={N} token", flush=True)

    icept = PrefillInterceptor(model, args.target_layer)
    icept.install()
    input_ids = input_ids.to(next(model.parameters()).device)
    with MemTracker() as mt:
        icept.run_prefill(input_ids,
                          need_hidden_states=(args.group_layer != "final"))
    prefill_peak = mt.peak_mb
    print(f"[prefill] 完成，峰值 RSS {prefill_peak:.0f}MB", flush=True)

    H_raw = hidden_from_capture(icept.cap, args.group_layer)
    if args.group_layer != "final":
        print(f"[gsc-scan] 使用第 {args.group_layer} 层隐状态分组", flush=True)
    # v0.2.0 各向异性修正：度量变换（centered/l2/cosine）
    H = metric_transform(H_raw, args.metric)
    H = smooth_hidden(H, args.smooth_window,
                      renormalize=(args.metric != "l2"))
    if args.smooth_window > 1:
        print(f"[gsc-scan] 隐状态滑动窗口平滑 w={args.smooth_window}，"
              f"度量={args.metric}", flush=True)

    # ---- 2. (k × tau) 联合扫描：真实隐状态上的 GSC 质量（E0 校准演练） ----
    # 首轮实测教训：真实隐空间是「话题 × 位置」的多尺度流形——
    # 同话题不同出现位置可能各自成簇（ARI_topic 低但 ARI_occurrence 高），
    # 因此同时报告两个 ground truth 的 ARI。
    # baseline-only：跳过 (k,τ) 扫描（Kaggle 流程第一步只验证原始 needle 基线）
    k_grid = [] if args.baseline_only else [int(x) for x in args.k_grid.split(",")]
    tau_grid = [] if args.baseline_only else [float(x) for x in args.tau_grid.split(",")]
    scan = []
    for kk in k_grid:
        idx, sim = _knn(H, kk, backend="torch", metric=args.metric)
        mut = mutual_knn_rate(idx)
        same = (truth[:N][idx] == truth[:N][:, None])
        purity = float(same.mean())
        sim_q = np.percentile(sim, [5, 50, 95]).round(4).tolist()
        print(f"[gsc-scan] k={kk}: 互 kNN 互通率={mut:.3f}  kNN 纯度={purity:.3f}  "
              f"top-k 相似度 p5/p50/p95={sim_q}", flush=True)
        for tau in tau_grid:
            ga = MockSemanticGrouper(k=kk, tau=tau, metric=args.metric).fit(H)
            ari_t = adjusted_rand_index(truth[:N], ga.labels[:N])
            ari_o = adjusted_rand_index(truth_occ[:N], ga.labels[:N])
            sizes = ga.sizes
            frag = float(sizes[sizes < 8].sum() / N) if len(sizes) else 1.0
            scan.append({"k": kk, "tau": tau, "n_groups": int(ga.n_groups),
                         "ari_topic": round(ari_t, 4), "ari_occurrence": round(ari_o, 4),
                         "fragmentation": round(frag, 4),
                         "intra_sim": round(float(ga.meta["intra_sim"]), 4)})
        # 每个 k 打印 ARI_topic 最高的一行
        rows_k = [r for r in scan if r["k"] == kk]
        bk = max(rows_k, key=lambda r: r["ari_topic"])
        print(f"  k={kk} 最优: tau={bk['tau']:.2f} G={bk['n_groups']} "
              f"ARI_topic={bk['ari_topic']:.4f} ARI_occ={bk['ari_occurrence']:.4f}", flush=True)
    report["stages"]["gsc_scan"] = {"k_grid": k_grid, "tau_grid": tau_grid, "rows": scan}

    # 选定配置：每个 k 取「ARI_topic 最高且碎片率 <= 0.10」的 tau；无合规则取 ARI 最高
    selected = []
    for kk in k_grid:
        rows_k = [r for r in scan if r["k"] == kk and r["fragmentation"] <= 0.10]
        if not rows_k:
            rows_k = [r for r in scan if r["k"] == kk]
        if rows_k:
            selected.append(max(rows_k, key=lambda r: (r["ari_topic"], r["tau"])))
    needle_cfg = (max(selected, key=lambda r: (r["ari_topic"], -r["n_groups"]))
                  if selected else None)
    if needle_cfg is not None:
        print(f"[gsc-scan] needle 测试采用配置 k={needle_cfg['k']} tau={needle_cfg['tau']} "
              f"(G={needle_cfg['n_groups']}, ARI_topic={needle_cfg['ari_topic']})", flush=True)
    elif args.baseline_only:
        print("[baseline-only] 跳过扫描与压缩，仅验证原始 needle 基线", flush=True)

    # ---- 3. 单层 KV 重组保真度：V0 重排不变性 + V1/V2 压缩率-保真度曲线 ----
    print(f"[fidelity] 目标层 L{args.target_layer}", flush=True)
    with torch.no_grad():
        q = icept.cap["q_post"]                       # (1, 14, N, hd)
        k_post = icept.cap["k_post"]                  # (1, 2, N, hd)
        v_raw = icept.cap["v_raw"]                    # (1, N, 2*hd)
        attn = model.model.layers[args.target_layer].self_attn
        n_rep = q.shape[1] // k_post.shape[1]
        hd = q.shape[-1]
        v = v_raw.view(1, N, k_post.shape[1], hd).transpose(1, 2)   # (1,2,N,hd)
        ref = icept.cap["attn_out"]                   # (1, N, hidden) 原始注意力输出
        scaling = attn.scaling
        kv_bytes_per_slot = 2 * k_post.shape[1] * hd * q.element_size()

        def finish(o: torch.Tensor) -> torch.Tensor:
            o = o.transpose(1, 2).reshape(1, N, -1).contiguous()
            return attn.o_proj(o)

        # V0：重排不变性（群序 gather/scatter，数学上应等价；与 labels 无关）
        if selected:
            labels0 = MockSemanticGrouper(k=selected[0]["k"], tau=selected[0]["tau"],
                                          metric=args.metric).fit(H).labels[:N]
            perm = np.argsort(labels0, kind="stable")
            perm_t = torch.as_tensor(perm, device=q.device)
            K2 = repeat_kv(k_post, n_rep)[:, :, perm_t, :]
            V2 = repeat_kv(v, n_rep)[:, :, perm_t, :]
            pos = torch.arange(N, device=q.device)
            causal = pos[perm_t][None, None, None, :] > pos[None, None, :, None]
            scores = torch.matmul(q, K2.transpose(-1, -2)) * scaling
            scores = scores.masked_fill(causal, float("-inf"))
            cos_v0 = cosine_per_position(finish(torch.matmul(torch.softmax(scores, dim=-1), V2)), ref)
        else:
            cos_v0 = None

        # 每个选定配置：V1/V2/V3（v0.1.x 基线，保持可比）+ V1.5 家族（v0.2.0）
        sweep = []
        for cfg in selected:
            ga = MockSemanticGrouper(k=cfg["k"], tau=cfg["tau"], metric=args.metric).fit(H)
            labels = ga.labels[:N]
            G = ga.n_groups
            km = repeat_kv(scatter_mean(k_post, labels, G), n_rep)
            vm = repeat_kv(scatter_mean(v, labels, G), n_rep)
            scores = torch.matmul(q, km.transpose(-1, -2)) * scaling
            cos_v1 = cosine_per_position(finish(torch.matmul(torch.softmax(scores, dim=-1), vm)), ref)
            # V2：关系图掩码（同群 + top-p 边）
            cents = np.stack([H[labels == g].mean(axis=0) for g in range(G)])
            rg = MockRelationGraph(top_p=args.top_p)
            es = rg.build_full(cents.astype(np.float32), turn=0)
            edge_mask = torch.zeros(G, G, dtype=torch.bool)
            for s, d in zip(es.src.tolist(), es.dst.tolist()):
                edge_mask[s, d] = True
                edge_mask[d, s] = True
            allowed = edge_mask | torch.eye(G, dtype=torch.bool)
            qg_allowed = allowed[torch.as_tensor(labels, device=q.device)]     # (N, G)
            scores = torch.matmul(q, km.transpose(-1, -2)) * scaling
            scores = scores.masked_fill(~qg_allowed[None, None, :, :], float("-inf"))
            cos_v2 = cosine_per_position(finish(torch.matmul(torch.softmax(scores, dim=-1), vm)), ref)

            # V3：每群 r 个代表 token（与群质心余弦最高者），全体可访问——温和压缩档位
            r = max(1, args.reps_per_group)
            cents_g = np.stack([H[labels == g].mean(axis=0) for g in range(G)])
            rep_idx: List[int] = []
            for g in range(G):
                members = np.flatnonzero(labels == g)
                if len(members) <= r:
                    rep_idx.extend(members.tolist())
                else:
                    sims_g = H[members] @ cents_g[g]
                    rep_idx.extend(members[np.argsort(-sims_g)[:r]].tolist())
            rep_t = torch.as_tensor(np.asarray(rep_idx, dtype=np.int64), device=q.device)
            Kr = repeat_kv(k_post, n_rep)[:, :, rep_t, :]
            Vr = repeat_kv(v, n_rep)[:, :, rep_t, :]
            scores = torch.matmul(q, Kr.transpose(-1, -2)) * scaling
            cos_v3 = cosine_per_position(finish(torch.matmul(torch.softmax(scores, dim=-1), Vr)), ref)

            # ================= v0.2.0 V1.5 家族（DeepSeek 评审）=================
            # 注意力入度（target 层因果 softmax 列和），topk/weighted 共用
            imp = attention_importance(q, k_post, scaling, n_rep)      # (1, Hkv, N)
            Hkv = k_post.shape[1]
            # sink/window 特权扩展后的 labels
            lab_p = apply_privileged_tokens(labels, args.sink_tokens,
                                            args.window_tokens, force_bos=False)
            Gp = int(lab_p.max()) + 1
            n_priv = int(Gp - G)
            idx_p = torch.as_tensor(lab_p, device=q.device)
            qpos = torch.arange(N, device=q.device)

            # V1.5-a flat_mean + sink（位置无关口径，与 V1 可比；K 均值范数重标定）
            km_p_raw = scatter_mean(k_post, lab_p, Gp)
            if args.kmean_renorm == "member":
                km_p_raw = renorm_to_member_avg(km_p_raw, k_post, lab_p, Gp)
            km_p = repeat_kv(km_p_raw, n_rep)
            vm_p = repeat_kv(scatter_mean(v, lab_p, Gp), n_rep)
            scores = torch.matmul(q, km_p.transpose(-1, -2)) * scaling
            if args.group_score_prior == "logcount":
                cnt_p = np.bincount(lab_p, minlength=Gp).astype(np.float32)
                lc_p = np.log(np.maximum(cnt_p, 1.0))
                scores = scores + torch.as_tensor(lc_p, device=q.device)[None, None, None, :]
            cos_flat_sink = cosine_per_position(
                finish(torch.matmul(torch.softmax(scores, dim=-1), vm_p)), ref)

            # V1.5-a' flat_mean + sink + 因果一致掩码（群最早成员位置 ≤ i 才可访问）
            gmin = np.zeros(Gp, dtype=np.int64)
            for g in range(Gp):
                mem = np.flatnonzero(lab_p == g)
                gmin[g] = mem.min() if mem.size else 0
            allowed_p = (torch.as_tensor(gmin, device=q.device)[None, None, None, :]
                         <= qpos[None, None, :, None])                    # (1,1,N,Gp)
            scores = torch.matmul(q, km_p.transpose(-1, -2)) * scaling
            scores = scores.masked_fill(~allowed_p, float("-inf"))
            cos_flat_sink_causal = cosine_per_position(
                finish(torch.matmul(torch.softmax(scores, dim=-1), vm_p)), ref)

            # V1.5-b weighted + sink（注意力入度加权群均值；K 均值范数重标定）
            wsum = torch.zeros(1, Hkv, Gp, dtype=imp.dtype, device=imp.device)
            wsum.index_add_(2, idx_p, imp)
            w = imp / wsum[:, :, idx_p].clamp(min=1e-9)                   # (1,Hkv,N)
            km_w_raw = scatter_wmean(k_post, w, lab_p, Gp)
            if args.kmean_renorm == "member":
                km_w_raw = renorm_to_member_avg(km_w_raw, k_post, lab_p, Gp)
            km_w = repeat_kv(km_w_raw, n_rep)
            vm_w = repeat_kv(scatter_wmean(v, w, lab_p, Gp), n_rep)
            scores = torch.matmul(q, km_w.transpose(-1, -2)) * scaling
            if args.group_score_prior == "logcount":
                cnt_p = np.bincount(lab_p, minlength=Gp).astype(np.float32)
                lc_p = np.log(np.maximum(cnt_p, 1.0))
                scores = scores + torch.as_tensor(lc_p, device=q.device)[None, None, None, :]
            cos_weighted_sink = cosine_per_position(
                finish(torch.matmul(torch.softmax(scores, dim=-1), vm_w)), ref)

            # V1.5-a topk + sink（每群入度 top-K 真实 KV + 槽位位置因果掩码）
            K = max(1, args.topk_per_group)
            sel_list = []
            for h in range(Hkv):
                _, tsel = select_topk_per_group(imp[0, h].numpy(), lab_p, Gp, K)
                sel_list.append(torch.as_tensor(tsel, device=q.device))
            idx_sel = torch.stack(sel_list, dim=0).unsqueeze(0)           # (1,Hkv,S)
            S = idx_sel.shape[-1]
            Kt = torch.gather(k_post, 2, idx_sel.unsqueeze(-1).expand(-1, -1, -1, hd))
            Vt = torch.gather(v, 2, idx_sel.unsqueeze(-1).expand(-1, -1, -1, hd))
            Kt_e = repeat_kv(Kt, n_rep)
            Vt_e = repeat_kv(Vt, n_rep)
            spos = idx_sel.repeat_interleave(n_rep, dim=1)[0]             # (Hq,S)
            causal_t = spos[None, :, None, :] > qpos[None, None, :, None]  # (1,Hq,N,S)
            scores = torch.matmul(q, Kt_e.transpose(-1, -2)) * scaling
            scores = scores.masked_fill(causal_t, float("-inf"))
            cos_topk_sink = cosine_per_position(
                finish(torch.matmul(torch.softmax(scores, dim=-1), Vt_e)), ref)

            row = {"k": cfg["k"], "tau": cfg["tau"], "n_groups": G,
                   "compression_ratio": round(N / max(G, 1), 1),
                   "v1_group_mean": cos_v1, "v2_relation_masked": cos_v2,
                   "v3_reps": cos_v3, "v3_slots": len(rep_idx),
                   "v3_compression": round(N / max(len(rep_idx), 1), 1),
                   "v15": {
                       "flat_mean_sink": cos_flat_sink,
                       "flat_mean_sink_causal": cos_flat_sink_causal,
                       "weighted_sink": cos_weighted_sink,
                       "topk_sink": cos_topk_sink,
                       "n_priv": n_priv, "topk_slots": int(S),
                       "flat_sink_compression": round(N / max(Gp, 1), 1),
                       "topk_compression": round(N / max(S, 1), 1),
                   }}
            sweep.append(row)
            v15 = row["v15"]
            print(f"  k={cfg['k']} tau={cfg['tau']}: G={G:4d} | "
                  f"V1({row['compression_ratio']:.0f}x) cos={cos_v1['mean']:.4f} | "
                  f"V2 cos={cos_v2['mean']:.4f} | "
                  f"V3({row['v3_compression']:.0f}x) cos={cos_v3['mean']:.4f} | "
                  f"V1.5 flat+sink({v15['flat_sink_compression']:.0f}x) "
                  f"cos={cos_flat_sink['mean']:.4f}/causal {cos_flat_sink_causal['mean']:.4f} | "
                  f"weighted {cos_weighted_sink['mean']:.4f} | "
                  f"topk({v15['topk_compression']:.0f}x) {cos_topk_sink['mean']:.4f}",
                  flush=True)

    fidelity = {
        "target_layer": args.target_layer, "n_tokens": N,
        "v0_reorder_invariance": cos_v0,
        "kv_bytes_original_per_layer": N * kv_bytes_per_slot,
        "sweep": sweep,
    }
    if cos_v0 is not None:
        print(f"  V0 重排不变性: cos mean={cos_v0['mean']:.6f} min={cos_v0['min']:.6f}",
              flush=True)
    report["stages"]["fidelity"] = fidelity
    icept.uninstall()

    # ---- 4. needle 检索：原始 vs 全模型压缩（E0 相位敏感性玩具版） ----
    def needle_hit(answer: str, target: str = "42") -> bool:
        """归一化后的精确匹配（避免 '1.414213' 含 '42' 的子串误判）。"""
        import re
        ans = answer.strip().strip("。，.,!！?？:：\"'""")
        nums = re.findall(r"\d+(?:\.\d+)?", ans)
        return target in nums or ans == target

    needle_results = []
    if not args.skip_needle:
        positions = [float(x) for x in args.needle_positions.split(",")]
        for frac in positions:
            ids_n, _, _, npos, qstart = build_structured_input(
                tok, args.paragraphs, args.repeats, args.ctx,
                needle=args.needle, needle_frac=frac,
                question=args.question, seed=7)
            Nn = ids_n.shape[1]
            ids_n = ids_n.to(next(model.parameters()).device)
            print(f"[needle] 位置 {frac:.2f} (token {npos})，N={Nn}", flush=True)
            row = {"position_frac": frac, "needle_token_pos": int(npos)}

            # 4a. 原始模型（Kaggle 流程第一步：先确认无压缩基线命中率正常）
            attn_mask = torch.ones_like(ids_n)
            if torch.cuda.is_available():
                torch.cuda.reset_peak_memory_stats()
            t_gen_o0 = time.time()
            with torch.no_grad():
                out_o = model.generate(ids_n, attention_mask=attn_mask,
                                       max_new_tokens=args.max_new_tokens,
                                       do_sample=False, use_cache=True,
                                       pad_token_id=tok.eos_token_id or tok.pad_token_id)
            t_gen_orig_s = time.time() - t_gen_o0
            if torch.cuda.is_available():
                row["orig_cuda_peak_allocated_mb"] = round(
                    torch.cuda.max_memory_allocated() / 1e6, 1)
            ans_o = tok.decode(out_o[0][Nn:], skip_special_tokens=True)
            row["original_answer"] = ans_o.strip()
            row["original_hit"] = needle_hit(ans_o)

            # 4b. 压缩模型（--baseline-only 时跳过：只验证原始基线）
            if args.baseline_only or needle_cfg is None:
                needle_results.append(row)
                print(f"  原始: {row['original_answer']!r} "
                      f"(hit={row['original_hit']}) [baseline-only]", flush=True)
                continue

            # 4b-0. 标准 prefill 拿分组（度量/平滑与主扫描一致）
            icept2 = PrefillInterceptor(model, args.target_layer)
            icept2.install()
            t_pre_o0 = time.time()
            icept2.run_prefill(ids_n,
                               need_hidden_states=(args.group_layer != "final"))
            t_pre_orig_s = time.time() - t_pre_o0
            Hn = smooth_hidden(metric_transform(
                hidden_from_capture(icept2.cap, args.group_layer), args.metric),
                args.smooth_window, renormalize=(args.metric != "l2"))
            gan = MockSemanticGrouper(k=needle_cfg["k"], tau=needle_cfg["tau"],
                                      metric=args.metric).fit(Hn)
            icept2.uninstall()

            n_layers = model.config.num_hidden_layers
            compress_layers = parse_layer_spec(args.compress_layers, n_layers)
            row["strategy"] = {
                "mode": args.compress_mode, "metric": args.metric,
                "compress_layers": args.compress_layers,
                "n_compressed_layers": len(compress_layers),
                "sink_tokens": args.sink_tokens, "window_tokens": args.window_tokens,
                "topk_per_group": args.topk_per_group, "mid_scope": args.mid_scope,
            }

            # 4b-1. 全模型压缩 prefill 的端到端保真度（残差流主导最终隐状态，
            #       单层注意力余弦会低估真实影响，必须看最终隐状态/logits）
            comp_hidden = {}
            def cap_norm(_m, _inp, out):
                comp_hidden["h"] = out.detach()
            h_norm = model.model.norm.register_forward_hook(cap_norm)
            patcher = CompressedAttentionPatcher(
                model, mode=args.compress_mode, topk_per_group=args.topk_per_group,
                sink_tokens=args.sink_tokens, window_tokens=args.window_tokens,
                compress_layers=compress_layers, kmean_renorm=args.kmean_renorm,
                group_score_prior=args.group_score_prior, mid_scope=args.mid_scope)
            patcher.set_groups(gan.labels[:Nn], Nn)
            patcher.install()
            try:
                if torch.cuda.is_available():
                    torch.cuda.reset_peak_memory_stats()
                t_pre_c0 = time.time()
                with torch.no_grad():
                    out_c_pre = model(ids_n, attention_mask=attn_mask, use_cache=False)
                t_pre_compress_s = time.time() - t_pre_c0
            finally:
                patcher.uninstall()
                h_norm.remove()
            if torch.cuda.is_available():
                row["comp_cuda_peak_allocated_mb"] = round(
                    torch.cuda.max_memory_allocated() / 1e6, 1)
            h_cos = cosine_per_position(comp_hidden["h"], icept2.cap["final_hidden"])
            agree = float((out_c_pre.logits.argmax(-1) ==
                           icept2.cap["logits"].argmax(-1)).float().mean())
            # 内存卫生：logits 各占 ~1.3GB（N×vocab×4B），立即释放
            del out_c_pre
            icept2.cap["logits"] = None
            icept2.cap["hidden_states"] = None
            row["prefill_fidelity"] = {"final_hidden_cos": h_cos,
                                       "logits_argmax_agreement": round(agree, 4)}

            # KV 显存记账（解析值：真实 Triton 实现中压缩层不落全量 cache；
            # 慢速验证器为保持 RoPE 位置正确仍写全量 cache，故以解析值为准）
            head_dim = model.config.hidden_size // model.config.num_attention_heads
            esize = next(model.parameters()).element_size()
            kv_slot_bytes = 2 * model.config.num_key_value_heads * head_dim * esize
            slots = patcher.kv_slots()
            kv_orig_mb = kv_slot_bytes * Nn * n_layers / 1e6
            kv_comp_mb = kv_slot_bytes * (slots * len(compress_layers)
                                          + Nn * (n_layers - len(compress_layers))) / 1e6
            row["kv_bytes"] = {
                "original_mb": round(kv_orig_mb, 1),
                "compressed_mb": round(kv_comp_mb, 1),
                "slots_per_compressed_layer": int(slots),
                "n_compressed_layers": len(compress_layers),
                "saving_pct": round(100 * (1 - kv_comp_mb / max(kv_orig_mb, 1e-9)), 1),
            }
            print(f"  压缩 prefill 保真度: final_hidden cos={h_cos['mean']:.4f} "
                  f"(p05={h_cos['p05']:.4f})  logits argmax 一致率={agree:.4f}", flush=True)
            print(f"  KV 记账: 原始 {kv_orig_mb:.0f}MB -> 压缩 {kv_comp_mb:.0f}MB "
                  f"(层均槽位 {slots}，压缩层 {len(compress_layers)}/{n_layers}，"
                  f"节省 {row['kv_bytes']['saving_pct']}%)", flush=True)

            patcher2 = CompressedAttentionPatcher(
                model, mode=args.compress_mode, topk_per_group=args.topk_per_group,
                sink_tokens=args.sink_tokens, window_tokens=args.window_tokens,
                compress_layers=compress_layers, kmean_renorm=args.kmean_renorm,
                group_score_prior=args.group_score_prior, mid_scope=args.mid_scope)
            patcher2.set_groups(gan.labels[:Nn], Nn)
            patcher2.install()
            try:
                t_gen_c0 = time.time()
                with torch.no_grad():
                    out_c = model.generate(ids_n, attention_mask=attn_mask,
                                           max_new_tokens=args.max_new_tokens,
                                           do_sample=False, use_cache=True,
                                           pad_token_id=tok.eos_token_id or tok.pad_token_id)
                t_gen_compress_s = time.time() - t_gen_c0
            finally:
                patcher2.uninstall()
            ans_c = tok.decode(out_c[0][Nn:], skip_special_tokens=True)
            row["compressed_answer"] = ans_c.strip()
            row["compressed_hit"] = needle_hit(ans_c)
            row["n_groups"] = int(gan.n_groups)
            row["kv_compression"] = round(Nn / max(gan.n_groups, 1), 1)
            row["config"] = {"k": needle_cfg["k"], "tau": needle_cfg["tau"]}

            # ---- E1.5 记录（DS-20261018-006）：延迟 / 显存 / 中段留存率 ----
            import resource
            lat = {
                "original_prefill_s": round(t_pre_orig_s, 2),
                "compressed_prefill_s": round(t_pre_compress_s, 2),
                "original_generate_s": round(t_gen_orig_s, 2),
                "compressed_generate_s": round(t_gen_compress_s, 2),
                "decode_per_token_ms_original": round(
                    1000 * (t_gen_orig_s - t_pre_orig_s) / max(args.max_new_tokens, 1), 1),
                "decode_per_token_ms_compressed": round(
                    1000 * (t_gen_compress_s - t_pre_compress_s) / max(args.max_new_tokens, 1), 1),
                "note": "decode_per_token ≈ (generate 总时长 − 独立 prefill 时长) / max_new_tokens；"
                        "CPU 上的近似估计（generate 含采样与逐 token 循环开销）",
            }
            lat["slowdown_pct"] = round(
                100 * (lat["decode_per_token_ms_compressed"]
                       / max(lat["decode_per_token_ms_original"], 1e-9) - 1), 1)
            row["latency"] = lat
            if torch.cuda.is_available():
                row["peak_memory_mb"] = round(
                    torch.cuda.max_memory_allocated() / 1e6, 1)
            else:
                row["peak_memory_mb"] = round(
                    resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
                row["peak_memory_note"] = "CPU 进程峰值 RSS（单调累计，非本次增量）"
            if args.compress_mode == "hybrid":
                n_mid_tokens = max(Nn - patcher2.n_priv, 1)
                row["hybrid"] = {
                    "mid_scope": args.mid_scope,
                    "mid_slots": patcher2.mid_slots(),
                    "mid_kv_retention_ratio": round(patcher2.mid_slots() / n_mid_tokens, 4),
                    "sink_tokens": patcher2.n_sink,
                    "window_tokens": patcher2.n_window,
                }
            print(f"  原始: {row['original_answer']!r} (hit={row['original_hit']}) | "
                  f"压缩({args.compress_mode},G={gan.n_groups}): "
                  f"{row['compressed_answer']!r} (hit={row['compressed_hit']}) | "
                  f"decode/token 压缩 {lat['decode_per_token_ms_compressed']}ms vs 原始 "
                  f"{lat['decode_per_token_ms_original']}ms ({lat['slowdown_pct']:+}%)",
                  flush=True)
            needle_results.append(row)
    report["stages"]["needle"] = needle_results

    # 无压缩基线参考（DS-20261018-006：转 7B 前必查 0.5B 原生检索能力）
    if needle_results:
        hits = [r["original_hit"] for r in needle_results]
        report["baseline_reference"] = {
            "description": "0.5B 无压缩基线 needle 命中（原始模型的检索能力上限参考；"
                           "H-DS7 证伪线仲裁依据：argmax<45% 且本命中率<70% → 模型容量瓶颈）",
            "original_needle_hits": hits,
            "original_hit_rate": round(sum(hits) / len(hits), 4),
        }

    report["prefill_peak_rss_mb"] = round(prefill_peak, 1)
    report["wall_time_s"] = round(time.time() - t_start, 1)
    if torch.cuda.is_available():
        report["cuda_memory"] = {
            "max_memory_allocated_mb": round(torch.cuda.max_memory_allocated() / 1e6, 1),
            "max_memory_reserved_mb": round(torch.cuda.max_memory_reserved() / 1e6, 1),
        }

    p = Path(args.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n[done] 结果写入 {p}（总耗时 {report['wall_time_s']}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
