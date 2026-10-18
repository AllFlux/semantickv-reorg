# -*- coding: utf-8 -*-
"""mock_grouper.py — SemanticGrouper 的 Mock 实现。

算法：GSC（Graph-based Sparse Clustering，图式稀疏聚类）
  1) kNN 图      ：faiss IndexFlatIP（CPU）或 torch 分块矩阵乘；分块计算，杜绝 N×N 稠密矩阵
  2) 互 kNN 剪枝  ：仅保留双向 top-k 且相似度 ≥ tau 的边（稀疏化核心）
  3) 连通分量     ：scipy.sparse.csgraph.connected_components（C 实现，代替 Python 并查集）
  4) 标签传播    ：scipy.stats.mode 对邻居标签做多数投票，迭代至稳定
  5) 小群合并    ：小于 min_size 的群并入质心最近的大群

复杂度：kNN O(N²·d)（BLAS 加速，与 N² 成正比但常数极小）；其余步骤 O(N·k)。
内存  ：O(N·d + N·k)，与 N² 无关（见 CPU_SIM_REPORT.md 的实测）。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional, Tuple

import numpy as np

# kNN 后端选择：
#   本机（32 可见核的容器）实测：faiss-cpu(pip 轮子) 仅 6-25 GFLOPS，而 torch
#   矩阵乘 8 线程即 240 GFLOPS——且线程越多越慢（超订阅）。故默认 torch。
#   可用 backend="faiss" 显式切换；线程数用环境变量 SEMANTICKV_THREADS 覆盖。
SEMANTICKV_THREADS = int(os.environ.get("SEMANTICKV_THREADS", "8"))

try:
    import torch as _torch
    _torch.set_num_threads(SEMANTICKV_THREADS)
    _TORCH_OK = True
except Exception:  # pragma: no cover
    _TORCH_OK = False


# ---------------------------------------------------------------- 内存采样


def rss_mb() -> float:
    """当前进程 RSS（MB）。"""
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS"):
                    return float(line.split()[1]) / 1024.0
    except OSError:
        pass
    return float("nan")


def cgroup_mem_limit_mb() -> float:
    """容器 cgroup 内存上限（MB）。注意：`free` 看到的是宿主内存，
    容器实际可用可能远小于此（本机 free=123GB 但 cgroup 上限 8GB）。"""
    for path, scale in (("/sys/fs/cgroup/memory.max", 1.0),
                        ("/sys/fs/cgroup/memory/memory.limit_in_bytes", 1.0)):
        try:
            with open(path, "r", encoding="utf-8") as f:
                v = f.read().strip()
            if v.isdigit():
                limit = int(v)
                if 0 < limit < (1 << 62):
                    return limit / 1048576.0
        except OSError:
            continue
    return float("nan")


class MemTracker:
    """区间 RSS 峰值采样器（异步线程，默认 50ms 间隔）。"""

    def __init__(self, interval: float = 0.05):
        self.interval = interval
        self.peak_mb = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _loop(self):
        while not self._stop.is_set():
            self.peak_mb = max(self.peak_mb, rss_mb())
            self._stop.wait(self.interval)

    def __enter__(self) -> "MemTracker":
        self.peak_mb = rss_mb()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


# ---------------------------------------------------------------- kNN


def _knn(X: np.ndarray, k: int, chunk: int = 256, backend: str = "auto",
         metric: str = "cosine"):
    """对 X 求 top-k 最近邻（不含自身）。

    返回 (idx (N,k) int64, sim (N,k) float32)。
    分块计算保证峰值内存 O(chunk·N)，与 N² 无关。
    backend: "auto"（默认 torch）| "torch" | "faiss"
    metric :
      "cosine" —— 输入应已 L2 归一化，内积即余弦（默认，v0.1.x 行为）；
      "l2"     —— 幅值感知相似度 2ab/(|a|²+|b|²) ∈ [-1,1]：对单位向量
                 退化为余弦，对原始隐状态保留幅值信息（Attention Sink
                 token 幅值极大，余弦度量下与普通 token 不可分）。
                 需要原始（未归一化）输入；faiss 后端不支持，自动回落 torch。
    """
    N, d = X.shape
    if backend == "auto":
        backend = "torch" if _TORCH_OK else "faiss"
    if backend == "faiss" and metric != "cosine":
        backend = "torch"

    idx = np.empty((N, k + 1), dtype=np.int64)
    sim = np.empty((N, k + 1), dtype=np.float32)
    if backend == "faiss":
        import faiss
        index = faiss.IndexFlatIP(d)          # 内积 = 余弦（输入已归一化）
        index.add(X)
        for s in range(0, N, chunk):
            e = min(s + chunk, N)
            D, I = index.search(np.ascontiguousarray(X[s:e]), k + 1)
            idx[s:e] = I
            sim[s:e] = D
    else:  # torch 分块矩阵乘
        import torch
        t = torch.from_numpy(X)
        nb = (t * t).sum(dim=1) if metric == "l2" else None   # (N,) 幅值平方
        for s in range(0, N, chunk):
            e = min(s + chunk, N)
            S = t[s:e] @ t.T                  # (chunk, N) 分块，内存可控
            if metric == "l2":
                na = nb[s:e, None]
                S = 2.0 * S / (na + nb[None, :]).clamp(min=1e-12)
            D, I = torch.topk(S, k + 1, dim=1)
            idx[s:e] = I.numpy()
            sim[s:e] = D.numpy()

    # 向量化剔除自身列（自相似恒为 1.0，必在首位）
    rows = np.arange(N)
    has_self = idx == rows[:, None]
    present = has_self.any(axis=1)
    first_self = has_self.argmax(axis=1)
    pos = np.where(present[:, None], first_self[:, None], -1) + 1 + np.arange(k)[None, :]
    pos = np.clip(pos, 0, k)
    return idx[rows[:, None], pos], sim[rows[:, None], pos]


def _knn_pairwise(Q: np.ndarray, X: np.ndarray, k: int, metric: str = "cosine"):
    """Q × X 的 top-k。Q 是 X 的尾部（partial_fit 场景），需剔除自身。
    metric: "cosine"（输入已归一化）| "l2"（幅值感知 2ab/(|a|²+|b|²)）。"""
    Nq = Q.shape[0]
    idx = np.empty((Nq, k + 1), dtype=np.int64)
    sim = np.empty((Nq, k + 1), dtype=np.float32)
    off = X.shape[0] - Nq
    if _TORCH_OK:
        import torch
        tq, tx = torch.from_numpy(Q), torch.from_numpy(X)
        nb_q = (tq * tq).sum(dim=1) if metric == "l2" else None
        nb_x = (tx * tx).sum(dim=1) if metric == "l2" else None
        for s in range(0, Nq, 512):
            e = min(s + 512, Nq)
            S = tq[s:e] @ tx.T
            if metric == "l2":
                S = 2.0 * S / (nb_q[s:e, None] + nb_x[None, :]).clamp(min=1e-12)
            D, I = torch.topk(S, k + 1, dim=1)
            idx[s:e] = I.numpy()
            sim[s:e] = D.numpy()
    else:
        import faiss
        index = faiss.IndexFlatIP(X.shape[1])
        index.add(X)
        for s in range(0, Nq, 512):
            e = min(s + 512, Nq)
            D, I = index.search(np.ascontiguousarray(Q[s:e]), k + 1)
            idx[s:e] = I
            sim[s:e] = D
    rows = np.arange(Nq)
    self_mask = idx == (rows + off)[:, None]
    present = self_mask.any(axis=1)
    first = self_mask.argmax(axis=1)
    pos = np.clip(np.where(present[:, None], first[:, None], -1) + 1 + np.arange(k)[None, :], 0, k)
    return idx[rows[:, None], pos], sim[rows[:, None], pos]


def _mutual_edges(idx: np.ndarray, sim: np.ndarray, tau: float):
    """互 kNN + 相似度阈值剪枝。返回 (rows, cols, w)。"""
    N, k = idx.shape
    idx_sorted = np.sort(idx, axis=1)
    rows = np.repeat(np.arange(N), k)
    cols = idx.ravel()
    # 批量 searchsorted：把「在第 col 行的有序数组里找 row」编码为全局有序键
    sorted_flat = (idx_sorted + np.arange(N)[:, None] * (N + 1)).ravel()
    q = rows + cols * (N + 1)
    pos = np.clip(np.searchsorted(sorted_flat, q), 0, sorted_flat.size - 1)
    found = sorted_flat[pos] == q
    w = sim.ravel()
    keep = found & (w >= tau)
    return rows[keep], cols[keep], w[keep].astype(np.float32)


def _components(N: int, rows: np.ndarray, cols: np.ndarray):
    """稀疏邻接矩阵的连通分量（C 实现，替代 Python 并查集）。"""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    A = coo_matrix((np.ones(rows.size, dtype=np.int8), (rows, cols)), shape=(N, N))
    return connected_components(A, directed=False)


def _label_prop(idx: np.ndarray, labels: np.ndarray, n_iters: int = 5) -> np.ndarray:
    """标签传播：对每个 token 的 kNN 邻居标签做多数投票，迭代至稳定。"""
    from scipy.stats import mode

    cur = labels.copy()
    for _ in range(n_iters):
        m = np.asarray(mode(cur[idx], axis=1, keepdims=False).mode).ravel()
        if np.array_equal(m, cur):
            break
        cur = m
    return cur


def _centroids_and_sizes(X: np.ndarray, labels: np.ndarray, G: int):
    """稀疏 one-hot 矩阵乘计算质心与群规模（比 np.add.at 快一个量级）。"""
    from scipy.sparse import csr_matrix

    N = X.shape[0]
    onehot = csr_matrix((np.ones(N), (np.arange(N), labels)), shape=(N, G))
    sums = np.asarray(onehot.T @ X)                       # (G, d)
    counts = np.asarray(onehot.sum(axis=0)).ravel().astype(np.int64)
    cents = sums / np.maximum(counts, 1)[:, None]
    cents /= np.maximum(np.linalg.norm(cents, axis=1, keepdims=True), 1e-12)
    return cents.astype(np.float32), counts


class MockSemanticGrouper:
    """满足 eval_runner.SemanticGrouper 接口的 Mock 实现（GSC）。"""

    def __init__(self, k: int = 16, tau: float = 0.3, min_size: int = 16,
                 n_prop_iters: int = 5, chunk: int = 256, backend: str = "auto",
                 project_dim: Optional[int] = None, seed: int = 0,
                 metric: str = "cosine"):
        """
        k           : kNN 的 k
        tau         : 互 kNN 边的相似度阈值（同时用作新群判定阈值）
        min_size    : 小于该规模的群并入最近大群
        project_dim : 若设置（如 128），先用高斯随机投影降维（JL 保距），
                      用于在纯 CPU 上压低 O(N²·d) 的 kNN 成本；会记录在 meta
        metric      : "cosine"（默认，输入需 L2 归一化）| "l2"（幅值感知
                      相似度 2ab/(|a|²+|b|²)，输入保持原始幅值）
        """
        self.k = k
        self.tau = tau
        self.min_size = min_size
        self.n_prop_iters = n_prop_iters
        self.chunk = chunk
        self.backend = backend
        self.project_dim = project_dim
        self.seed = seed
        self.metric = metric

        # 增量状态（partial_fit 使用）
        self._X_all: Optional[np.ndarray] = None
        self._labels_all: Optional[np.ndarray] = None
        self._centroids: Optional[np.ndarray] = None

    # ------------------------------------------------------------ 内部

    def _project(self, X: np.ndarray, fit_proj: bool = False) -> np.ndarray:
        if not self.project_dim:
            return X
        if fit_proj:
            rng = np.random.default_rng(self.seed)
            self._proj = rng.standard_normal((X.shape[1], self.project_dim), dtype=np.float32)
            self._proj /= np.sqrt(self.project_dim)
        Z = X @ self._proj
        Z /= np.maximum(np.linalg.norm(Z, axis=1, keepdims=True), 1e-12)
        return np.ascontiguousarray(Z.astype(np.float32))

    # ------------------------------------------------------------ 接口

    def fit(self, embeddings: np.ndarray):
        from eval_runner import GroupAssignment

        t = {}
        t0 = time.perf_counter()
        X = np.ascontiguousarray(embeddings, dtype=np.float32)
        Z = self._project(X, fit_proj=True)
        N = Z.shape[0]
        t["project"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        idx, sim = _knn(Z, self.k, chunk=self.chunk, backend=self.backend,
                        metric=self.metric)
        t["knn"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        rows, cols, w = _mutual_edges(idx, sim, self.tau)
        t["mutual_prune"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        n_comp, labels = _components(N, rows, cols)
        t["components"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        labels = _label_prop(idx, labels, self.n_prop_iters)
        t["label_prop"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        labels = self._compact_and_merge(Z, labels)
        t["merge_small"] = time.perf_counter() - t0

        G = int(labels.max()) + 1
        cents, sizes = _centroids_and_sizes(Z, labels, G)
        intra = float(np.mean(np.einsum("ij,ij->i", Z, cents[labels])))
        t["total"] = sum(v for k2, v in t.items() if k2 != "total")

        # 保存增量状态
        self._X_all = Z
        self._labels_all = labels
        self._centroids = cents

        return GroupAssignment(
            labels=labels.astype(np.int32),
            centroids=cents,
            sizes=sizes,
            n_groups=G,
            meta={
                "algo": "GSC",
                "k": self.k, "tau": self.tau,
                "project_dim": self.project_dim,
                "n_components_raw": int(n_comp),
                "n_mutual_edges": int(rows.size),
                "intra_sim": intra,
                "timings": {k2: round(v, 4) for k2, v in t.items()},
                "n_tokens": int(N),
            },
        )

    def _compact_and_merge(self, Z: np.ndarray, labels: np.ndarray) -> np.ndarray:
        """紧凑编号 + 小群并入质心最近的大群。"""
        from scipy.sparse import csr_matrix

        uniq, inv = np.unique(labels, return_inverse=True)
        G0 = uniq.size
        sizes = np.bincount(inv, minlength=G0)
        large = sizes >= self.min_size
        if not large.any():
            large[int(sizes.argmax())] = True
        large_ids = np.flatnonzero(large)
        G = large_ids.size

        remap = -np.ones(G0, dtype=np.int64)
        remap[large_ids] = np.arange(G)
        lab = remap[inv]                       # 小群 token 暂为 -1
        big_mask = lab >= 0

        # 大群质心（稀疏 one-hot 乘）
        onehot = csr_matrix((np.ones(int(big_mask.sum())),
                             (np.flatnonzero(big_mask), lab[big_mask])),
                            shape=(Z.shape[0], G))
        sums = np.asarray(onehot.T @ Z)
        counts = np.asarray(onehot.sum(axis=0)).ravel()
        cents = sums / np.maximum(counts, 1)[:, None]
        cents /= np.maximum(np.linalg.norm(cents, axis=1, keepdims=True), 1e-12)

        # 小群 token 挂到质心最近的大群
        if (~big_mask).any():
            sims = Z[~big_mask] @ cents.T
            lab[~big_mask] = sims.argmax(axis=1)
        return lab.astype(np.int64)

    def partial_fit(self, new_embeddings: np.ndarray, prev):
        """增量语义分组：新 token 挂靠/新建群 + 仅对新 token 做局部标签传播。

        不重算全量 kNN；kNN 只查 新 token × 全量（成本 O(N_new·N·d)）。
        """
        from eval_runner import GroupAssignment
        from scipy.stats import mode

        t0 = time.perf_counter()
        assert self._X_all is not None, "partial_fit 前必须先 fit"
        Xn = np.ascontiguousarray(new_embeddings, dtype=np.float32)
        Zn = self._project(Xn)
        Zall = self._X_all
        N_old = Zall.shape[0]

        # 1) 新 token 与旧质心相似度 -> 挂靠或新建
        sims = Zn @ self._centroids.T                       # (N_new, G_old)
        best = sims.argmax(axis=1)
        best_sim = sims.max(axis=1)
        new_cluster = best_sim < self.tau
        lab_new = best.copy()
        if new_cluster.any():
            # 未匹配 token 内部做一次小型互 kNN 聚类 + 标签传播：
            # 同一新话题归为同一新群，而不是每 token 各成一族（早期版本的 bug）。
            # 注意：互 kNN 互通率通常只有 40% 左右，仅靠连通分量会碎裂，
            # 必须跟标签传播（与 fit 相同）才能合并碎片。
            um = np.flatnonzero(new_cluster)
            Zu = Zn[um]
            idx_u, sim_u = _knn(Zu, self.k, chunk=self.chunk, backend=self.backend,
                                metric=self.metric)
            rows_u, cols_u, w_u = _mutual_edges(idx_u, sim_u, self.tau)
            _, lab_u = _components(Zu.shape[0], rows_u, cols_u)
            lab_u = _label_prop(idx_u, lab_u, n_iters=3)
            lab_new[um] = int(self._labels_all.max()) + 1 + lab_u

        # 2) 全量矩阵追加
        Zall = np.vstack([Zall, Zn])
        labels_all = np.concatenate([self._labels_all, lab_new])

        # 3) 局部标签传播：只更新新 token（邻居 = 新 token 在全体上的 kNN）
        idx_n, _ = _knn_pairwise(Zn, Zall, self.k, metric=self.metric)   # (N_new, k) 不含自身
        cur = labels_all.copy()
        new_pos = np.arange(N_old, Zall.shape[0])
        for _ in range(3):
            m = np.asarray(mode(cur[idx_n], axis=1, keepdims=False).mode).ravel()
            cur[new_pos] = m
        labels_all = cur

        # 4) 质心重算（稀疏乘法，O(N·d)）
        G = int(labels_all.max()) + 1
        cents, sizes = _centroids_and_sizes(Zall, labels_all, G)

        self._X_all = Zall
        self._labels_all = labels_all
        self._centroids = cents

        return GroupAssignment(
            labels=labels_all.astype(np.int32),
            centroids=cents,
            sizes=sizes,
            n_groups=G,
            meta={
                "algo": "GSC-partial",
                "n_new": int(Zn.shape[0]),
                "n_new_tokens_unmatched": int(new_cluster.sum()),
                "n_new_clusters": int(np.unique(lab_new[new_cluster]).size),
                "intra_sim": float(np.mean(np.einsum("ij,ij->i", Zall, cents[labels_all]))),
                "timings": {"partial_total": round(time.perf_counter() - t0, 4)},
                "n_tokens": int(Zall.shape[0]),
            },
        )

    def centroids(self) -> np.ndarray:
        assert self._centroids is not None, "centroids 前必须先 fit"
        return self._centroids
