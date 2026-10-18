# -*- coding: utf-8 -*-
"""anchor_grouper.py — AnchorBasedSemanticGrouper 空壳类（调整二：提前准备真实环境接口）。

=====================================================================
设计动机
=====================================================================
Mock 版 SemanticGrouper 走 GSC 聚类，输入是「原始 token embedding 矩阵」。
但在真实 V4.1-Flash-1017 环境中，若采用**模型自发语义锚点**路线，
分组依据不再是静态 embedding，而是 prefill 阶段可捕获的模型内部信号：

  - 隐状态 H（最后一层或指定层）：(N, d)，与 embedding 同构，天然可分组；
  - Attention 矩阵 A：prefill 时 (n_heads, N, N)，直接存会爆内存，只存摘要：
      * 锚点 token 的 query-key 行（anchor rows）：(M, N)，M 为锚点数；
      * 或按 query 分块的 top-k 列索引（块内摘要）；
  - 锚点向量 a_j：从隐状态中按规则选取（段首 token / 每 s 个 token /
    注意力熵最低的 token 等），构成锚点集合 A = {a_1..a_M}。

由此得到 token-锚点相似度 S = H @ A^T（(N, M) 矩阵），与 GSC 里
「token-质心相似度」同构——**同一套 GSC 流水线可以照跑**，只是质心的
初值与更新来源从「聚类结果」变成「模型注意力选出的锚点」。

=====================================================================
有卡环境填充点（TODO_ANCHOR 标记）
=====================================================================
1. `register_prefill_hooks(model, storage)`：给指定层挂 forward hook，
   把 prefill 的隐状态/注意力摘要写进 PrefillCapture；
2. `_anchors_from_capture(capture)`：从 capture 里提取锚点向量；
3. `fit / partial_fit` 内部：把 capture 转成 GSC 的输入矩阵
   （可直接用 H，或 H 拼接 S），然后复用 mock_grouper 的 GSC 步骤
   （kNN -> 互 kNN -> 连通分量 -> 标签传播 -> 小群合并）；
4. `centroids()`：按当前 labels 对 H 求均值（L2 归一化）。

接口签名与 eval_runner.SemanticGrouper 完全一致，因此：
  - eval_runner.check_interfaces() 可校验；
  - cpu_sim / E1-E4 的 Mock 流水线可无缝替换实现做 A/B。

本文件在无卡环境**可导入、可实例化、可校验签名**，但 fit/partial_fit
调用时会抛出带指引的 NotImplementedError（不静默失败）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

from eval_runner import GroupAssignment, SemanticGrouper


# =====================================================================
# Prefill 捕获的数据结构
# =====================================================================

@dataclass
class PrefillCapture:
    """prefill 阶段捕获的模型内部信号（由 forward hook 填充）。

    hidden_states : (N, d) float32   指定层的隐状态（L2 归一化前后均可，
                      建议与 embedding 同旋即归一化）
    attn_rows     : Optional (M, N) float32  锚点 token 的 query-key 行摘要
                      （M 个锚点 × N 个 token 的原始注意力 logits 或概率）
    attn_col_topk : Optional List[np.ndarray]  按 query 分块的 top-k 列摘要，
                      每元素 (block, k) int32 + (block, k) float32 权重
    anchor_idx    : (M,) int64       锚点 token 在序列中的位置
    positions     : (N,) int64       token 位置（多轮对话时的全局位置）
    layer_idx     : int             捕获来自哪一层
    n_heads       : int             头数（用于注意力摘要归一化）
    meta          : dict            捕获元数据（耗时、hook 名、模型配置等）
    """
    hidden_states: np.ndarray
    attn_rows: Optional[np.ndarray] = None
    attn_col_topk: Optional[List[Tuple[np.ndarray, np.ndarray]]] = None
    anchor_idx: Optional[np.ndarray] = None
    positions: Optional[np.ndarray] = None
    layer_idx: int = -1
    n_heads: int = 1
    meta: dict = field(default_factory=dict)

    @property
    def n_tokens(self) -> int:
        return int(self.hidden_states.shape[0])

    @property
    def dim(self) -> int:
        return int(self.hidden_states.shape[1])


# 允许的锚点来源（构造时校验，避免拼写错误静默走错分支）
ANCHOR_SOURCES = (
    "hidden_states",        # 只用隐状态（退化：与 embedding 分组同构）
    "attn_rows",            # 锚点行的注意力摘要 -> token-锚点相似度
    "attn_col_topk",        # query 分块 top-k 摘要 -> 稀疏 token-锚点相似度
    "hybrid",               # 隐状态 + 注意力摘要拼接
)


# =====================================================================
# AnchorBasedSemanticGrouper
# =====================================================================

class AnchorBasedSemanticGrouper(SemanticGrouper):
    """基于模型自发语义锚点的分组器（真实环境实现，当前为空壳）。

    与 MockSemanticGrouper 的接口契约（fit / partial_fit / centroids）一致，
    输入参数名与含义一致；差别仅在**输入内容的来源**：

      - Mock：embeddings = 合成 token embedding (N, d)
      - 本类：embeddings = PrefillCapture（或由其派生的 (N, d) 矩阵），
        内容由 prefill forward hook 捕获。

    参数
    ----
    k : int
        GSC 的 kNN 邻域数（与 Mock 一致）
    tau : float
        互 kNN 相似度阈值（与 Mock 一致）
    anchor_source : str
        锚点信号来源，见 ANCHOR_SOURCES
    layer_idx : int
        捕获哪一层的隐状态（-1 表示最后一层）
    anchors_per_seq : int
        每段序列选取的锚点数上限（段首 + 等距采样）
    hook_points : List[str]
        需要挂 hook 的模块名模式（如 ["model.layers.0.self_attn"]），
        None 表示由调用方在 register_prefill_hooks 时显式指定
    device : str
        "cuda" / "cpu"（hook 张量搬运目标）
    dtype : str
        捕获张量精度（"float32" 推荐；float16 省显存但损相似度精度）
    """

    def __init__(self,
                 k: int = 16,
                 tau: float = 0.3,
                 anchor_source: str = "hybrid",
                 layer_idx: int = -1,
                 anchors_per_seq: int = 32,
                 hook_points: Optional[List[str]] = None,
                 device: str = "cuda",
                 dtype: str = "float32"):
        if anchor_source not in ANCHOR_SOURCES:
            raise ValueError(
                f"anchor_source 必须是 {ANCHOR_SOURCES} 之一，收到 {anchor_source!r}")
        if k < 2:
            raise ValueError("k 必须 >= 2（互 kNN 至少需要 2 个邻居）")
        self.k = int(k)
        self.tau = float(tau)
        self.anchor_source = anchor_source
        self.layer_idx = int(layer_idx)
        self.anchors_per_seq = int(anchors_per_seq)
        self.hook_points = hook_points
        self.device = device
        self.dtype = dtype
        # 运行时状态
        self._capture: Optional[PrefillCapture] = None
        self._labels: Optional[np.ndarray] = None
        self._centroids: Optional[np.ndarray] = None
        self._hook_handles: List[Any] = []
        self._storage: Dict[str, List[Any]] = {}

    # ------------------------------------------------------------
    # 真实环境填充点 1：hook 注册（TODO_ANCHOR-1）
    # ------------------------------------------------------------

    def register_prefill_hooks(self, model: Any, layer_names: Optional[List[str]] = None) -> List[Any]:
        """给模型挂 forward hook，把 prefill 信号写进 self._storage。

        有卡环境实现要点（TODO_ANCHOR-1）：
          1. 遍历 model.named_modules()，按 layer_names（或 self.hook_points）
             匹配目标模块（建议挂在 self_attn 与目标层的 output）；
          2. hook 签名 fn(module, input, output)：
               - 从 output 取隐状态/注意力（不同模型族 output 结构不同，
                 V4.1-Flash-1017 需按实际 forward 返回值解包）；
               - **只存摘要**：注意力矩阵 (n_heads, N, N) 不可全存，
                 按 anchor_idx 取行（M × N）或按 query 分块取 top-k 列；
               - 张量 .detach().to(self.device, self.dtype).cpu().numpy()；
               - 追加进 self._storage[module_name]；
          3. 返回 hook handle 列表（保存到 self._hook_handles，供 detach）。

        返回: hook handles（当前空壳返回空列表）
        """
        # 空壳：有卡环境在此填充。签名与返回类型已稳定。
        self._hook_handles = []
        return self._hook_handles

    def detach_hooks(self) -> None:
        """摘除所有已注册 hook（幂等）。"""
        for h in self._hook_handles:
            try:
                h.remove()
            except Exception:
                pass
        self._hook_handles = []

    # ------------------------------------------------------------
    # 真实环境填充点 2：锚点提取（TODO_ANCHOR-2）
    # ------------------------------------------------------------

    def _anchors_from_capture(self, capture: PrefillCapture) -> np.ndarray:
        """从 capture 提取锚点向量 A (M, d)。

        候选规则（有卡环境按 anchor_source 分支实现）：
          - hidden_states: 段首 token + 每 s 个等距 token 的隐状态；
          - attn_rows:    对锚点行做 softmax 后与 H 加权求和
                          （注意力意义上的「代表 token」）；
          - attn_col_topk: 按 top-k 权重对 H 加权；
          - hybrid:       H[anchor_idx] 与注意力加权向量拼接/平均。

        返回的锚点集合用于构造 token-锚点相似度 S = H @ A^T，
        再送入 GSC（与 Mock 的 kNN 步骤同构）。
        """
        raise NotImplementedError(
            "[TODO_ANCHOR-2] _anchors_from_capture 需要在有卡环境填充："
            "按 anchor_source 分支从 PrefillCapture 提取锚点向量。"
            "接口约定：输入 PrefillCapture，输出 (M, d) float32 L2 归一化矩阵。")

    # ------------------------------------------------------------
    # 真实环境填充点 3：GSC 流水线入口（TODO_ANCHOR-3）
    # ------------------------------------------------------------

    @staticmethod
    def _to_matrix(embeddings: Union[np.ndarray, PrefillCapture]) -> Tuple[np.ndarray, Optional[PrefillCapture]]:
        """把入参统一成 (N, d) 矩阵；若是 PrefillCapture 则一并返回。

        有卡环境的 fit/partial_fit 内部应先调用本方法，再走 GSC。
        """
        if isinstance(embeddings, PrefillCapture):
            H = embeddings.hidden_states
            return H, embeddings
        H = np.asarray(embeddings, dtype=np.float32)
        return H, None

    def fit(self, embeddings: Union[np.ndarray, PrefillCapture]) -> GroupAssignment:
        """全量语义分组（签名与 SemanticGrouper.fit 一致）。

        有卡环境实现步骤（TODO_ANCHOR-3）：
          1. H, capture = self._to_matrix(embeddings)
          2. 若 anchor_source != "hidden_states"：
                 A = self._anchors_from_capture(capture)
                 S = H @ A.T            # (N, M) token-锚点相似度
                 X = 拼接(H, S) 或直接用 S 作为 GSC 输入
             否则 X = H
          3. 复用 mock_grouper 的 GSC 步骤：
                 knn -> _mutual_edges(X, k, tau) -> _components
                 -> _label_prop -> _centroids_and_sizes
          4. 写 self._labels / self._centroids，返回 GroupAssignment
             （labels 紧凑连续；centroids 为 L2 归一化的群均值）
        """
        raise NotImplementedError(
            "[TODO_ANCHOR-3] fit 需要在有卡环境填充：hook 捕获 -> 锚点提取 -> "
            "GSC 分组。无卡环境请使用 MockSemanticGrouper 验证流水线。")

    def partial_fit(self,
                    new_embeddings: Union[np.ndarray, PrefillCapture],
                    prev: GroupAssignment) -> GroupAssignment:
        """增量语义分组（签名与 SemanticGrouper.partial_fit 一致）。

        有卡环境实现要点（TODO_ANCHOR-4）：
          1. 新 token 的隐状态/注意力摘要同样由 hook 捕获
             （prefill 只跑新增部分，KV cache 承接历史）；
          2. 用 prev.centroids（或上一轮锚点集合）做新 token 的挂靠判定：
                 sim >= tau -> 挂靠最近群，群质心 EMA 更新；
                 sim <  tau -> 新 token 子集内跑小型 GSC 建新群；
          3. 标签传播修复碎片（与 Mock partial_fit 一致的流程）；
          4. 输出覆盖「旧 + 新」全部 token 的 GroupAssignment。
        """
        raise NotImplementedError(
            "[TODO_ANCHOR-4] partial_fit 需要在有卡环境填充：增量 hook 捕获 + "
            "质心挂靠 + 小型 GSC 建新群 + 标签传播。")

    def centroids(self) -> np.ndarray:
        """当前群质心 (G, d) float32（签名与 SemanticGrouper.centroids 一致）。

        有卡环境：对 self._labels 分群求 H 的均值再 L2 归一化；
        空壳状态（未 fit）返回空矩阵，形状 (0, d_unknown)。
        """
        if self._centroids is None:
            d = 0
            if self._capture is not None:
                d = self._capture.dim
            return np.empty((0, d), dtype=np.float32)
        return self._centroids

    # ------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------

    def config(self) -> dict:
        """当前配置（供实验日志记录，保证可复现）。"""
        return {
            "k": self.k, "tau": self.tau,
            "anchor_source": self.anchor_source,
            "layer_idx": self.layer_idx,
            "anchors_per_seq": self.anchors_per_seq,
            "hook_points": self.hook_points,
            "device": self.device, "dtype": self.dtype,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"AnchorBasedSemanticGrouper({self.config()})"
