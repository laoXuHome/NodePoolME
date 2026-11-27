"""Runtime configuration. Everything is overridable through environment variables."""
import os
from pathlib import Path

APP_NAME = "NodePoolME"
VERSION = "2.011"

DATA_DIR = Path(os.getenv("NODEPOOL_DATA_DIR", "./data")).resolve()
CONFIG_DIR = Path(os.getenv("NODEPOOL_CONFIG_DIR", str(DATA_DIR / "config"))).resolve()
LOG_DIR = Path(os.getenv("NODEPOOL_LOG_DIR", str(DATA_DIR / "logs"))).resolve()
BACKUP_DIR = Path(os.getenv("NODEPOOL_BACKUP_DIR", str(DATA_DIR / "backup"))).resolve()
SUB_DIR = DATA_DIR / "sub"

# 维护状态目录（v1.10 计划 §2.6）：存放 maintenance.json。
# 必须与业务 SQLite 库**同文件系统**（启动自检校验 st_dev），但**不得**等于 DATA_DIR
# ——维护文件与业务数据分离，避免清理 data 目录时误删维护文件。
STATE_DIR = Path(os.getenv("NODEPOOL_STATE_DIR", str(DATA_DIR / "state"))).resolve()
STATE_DIR_CONFIG_ERROR = (
    "state_dir_equals_data_dir" if STATE_DIR == DATA_DIR else None
)

DB_PATH = Path(os.getenv("NODEPOOL_DB", str(DATA_DIR / "nodepool.db"))).resolve()

# 迁移入口开关（v1.10 计划 §3.4）：默认关闭。P-1.6 只允许在维护窗口内、对副本
# 执行空迁移 dry-run；生产迁移窗口（P-1.8）才由运维显式置 1。
MIGRATION_ENABLED = os.getenv("NODEPOOL_MIGRATION_ENABLED", "0").strip() == "1"

# 非 DB 诊断通道（v1.10 设计 §1.3）：K1 未成立 / halt / 失败时唯一允许的落点。
# 必须与业务库同卷（STATE_DIR 已由启动自检校验 st_dev），不得写进业务表。
MIGRATION_FAULT_LOG = Path(os.getenv("NODEPOOL_MIGRATION_FAULT_LOG",
                                     str(STATE_DIR / "migration-fault.log"))).resolve()

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

# 公共下载测速源：全部为直连 HTTPS，不需要注册或自建服务器。
# priority 0 = 主测速源（亚洲/欧洲各一组），1 = 备用；首次使用前必须通过健康检查。
DEFAULT_SPEED_SOURCES = [
    {"name": "Vultr 新加坡", "region": "SG", "priority": 0,
     "url": "https://sgp-ping.vultr.com/vultr.com.100MB.bin"},
    {"name": "OVHcloud 新加坡", "region": "SG", "priority": 0,
     "url": "https://sin1-sgp.speedtest.network.ovh.net/files/100Mb.dat"},
    {"name": "Hetzner 纽伦堡", "region": "DE", "priority": 0,
     "url": "https://nbg1-speed.hetzner.com/100MB.bin"},
    {"name": "Hetzner 法尔肯施泰因", "region": "DE", "priority": 1,
     "url": "https://fsn1-speed.hetzner.com/100MB.bin"},
    {"name": "OVHcloud 法国", "region": "FR", "priority": 1,
     "url": "https://lil2-gra.speedtest.network.ovh.net/files/100Mb.dat"},
    {"name": "Vultr 东京", "region": "JP", "priority": 1,
     "url": "https://hnd-jp-ping.vultr.com/vultr.com.100MB.bin"},
    {"name": "Vultr 洛杉矶", "region": "US", "priority": 1,
     "url": "https://lax-ca-us-ping.vultr.com/vultr.com.100MB.bin"},
    {"name": "Hetzner 俄勒冈", "region": "US", "priority": 1,
     "url": "https://hil-speed.hetzner.com/100MB.bin"},
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
    "sub_filter_keywords": "",
    "sub_filter_speed_levels": "",
    "dispersion_mask": "24",
    "speed_enabled": "1",
    "speed_primary_sources": "3",
    "speed_connections": "4",
    "speed_parallel_nodes": "4",
    "speed_warmup_seconds": "2",
    "speed_sample_seconds": "10",
    "speed_connect_timeout": "8",
    "speed_total_timeout": "20",
    "speed_retry": "1",
    # 定时测速：北京时间晚高峰窗口内随机分散，每天或每隔 N 天一次
    "speed_schedule_enabled": "1",
    "speed_timezone": "Asia/Shanghai",
    "speed_window_start": "18:00",
    "speed_window_end": "23:30",
    "speed_schedule_every_days": "1",
    "speed_schedule_last_date": "",
    # 单节点单轮总下载流量上限（所有测速源累计）
    "speed_traffic_limit_mb": "50",
    # 最新有效结果保留时长；过期只显示待测速，不判故障、不补测
    "speed_result_ttl_days": "3",
    # 四档速度阈值，单位 MB/s（512 KB/s = 0.5 MB/s）
    "speed_grade_excellent": "3",
    "speed_grade_good": "1.5",
    "speed_grade_poor": "0.5",
}

for _d in (DATA_DIR, CONFIG_DIR, LOG_DIR, BACKUP_DIR, SUB_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# 合法配置：覆盖路径不存在时创建空目录（只建空目录，不写任何业务文件）。
# 非法配置（STATE_DIR == DATA_DIR）**不建目录**：由启动自检拦下并只禁用迁移入口。
if STATE_DIR_CONFIG_ERROR is None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
