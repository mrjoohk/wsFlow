"""Jira 자격 증명 로드 — D-007.

**저장소에 들어갈 수 있는 어떤 파일에도 토큰을 쓰지 않는다.** `.gitignore` 에
기대지 않고 위치 자체를 저장소 밖에 둔다. 우선순위는 환경변수 → 사용자 설정 파일.

파일 형태는 둘 다 받는다. 사이트가 하나뿐인 보통의 경우는 평탄한 형태로 충분하고,
계정이 다른 사이트가 섞이면 ``sites`` 로 나눈다. 지정 사이트는 exact match만 허용한다::

    {"email": "me@example.com", "api_token": "..."}
    {"sites": {"https://x.atlassian.net": {"email": "...", "api_token": "..."}}}

이 모듈은 토큰 **값**을 밖으로 내보내지 않는다. ``status()`` 는 설정 여부와
가린 이메일만 돌려준다 — 서버 응답에 토큰이 실리는 경로를 아예 만들지 않기 위해서다.
"""
from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path

ENV_EMAIL = "JIRA_EMAIL"
ENV_TOKEN = "JIRA_API_TOKEN"

# 키 이름은 여러 갈래를 받는다. 환경변수 이름(JIRA_EMAIL·JIRA_API_TOKEN)을 그대로
# 파일에 적는 것이 가장 자연스러운 읽기이고, 실제로 그렇게 쓰인다(F-003). 받아들이지
# 않으면 "파일은 있는데 없다고 한다"가 되어 원인을 찾는 데 시간이 든다.
_EMAIL_KEYS = frozenset(("email", "user", "username", "account", "jira_email", "jira_user"))
_TOKEN_KEYS = frozenset(("api_token", "apitoken", "token", "jira_api_token", "jira_token"))


def _norm(k: str) -> str:
    return k.strip().lower().replace("-", "_")


class CredentialError(Exception):
    pass


def cred_path() -> Path:
    """자격 파일 위치. Windows 는 %APPDATA%\\wsFlow, 그 외는 ~/.config/wsFlow."""
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "wsFlow" / "credentials.json"
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "wsFlow" / "credentials.json"


def _pick(d: dict) -> tuple[str, str, list[str]]:
    """(이메일, 토큰, 파일에 있던 키 이름들). **값은 절대 함께 돌려주지 않는다.**"""
    email = token = ""
    for k, v in d.items():
        if not isinstance(v, (str, int)):
            continue
        n = _norm(k)
        if not email and n in _EMAIL_KEYS:
            email = str(v).strip()
        if not token and n in _TOKEN_KEYS:
            token = str(v).strip()
    return email, token, sorted(d)


def _from_file(site_url: str | None) -> tuple[str, str, Path | None, list[str]]:
    p = cred_path()
    if not p.is_file():
        return "", "", None, []
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        raise CredentialError(f"{p} 를 읽지 못했습니다: {e}") from e
    if not isinstance(data, dict):
        raise CredentialError(f"{p} 의 최상위가 객체가 아닙니다")
    sites = data.get("sites")
    if isinstance(sites, dict) and sites:
        entry = None
        if site_url:
            key = site_url.rstrip("/")
            for k, v in sites.items():
                if k.rstrip("/") == key:
                    entry = v
                    break
        if entry is None and not site_url and len(sites) == 1:
            entry = next(iter(sites.values()))
        if not isinstance(entry, dict):
            raise CredentialError(f"{p} 의 sites 에 {site_url or '해당 사이트'} 항목이 없습니다")
        e, t, found = _pick(entry)
    else:
        e, t, found = _pick(data)
    return e, t, p, found


def load(site_url: str | None = None) -> dict:
    """{"email", "api_token", "source"} 또는 CredentialError."""
    email = (os.environ.get(ENV_EMAIL) or "").strip()
    token = (os.environ.get(ENV_TOKEN) or "").strip()
    if email and token:
        return {"email": email, "api_token": token, "source": "env"}
    email, token, p, found = _from_file(site_url)
    if email and token:
        return {"email": email, "api_token": token, "source": str(p)}

    # "파일이 없다"·"파일은 있는데 키 이름이 다르다"·"한쪽만 있다" 를 구별해서 말한다.
    # 셋을 한 문장으로 뭉치면 파일을 제대로 써 놓고도 원인을 못 찾는다(F-003).
    if p is None:
        raise CredentialError(
            f"자격이 없습니다 — {ENV_EMAIL}/{ENV_TOKEN} 환경변수를 설정하거나 "
            f"{cred_path()} 를 만드십시오. Basic 인증은 토큰만으로는 안 되고 계정 이메일이 함께 필요합니다.")
    if not (email or token):
        raise CredentialError(
            f"{p} 는 있으나 인식할 수 있는 키가 없습니다. 파일에 있는 키: {found}. "
            f"이메일은 {sorted(_EMAIL_KEYS)} 중 하나, 토큰은 {sorted(_TOKEN_KEYS)} 중 하나로 적으십시오"
            " (대소문자·하이픈 무시).")
    missing = "토큰" if email else "이메일"
    raise CredentialError(f"{p} 에 {missing}이 없습니다. 파일에 있는 키: {found}. "
                          "Basic 인증은 이메일과 토큰이 둘 다 필요합니다.")


def mask_email(email: str) -> str:
    if "@" not in email:
        return (email[:1] + "***") if email else ""
    local, _, domain = email.partition("@")
    return f"{local[:1]}{'*' * max(len(local) - 1, 1)}@{domain}"


def perm_warning(p: Path) -> str:
    """POSIX 에서 남이 읽을 수 있으면 경고. Windows ACL 은 여기서 보지 않는다."""
    if os.name == "nt" or not p.is_file():
        return ""
    mode = stat.S_IMODE(p.stat().st_mode)
    if mode & 0o077:
        return f"권한이 넓습니다({oct(mode)}) — chmod 600 {p} 를 권합니다"
    return ""


def status(site_url: str | None = None) -> dict:
    """설정 여부만 돌려준다. **토큰 값은 절대 담지 않는다**(D-007)."""
    p = cred_path()
    out = {"configured": False, "source": None, "email": None,
           "path": str(p), "path_exists": p.is_file(), "warning": "", "error": None}
    try:
        c = load(site_url)
    except CredentialError as e:
        out["error"] = str(e)
        return out
    out.update(configured=True, source=c["source"], email=mask_email(c["email"]))
    if c["source"] != "env":
        out["warning"] = perm_warning(p)
    return out


def basic_header(creds: dict) -> str:
    raw = f"{creds['email']}:{creds['api_token']}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")
