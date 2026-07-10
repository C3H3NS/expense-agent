"""
配置管理模块 —— 所有配置从这里读取，不要在其他文件里写死任何值。
"""
from pydantic_settings import BaseSettings
from typing import Optional
import os
import warnings


class Settings(BaseSettings):
    """应用全局配置"""

    # 应用
    app_name: str = "Expense-Agent"
    debug: bool = False
    port: int = 8000

    # Anthropic
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-20250514"

    # 百度 OCR
    baidu_api_key: str = ""
    baidu_secret_key: str = ""

    # 飞书
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_bot_webhook: str = ""
    feishu_verification_token: str = ""

    # 公司信息 & 规则默认值
    company_name: str = ""
    expense_monthly_limit: int = 3000
    expense_single_limit: int = 500
    expense_date_range_days: int = 7

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()

# 启动时检查关键配置
if os.path.exists(".env"):
    required_keys = ["anthropic_api_key", "baidu_api_key", "feishu_app_id"]
    missing = [k for k in required_keys if not getattr(settings, k)]
    if missing:
        warnings.warn(f"缺少必要配置项: {missing}，请检查 .env 文件")
