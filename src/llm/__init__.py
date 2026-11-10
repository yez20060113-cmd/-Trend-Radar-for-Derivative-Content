"""模型调用层。

设计上的三条硬约束（都来自 2026-11-09 的教训）：

1. **失败即降级**：日报每天 09:00 必须发。任何一层模型调用超时、报错、返回
   非法 JSON，都只能「跳过这一层」，不能抛出异常让整轮失败。
2. **可回放**：`temperature=0` + prompt 带版本号 + 响应按输入 hash 缓存。
   同一批输入重跑，结果必须一致，否则跨天复用治理（judge/history.py 的 streak）
   会失真。
3. **可审计**：每次调用的耗时、token、原始响应都落盘到 `run_dir/llm_calls/`，
   出问题能一条条翻。

供应商走 OpenAI 兼容协议（DeepSeek / 通义 / Kimi / Ollama 都支持），
换供应商只改 `base_url` + `model` 两行配置，代码不动。

导入用惰性方式：`client` 只在真正用到时才加载，这样
`python -m src.llm.client` 不会触发「模块已在 sys.modules」的警告。
"""

from __future__ import annotations

from typing import Any

__all__ = ["LLM", "LLMResult", "available", "from_config", "probe"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from . import client

        return getattr(client, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
