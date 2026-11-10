"""配置与词库加载。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
CORE_DIR = ROOT / "CoreAssets"
SANDBOX_DIR = ROOT / "sandbox"
DATA_DIR = ROOT / "data"

# 密钥只从 .env 读；文件不存在时静默跳过，不阻塞离线功能
# 编码容错：Windows 记事本可能存成 GBK，先试 utf-8 再退 gbk（本文件曾被写成 GBK）
for _enc in ("utf-8", "gbk"):
    try:
        load_dotenv(ROOT / ".env", encoding=_enc)
        break
    except (UnicodeDecodeError, OSError):
        continue


def env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def load_sources() -> dict[str, Any]:
    path = CONFIG_DIR / "sources.yaml"
    if not path.exists():
        raise FileNotFoundError(f"缺少配置文件：{path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_keywords() -> dict[str, Any]:
    path = CORE_DIR / "keywords.json"
    if not path.exists():
        raise FileNotFoundError(f"缺少词库：{path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def keyword_layer(kw: dict[str, Any], layer: str, key: str) -> list[str]:
    """安全取词库里的某个列表。"""
    node = kw.get(layer, {})
    if not isinstance(node, dict):
        return []
    value = node.get(key, [])
    return [str(x) for x in value] if isinstance(value, list) else []


def top_list(kw: dict[str, Any], key: str) -> list[str]:
    """取词库顶层的列表（如 `噪声黑名单`）。"""
    value = kw.get(key, [])
    return [str(x) for x in value] if isinstance(value, list) else []


def alias_map(kw: dict[str, Any]) -> dict[str, list[str]]:
    node = kw.get("别名映射", {})
    if not isinstance(node, dict):
        return {}
    return {str(k): [str(x) for x in v] for k, v in node.items() if isinstance(v, list)}


def subject_profiles(kw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """主体画像：tier / weight / region / type / org。缺失时按 weight=1.0 兜底。"""
    node = kw.get("锚点层", {})
    raw = node.get("主体画像") if isinstance(node, dict) else None
    out: dict[str, dict[str, Any]] = {}
    if isinstance(raw, dict):
        for name, info in raw.items():
            if isinstance(info, dict):
                item = dict(info)
                item.setdefault("weight", 1.0)
                item.setdefault("tier", "S")
                out[str(name)] = item
    return out


def member_map(kw: dict[str, Any]) -> dict[str, str]:
    """成员 → 所属团体。用于圈层归因：命中「宋亚轩」等于命中「时代少年团」。"""
    node = kw.get("锚点层", {})
    raw = node.get("成员归属") if isinstance(node, dict) else None
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        for group, members in raw.items():
            if isinstance(members, list):
                for m in members:
                    if m and m not in out:
                        out[str(m)] = str(group)
    return out


def pattern_config(kw: dict[str, Any]) -> dict[str, Any]:
    """模式层：模板骨架 + 校验词 + 已知实例。缺失时返回空结构，不阻塞运行。"""
    node = kw.get("模式层", {})
    if not isinstance(node, dict):
        node = {}
    def _list(key: str) -> list[str]:
        value = node.get(key, [])
        return [str(x) for x in value] if isinstance(value, list) else []
    known = node.get("已知实例", {})
    return {
        "skeletons": _list("模板骨架"),
        "context_words": _list("骨架校验词"),
        "self_validating": _list("自带校验骨架"),
        "known": known if isinstance(known, dict) else {},
    }
