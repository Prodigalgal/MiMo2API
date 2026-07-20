"""Xiaomi account email registration via pure HTTP API.

Flow:
1. AES-encrypt email+password, RSA-wrap AES key (EUI header)
2. Image captcha: GET /pass/getCode?icodeType=register
3. POST /pass/sendEmailRegTicket  (triggers verification email)
4. Poll temp mail for code
5. POST /pass/verifyEmailRegTicket
6. Login via existing xiaomi_login to obtain aistudio tokens

Region must NOT be CN (user requirement). Default: US.
"""

from __future__ import annotations

import base64
import hashlib
import random
import re
import string
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

import httpx

from .temp_mail import (
    TempMailConfig,
    TempAddress,
    TempMailError,
    create_address,
    wait_for_code,
)

ACCOUNT = "https://account.xiaomi.com"
AISTUDIO = "https://aistudio.xiaomimimo.com"
SID_DEFAULT = "xiaomichatbot"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)

# Production public key used by account.xiaomi.com frontend encryptAes
_RSA_PUB_B64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCYEVrK/4Mahiv0pUJgTybx4J9P5dUT"
    "/Y0PuwMbk+gMU+jrZnBiXGv6/hCH1avIhoBcE535F8nJQQN3UavZdFkYidsoXuEnat3+"
    "eVTp3FslyhRwIBDF09v4vDhRtxFOT+R7uH7h/mzmyA2/+lfIMWGIrffXprYizbV76+YQ"
    "KhoqFQIDAQAB"
)
_AES_IV = b"0102030405060708"
_AES_KEY_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*"

# In-memory pending registration sessions (captcha step)
_PENDING_REG: Dict[str, "PendingRegister"] = {}


class XiaomiRegisterError(Exception):
    def __init__(self, message: str, code: Optional[int] = None, data: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.data = data or {}


@dataclass
class PendingRegister:
    session_id: str
    email: str
    password: str
    region: str
    sid: str
    device_id: str
    encrypted_email: str
    encrypted_password: str
    eui: str
    mail_jwt: str
    mail_address: str
    cookies: List[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    ticket_sent: bool = False
    captcha_b64: str = ""

    def to_public(self) -> dict:
        return {
            "session_id": self.session_id,
            "email": self.email,
            "region": self.region,
            "need_captcha": not self.ticket_sent,
            "ticket_sent": self.ticket_sent,
            "captcha_image": self.captcha_b64,
            "message": (
                "请填写图片验证码后继续"
                if not self.ticket_sent
                else "验证码邮件已发送，正在等待邮箱验证码"
            ),
        }


def _strip_json(text: str) -> dict:
    import json as _json

    if not text:
        return {}
    if text.startswith("&&&START&&&"):
        text = text[11:]
    try:
        return _json.loads(text)
    except Exception:
        return {"_raw": text[:500]}


def _new_device_id() -> str:
    return "wb_" + str(uuid.uuid4())


def random_password(length: int = 12) -> str:
    """8–16 chars, mix letters+digits+special (Xiaomi policy)."""
    length = max(8, min(16, length))
    lower = string.ascii_lowercase
    upper = string.ascii_uppercase
    digits = string.digits
    special = "!@#$%&*"
    # ensure at least 2 character classes
    chars = [
        random.choice(lower),
        random.choice(upper),
        random.choice(digits),
        random.choice(special),
    ]
    pool = lower + upper + digits + special
    chars += [random.choice(pool) for _ in range(length - 4)]
    random.shuffle(chars)
    return "".join(chars)


def encrypt_aes(fields: Dict[str, str]) -> Dict[str, Any]:
    """Mirror frontend encryptAes: AES-CBC + RSA-PKCS1 encrypt of AES key."""
    from Crypto.Cipher import AES, PKCS1_v1_5
    from Crypto.PublicKey import RSA
    from Crypto.Util.Padding import pad

    key_str = "".join(random.choice(_AES_KEY_CHARS) for _ in range(16))
    key_bytes = key_str.encode("utf-8")
    iv = _AES_IV

    encrypted_params: Dict[str, str] = {}
    for k, v in fields.items():
        cipher = AES.new(key_bytes, AES.MODE_CBC, iv)
        ct = cipher.encrypt(pad(str(v).encode("utf-8"), AES.block_size))
        # CryptoJS AES.encrypt default output: OpenSSL-compatible Base64 (Salted__...)
        # Frontend uses CryptoJS which by default produces OpenSSL salted format when
        # passphrase is WordArray... Actually they pass WordArray key directly:
        #   CryptoJS.AES.encrypt(t, Q, {iv:i, padding:Pkcs7}).toString()
        # With WordArray key, CryptoJS does NOT salt — ciphertext is Base64(iv||ciphertext)?
        # Actually CryptoJS when key is WordArray: ciphertext only Base64 of raw ciphertext
        # (CipherParams.toString() uses OpenSSLFormatter which without salt is just Base64(ciphertext))
        encrypted_params[k] = base64.b64encode(ct).decode("ascii")

    # RSA encrypt of base64(aes_key) — JS: rsa.encrypt(btoa(key))
    pub_der = base64.b64decode(_RSA_PUB_B64)
    rsa_key = RSA.import_key(pub_der)
    rsa_cipher = PKCS1_v1_5.new(rsa_key)
    aes_key_b64 = base64.b64encode(key_bytes).decode("ascii")
    rsa_out = rsa_cipher.encrypt(aes_key_b64.encode("ascii"))
    rsa_b64 = base64.b64encode(rsa_out).decode("ascii")

    keys_joined = base64.b64encode(",".join(fields.keys()).encode("utf-8")).decode("ascii")
    eui = f"{rsa_b64}.{keys_joined}"
    return {"EUI": eui, "encryptedParams": encrypted_params}


def _client(device_id: str, cookies: Optional[List[dict]] = None) -> httpx.AsyncClient:
    c = httpx.AsyncClient(
        follow_redirects=False,
        timeout=30.0,
        headers={
            "User-Agent": UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "application/json, text/plain, */*",
        },
    )
    for domain in ("xiaomi.com", "account.xiaomi.com", "mi.com"):
        c.cookies.set("sdkVersion", "accountsdk-18.8.15", domain=domain)
        c.cookies.set("deviceId", device_id, domain=domain)
        c.cookies.set("uLocale", "en", domain=domain)
    if cookies:
        for item in cookies:
            try:
                domain = (item.get("domain") or "account.xiaomi.com").lstrip(".")
                c.cookies.set(
                    item["name"],
                    item["value"],
                    domain=domain,
                    path=item.get("path") or "/",
                )
            except Exception:
                pass
    return c


def _dump_cookies(client: httpx.AsyncClient) -> List[dict]:
    out = []
    for cookie in client.cookies.jar:
        out.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain or "",
                "path": cookie.path or "/",
            }
        )
    return out


def _prune_pending() -> None:
    now = time.time()
    for k, v in list(_PENDING_REG.items()):
        if now - v.created_at > 1800:
            _PENDING_REG.pop(k, None)


async def fetch_captcha(client: httpx.AsyncClient) -> str:
    """GET image captcha; returns base64 data-url. Sets ick cookie on client."""
    r = await client.get(
        f"{ACCOUNT}/pass/getCode",
        params={"icodeType": "register", "_": int(time.time() * 1000)},
        headers={
            "Referer": f"{ACCOUNT}/fe/service/register",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        },
    )
    if r.status_code != 200 or not r.content:
        raise XiaomiRegisterError(f"获取验证码图片失败 HTTP {r.status_code}")
    b64 = base64.b64encode(r.content).decode("ascii")
    ctype = r.headers.get("content-type") or "image/jpeg"
    if "png" in ctype:
        mime = "image/png"
    elif "gif" in ctype:
        mime = "image/gif"
    else:
        mime = "image/jpeg"
    return f"data:{mime};base64,{b64}"


async def start_register(
    mail_cfg: TempMailConfig,
    *,
    region: str = "US",
    password: Optional[str] = None,
    sid: str = SID_DEFAULT,
    domain: Optional[str] = None,
) -> Dict[str, Any]:
    """Create temp mail + encrypt credentials + fetch captcha. Returns pending session."""
    region = (region or "US").upper().strip()
    if region in ("CN", "ZH", "CHINA"):
        raise XiaomiRegisterError("注册地区不能选择中国（CN），请使用 US / SG / JP 等")

    password = password or random_password()
    addr = await create_address(mail_cfg, domain=domain)
    enc = encrypt_aes({"email": addr.address, "password": password})
    ep = enc["encryptedParams"]

    device_id = _new_device_id()
    client = _client(device_id)
    try:
        captcha_b64 = await fetch_captcha(client)
        sid_session = uuid.uuid4().hex
        pending = PendingRegister(
            session_id=sid_session,
            email=addr.address,
            password=password,
            region=region,
            sid=sid or SID_DEFAULT,
            device_id=device_id,
            encrypted_email=ep["email"],
            encrypted_password=ep["password"],
            eui=enc["EUI"],
            mail_jwt=addr.jwt,
            mail_address=addr.address,
            cookies=_dump_cookies(client),
            captcha_b64=captcha_b64,
            ticket_sent=False,
        )
        _PENDING_REG[sid_session] = pending
        _prune_pending()
        return {
            "ok": True,
            "need_captcha": True,
            "session_id": sid_session,
            "email": addr.address,
            "password": password,
            "region": region,
            "captcha_image": captcha_b64,
            "mail_jwt": addr.jwt,
            "message": "临时邮箱已创建，请填写图片验证码后继续注册",
        }
    finally:
        await client.aclose()


async def refresh_captcha(session_id: str) -> Dict[str, Any]:
    pending = _PENDING_REG.get(session_id or "")
    if not pending:
        raise XiaomiRegisterError("注册会话已过期，请重新开始")
    client = _client(pending.device_id, pending.cookies)
    try:
        captcha_b64 = await fetch_captcha(client)
        pending.captcha_b64 = captcha_b64
        pending.cookies = _dump_cookies(client)
        _PENDING_REG[session_id] = pending
        return {
            "ok": True,
            "session_id": session_id,
            "captcha_image": captcha_b64,
            "email": pending.email,
        }
    finally:
        await client.aclose()


async def _send_email_reg_ticket(
    client: httpx.AsyncClient,
    pending: PendingRegister,
    icode: str,
) -> dict:
    body = {
        "email": pending.encrypted_email,
        "password": pending.encrypted_password,
        "region": pending.region,
        "sid": pending.sid,
        "icode": (icode or "").strip(),
        "_json": "true",
    }
    r = await client.post(
        f"{ACCOUNT}/pass/sendEmailRegTicket",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": ACCOUNT,
            "Referer": f"{ACCOUNT}/fe/service/register",
            "X-Requested-With": "XMLHttpRequest",
            "EUI": pending.eui,
        },
    )
    return _strip_json(r.text)


async def _verify_email_reg_ticket(
    client: httpx.AsyncClient,
    pending: PendingRegister,
    ticket: str,
    *,
    device_fingerprint: str = "",
) -> dict:
    # qs for aistudio STS-friendly login after register
    qs = f"%3Fsid%3D{pending.sid}%26_json%3Dtrue"
    callback = f"{AISTUDIO}/sts"
    body = {
        "ticket": ticket.strip(),
        "region": pending.region,
        "email": pending.encrypted_email,
        "env": "web",
        "qs": qs,
        "isAcceptLicense": "true",
        "sid": pending.sid,
        "password": pending.encrypted_password,
        "policyName": "globalmiaccount",
        "callback": callback,
        "deviceFingerprint": device_fingerprint or hashlib.md5(
            f"{pending.device_id}-{time.time()}".encode()
        ).hexdigest(),
        "_json": "true",
    }
    r = await client.post(
        f"{ACCOUNT}/pass/verifyEmailRegTicket",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": ACCOUNT,
            "Referer": f"{ACCOUNT}/fe/service/register",
            "X-Requested-With": "XMLHttpRequest",
            "EUI": pending.eui,
        },
    )
    return _strip_json(r.text)


async def submit_captcha_and_register(
    session_id: str,
    icode: str,
    mail_cfg: TempMailConfig,
    *,
    otp_timeout: float = 120.0,
) -> Dict[str, Any]:
    """Submit captcha → send mail ticket → wait code → verify → return credentials."""
    pending = _PENDING_REG.get(session_id or "")
    if not pending:
        raise XiaomiRegisterError("注册会话已过期，请重新开始")
    if not (icode or "").strip():
        raise XiaomiRegisterError("请填写验证码")

    client = _client(pending.device_id, pending.cookies)
    try:
        j = await _send_email_reg_ticket(client, pending, icode)
        code = j.get("code")
        if code not in (0, "0", None):
            # captcha wrong → refresh image for retry
            if int(code or 0) in (87001, 70014, 1200212):
                try:
                    pending.captcha_b64 = await fetch_captcha(client)
                    pending.cookies = _dump_cookies(client)
                    _PENDING_REG[session_id] = pending
                except Exception:
                    pass
                raise XiaomiRegisterError(
                    j.get("desc") or j.get("description") or "验证码错误",
                    code=int(code) if code is not None else None,
                    data={**j, "captcha_image": pending.captcha_b64},
                )
            if int(code or 0) == 25001:
                raise XiaomiRegisterError("邮箱已被注册", code=25001, data=j)
            raise XiaomiRegisterError(
                j.get("desc") or j.get("description") or f"发送注册邮件失败: {j}",
                code=int(code) if str(code).isdigit() else None,
                data=j,
            )

        pending.ticket_sent = True
        pending.cookies = _dump_cookies(client)
        _PENDING_REG[session_id] = pending

        # Wait for email OTP from temp mail
        mail_code = await wait_for_code(
            mail_cfg,
            pending.mail_jwt,
            timeout=otp_timeout,
        )

        j2 = await _verify_email_reg_ticket(client, pending, mail_code)
        code2 = j2.get("code")
        if code2 not in (0, "0", None):
            raise XiaomiRegisterError(
                j2.get("desc") or j2.get("description") or f"校验邮箱验证码失败: {j2}",
                code=int(code2) if str(code2).isdigit() else None,
                data=j2,
            )

        # Registration succeeded — login to get MiMo tokens
        from .xiaomi_login import login_with_password, XiaomiLoginError

        login_result = await login_with_password(pending.email, pending.password)
        # If need OTP for login, auto-handle via temp mail
        if login_result.get("need_otp") and login_result.get("session_id"):
            from .xiaomi_login import send_pending_email_otp

            otp_sid = login_result["session_id"]
            await send_pending_email_otp(otp_sid)
            login_code = await wait_for_code(
                mail_cfg,
                pending.mail_jwt,
                timeout=otp_timeout,
            )
            login_result = await login_with_password(
                pending.email,
                pending.password,
                otp_code=login_code,
                session_id=otp_sid,
            )

        if not login_result.get("ok"):
            # still return credentials so user can import later
            _PENDING_REG.pop(session_id, None)
            return {
                "ok": True,
                "registered": True,
                "logged_in": False,
                "email": pending.email,
                "password": pending.password,
                "region": pending.region,
                "mail_jwt": pending.mail_jwt,
                "login": login_result,
                "message": "注册成功，但自动登录未完成，请手动导入",
            }

        tokens = login_result.get("tokens") or {}
        _PENDING_REG.pop(session_id, None)
        return {
            "ok": True,
            "registered": True,
            "logged_in": True,
            "email": pending.email,
            "password": pending.password,
            "region": pending.region,
            "mail_jwt": pending.mail_jwt,
            "tokens": tokens,
            "message": "注册并登录成功",
        }
    except TempMailError as e:
        raise XiaomiRegisterError(str(e), data=e.data) from e
    finally:
        await client.aclose()


async def auto_register(
    mail_cfg: TempMailConfig,
    *,
    region: str = "US",
    password: Optional[str] = None,
    icode: Optional[str] = None,
    session_id: Optional[str] = None,
    otp_timeout: float = 120.0,
    domain: Optional[str] = None,
) -> Dict[str, Any]:
    """
    High-level register entry:
    - no session: start (return captcha)
    - session + icode: complete registration
    """
    if session_id and icode:
        return await submit_captcha_and_register(
            session_id, icode, mail_cfg, otp_timeout=otp_timeout
        )
    if session_id and not icode:
        raise XiaomiRegisterError("请填写验证码")
    return await start_register(
        mail_cfg, region=region, password=password, domain=domain
    )
