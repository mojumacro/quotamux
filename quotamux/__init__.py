"""Quotamux —— 多模型多订阅额度调度器。

Route your coder to the subscription with the most quota left.
"""
from .core import Pool, collect, load_registry, pick, pick_spread

__version__ = "0.2.0"
__all__ = ["Pool", "collect", "load_registry", "pick", "pick_spread", "__version__"]
