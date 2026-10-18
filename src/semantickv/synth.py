# -*- coding: utf-8 -*-
"""synth.py — 合成语义空间生成器（纯 CPU）。

说明：用户要求「随机生成 Token Embedding 模拟语义空间」。纯均匀随机没有任何
可聚类结构，聚类指标会退化为无意义。因此默认生成 **高斯混合语义空间**
（每个「话题」一个质心，token = 质心 + 噪声），这仍是随机数据，但带有
可控的语义结构，使分组/建图算法有可验证的真值（ARI）。
可用 synth="uniform" 生成无结构均匀随机点作为压力对照。
"""

from __future__ import annotations

from typing import Optional, Tuple

import numpy as np


def make_semantic_batch(
    n: int,
    d: int = 768,
    n_topics: int = 32,
    base_topic_std: float = 0.65,
    rng: Optional[np.random.Generator] = None,
    synth: str = "mixture",
    centers: Optional[np.ndarray] = None,
    return_centers: bool = False,
):
    """生成 (embeddings, true_labels)（或加 centers 的三元组）。

    输入:
      n               token 数
      d               embedding 维度
      n_topics        话题（真值群）数（centers 提供时忽略）
      base_topic_std  话题内噪声基准；实际话题内标准差 = base_topic_std / sqrt(d)。
                      如此缩放后，话题内余弦 ≈ 1/(1+base²)（与维度无关，
                      默认 ≈0.70），话题间余弦 ≈0——高维下若用绝对 std，
                      噪声能量 σ²d 会淹没话题结构（早期版本踩过这个坑）。
      synth           "mixture"（默认，高斯混合）| "uniform"（无结构对照）
      centers         外部提供的话题质心 (n_topics, d)，用于多批次共享同一
                      话题空间（如 partial_fit 测试、多轮模拟）
      return_centers  True 时返回 (X, labels, centers)
    输出:
      embeddings (n, d) float32，L2 归一化
      true_labels (n,) int32
    """
    rng = rng or np.random.default_rng(0)
    if synth == "uniform":
        X = rng.standard_normal((n, d), dtype=np.float32)
        labels = np.zeros(n, dtype=np.int32)
        if return_centers:
            return X, labels, None
        return X, labels
    if centers is None:
        centers = rng.standard_normal((n_topics, d)).astype(np.float32)
        centers /= np.linalg.norm(centers, axis=1, keepdims=True)
    n_topics = centers.shape[0]
    topic_std = base_topic_std / np.sqrt(d)
    # 分块生成：500k×768 时若一次性 fancy-index + 加噪声，瞬态峰值 ≈3 倍
    # 数据大小（≈4.6GB），会撑爆小内存容器（本机 cgroup 上限仅 8GB）。
    X = np.empty((n, d), dtype=np.float32)
    labels = rng.integers(0, n_topics, size=n).astype(np.int32)
    block = max(1, min(n, 2_000_000 // max(d, 1)))      # ≈8MB 一块
    for s in range(0, n, block):
        e = min(s + block, n)
        X[s:e] = centers[labels[s:e]] + topic_std * rng.standard_normal(
            (e - s, d), dtype=np.float32)
    X /= np.linalg.norm(X, axis=1, keepdims=True)
    out = (np.ascontiguousarray(X), labels)
    if return_centers:
        return (*out, centers)
    return out


def adjusted_rand_index(labels_true: np.ndarray, labels_pred: np.ndarray) -> float:
    """ARI（调整兰德指数）。用列联表 O(N) 实现，不依赖 sklearn。"""
    lt = np.asarray(labels_true).ravel()
    lp = np.asarray(labels_pred).ravel()
    if lt.size != lp.size:
        raise ValueError("标签长度不一致")
    # 列联表
    classes, lt_idx = np.unique(lt, return_inverse=True)
    clusters, lp_idx = np.unique(lp, return_inverse=True)
    cont = np.zeros((classes.size, clusters.size), dtype=np.int64)
    np.add.at(cont, (lt_idx, lp_idx), 1)

    def comb2(x: np.ndarray) -> np.ndarray:
        return x * (x - 1) / 2.0

    sum_comb = comb2(cont).sum()
    sum_comb_rows = comb2(cont.sum(axis=1)).sum()
    sum_comb_cols = comb2(cont.sum(axis=0)).sum()
    n = lt.size
    total = comb2(n)
    expected = sum_comb_rows * sum_comb_cols / total if total > 0 else 0.0
    max_index = (sum_comb_rows + sum_comb_cols) / 2.0
    denom = max_index - expected
    if denom == 0:
        return 1.0
    return float((sum_comb - expected) / denom)
