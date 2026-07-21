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
from typing import Any, Callable, Dict, List, Optional

import httpx

# progress_cb(stage, message, email=None)
ProgressCb = Optional[Callable[[str, str, Optional[str]], None]]


def _emit(cb: ProgressCb, stage: str, message: str, email: Optional[str] = None) -> None:
    if not cb:
        return
    try:
        cb(stage, message, email)
    except Exception:
        pass

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

# Safe non-CN regions for registration (country codes used by Xiaomi account)
REGISTER_REGIONS = (
    "US", "SG", "JP", "HK", "TW", "GB", "DE", "FR", "IT", "ES",
    "NL", "AU", "CA", "KR", "IN", "ID", "TH", "MY", "PH", "VN",
    "BR", "MX", "PL", "SE", "CH", "AT", "BE", "IE", "NZ", "AE",
)
_CN_ALIASES = frozenset({"CN", "ZH", "CHINA", "PRC"})


def resolve_region(region: Optional[str] = None) -> str:
    """Resolve region code. RANDOM/AUTO/* → pick from REGISTER_REGIONS. Never CN."""
    r = (region or "US").strip().upper()
    if r in ("RANDOM", "RAND", "AUTO", "*", "RND"):
        return random.choice(REGISTER_REGIONS)
    if r in _CN_ALIASES:
        raise XiaomiRegisterError("注册地区不能选择中国（CN），请使用 US / SG / JP 或 RANDOM")
    # allow any non-empty 2-letter-ish code except CN; prefer known list
    if len(r) < 2:
        return random.choice(REGISTER_REGIONS)
    return r

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


_OCR_ENGINE = None
_OCR_INITED = False


def ocr_available() -> bool:
    """Whether ddddocr (or compatible) can be loaded."""
    try:
        _get_ocr()
        return _OCR_ENGINE is not None
    except Exception:
        return False


def _get_ocr():
    global _OCR_ENGINE, _OCR_INITED
    if _OCR_INITED:
        return _OCR_ENGINE
    _OCR_INITED = True
    try:
        import ddddocr  # type: ignore

        _OCR_ENGINE = ddddocr.DdddOcr(show_ad=False)
    except Exception as e:
        print(f"[Register] ddddocr unavailable, captcha auto-solve disabled: {e}")
        _OCR_ENGINE = None
    return _OCR_ENGINE


def solve_captcha_image(image_bytes: bytes) -> str:
    """OCR image captcha → cleaned alphanumeric/text string."""
    ocr = _get_ocr()
    if ocr is None:
        return ""
    try:
        raw = ocr.classification(image_bytes)
    except Exception as e:
        print(f"[Register] OCR failed: {e}")
        return ""
    # keep letters/digits (Xiaomi register captcha is usually alphanumeric)
    text = re.sub(r"[^0-9A-Za-z]", "", str(raw or ""))
    return text


def solve_captcha_candidates(image_bytes: bytes) -> List[str]:
    """OCR + case variants (Xiaomi icode often case-insensitive)."""
    primary = solve_captcha_image(image_bytes)
    if not primary:
        return []
    out: List[str] = []
    for t in (primary, primary.lower(), primary.upper()):
        if t and t not in out:
            out.append(t)
    return out


def _captcha_data_url(image_bytes: bytes, content_type: str = "") -> str:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    ctype = (content_type or "image/jpeg").lower()
    if "png" in ctype:
        mime = "image/png"
    elif "gif" in ctype:
        mime = "image/gif"
    else:
        mime = "image/jpeg"
    return f"data:{mime};base64,{b64}"


async def fetch_captcha(client: httpx.AsyncClient) -> tuple:
    """GET image captcha. Returns (raw_bytes, data_url). Sets ick cookie on client."""
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
    data_url = _captcha_data_url(r.content, r.headers.get("content-type") or "")
    return r.content, data_url


async def start_register(
    mail_cfg: TempMailConfig,
    *,
    region: str = "US",
    password: Optional[str] = None,
    sid: str = SID_DEFAULT,
    domain: Optional[str] = None,
    auto_captcha: bool = True,
    captcha_retries: int = 8,
    otp_timeout: float = 120.0,
    on_progress: ProgressCb = None,
) -> Dict[str, Any]:
    """Create temp mail + encrypt credentials.

    When auto_captcha=True (default) and ddddocr available: OCR + retry until
    ticket sent, then wait mail code and finish registration in one shot.
    Otherwise returns need_captcha for manual UI fill.
    """
    region = resolve_region(region)
    _emit(on_progress, "region", f"选定注册地区 {region}")
    print(f"[Register] using region={region}")

    password = password or random_password()
    _emit(on_progress, "temp_mail", "正在创建临时邮箱…")
    addr = await create_address(mail_cfg, domain=domain)
    _emit(on_progress, "temp_mail", f"已获取邮箱 {addr.address}", addr.address)

    enc = encrypt_aes({"email": addr.address, "password": password})
    ep = enc["encryptedParams"]
    _emit(on_progress, "encrypt", "凭证已加密", addr.address)

    device_id = _new_device_id()
    client = _client(device_id)
    try:
        _emit(on_progress, "captcha", "正在获取图片验证码…", addr.address)
        img_bytes, captcha_b64 = await fetch_captcha(client)
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

        can_auto = auto_captcha and ocr_available()
        if can_auto:
            return await _auto_solve_and_finish(
                client,
                pending,
                mail_cfg,
                first_image=img_bytes,
                first_data_url=captcha_b64,
                captcha_retries=captcha_retries,
                otp_timeout=otp_timeout,
                on_progress=on_progress,
            )

        _emit(on_progress, "captcha", "等待手动填写图片验证码", addr.address)
        return {
            "ok": True,
            "need_captcha": True,
            "auto_captcha": False,
            "ocr_available": ocr_available(),
            "session_id": sid_session,
            "email": addr.address,
            "password": password,
            "region": region,
            "captcha_image": captcha_b64,
            "mail_jwt": addr.jwt,
            "message": (
                "临时邮箱已创建，请填写图片验证码后继续注册"
                + ("" if ocr_available() else "（未安装 ddddocr，无法自动识别）")
            ),
        }
    finally:
        await client.aclose()


async def refresh_captcha(session_id: str) -> Dict[str, Any]:
    pending = _PENDING_REG.get(session_id or "")
    if not pending:
        raise XiaomiRegisterError("注册会话已过期，请重新开始")
    client = _client(pending.device_id, pending.cookies)
    try:
        _img, captcha_b64 = await fetch_captcha(client)
        pending.captcha_b64 = captcha_b64
        pending.cookies = _dump_cookies(client)
        _PENDING_REG[session_id] = pending
        return {
            "ok": True,
            "session_id": session_id,
            "captcha_image": captcha_b64,
            "email": pending.email,
            "ocr_available": ocr_available(),
        }
    finally:
        await client.aclose()


def _is_captcha_error(code) -> bool:
    try:
        return int(code or 0) in (87001, 70014, 1200212)
    except Exception:
        return False


async def _auto_solve_and_finish(
    client: httpx.AsyncClient,
    pending: PendingRegister,
    mail_cfg: TempMailConfig,
    *,
    first_image: Optional[bytes] = None,
    first_data_url: str = "",
    captcha_retries: int = 8,
    otp_timeout: float = 120.0,
    on_progress: ProgressCb = None,
) -> Dict[str, Any]:
    """OCR captcha with retries, then complete register+login."""
    attempts: List[dict] = []
    img_bytes = first_image
    data_url = first_data_url or pending.captcha_b64
    email = pending.email

    import asyncio

    _emit(on_progress, "ocr", f"开始 OCR 识别验证码（最多 {captcha_retries} 次）", email)

    for i in range(max(1, captcha_retries)):
        if img_bytes is None:
            _emit(on_progress, "captcha", f"刷新图片验证码（第 {i + 1} 次）", email)
            img_bytes, data_url = await fetch_captcha(client)
            pending.captcha_b64 = data_url
            pending.cookies = _dump_cookies(client)
            _PENDING_REG[pending.session_id] = pending

        candidates = solve_captcha_candidates(img_bytes)
        attempts.append({
            "try": i + 1,
            "ocr": candidates[0] if candidates else "",
            "candidates": candidates,
            "len": len(img_bytes or b""),
        })
        if not candidates or len(candidates[0]) < 3:
            _emit(on_progress, "ocr", f"OCR 结果过短/为空，重试（{i + 1}/{captcha_retries}）", email)
            print(f"[Register] OCR empty/too short on try {i + 1}, refresh captcha")
            img_bytes = None
            await asyncio.sleep(0.4)
            continue

        _emit(
            on_progress,
            "ocr",
            f"OCR 识别为 {candidates[0]!r}，提交注册发信（{i + 1}/{captcha_retries}）",
            email,
        )
        captcha_ok = False
        last_j: dict = {}
        used_icode = ""
        for icode in candidates:
            used_icode = icode
            j = await _send_email_reg_ticket(client, pending, icode)
            last_j = j
            code = j.get("code")
            if code in (0, "0", None):
                captcha_ok = True
                break
            if int(code or 0) == 25001:
                raise XiaomiRegisterError("邮箱已被注册", code=25001, data=j)
            if not _is_captcha_error(code):
                raise XiaomiRegisterError(
                    j.get("desc") or j.get("description") or f"发送注册邮件失败: {j}",
                    code=int(code) if str(code).isdigit() else None,
                    data=j,
                )
            _emit(on_progress, "ocr", f"验证码 {icode!r} 被拒，尝试变体", email)
            print(f"[Register] captcha reject icode={icode!r} try={i + 1}")

        if captcha_ok:
            pending.ticket_sent = True
            pending.cookies = _dump_cookies(client)
            _PENDING_REG[pending.session_id] = pending
            _emit(on_progress, "send_ticket", f"图片验证通过（{used_icode}），注册邮件已发送", email)
            print(f"[Register] captcha OCR ok: {used_icode!r} after {i + 1} image(s)")
            result = await _finish_after_ticket_sent(
                client,
                pending,
                mail_cfg,
                otp_timeout=otp_timeout,
                on_progress=on_progress,
            )
            result["captcha_auto"] = True
            result["captcha_attempts"] = attempts
            result["captcha_ocr"] = used_icode
            return result

        # all candidates failed → new image
        img_bytes = None
        await asyncio.sleep(0.5)

    # all OCR retries failed → hand off to manual
    if img_bytes is None:
        try:
            img_bytes, data_url = await fetch_captcha(client)
            pending.captcha_b64 = data_url
            pending.cookies = _dump_cookies(client)
            _PENDING_REG[pending.session_id] = pending
        except Exception:
            data_url = pending.captcha_b64

    return {
        "ok": True,
        "need_captcha": True,
        "auto_captcha": True,
        "ocr_available": True,
        "captcha_auto_failed": True,
        "captcha_attempts": attempts,
        "session_id": pending.session_id,
        "email": pending.email,
        "password": pending.password,
        "region": pending.region,
        "captcha_image": data_url or pending.captcha_b64,
        "mail_jwt": pending.mail_jwt,
        "message": f"自动识别验证码失败（已重试 {len(attempts)} 次），请手动填写",
    }


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


async def _finish_after_ticket_sent(
    client: httpx.AsyncClient,
    pending: PendingRegister,
    mail_cfg: TempMailConfig,
    *,
    otp_timeout: float = 120.0,
    on_progress: ProgressCb = None,
) -> Dict[str, Any]:
    """After sendEmailRegTicket succeeded: wait mail code → verify → login."""
    email = pending.email
    seen_mail_ids: set = set()

    # Snapshot existing mails so we only accept NEW codes
    try:
        from .temp_mail import list_parsed_mails

        for m in await list_parsed_mails(mail_cfg, pending.mail_jwt, limit=20):
            mid = m.get("id") or m.get("message_id")
            if mid is not None:
                seen_mail_ids.add(mid)
    except Exception:
        pass

    _emit(on_progress, "wait_mail", f"等待注册验证码邮件（超时 {int(otp_timeout)}s）…", email)
    mail_code = await wait_for_code(
        mail_cfg,
        pending.mail_jwt,
        timeout=otp_timeout,
        seen_ids=seen_mail_ids,
    )
    _emit(on_progress, "wait_mail", "已收到注册验证码，正在校验…", email)
    print(f"[Register] got register mail code for {pending.email}")

    j2 = await _verify_email_reg_ticket(client, pending, mail_code)
    code2 = j2.get("code")
    if code2 not in (0, "0", None):
        raise XiaomiRegisterError(
            j2.get("desc") or j2.get("description") or f"校验邮箱验证码失败: {j2}",
            code=int(code2) if str(code2).isdigit() else None,
            data=j2,
        )
    _emit(on_progress, "verify", "注册验证码校验通过", email)

    pending.cookies = _dump_cookies(client)
    _PENDING_REG[pending.session_id] = pending

    # Prefer passToken from verify response / cookies over password re-login
    pass_token = str(j2.get("passToken") or "")
    user_id = str(j2.get("userId") or "")
    c_user_id = str(j2.get("cUserId") or "")
    if not pass_token:
        for c in pending.cookies:
            if c.get("name") == "passToken" and c.get("value"):
                pass_token = c["value"]
            elif c.get("name") == "userId" and c.get("value"):
                user_id = user_id or c["value"]
            elif c.get("name") == "cUserId" and c.get("value"):
                c_user_id = c_user_id or c["value"]

    tokens: dict = {}
    login_result: dict = {}

    if pass_token:
        try:
            from .xiaomi_login import renew_with_pass_token

            _emit(on_progress, "login", "使用 passToken 换取 MiMo token…", email)
            t = await renew_with_pass_token(
                email=pending.email,
                pass_token=pass_token,
                user_id=user_id,
                c_user_id=c_user_id,
                device_id=pending.device_id,
            )
            tokens = t.to_dict()
            login_result = {"ok": True, "tokens": tokens, "via": "passToken"}
            _emit(on_progress, "login", "passToken 换票成功", email)
        except Exception as e:
            print(f"[Register] passToken exchange failed: {e}")
            _emit(on_progress, "login", f"passToken 换票失败，尝试密码登录: {e}", email)
            login_result = {"ok": False, "error": str(e)[:200]}

    if not tokens:
        try:
            from .xiaomi_login import login_with_password, send_pending_email_otp

            _emit(on_progress, "login", "密码登录中…", email)
            login_result = await login_with_password(pending.email, pending.password)
            if login_result.get("need_otp") and login_result.get("session_id"):
                otp_sid = login_result["session_id"]
                _emit(on_progress, "login_otp", "登录需二次验证，发送邮件验证码…", email)
                await send_pending_email_otp(otp_sid)
                _emit(on_progress, "login_otp", "等待登录验证码邮件…", email)
                # Must get a NEW mail (seen_ids already has register mail)
                login_code = await wait_for_code(
                    mail_cfg,
                    pending.mail_jwt,
                    timeout=otp_timeout,
                    seen_ids=seen_mail_ids,
                )
                _emit(on_progress, "login_otp", "已收到登录验证码，提交中…", email)
                print(f"[Register] got login OTP for {pending.email}")
                login_result = await login_with_password(
                    pending.email,
                    pending.password,
                    otp_code=login_code,
                    session_id=otp_sid,
                )
            if login_result.get("ok") and login_result.get("tokens"):
                tokens = login_result["tokens"]
                _emit(on_progress, "login", "密码/OTP 登录成功", email)
        except Exception as e:
            print(f"[Register] password login failed: {e}")
            _emit(on_progress, "login", f"密码登录失败: {e}", email)
            login_result = {"ok": False, "error": str(e)[:200], "need_manual": True}

    session_id = pending.session_id
    if not tokens:
        _PENDING_REG.pop(session_id, None)
        _emit(on_progress, "done", "注册成功但自动登录未完成", email)
        return {
            "ok": True,
            "registered": True,
            "logged_in": False,
            "email": pending.email,
            "password": pending.password,
            "region": pending.region,
            "mail_jwt": pending.mail_jwt,
            "login": login_result,
            "message": "注册成功（图片验证码已自动通过），但自动登录未完成，请用邮箱密码手动导入",
        }

    _PENDING_REG.pop(session_id, None)
    _emit(on_progress, "done", f"注册并登录成功 · region={pending.region}", email)
    return {
        "ok": True,
        "registered": True,
        "logged_in": True,
        "email": pending.email,
        "password": pending.password,
        "region": pending.region,
        "mail_jwt": pending.mail_jwt,
        "tokens": tokens,
        "message": "注册并登录成功（图片验证码 OCR 自动通过）",
    }


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
            if _is_captcha_error(code):
                try:
                    _img, pending.captcha_b64 = await fetch_captcha(client)
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
        return await _finish_after_ticket_sent(
            client, pending, mail_cfg, otp_timeout=otp_timeout
        )
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
    auto_captcha: bool = True,
    captcha_retries: int = 8,
    on_progress: ProgressCb = None,
) -> Dict[str, Any]:
    """
    High-level register entry:
    - no session: start (auto OCR captcha when possible, else return captcha)
    - session + icode: complete registration manually
    - session + auto_captcha retry without icode: re-run OCR on new captchas
    """
    if session_id and icode:
        return await submit_captcha_and_register(
            session_id, icode, mail_cfg, otp_timeout=otp_timeout
        )

    # Resume pending session: auto OCR retries
    if session_id and not icode and auto_captcha:
        pending = _PENDING_REG.get(session_id)
        if not pending:
            raise XiaomiRegisterError("注册会话已过期，请重新开始")
        if not ocr_available():
            raise XiaomiRegisterError("请填写验证码（OCR 不可用）")
        client = _client(pending.device_id, pending.cookies)
        try:
            return await _auto_solve_and_finish(
                client,
                pending,
                mail_cfg,
                captcha_retries=captcha_retries,
                otp_timeout=otp_timeout,
                on_progress=on_progress,
            )
        finally:
            await client.aclose()

    if session_id and not icode:
        raise XiaomiRegisterError("请填写验证码")

    return await start_register(
        mail_cfg,
        region=region,
        password=password,
        domain=domain,
        auto_captcha=auto_captcha,
        captcha_retries=captcha_retries,
        otp_timeout=otp_timeout,
        on_progress=on_progress,
    )
