#!/usr/bin/env python3
"""
妙手ERP 新订单提醒 v2（本地主推）
================================
v1 的三个问题与修复：

| 问题 | v1 根因 | v2 修法 |
|------|---------|---------|
| 重复推送 | 推送成功才记录，失败反复重试；本地/云端各存各的状态 | 统一状态机：pending→notified，先占位后推送 |
| 漏单 | 推送失败后 last_check 仍推进到 now，订单掉出 5 分钟窗口；只拉第 1 页 20 条 | 失败订单进 pending 队列（带快照，无需重新拉取即可重试）；分页拉全量 |
| 每天只 5 次 | Server酱免费版 5 条/天 | 主通道 PushPlus(200/天)，Server酱降级备用；额度/限流自动切换 + 积压自动合并推送 |

状态机：订单 → pending（待推，含快照）→ 推送成功 → notified（终态）
"""

import hmac
import hashlib
import json
import sys
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

from push_channels import Notifier
from cloud_sync import CloudSync
from desktop_alert import DesktopAlert

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
STATE_FILE = SCRIPT_DIR / "orders_state.json"
LOG_FILE = SCRIPT_DIR / "monitor.log"

TZ_SHANGHAI = timezone(timedelta(hours=8))
API_PATH = "/open/v1/order/package/fetch/search_package_list"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("monitor")

PLATFORM_SHORT = {"tiktok": "TK", "shopee": "SP", "lazada": "LZ"}
# pending 重试上限：超过这个时间/次数就放弃（避免无限重试骚扰）
MAX_PENDING_AGE_HOURS = 72
MAX_ATTEMPTS = 40


# ---------------- 基础 IO ----------------
def load_config():
    if not CONFIG_FILE.exists():
        log.error(f"配置文件不存在: {CONFIG_FILE}")
        sys.exit(1)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
        except json.JSONDecodeError:
            log.error("状态文件损坏，重建")
            s = {}
    else:
        s = {}
    # 兼容 v1 的 notified_order_ids 数组
    s.setdefault("notified", {})
    s.setdefault("pending", {})
    for oid in s.pop("notified_order_ids", []):
        s["notified"].setdefault(str(oid), {"sn": "", "ts": ""})
    return s


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_FILE)  # 原子写，避免崩溃时状态文件半截


# ---------------- 妙手 API ----------------
def generate_sign(app_secret, path, timestamp, app_key, body_json=""):
    content = app_secret + path + str(timestamp) + app_key
    if body_json:
        content += body_json
    content += app_secret
    return hmac.new(app_secret.encode(), content.encode(), hashlib.sha256).hexdigest()


def call_api(cfg, body, retries=4):
    path = API_PATH
    body_json = json.dumps(body, ensure_ascii=False)
    for attempt in range(retries):
        ts = int(time.time())
        headers = {
            "x-app-key": cfg["app_key"],
            "x-timestamp": str(ts),
            "x-sign": generate_sign(cfg["app_secret"], path, ts, cfg["app_key"], body_json),
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(cfg["base_url"] + path, headers=headers,
                                 data=body_json, timeout=30)
            result = resp.json()
        except Exception as e:
            log.warning(f"[API] 请求异常({attempt + 1}/{retries}): {e}")
            time.sleep(3)
            continue

        code = result.get("code") or result.get("result", "")
        if code == "success":
            return result
        if code == "accountApiQpsRateLimit":
            log.warning("[API] 触发 QPS 限流，等待 5s 重试")
            time.sleep(5)
            continue
        msg = str(result.get("message", ""))
        if "没有符合条件的数据" in msg:
            return {"_empty": True}
        log.warning(f"[API] code={code} msg={msg}")
        time.sleep(2)
    return None


def fetch_all_orders(cfg, mon_cfg, since: datetime):
    """分页拉取全量订单（v1 只拉第 1 页，窗口内超 20 单会漏）"""
    page_size = mon_cfg.get("page_size", 50)
    max_pages = mon_cfg.get("max_pages", 20)
    orders, seen_keys = [], set()

    for page in range(1, max_pages + 1):
        body = {
            "page": page,
            "pageSize": page_size,
            "gmtModifiedFrom": since.strftime("%Y-%m-%d %H:%M:%S"),
        }
        result = call_api(cfg, body)
        if result is None:
            log.error(f"[API] 第 {page} 页拉取失败，停止翻页")
            break
        if result.get("_empty"):
            break

        data = result.get("data")
        batch = []
        if isinstance(data, dict):
            batch = data.get("orderPackageList", [])
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    batch.extend(item.get("orderPackageList", []))

        for o in batch:
            oi = o.get("orderInfo", {})
            key = str(oi.get("opOrderId", "")) or oi.get("platformOrderSn", "")
            # 一单多包裹：同一 opOrderId 只保留一条
            if key and key not in seen_keys:
                seen_keys.add(key)
                orders.append(o)

        log.info(f"[API] 第 {page} 页 {len(batch)} 条，累计去重后 {len(orders)} 条")
        if len(batch) < page_size:
            break
        time.sleep(1.5)  # 分页间隔，防 QPS 限流

    return orders


# ---------------- 订单处理 ----------------
def snapshot(order: dict) -> dict:
    """抽取推送所需的最小快照，pending 重试时无需重新拉取订单"""
    oi = order.get("orderInfo", {})
    items = order.get("items", [])
    consignee = order.get("consigneeInfo", {})
    return {
        "sn": oi.get("platformOrderSn", "无"),
        "start": oi.get("gmtOrderStart", ""),
        "platform": order.get("platformName", order.get("platform", "未知")),
        "shop": order.get("shopName", "未知店铺"),
        # 出单地区：妙手在顶层和 orderInfo 里都给了 site/siteName，
        # 顶层更全（如 site=MY, siteName=马来），拿不到再退回 orderInfo
        "site": order.get("site") or oi.get("site", ""),
        "site_name": order.get("siteName") or oi.get("siteName", ""),
        "status": oi.get("appOrderStatusText", oi.get("appOrderStatus", "未知")),
        "amount": oi.get("orderAmount", 0),
        "currency": oi.get("currency", ""),
        "country": consignee.get("countryName", consignee.get("country", "")),
        "province": consignee.get("state", ""),
        "city": consignee.get("city", ""),
        "items": [
            {
                "title": it.get("title", "未知商品"),
                "qty": it.get("quantity", 0),
                "price": it.get("discountedPrice", it.get("originalPrice", 0)),
            }
            for it in items[:5]
        ],
        "item_count": len(items),
        "added_at": datetime.now(TZ_SHANGHAI).isoformat(),
        "attempts": 0,
        "last_error": "",
    }


def region_tag(s: dict) -> str:
    """出单地区标签，如「马来 MY」。取不到就返回空串。

    注意：本次改动之前入队/入名单的老快照没有 site 字段，一律用 .get 兜底，
    不要写 s["site"]，否则重试老单会 KeyError。
    """
    site = str(s.get("site") or "").strip()
    name = str(s.get("site_name") or "").strip()
    if name and site and name != site:
        return f"{name} {site}"
    return name or site


def title_new(s: dict, suffix: str = "") -> str:
    """统一的新单标题：出单地区放最前，扫一眼就知道是哪个站点出的单"""
    tag = region_tag(s)
    return f"{'【' + tag + '】' if tag else ''}您有一条新的{short(s)} 订单{suffix}"


def format_message(s: dict) -> str:
    lines = [f"  - {i['title']} ×{i['qty']}  {i['price']:.2f} {s['currency']}"
             for i in s["items"]]
    if s["item_count"] > 5:
        lines.append(f"  ... 共 {s['item_count']} 件商品")
    where = " / ".join(p for p in (s.get("country", ""), s.get("province", ""),
                                   s.get("city", "")) if p) or "未知"
    return (f"★ 出单地区: {region_tag(s) or '未知'}\n"
            f"平台: {s['platform']} | 店铺: {s['shop']}\n"
            f"订单号: {s['sn']}\n"
            f"状态: {s['status']} | 金额: {s['amount']:.2f} {s['currency']}\n"
            f"收货地: {where}\n\n"
            f"商品:\n" + "\n".join(lines) +
            f"\n\n⏰ 下单 {s['start']}")


def short(s: dict) -> str:
    return PLATFORM_SHORT.get(str(s["platform"]).lower(), s["platform"])


def format_batch(items: list[dict]) -> str:
    """积压时合并成一条推送：1 次推送覆盖 N 单，绕开日额度与频率限制"""
    total = sum(i["amount"] for i in items)
    cur = items[0]["currency"] if items else ""
    lines = [f"{i + 1}. 【{region_tag(s) or '未知'}】{s['sn']} | "
             f"{s['amount']:.2f} {s['currency']} | {s.get('country', '')}"
             for i, s in enumerate(items)]
    return (f"共 {len(items)} 单，合计 {total:.2f} {cur}\n\n" + "\n".join(lines) +
            f"\n\n⏰ {datetime.now(TZ_SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')}")


def push_pending(notifier: Notifier, state: dict, merge_threshold: int,
                 sent_snaps: list | None = None) -> int:
    """推送 pending 队列：成功→notified，失败→保留重试。返回本轮新推成功的单数。

    sent_snaps：可选。把本轮**推送成功**的订单快照收集进来，供调用方统一播报语音。
    只有真的推成功才收 —— 播报和推送要保持一致，不能推失败还念一句，白高兴。
    """
    pending = state["pending"]
    if not pending:
        return 0

    # 先清理过期/超限的
    now = datetime.now(TZ_SHANGHAI)
    for oid, s in list(pending.items()):
        added = datetime.fromisoformat(s["added_at"])
        age_h = (now - added).total_seconds() / 3600
        if age_h > MAX_PENDING_AGE_HOURS or s["attempts"] >= MAX_ATTEMPTS:
            log.error(f"[放弃] {s['sn']} 重试 {s['attempts']} 次 / 已 {age_h:.0f}h 仍未成功，"
                      f"最后错误: {s['last_error']}")
            state["notified"][oid] = {"sn": s["sn"], "ts": now.isoformat(), "gave_up": True}
            del pending[oid]

    if not pending:
        return 0

    # 积压过多 → 合并成一条，节省额度
    if len(pending) >= merge_threshold:
        batch = list(pending.values())[:20]
        oids = list(pending.keys())[:20]
        if len(batch) == 1:
            title = title_new(batch[0])
            content = format_message(batch[0])
        else:
            sites = sorted({region_tag(s) for s in batch if region_tag(s)})
            scope = f"【{'/'.join(sites)}】" if sites else ""
            title = f"{scope}新订单 {len(batch)} 单（合并推送）"
            content = format_batch(batch)
        ok, info = notifier.send(title, content)
        sent = 0
        for oid, s in zip(oids, batch):
            if ok:
                state["notified"][oid] = {"sn": s["sn"],
                                          "ts": datetime.now(TZ_SHANGHAI).isoformat()}
                del pending[oid]
                sent += 1
                if sent_snaps is not None:
                    sent_snaps.append(s)
            else:
                s["attempts"] += 1
                s["last_error"] = info
        if not ok:
            log.error(f"[推送] 合并推送失败: {info}")
        return sent

    # 正常情况：逐单推送
    sent = 0
    for oid, s in list(pending.items()):
        ok, info = notifier.send(title_new(s), format_message(s))
        if ok:
            state["notified"][oid] = {"sn": s["sn"],
                                      "ts": datetime.now(TZ_SHANGHAI).isoformat()}
            del pending[oid]
            sent += 1
            if sent_snaps is not None:
                sent_snaps.append(s)
        else:
            s["attempts"] += 1
            s["last_error"] = info
            log.error(f"[推送] {s['sn']} 失败（第 {s['attempts']} 次）: {info}")
    return sent


# ---------------- 主流程 ----------------
def run_once(cfg, state, cs=None, alerter: DesktopAlert | None = None):
    log.info("=" * 56)
    mon = cfg["monitor"]
    notifier = Notifier(cfg, state)
    now = datetime.now(TZ_SHANGHAI)

    # 关键一步：先吸收"云端已推名单"，避免 Mac 关机期间云端推过的单在本地重播一遍
    if cs is not None:
        try:
            cs.absorb(state)
        except Exception as e:
            log.warning(f"[同步] 吸收云端状态异常（不影响本轮）: {e}")

    window = now - timedelta(hours=mon.get("query_window_hours", 6))
    max_age = now - timedelta(hours=mon.get("new_order_max_age_hours", 24))

    log.info(f"拉取 {window.strftime('%m-%d %H:%M')} 之后变更的包裹（分页全量）")
    orders = fetch_all_orders(cfg["miaoshou"], mon, window)
    log.info(f"本轮获取 {len(orders)} 条去重后订单")

    new_cnt = 0
    for order in orders:
        oi = order.get("orderInfo", {})
        oid = str(oi.get("opOrderId", ""))
        if not oid or oid in state["notified"] or oid in state["pending"]:
            continue

        start_str = oi.get("gmtOrderStart", "")
        try:
            start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_SHANGHAI)
        except Exception:
            start_dt = now

        if start_dt < max_age:
            # 老订单（仅因物流/状态同步被捞回）：静默标记，不推送，避免历史单轰炸
            state["notified"][oid] = {"sn": oi.get("platformOrderSn", ""),
                                      "ts": now.isoformat(), "silent": True}
            continue

        state["pending"][oid] = snapshot(order)
        new_cnt += 1
        log.info(f"[新单] {oi.get('platformOrderSn')} 下单 {start_str}")

    log.info(f"新单 {new_cnt} 个，pending 队列 {len(state['pending'])} 个")

    sent = 0
    sent_snaps: list = []
    if not notifier.has_budget():
        log.error("[额度] 所有通道今日额度已用尽，pending 保留到次日自动补推")
    else:
        sent = push_pending(notifier, state, mon.get("merge_threshold", 3), sent_snaps)

    # 推送成功 → 本机念一句（多单合并成一句，避免几句话叠在一起听不清）
    if alerter is not None and sent_snaps:
        try:
            alerter.announce(sent_snaps)
        except Exception as e:
            log.warning(f"[语音] 播报异常（不影响推送）: {e}")

    save_state(state)

    # 推成功的单回写仓库，让云端知道"这些单本地已经推过"，避免云端重复补推
    if cs is not None and sent:
        try:
            cs.publish(state, force=True)
        except Exception as e:
            log.warning(f"[同步] 回写云端状态异常（不影响推送）: {e}")
    elif cs is not None:
        try:
            cs.publish(state)          # 未推单，按节流静默同步即可
        except Exception as e:
            log.warning(f"[同步] 回写云端状态异常: {e}")

    log.info(f"本轮完成 | 已通知 {len(state['notified'])} | 待推 {len(state['pending'])} "
             f"| 本轮推送 {sent} | 今日发送 {state.get('sent_today', {})}")


def main():
    cfg = load_config()
    state = load_state()
    interval = cfg["monitor"].get("poll_interval_seconds", 180)
    cs = CloudSync(cfg.get("cloud_sync", {}))
    if cs.enabled:
        log.info(f"云端状态同步已启用 | 仓库 {cs.repo} | 节流 {cs.throttle}s"
                 + ("" if cs.token else " | ⚠️ 缺少 token，只能读不能写"))

    # 本机语音提醒（只本地跑；云端 Actions 无音频设备，不启用）
    alerter = DesktopAlert(cfg.get("desktop_alert", {}))
    alerter.check()

    log.info(f"妙手订单监控 v2 启动 | 轮询 {interval}s | 主通道 "
             f"{cfg['push'].get('channel_order', ['pushplus'])[0]}")
    log.info(f"电脑语音提醒: {'✅ 开' if alerter.ok else '⏭️ 关'}（{alerter.reason}）")

    while True:
        try:
            run_once(cfg, state, cs, alerter)
        except Exception as e:
            log.exception(f"轮询异常: {e}")
        nxt = datetime.now(TZ_SHANGHAI) + timedelta(seconds=interval)
        log.info(f"下一轮: {nxt.strftime('%H:%M:%S')}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
