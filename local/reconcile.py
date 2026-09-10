#!/usr/bin/env python3
"""
订单对账（本地 + 云端共用）
==========================
作用：兜住"实时推送链路"漏掉的订单。

  --hours N           回看最近 N 小时的全量订单（默认 24）
  --fix               发现漏单立即补推（本地用）
  --report            只发一条"当日订单汇总"日报
  --cloud-fix         云端接管：只推本地没来得及推的单（去重靠 state/ 下两个状态文件）
  --min-age-minutes N 云端接管模式下，下单不足 N 分钟的单先留给本地（默认 15）
  --quiet             不推送，只在 stdout 打印（排查用）

云端建议：每 10 分钟跑 `--cloud-fix --hours 24`（Mac 关机时由它接管逐单通知）
本地建议：每天 23:50 跑 `--hours 24 --fix`
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TZ = timezone(timedelta(hours=8))
TZ_SHANGHAI = TZ   # 别名：本文件与 monitor.py 里的写法不一致，这里统一兜住

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("reconcile")

sys.path.insert(0, str(SCRIPT_DIR))
from monitor import (load_config, load_state, save_state, fetch_all_orders,  # noqa: E402
                     snapshot, format_message, format_batch, short)
from push_channels import Notifier  # noqa: E402


def build_cfg():
    """本地读 config.json；云端（无 config.json）读环境变量"""
    cfg_file = SCRIPT_DIR / "config.json"
    if cfg_file.exists():
        return load_config()
    return {
        "miaoshou": {
            "app_key": os.environ["MIAOSHOU_APP_KEY"],
            "app_secret": os.environ["MIAOSHOU_APP_SECRET"],
            "base_url": "https://openapi-erp.91miaoshou.com",
        },
        "push": {
            "channel_order": ["email", "serverchan"],
            "min_interval_seconds": 13,
            "daily_limit": {"wxpusher": 100000, "wecom": 100000,
                            "wecom_bot": 100000, "serverchan": 5,
                            "email": 100000},
            "wxpusher": {
                "app_token": os.environ.get("WXPUSHER_APP_TOKEN", ""),
                "uid": os.environ.get("WXPUSHER_UID", ""),
                "min_interval_seconds": 2,
            },
            "wecom_bot": {"key": os.environ.get("WECOM_BOT_KEY", ""),
                          "min_interval_seconds": 4},
            "wecom": {
                "corpid": os.environ.get("WECOM_CORPID", ""),
                "secret": os.environ.get("WECOM_SECRET", ""),
                "agentid": os.environ.get("WECOM_AGENTID", ""),
                "touser": os.environ.get("WECOM_TOUSER", "@all"),
                "min_interval_seconds": 2,
            },
            "serverchan": {"send_key": os.environ.get("SERVERCHAN_SEND_KEY", "")},
            "email": {
                "host": os.environ.get("SMTP_HOST", ""),
                "port": os.environ.get("SMTP_PORT", "465"),
                "user": os.environ.get("SMTP_USER", ""),
                "password": os.environ.get("SMTP_PASSWORD", ""),
                "to": os.environ.get("SMTP_TO", ""),
            },
        },
        "monitor": {"page_size": 50, "max_pages": 20, "merge_threshold": 3,
                    "new_order_max_age_hours": 24},
    }


def daily_report(orders: list) -> str:
    """当日订单汇总：一眼看清今天几单、多少钱、有没有漏"""
    if not orders:
        return "今日暂无订单。"
    total, cur = 0, ""
    lines = []
    for i, o in enumerate(orders, 1):
        oi = o.get("orderInfo", {})
        amount = oi.get("orderAmount", 0)
        cur = oi.get("currency", cur)
        total += amount
        country = o.get("consigneeInfo", {}).get("countryName", "")
        lines.append(f"{i}. {oi.get('platformOrderSn', '无')} | {amount:.2f} {cur} "
                     f"| {country} | {oi.get('gmtOrderStart', '')}")
    return (f"今日共 {len(orders)} 单，合计 {total:.2f} {cur}\n\n" + "\n".join(lines[:50]) +
            (f"\n... 另有 {len(orders) - 50} 单" if len(orders) > 50 else ""))


ROOT = SCRIPT_DIR.parent
CLOUD_STATE_FILE = ROOT / "state" / "cloud_state.json"
LOCAL_STATE_FILE = ROOT / "state" / "local_state.json"
MAX_STATE_ENTRIES = 1000


def _load_pushed(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("pushed") or {}
    except Exception:
        return {}


def _save_pushed(path: Path, pushed: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"updated_at": datetime.now(TZ_SHANGHAI).isoformat(), "pushed": pushed},
        ensure_ascii=False, indent=2), encoding="utf-8")


def cloud_fix(cfg, hours: int, min_age_minutes: int = 15) -> int:
    """
    云端接管模式：只推「本地来不及推」的订单。

    去重靠两个状态文件（都在仓库里，双方都读）：
        state/local_state.json  ← 本机 monitor 推完回写
        state/cloud_state.json  ← 本函数推完回写（由 workflow git push 提交）

    再叠一层时间余量：下单不足 min_age_minutes 的单**先不动**，留给本地（3 分钟轮询）。
    这样本地在线时永远不会和云端撞车，本地离线时云端才接管。
    """
    now = datetime.now(TZ_SHANGHAI)
    since = now - timedelta(hours=hours)
    # 与本地一致的护栏：妙手按「包裹修改时间」过滤，物流同步会把旧单捞回来，
    # 没有这道闸，云端接管时会把几个月前的单当新单推出去
    max_age = now - timedelta(hours=cfg["monitor"].get("new_order_max_age_hours", 24))

    cloud_pushed = _load_pushed(CLOUD_STATE_FILE)
    local_pushed = _load_pushed(LOCAL_STATE_FILE)
    known = set(cloud_pushed) | set(local_pushed)
    log.info(f"[云端] 已知已推 {len(known)} 单"
             f"（本地 {len(local_pushed)} + 云端 {len(cloud_pushed)}）")

    log.info(f"[云端] 拉取 {since.strftime('%m-%d %H:%M')} 之后的订单")
    orders = fetch_all_orders(cfg["miaoshou"], cfg["monitor"], since)
    log.info(f"[云端] 拉到 {len(orders)} 条订单")

    candidates, deferred, too_old = [], 0, 0
    for o in orders:
        oi = o.get("orderInfo", {})
        oid = str(oi.get("opOrderId", ""))
        if not oid or oid in known:
            continue
        try:
            start_dt = datetime.strptime(oi.get("gmtOrderStart", ""),
                                         "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_SHANGHAI)
        except Exception:
            start_dt = now
        if start_dt < max_age:
            too_old += 1
            log.info(f"[云端] 跳过过旧订单（{oi.get('gmtOrderStart')}）: "
                     f"{oi.get('platformOrderSn')}")
            continue
        age_min = (now - start_dt).total_seconds() / 60
        if age_min < min_age_minutes:
            deferred += 1
            log.info(f"[云端] 暂缓（{age_min:.0f} 分钟前下单，留给本地）: "
                     f"{oi.get('platformOrderSn')}")
            continue
        candidates.append(o)

    if deferred:
        log.info(f"[云端] 本轮暂缓 {deferred} 单（等本地先推）")
    if too_old:
        log.info(f"[云端] 跳过 {too_old} 单过旧订单（仅因物流同步被捞回）")

    if not candidates:
        log.info("[云端] ✅ 无需要补推的订单")
        return 0

    log.warning(f"[云端] 发现 {len(candidates)} 单本地未推，接管补推：")
    for o in candidates:
        oi = o.get("orderInfo", {})
        log.warning(f"   {oi.get('platformOrderSn')} | {oi.get('orderAmount')} "
                    f"{oi.get('currency', '')} | 下单 {oi.get('gmtOrderStart')}")

    notifier = Notifier(cfg, {"notified": {}, "pending": {}})
    snaps = [snapshot(o) for o in candidates[:20]]
    if len(snaps) == 1:
        title = f"您有一条新的{short(snaps[0])} 订单（云端补推）"
        content = format_message(snaps[0])
    else:
        title = f"云端补推 {len(snaps)} 单（本机可能离线）"
        content = format_batch(snaps)

    ok, info = notifier.send(title, content)
    log.info(f"[云端] 推送 {'成功' if ok else '失败'}: {info}")
    if not ok:
        return 0

    ts = now.isoformat()
    for o in candidates[:20]:
        oi = o.get("orderInfo", {})
        cloud_pushed[str(oi.get("opOrderId", ""))] = {
            "sn": oi.get("platformOrderSn", ""), "ts": ts}

    # 裁剪，防止状态文件无限膨胀
    if len(cloud_pushed) > MAX_STATE_ENTRIES:
        ordered = sorted(cloud_pushed.items(), key=lambda kv: kv[1].get("ts", ""), reverse=True)
        cloud_pushed = dict(ordered[:MAX_STATE_ENTRIES])

    _save_pushed(CLOUD_STATE_FILE, cloud_pushed)
    log.info(f"[云端] 已记录 {len(candidates[:20])} 单到 {CLOUD_STATE_FILE.name}，"
             f"下次不会再推")
    return len(candidates[:20])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--fix", action="store_true", help="补推漏掉的订单（本地用）")
    ap.add_argument("--report", action="store_true", help="发送当日汇总日报")
    ap.add_argument("--cloud-fix", action="store_true",
                    help="云端接管模式：只推本地没来得及推的单（需 state/ 状态文件）")
    ap.add_argument("--min-age-minutes", type=int, default=15,
                    help="云端接管模式下，下单不足 N 分钟的单先留给本地（默认 15）")
    ap.add_argument("--quiet", action="store_true", help="只打印不推送")
    args = ap.parse_args()

    cfg = build_cfg()
    mon = cfg["monitor"]

    if args.cloud_fix:
        return cloud_fix(cfg, args.hours, args.min_age_minutes)

    now = datetime.now(TZ_SHANGHAI)
    since = now - timedelta(hours=args.hours)

    log.info(f"对账：拉取 {since.strftime('%m-%d %H:%M')} 之后的订单")
    orders = fetch_all_orders(cfg["miaoshou"], mon, since)
    log.info(f"拉到 {len(orders)} 条订单")

    state = load_state()
    notifier = Notifier(cfg, state)

    if args.report:
        # 日报同样要过滤旧单：妙手按包裹修改时间过滤，物流同步会把历史单捞回来，
        # 不过滤的话"今日 N 单"里会混进几个月前的订单
        report_max_age = now - timedelta(hours=mon.get("new_order_max_age_hours", 24))
        fresh = []
        for o in orders:
            oi = o.get("orderInfo", {})
            try:
                sdt = datetime.strptime(oi.get("gmtOrderStart", ""),
                                        "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_SHANGHAI)
            except Exception:
                fresh.append(o)
                continue
            if sdt >= report_max_age:
                fresh.append(o)
        if len(fresh) != len(orders):
            log.info(f"日报过滤：{len(orders)} 条中剔除 {len(orders) - len(fresh)} 条过旧订单")
        text = daily_report(fresh)
        now_str = now.strftime("%m-%d %H:%M")
        if args.quiet:
            print(text)
        else:
            ok, info = notifier.send(f"订单日报 {now.strftime('%m-%d')}", text)
            log.info(f"日报推送 {'成功' if ok else '失败'}: {info}")
        return

    # 找出"拉到了但从未通知过"的订单
    missing = []
    for o in orders:
        oi = o.get("orderInfo", {})
        oid = str(oi.get("opOrderId", ""))
        if not oid:
            continue
        if oid in state["notified"] or oid in state["pending"]:
            continue
        missing.append(o)

    if not missing:
        log.info("✅ 对账通过：无漏单")
        return

    log.warning(f"⚠️ 发现 {len(missing)} 单未通知")
    for o in missing:
        oi = o.get("orderInfo", {})
        log.warning(f"   漏单: {oi.get('platformOrderSn')} | "
                    f"{oi.get('orderAmount')} {oi.get('currency', '')} | "
                    f"下单 {oi.get('gmtOrderStart')}")

    if not args.fix or args.quiet:
        return

    # 补推：合并成一条，避免刷屏 + 省额度
    snaps = [snapshot(o) for o in missing]
    if len(snaps) == 1:
        title = f"补推漏单：{short(snaps[0])} {snaps[0]['sn']}"
        content = format_message(snaps[0])
    else:
        title = f"补推漏单 {len(snaps)} 单"
        content = format_batch(snaps)

    ok, info = notifier.send(title, content)
    log.info(f"补推 {'成功' if ok else '失败'}: {info}")
    if ok:
        for o in missing:
            oid = str(o.get("orderInfo", {}).get("opOrderId", ""))
            state["notified"][oid] = {"sn": o.get("orderInfo", {}).get("platformOrderSn", ""),
                                      "ts": now.isoformat(), "source": "reconcile"}
        save_state(state)


if __name__ == "__main__":
    main()
