#!/usr/bin/env python3
"""
把本地 config.json 里的推送参数同步到 GitHub 仓库的 Actions Secrets。

用法：
    GH_TOKEN=<你的token> python set_gh_secrets.py [owner/repo]

token 需要 permissions: Secrets = Read and write（细粒度）
或 classic token 的 repo scope。

只读本地 config.json，不打印任何密钥明文（只显示长度与前缀）。
"""
import base64
import json
import os
import sys
from pathlib import Path

import requests
from nacl import encoding, public

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REPO = "dbsjfe/miaoshou-monitor"
API = "https://api.github.com"


def build_secret_map(cfg: dict) -> dict:
    """从本地配置里挑出云端需要的 secret 名 -> 值"""
    push = cfg["push"]
    email = push.get("email", {})
    out = {
        "MIAOSHOU_APP_KEY": cfg["miaoshou"]["app_key"],
        "MIAOSHOU_APP_SECRET": cfg["miaoshou"]["app_secret"],
        "SMTP_HOST": email.get("host", ""),
        "SMTP_PORT": str(email.get("port", "")),
        "SMTP_USER": email.get("user", ""),
        "SMTP_PASSWORD": email.get("password", ""),
        "SMTP_TO": email.get("to", ""),
        "SERVERCHAN_SEND_KEY": push.get("serverchan", {}).get("send_key", ""),
    }
    # 可选通道：有值才写，避免覆盖成空
    optional = {
        "WXPUSHER_APP_TOKEN": push.get("wxpusher", {}).get("app_token", ""),
        "WXPUSHER_UID": push.get("wxpusher", {}).get("uid", ""),
        "WECOM_BOT_KEY": push.get("wecom_bot", {}).get("key", ""),
        "WECOM_CORPID": push.get("wecom", {}).get("corpid", ""),
        "WECOM_SECRET": push.get("wecom", {}).get("secret", ""),
        "WECOM_AGENTID": push.get("wecom", {}).get("agentid", ""),
    }
    for k, v in optional.items():
        if v and not str(v).startswith("PLEASE_FILL_IN"):
            out[k] = v
    return {k: v for k, v in out.items() if v and not str(v).startswith("PLEASE_FILL_IN")}


def encrypt(public_key: str, secret_value: str) -> str:
    """用仓库公钥做 libsodium sealed box 加密，GitHub 要求"""
    pk = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
    sealed = public.SealedBox(pk)
    return base64.b64encode(
        sealed.encrypt(secret_value.encode("utf-8"))).decode("utf-8")


def main() -> int:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("❌ 缺少 GH_TOKEN 环境变量")
        return 2
    repo = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REPO

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # 1. 校验 token 与仓库
    r = requests.get(f"{API}/repos/{repo}", headers=headers, timeout=20)
    if r.status_code == 401:
        print("❌ token 无效或已过期（401）")
        return 1
    if r.status_code != 200:
        print(f"❌ 读仓库失败 {r.status_code}: {r.text[:200]}")
        return 1
    info = r.json()
    print(f"✅ 仓库 {info['full_name']}（{info['visibility']}）")

    # 2. 拿仓库公钥
    r = requests.get(f"{API}/repos/{repo}/actions/secrets/public-key",
                     headers=headers, timeout=20)
    if r.status_code != 200:
        print(f"❌ 取公钥失败 {r.status_code}: {r.text[:200]}")
        print("   常见原因：token 缺少 Secrets 写权限（细粒度需 Secrets: Read and write）")
        return 1
    key_id, pub_key = r.json()["key_id"], r.json()["key"]

    # 3. 逐个写入
    cfg = json.loads((SCRIPT_DIR / "config.json").read_text(encoding="utf-8"))
    secrets = build_secret_map(cfg)
    print(f"\n待写入 {len(secrets)} 个 Secret：")
    ok = fail = 0
    for name, value in secrets.items():
        body = {"encrypted_value": encrypt(pub_key, str(value)), "key_id": key_id}
        r = requests.put(f"{API}/repos/{repo}/actions/secrets/{name}",
                         headers=headers, json=body, timeout=20)
        mark = "✅" if r.status_code in (201, 204) else "❌"
        if r.status_code in (201, 204):
            ok += 1
        else:
            fail += 1
        preview = str(value)[:6] + "..." if len(str(value)) > 6 else "***"
        print(f"  {mark} {name:22s} ({preview})  {r.status_code}")
        if r.status_code not in (201, 204):
            print(f"       {r.text[:160]}")

    print(f"\n结果：成功 {ok} / 失败 {fail}")
    if fail == 0:
        print("→ 去 Actions 页手动跑一次 workflow_dispatch 即可验证：")
        print(f"  https://github.com/{repo}/actions")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
