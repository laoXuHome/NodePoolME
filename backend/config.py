"""Runtime configuration. Everything is overridable through environment variables."""
import os
from pathlib import Path

APP_NAME = "NodePoolME"
VERSION = "1.2.94"

DATA_DIR = Path(os.getenv("NODEPOOL_DATA_DIR", "./data")).resolve()
CONFIG_DIR = Path(os.getenv("NODEPOOL_CONFIG_DIR", str(DATA_DIR / "config"))).resolve()
LOG_DIR = Path(os.getenv("NODEPOOL_LOG_DIR", str(DATA_DIR / "logs"))).resolve()
BACKUP_DIR = Path(os.getenv("NODEPOOL_BACKUP_DIR", str(DATA_DIR / "backup"))).resolve()
SUB_DIR = DATA_DIR / "sub"

DB_PATH = Path(os.getenv("NODEPOOL_DB", str(DATA_DIR / "nodepool.db"))).resolve()

ADMIN_PASSWORD = os.getenv("NODEPOOL_ADMIN_PASSWORD", "nodepool")
SECRET_KEY = os.getenv("NODEPOOL_SECRET", "nodepool-default-secret-change-me")
SESSION_HOURS = int(os.getenv("NODEPOOL_SESSION_HOURS", "72"))

# Testing engine: "mihomo" (full protocol support, needs the core binary) or "tcp" (connectivity only)
TEST_ENGINE = os.getenv("NODEPOOL_TEST_ENGINE", "mihomo").lower()
MIHOMO_BIN = os.getenv("NODEPOOL_MIHOMO_BIN", "/usr/local/bin/mihomo")
MIHOMO_API = os.getenv("NODEPOOL_MIHOMO_API", "http://127.0.0.1:9090")
MIHOMO_BASE_PORT = int(os.getenv("NODEPOOL_MIHOMO_BASE_PORT", "31000"))
MIHOMO_BATCH = int(os.getenv("NODEPOOL_MIHOMO_BATCH", "80"))

# One fixed client identity: airports score/risk-control repeated UA switching, and
# Clash-family clients get the most complete node list from most panels.
HTTP_UA = os.getenv("NODEPOOL_UA", "clash-verge/v1.19.6")

DEFAULT_GROUPS = [
    {
        "name": "ChatGPT",
        "ident": "G",
        "url": "https://chatgpt.com/",
        "expect_status": "200-399",
        "sort_order": 1,
    },
    {
        "name": "Telegram",
        "ident": "TG",
        "url": "https://telegram.org/",
        "expect_status": "200-399",
        "sort_order": 2,
    },
    {
        "name": "YouTube",
        "ident": "Y",
        "url": "https://www.youtube.com/generate_204",
        "expect_status": "204",
        "sort_order": 3,
    },
]

# 机场节点名过滤：命中任一关键词的节点整条不导入，既不检测也不下发
DEFAULT_FILTER_KEYWORDS = [
    "官网", "工单", "客服", "下载", "失效", "复制", "客户端",
    "如果", "网站", "剩余", "套餐", "重置", "到期", "流量",
]

DEFAULT_SETTINGS = {
    "global_concurrency": "10",
    "group_concurrency": "5",
    "ip_concurrency": "1",
    "subnet_concurrency": "2",
    "request_interval_min_ms": "200",
    "request_interval_max_ms": "800",
    "test_timeout": "8",
    "test_interval_minutes": "30",
    "test_on_start": "1",
    "fail_threshold": "3",
    "history_days": "30",
    "sub_name": "NodePool Unified",
    "sub_token": "",
    "sub_max_latency": "1000",
    "sub_max_latency2": "1000",
    "latency_test_enabled": "1",   # kept for compatibility: the probe is always on
    "latency_test_url": "http://www.google.com/favicon.ico",
    "latency_test_name": "Google",
    "latency_test_url2": "http://www.wikipedia.org/favicon.ico",
    "latency_test_name2": "Wikipedia",
    "latency_test_status": "200-399",
    "latency_test_timeout": "8",
    "sub_publish_enabled": "0",
    "sub_publish_time": "04:30",
    "sub_publish_mode": "time",              # time = 每天固定时刻 / interval = 固定间隔
    "sub_publish_interval_minutes": "180",
    "sub_max_nodes": "0",
    "sub_min_groups": "1",
    "sub_include_unavailable": "0",
    "sub_required_tags": "",
    "dispersion_mask": "24",
}

for _d in (DATA_DIR, CONFIG_DIR, LOG_DIR, BACKUP_DIR, SUB_DIR):
    _d.mkdir(parents=True, exist_ok=True)
