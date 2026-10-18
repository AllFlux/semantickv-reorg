# -*- coding: utf-8 -*-
"""SemanticKV vLLM 插件包 —— 「摘要路由 + 索引重写」参考骨架（H-DS15 / H-DS17）。

⚠ 状态：参考骨架（scaffold），非可运行的 vLLM 补丁。
vLLM 内部 API（CacheEngine / KVCacheBlock / CUDA Graph 捕获边界）随版本剧烈变动，
本包固化的是**与版本无关的结构**：路由决策发生在 prefill 后、KV 写入前；
实际接入点需在目标 vLLM 版本（Kaggle 建议 vllm>=0.6）上按 README 的挂载点适配。

模块：
  semantic_router.SemanticRouter   —— GSC 数学聚类路由 + load_external_router 挂载点
  kv_rewrite.KVIndexRewriter       —— 槽位选择与索引重写（top-K/混合保护通用）
  learned_router.LearnedSemanticRouter —— DS-20261018-011 规格 Router 空壳
                                       （DS 规格类名 SemanticRouter，见该模块 docstring）
"""
from .semantic_router import SemanticRouter
from .kv_rewrite import KVIndexRewriter
from .learned_router import SemanticRouter as LearnedSemanticRouter

__all__ = ["SemanticRouter", "KVIndexRewriter", "LearnedSemanticRouter"]
