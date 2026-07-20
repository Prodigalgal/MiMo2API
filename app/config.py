"""配置管理模块"""

import os
import json
import threading
from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass, asdict


@dataclass
class TempMailSettings:
    """Cloudflare 临时邮箱（用户在 UI 配置，不写死）"""
    api_base: str = ""
    admin_password: str = ""
    domain: str = ""
    site_password: str = ""
    # default registration region (must not be CN)
    register_region: str = "US"

    def to_dict(self, mask: bool = True) -> dict:
        d = asdict(self)
        if mask and self.admin_password:
            d["admin_password"] = "***" if len(self.admin_password) <= 3 else (
                self.admin_password[:1] + "***" + self.admin_password[-1:]
            )
        if mask and self.site_password:
            d["site_password"] = "***" if self.site_password else ""
        d["configured"] = bool((self.api_base or "").strip() and (self.admin_password or "").strip())
        return d


@dataclass
class MimoAccount:
    """Mimo账号配置"""
    service_token: str
    user_id: str
    xiaomichatbot_ph: str
    login_time: str = ""
    last_test: str = ""
    is_valid: bool = False
    # Google/Xiaomi email+password import & auto-renew fields
    email: str = ""
    password: str = ""
    pass_token: str = ""
    c_user_id: str = ""
    device_id: str = ""
    auto_renew: bool = True
    last_renew: str = ""
    renew_error: str = ""
    # temp-mail JWT for auto OTP when re-login needs mail code
    mail_jwt: str = ""
    region: str = ""

    def to_dict(self):
        d = asdict(self)
        d["token_masked"] = self.service_token[:16] + "..." + self.service_token[-6:] if len(self.service_token) > 22 else "***"
        # never expose secrets in API responses
        if d.get("password"):
            d["password"] = "***" if self.password else ""
        if d.get("pass_token"):
            pt = self.pass_token
            d["pass_token_masked"] = (pt[:12] + "..." + pt[-6:]) if len(pt) > 20 else ("***" if pt else "")
            d["pass_token"] = d["pass_token_masked"]
        if d.get("mail_jwt"):
            mj = self.mail_jwt
            d["mail_jwt_masked"] = (mj[:12] + "..." + mj[-6:]) if len(mj) > 20 else ("***" if mj else "")
            d["mail_jwt"] = d["mail_jwt_masked"]
        d["has_password"] = bool(self.password)
        d["has_pass_token"] = bool(self.pass_token)
        d["has_mail_jwt"] = bool(self.mail_jwt)
        return d


@dataclass
class Config:
    """应用配置"""
    api_keys: str = "sk-default"
    admin_password: str = "admin"
    mimo_accounts: List[MimoAccount] = None
    models: List[str] = None  # 自定义模型列表，None 表示自动探测
    tools_passthrough: bool = False  # 全局工具透传模式
    temp_mail: TempMailSettings = None

    def __post_init__(self):
        if self.mimo_accounts is None:
            self.mimo_accounts = []
        if self.models is None:
            self.models = []
        if self.temp_mail is None:
            self.temp_mail = TempMailSettings()

    def to_dict(self):
        d = {
            "api_keys": self.api_keys,
            "admin_password": self.admin_password,
            "mimo_accounts": [acc.to_dict() for acc in self.mimo_accounts],
            "tools_passthrough": self.tools_passthrough,
            "temp_mail": self.temp_mail.to_dict(mask=True) if self.temp_mail else TempMailSettings().to_dict(),
        }
        if self.models:
            d["models"] = self.models
        return d

    def to_save_dict(self):
        """用于保存到文件的格式（不含 token_masked / 脱敏字段）"""
        skip = {
            "token_masked", "pass_token_masked", "mail_jwt_masked",
            "has_password", "has_pass_token", "has_mail_jwt",
        }
        tm = self.temp_mail or TempMailSettings()
        d = {
            "api_keys": self.api_keys,
            "admin_password": self.admin_password,
            "mimo_accounts": [
                {
                    k: getattr(acc, k)
                    for k in MimoAccount.__dataclass_fields__
                    if k not in skip
                }
                for acc in self.mimo_accounts
            ],
            "tools_passthrough": self.tools_passthrough,
            "temp_mail": {
                "api_base": tm.api_base,
                "admin_password": tm.admin_password,
                "domain": tm.domain,
                "site_password": tm.site_password,
                "register_region": tm.register_region or "US",
            },
        }
        if self.models:
            d["models"] = self.models
        return d


class ConfigManager:
    """配置管理器 - 线程安全"""

    def __init__(self, config_file: str = os.getenv("MIMO2API_CONFIG_FILE", "config.json")):
        self.config_file = Path(config_file)
        self.config = Config()
        self.lock = threading.RLock()
        self.account_idx = 0
        self.load()

    @staticmethod
    def _parse_temp_mail(data: dict) -> TempMailSettings:
        raw = data.get("temp_mail") or {}
        if not isinstance(raw, dict):
            raw = {}
        fields = {k: raw.get(k, getattr(TempMailSettings, k, "")) for k in TempMailSettings.__dataclass_fields__}
        # keep unmasked secrets if client sent ***
        return TempMailSettings(
            api_base=str(fields.get("api_base") or ""),
            admin_password=str(fields.get("admin_password") or ""),
            domain=str(fields.get("domain") or ""),
            site_password=str(fields.get("site_password") or ""),
            register_region=str(fields.get("register_region") or "US"),
        )

    def load(self):
        """加载配置"""
        if not self.config_file.exists():
            self.save()
            return
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                accounts = [
                    MimoAccount(**{k: v for k, v in acc.items() if k in MimoAccount.__dataclass_fields__})
                    for acc in data.get('mimo_accounts', [])
                ]
                self.config = Config(
                    api_keys=data.get('api_keys', 'sk-default'),
                    admin_password=data.get('admin_password', 'admin'),
                    mimo_accounts=accounts,
                    models=data.get('models', []),
                    tools_passthrough=data.get('tools_passthrough', False),
                    temp_mail=self._parse_temp_mail(data),
                )
        except Exception as e:
            print(f"加载配置失败: {e}")
            self.config = Config()
            self.save()

    def save(self):
        """保存配置"""
        with self.lock:
            try:
                with open(self.config_file, 'w', encoding='utf-8') as f:
                    json.dump(self.config.to_save_dict(), f, indent=2, ensure_ascii=False)
            except Exception as e:
                print(f"保存配置失败: {e}")

    def validate_api_key(self, key: str) -> bool:
        """验证API Key"""
        with self.lock:
            keys = [k.strip() for k in self.config.api_keys.split(',')]
            return key in keys

    def get_next_account(self) -> Optional[MimoAccount]:
        """获取下一个账号（轮询）"""
        with self.lock:
            if not self.config.mimo_accounts:
                return None
            account = self.config.mimo_accounts[self.account_idx % len(self.config.mimo_accounts)]
            self.account_idx += 1
            return account

    def get_temp_mail_settings(self) -> TempMailSettings:
        with self.lock:
            return self.config.temp_mail or TempMailSettings()

    def update_temp_mail(self, data: dict, *, keep_secrets_if_masked: bool = True) -> TempMailSettings:
        """Update temp mail settings. If password fields are '***' keep previous."""
        with self.lock:
            prev = self.config.temp_mail or TempMailSettings()
            api_base = (data.get("api_base") if data.get("api_base") is not None else prev.api_base) or ""
            admin_password = data.get("admin_password")
            site_password = data.get("site_password")
            if keep_secrets_if_masked:
                if admin_password is None or str(admin_password).strip() in ("", "***") or (
                    isinstance(admin_password, str) and admin_password.startswith("*") and admin_password.endswith("*") and len(admin_password) <= 6
                ):
                    # only treat exact mask placeholders as keep
                    if admin_password is None or str(admin_password) in ("", "***") or (
                        isinstance(admin_password, str) and "***" in admin_password and len(admin_password) <= 8
                    ):
                        admin_password = prev.admin_password
                if site_password is None or str(site_password) in ("", "***"):
                    site_password = prev.site_password
            domain = data.get("domain") if data.get("domain") is not None else prev.domain
            region = data.get("register_region") if data.get("register_region") is not None else prev.register_region
            region = (region or "US").upper()
            if region in ("CN", "ZH", "CHINA"):
                region = "US"
            self.config.temp_mail = TempMailSettings(
                api_base=str(api_base).strip().rstrip("/"),
                admin_password=str(admin_password or ""),
                domain=str(domain or "").strip(),
                site_password=str(site_password or ""),
                register_region=region,
            )
            self.save()
            return self.config.temp_mail

    def update_config(self, new_config: dict):
        """更新配置"""
        with self.lock:
            accounts = [
                MimoAccount(**{k: v for k, v in acc.items() if k in MimoAccount.__dataclass_fields__})
                for acc in new_config.get('mimo_accounts', [])
            ]
            # preserve temp_mail if not provided; merge carefully if provided
            prev_tm = self.config.temp_mail or TempMailSettings()
            tm_raw = new_config.get("temp_mail")
            if isinstance(tm_raw, dict):
                admin_pw = tm_raw.get("admin_password")
                site_pw = tm_raw.get("site_password")
                if admin_pw is None or str(admin_pw) in ("", "***") or (
                    isinstance(admin_pw, str) and "***" in admin_pw and len(admin_pw) <= 8
                ):
                    admin_pw = prev_tm.admin_password
                if site_pw is None or str(site_pw) in ("", "***"):
                    site_pw = prev_tm.site_password
                region = (tm_raw.get("register_region") or prev_tm.register_region or "US").upper()
                if region in ("CN", "ZH", "CHINA"):
                    region = "US"
                temp_mail = TempMailSettings(
                    api_base=str(tm_raw.get("api_base", prev_tm.api_base) or "").strip().rstrip("/"),
                    admin_password=str(admin_pw or ""),
                    domain=str(tm_raw.get("domain", prev_tm.domain) or "").strip(),
                    site_password=str(site_pw or ""),
                    register_region=region,
                )
            else:
                temp_mail = prev_tm
            self.config = Config(
                api_keys=new_config.get('api_keys', 'sk-default'),
                admin_password=new_config.get('admin_password', 'admin'),
                mimo_accounts=accounts,
                models=new_config.get('models', []),
                tools_passthrough=new_config.get('tools_passthrough', False),
                temp_mail=temp_mail,
            )
            self.save()

    def get_config(self) -> dict:
        """获取配置"""
        with self.lock:
            return self.config.to_dict()


# 全局配置管理器实例
config_manager = ConfigManager()
