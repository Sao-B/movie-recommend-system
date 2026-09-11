#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目根目录 config.yaml 的统一读取入口。

工具脚本的一切凭据都必须从这里取，禁止硬编码。
config.yaml 已在 .gitignore / .aiignore 中，不会进入版本库，也不会进 AI 上下文。

用法::

    from config_loader import mysql_kwargs, tmdb_config, vm_config

    conn = pymysql.connect(**mysql_kwargs())
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

import yaml

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")


class ConfigError(RuntimeError):
    """config.yaml 缺失或缺少必需字段。"""


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """读取并解析 config.yaml。"""
    path = path or CONFIG_PATH
    if not os.path.exists(path):
        raise ConfigError(
            f"未找到 {path}。请复制 config.example.yaml 为 config.yaml 并填入真实值。"
        )
    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    if not isinstance(config, dict):
        raise ConfigError(f"{path} 的顶层结构应为映射（dict）。")
    return config


def _require(config: Dict[str, Any], section: str, *keys: str) -> Dict[str, Any]:
    block = config.get(section)
    if not isinstance(block, dict):
        raise ConfigError(f"config.yaml 缺少 [{section}] 段。")
    missing = [k for k in keys if not block.get(k)]
    if missing:
        raise ConfigError(
            f"config.yaml 的 [{section}] 段缺少字段: {', '.join(missing)}"
        )
    return block


def mysql_kwargs(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """返回 pymysql.connect(**kwargs) 所需的 MySQL 连接参数。"""
    config = config or load_config()
    m = _require(config, "mysql", "host", "user", "password", "database")
    return {
        "host": m["host"],
        "port": int(m.get("port", 3306)),
        "user": m["user"],
        "password": m["password"],
        "database": m["database"],
        "charset": m.get("charset", "utf8mb4"),
    }


def tmdb_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """返回 TMDB 的 api_key / base_url / poster_base_url。"""
    config = config or load_config()
    t = _require(config, "tmdb", "api_key")
    return {
        "api_key": t["api_key"],
        "base_url": t.get("base_url", "https://api.themoviedb.org/3"),
        "poster_base_url": t.get("poster_base_url", "https://image.tmdb.org/t/p/w500"),
    }


def vm_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """返回虚拟机 SSH 与虚拟机内 MySQL 的连接参数（sync_movie_stats.py 用）。"""
    config = config or load_config()
    v = _require(
        config,
        "vm",
        "host",
        "ssh_user",
        "ssh_password",
        "mysql_user",
        "mysql_password",
        "mysql_database",
    )
    return dict(v)
