"""配置管理模块"""

import os
import json
import threading
from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass, asdict


def _clamp_int(val, default: int, lo: int, hi: int) -> int:
    try:
        n = int(val)
    except Exception:
        return default
    return max(lo, min(hi, n))


def _clamp_float(val, default: float, lo: float, hi: float) -> float:
    try:
        n = float(val)
    except Exception:
        return default
    return max(lo, min(hi, n))


@dataclass
class TempMailSettings:
    """Cloudflare 临时邮箱 + 自动注册参数（全部在 UI 配置，不写死）"""
    api_base: str = ""
    admin_password: str = ""
    domain: str = ""
    site_password: str = ""
    # default registration region (must not be CN)
    register_region: str = "US"
    # —— 自动注册精细参数 ——
    # 单次批量最多尝试次数（上限）
    batch_count: int = 1
    # 成功达到此数量即结束（0=不提前结束，跑满 batch_count）
    success_target: int = 1
    # 最大并发注册数
    concurrent: int = 1
    # 启动每个并发任务之间的等待秒数
    concurrent_interval: float = 3.0
    # 图片验证码 OCR 最大重试次数
    captcha_retries: int = 10
    # 等待邮箱验证码超时（秒）
    otp_timeout: int = 120
    # 默认是否自动 OCR 图片验证码
    auto_captcha: bool = True

    def normalized(self) -> "TempMailSettings":
        """Return a copy with values clamped to safe ranges."""
        region = (self.register_region or "US").upper()
        if region in ("CN", "ZH", "CHINA"):
            region = "US"
        batch = _clamp_int(self.batch_count, 1, 1, 50)
        # success_target: 0 means no early stop; otherwise 1..batch
        st = _clamp_int(self.success_target, 1, 0, 50)
        if st > batch:
            st = batch
        return TempMailSettings(
            api_base=(self.api_base or "").strip().rstrip("/"),
            admin_password=self.admin_password or "",
            domain=(self.domain or "").strip(),
            site_password=self.site_password or "",
            register_region=region,
            batch_count=batch,
            success_target=st,
            concurrent=_clamp_int(self.concurrent, 1, 1, 10),
            concurrent_interval=_clamp_float(self.concurrent_interval, 3.0, 0.0, 300.0),
            captcha_retries=_clamp_int(self.captcha_retries, 10, 1, 30),
            otp_timeout=_clamp_int(self.otp_timeout, 120, 30, 600),
            auto_captcha=bool(self.auto_captcha),
        )

    def to_dict(self, mask: bool = True) -> dict:
        n = self.normalized()
        d = asdict(n)
        if mask and n.admin_password:
            d["admin_password"] = "***" if len(n.admin_password) <= 3 else (
                n.admin_password[:1] + "***" + n.admin_password[-1:]
            )
        if mask and n.site_password:
            d["site_password"] = "***" if n.site_password else ""
        d["configured"] = bool(n.api_base and n.admin_password)
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
            "temp_mail": asdict(tm.normalized()),
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
        fields = TempMailSettings.__dataclass_fields__
        def g(key, default=None):
            if key in raw and raw[key] is not None:
                return raw[key]
            return default if default is not None else getattr(TempMailSettings, key, "")

        auto_captcha = g("auto_captcha", True)
        if isinstance(auto_captcha, str):
            auto_captcha = auto_captcha.strip().lower() not in ("0", "false", "no", "off")

        return TempMailSettings(
            api_base=str(g("api_base", "") or ""),
            admin_password=str(g("admin_password", "") or ""),
            domain=str(g("domain", "") or ""),
            site_password=str(g("site_password", "") or ""),
            register_region=str(g("register_region", "US") or "US"),
            batch_count=_clamp_int(g("batch_count", 1), 1, 1, 50),
            success_target=_clamp_int(g("success_target", 1), 1, 0, 50),
            concurrent=_clamp_int(g("concurrent", 1), 1, 1, 10),
            concurrent_interval=_clamp_float(g("concurrent_interval", 3.0), 3.0, 0.0, 300.0),
            captcha_retries=_clamp_int(g("captcha_retries", 10), 10, 1, 30),
            otp_timeout=_clamp_int(g("otp_timeout", 120), 120, 30, 600),
            auto_captcha=bool(auto_captcha),
        ).normalized()

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
        """Update temp mail + register settings. Password fields with '***' keep previous."""
        with self.lock:
            prev = (self.config.temp_mail or TempMailSettings()).normalized()
            merged = {
                "api_base": prev.api_base,
                "admin_password": prev.admin_password,
                "domain": prev.domain,
                "site_password": prev.site_password,
                "register_region": prev.register_region,
                "batch_count": prev.batch_count,
                "success_target": prev.success_target,
                "concurrent": prev.concurrent,
                "concurrent_interval": prev.concurrent_interval,
                "captcha_retries": prev.captcha_retries,
                "otp_timeout": prev.otp_timeout,
                "auto_captcha": prev.auto_captcha,
            }
            for k in merged:
                if k in data and data[k] is not None:
                    merged[k] = data[k]

            admin_password = merged["admin_password"]
            site_password = merged["site_password"]
            if keep_secrets_if_masked:
                if admin_password is None or str(admin_password) in ("", "***") or (
                    isinstance(admin_password, str) and "***" in admin_password and len(admin_password) <= 8
                ):
                    admin_password = prev.admin_password
                if site_password is None or str(site_password) in ("", "***"):
                    site_password = prev.site_password

            auto_captcha = merged["auto_captcha"]
            if isinstance(auto_captcha, str):
                auto_captcha = auto_captcha.strip().lower() not in ("0", "false", "no", "off")

            self.config.temp_mail = TempMailSettings(
                api_base=str(merged.get("api_base") or "").strip().rstrip("/"),
                admin_password=str(admin_password or ""),
                domain=str(merged.get("domain") or "").strip(),
                site_password=str(site_password or ""),
                register_region=str(merged.get("register_region") or "US"),
                batch_count=_clamp_int(merged.get("batch_count"), 1, 1, 50),
                success_target=_clamp_int(merged.get("success_target"), 1, 0, 50),
                concurrent=_clamp_int(merged.get("concurrent"), 1, 1, 10),
                concurrent_interval=_clamp_float(merged.get("concurrent_interval"), 3.0, 0.0, 300.0),
                captcha_retries=_clamp_int(merged.get("captcha_retries"), 10, 1, 30),
                otp_timeout=_clamp_int(merged.get("otp_timeout"), 120, 30, 600),
                auto_captcha=bool(auto_captcha),
            ).normalized()
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
                # reuse update_temp_mail merge logic without double-lock issues
                payload = dict(tm_raw)
                # temporarily assign via same rules
                admin_pw = payload.get("admin_password")
                site_pw = payload.get("site_password")
                if admin_pw is None or str(admin_pw) in ("", "***") or (
                    isinstance(admin_pw, str) and "***" in admin_pw and len(admin_pw) <= 8
                ):
                    payload["admin_password"] = prev_tm.admin_password
                if site_pw is None or str(site_pw) in ("", "***"):
                    payload["site_password"] = prev_tm.site_password
                # fill missing keys from previous
                for k in TempMailSettings.__dataclass_fields__:
                    if k not in payload or payload[k] is None:
                        payload[k] = getattr(prev_tm, k)
                temp_mail = self._parse_temp_mail({"temp_mail": payload})
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
