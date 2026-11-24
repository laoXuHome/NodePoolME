"""Request/response models."""
from typing import Literal, Optional

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    password: str


class PasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6, max_length=128)


class AirportIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    url: str = Field(min_length=4, max_length=2048)
    enabled: bool = True
    interval_minutes: int = Field(default=60, ge=0, le=1440)   # 0 = manual only
    daily_time: Optional[str] = None
    filter_keywords: Optional[list[str]] = None


class AirportPatch(BaseModel):
    name: Optional[str] = Field(default=None, max_length=60)
    url: Optional[str] = Field(default=None, max_length=2048)
    enabled: Optional[bool] = None
    interval_minutes: Optional[int] = Field(default=None, ge=0, le=1440)
    daily_time: Optional[str] = None
    filter_keywords: Optional[list[str]] = None


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    ident: str = Field(min_length=1, max_length=12)
    url: str = Field(min_length=8, max_length=500)
    method: str = "GET"
    timeout: float = Field(default=8, ge=1, le=60)
    expect_status: str = "200-399"
    expect_body: str = ""
    max_latency: int = Field(default=0, ge=0, le=60000)
    interval_minutes: int = Field(default=30, ge=1, le=1440)
    enabled: bool = True
    sort_order: int = 10


class GroupPatch(BaseModel):
    name: Optional[str] = None
    ident: Optional[str] = None
    url: Optional[str] = None
    method: Optional[str] = None
    timeout: Optional[float] = Field(default=None, ge=1, le=60)
    expect_status: Optional[str] = None
    expect_body: Optional[str] = None
    max_latency: Optional[int] = Field(default=None, ge=0, le=60000)
    interval_minutes: Optional[int] = Field(default=None, ge=1, le=1440)
    enabled: Optional[bool] = None
    sort_order: Optional[int] = None


class SubSettings(BaseModel):
    sub_name: Optional[str] = None
    sub_token: Optional[str] = None
    regenerate_token: bool = False
    sub_filter_keywords: Optional[str] = Field(default=None, max_length=4000)


class SettingsIn(BaseModel):
    global_concurrency: Optional[int] = Field(default=None, ge=1, le=300)
    group_concurrency: Optional[int] = Field(default=None, ge=1, le=100)
    ip_concurrency: Optional[int] = Field(default=None, ge=1, le=10)
    subnet_concurrency: Optional[int] = Field(default=None, ge=1, le=20)
    request_interval_min_ms: Optional[int] = Field(default=None, ge=0, le=10000)
    request_interval_max_ms: Optional[int] = Field(default=None, ge=0, le=10000)
    test_timeout: Optional[int] = Field(default=None, ge=1, le=60)
    test_interval_minutes: Optional[int] = Field(default=None, ge=1, le=1440)
    test_on_start: Optional[bool] = None
    fail_threshold: Optional[int] = Field(default=None, ge=1, le=20)
    history_days: Optional[int] = Field(default=None, ge=7, le=365)
    dispersion_mask: Optional[int] = None
    latency_test_url: Optional[str] = None
    latency_test_url2: Optional[str] = None
    latency_test_name: Optional[str] = Field(default=None, max_length=16)
    latency_test_name2: Optional[str] = Field(default=None, max_length=16)
    latency_test_status: Optional[str] = None
    latency_test_timeout: Optional[int] = Field(default=None, ge=1, le=60)
    sub_max_latency: Optional[int] = Field(default=None, ge=0, le=60000)
    sub_max_latency2: Optional[int] = Field(default=None, ge=0, le=60000)
    sub_publish_enabled: Optional[bool] = None
    sub_publish_time: Optional[str] = Field(default=None, pattern=r"^[0-2][0-9]:[0-5][0-9]$")
    sub_publish_mode: Optional[str] = Field(default=None, pattern=r"^(time|interval)$")
    sub_publish_interval_minutes: Optional[int] = Field(default=None, ge=5, le=1440)


class RunIn(BaseModel):
    force: bool = False


class NodeTestIn(BaseModel):
    group_ids: Optional[list[int]] = None
    mode: Literal["all", "latency", "tags"] = "all"
