#!/usr/bin/env python3
"""一次性自检：妙手 API 连通性 + 推送通道可用性。

所有密钥都从同目录的 config.json 读取（该文件已 gitignore），
**不要**把密钥硬编码进本文件 —— 2026-09-10 曾因此把 AppSecret / SendKey 泄进 public 仓库。

用法：
    /Users/jianguo/.workbuddy/binaries/python/envs/default/bin/python test_connectivity.py
"""
import hashlib
import hmac
import json
import time
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_URL = "https://openapi-erp.91miaoshou.com"


def load_cfg() -> dict:
    return json.loads((SCRIPT_DIR / "config.json").read_text(encoding="utf-8"))


def generate_sign(app_secret, path, timestamp, app_key, body_json=""):
    content = app_secret + path + str(timestamp) + app_key
    if body_json:
        content += body_json
    content += app_secret
    return hmac.new(app_secret.encode("utf-8"),
                    content.encode("utf-8"), hashlib.sha256).hexdigest()


def mask(v: str, keep: int = 4) -> str:
    v = str(v or "")
    return v[:keep] + "..." if len(v) > keep else "***"


def main() -> int:
    cfg = load_cfg()
    app_key = cfg["miaoshou"]["app_key"]
    app_secret = cfg["miaoshou"]["app_secret"]

    print("=" * 50)
    print("测试1: 调用妙手ERP API - 获取包裹列表")
    path = "/open/v1/order/package/fetch/search_package_list"
    body = {"page": 1, "pageSize": 5}
    body_json = json.dumps(body, ensure_ascii=False)
    timestamp = int(time.time())
    sign = generate_sign(app_secret, path, timestamp, app_key, body_json)

    headers = {
        "x-app-key": app_key,
        "x-timestamp": str(timestamp),
        "x-sign": sign,
        "Content-Type": "application/json",
    }
    print(f"URL: {BASE_URL}{path}")
    print(f"x-app-key: {mask(app_key, 8)}")
    print(f"x-timestamp: {timestamp}")

    try:
        resp = requests.post(BASE_URL + path, headers=headers, data=body_json, timeout=30)
        print(f"\n响应状态码: {resp.status_code}")
        result = resp.json()
        print(f"响应内容:\n{json.dumps(result, ensure_ascii=False, indent=2)[:1200]}")

        if result.get("code") == "success" or result.get("result") == "success":
            data = result.get("data", [])
            if not data:
                print("暂无订单数据")
                return 0
            for item in data:
                packages = item.get("orderPackageList", [])
                print(f"\n获取到 {len(packages)} 条包裹记录")
                for pkg in packages[:2]:
                    oi = pkg.get("orderInfo", {})
                    print(f"  订单ID: {oi.get('opOrderId')}")
                    print(f"  订单号: {oi.get('platformOrderSn')}")
                    print(f"  金额: {oi.get('orderAmount')} {oi.get('currency', '')}")
                    print(f"  状态: {oi.get('appOrderStatusText', oi.get('appOrderStatus', ''))}")
                    for it in pkg.get("items", [])[:3]:
                        print(f"    商品: {it.get('title')} ×{it.get('quantity')}")
                    print("  ---")
            return 0

        print(f"API返回错误: {result.get('message', '未知错误')}")
        return 1
    except Exception as e:
        print(f"请求失败: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
