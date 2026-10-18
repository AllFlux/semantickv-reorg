#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""env_tag.py — 结果 JSON 的环境标签（合作纪律 D3 环境隔离的机制载体）。

所有产出 results/*.json 的脚本必须写入 environment 块：
  - 真实模型运行：device/dtype/model/ctx（v41 runner 已带 config 块）；
  - CPU 合成模拟：kind=cpu-synthetic，显式声明「不构成 GPU 结论」。

check_discipline.py 的 D3 检验以本块为凭，缺失即 FAIL。
"""

from __future__ import annotations

import platform
from typing import Any, Dict

import numpy as np


def cpu_synthetic_env(note: str = "合成数据 CPU 模拟；仅验证流水线机械正确性，"
                                  "不构成对 GPU/真实模型结论的证据") -> Dict[str, Any]:
    """CPU 合成模拟运行的环境标签。"""
    return {
        "kind": "cpu-synthetic",
        "device": "cpu",
        "dtype": "float64" if np.zeros(1).dtype.itemsize == 8 else "float32",
        "model": "synthetic-mock",
        "ctx": None,
        "platform": platform.platform(),
        "note": note,
    }


def real_model_env(model: str, device: str, dtype: str, ctx: int,
                   note: str = "") -> Dict[str, Any]:
    """真实模型运行的环境标签。"""
    env = {
        "kind": "real-model",
        "device": device,
        "dtype": dtype,
        "model": model,
        "ctx": ctx,
        "platform": platform.platform(),
    }
    if note:
        env["note"] = note
    return env
