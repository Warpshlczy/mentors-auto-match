"""应用配置：默认值集中在此 + JSON 读写。

开发态：配置文件固定在 <项目根>/data/config.json。
打包态（PyInstaller）：配置与数据库改放用户目录，因为程序包本身是只读的。
storage.data_dir 只决定 SQLite 数据库与缓存的存放位置，不影响配置文件本身。
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

APP_NAME = "MentorMatch"

ROOT = Path(__file__).resolve().parent.parent
#: PyInstaller 打包后 __file__ 指向解压出来的临时目录，不能拿来存数据
FROZEN = bool(getattr(sys, "frozen", False))


def user_data_dir() -> Path:
    """打包后的数据目录：各平台的标准用户目录，保证可写、可持久。"""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        return Path(base) / APP_NAME if base else Path.home() / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


DEFAULT_DATA_DIR = user_data_dir() if FROZEN else ROOT / "data"
CONFIG_PATH = DEFAULT_DATA_DIR / "config.json"

# 院校白名单：名称必须与 CSRankings 的 institutions.csv 完全一致（注意其缩写写法），
# 见 README「数据源与覆盖范围」。填错 = 爬不到任何导师。
DEFAULT_SCHOOLS = [
    # 国内 985 / 211（CSRankings 有收录的）
    "Tsinghua University",
    "Peking University",
    "Zhejiang University",
    "Shanghai Jiao Tong University",
    "Harbin Institute of Technology",
    "Nanjing University",
    "Fudan University",
    "Beijing Institute of Technology",
    "Wuhan University",
    "Sun Yat-sen University",
    # 港澳
    "Chinese University of Hong Kong",
    "University of Hong Kong",
    # 海外 QS100
    "National University of Singapore",
    "ETH Zurich",
    "Carnegie Mellon University",
    "Univ. of Illinois at Urbana-Champaign",
]

DEFAULT_CONFIG: dict = {
    "llm": {
        "enabled": False,
        # Ollama 默认就暴露 OpenAI 兼容端点，云端 OpenAI 只需改 base_url + api_key
        "base_url": "http://localhost:11434/v1",
        "api_key": "",
        "model": "qwen2.5:7b",
        "timeout": 60,
    },
    "crawler": {
        "schools": list(DEFAULT_SCHOOLS),
        #: 每所勾选院校最多取多少位导师；0 = 该校在 CSRankings 里的全部导师
        "max_per_school": 30,
        "min_interval": 1.0,
        "max_interval": 2.0,
        "retries": 3,
        "fetch_papers": True,
        "paper_years": 5,
        "cache_ttl_days": 7,
    },
    "storage": {"data_dir": str(DEFAULT_DATA_DIR)},
}


def _deep_merge(base: dict, override: dict) -> dict:
    """用用户配置覆盖默认值，缺失的键保留默认值（新增配置项时不会 KeyError）。"""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load() -> dict:
    if CONFIG_PATH.exists():
        try:
            user_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            user_cfg = {}
    else:
        user_cfg = {}
    _drop_legacy_keys(user_cfg)
    return _deep_merge(DEFAULT_CONFIG, user_cfg)


def _drop_legacy_keys(user_cfg: dict) -> None:
    """删掉已废弃的配置项，避免它们在设置窗口保存时被原样写回。

    max_faculty 以前是「所有院校加起来的总量」，现在是每校上限（max_per_school），
    语义不同不能直接换算，所以丢弃旧值改用新的默认值。
    """
    crawler = user_cfg.get("crawler")
    if isinstance(crawler, dict):
        crawler.pop("max_faculty", None)


def save(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def data_dir(cfg: dict) -> Path:
    return Path(cfg["storage"]["data_dir"]).expanduser()


def cache_dir(cfg: dict) -> Path:
    return data_dir(cfg) / "cache"


def db_path(cfg: dict) -> Path:
    return data_dir(cfg) / "app.db"


def ensure_dirs(cfg: dict) -> None:
    cache_dir(cfg).mkdir(parents=True, exist_ok=True)
