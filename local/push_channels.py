#!/usr/bin/env python3
"""
推送通道层（邮件）
==================
本项目**只有邮件一个推送通道**，加上本机的电脑语音提醒（见 desktop_alert.py），
一共就这两条路。

历史背景（2026-09-11 收敛）：曾经同时挂过 PushPlus / 企业微信自建应用 /
企业微信群机器人 / WxPusher / Server酱，逐个试过之后全部去掉：
  - PushPlus ：免费版要实名认证 + 收认证费
  - 企业微信自建应用：强制「企业可信IP」，未认证企业还要公网 IP 回调，家用宽带配不通（60020）
  - 企业微信群机器人：能用，但消息只在企业微信里看，且与邮件重复
  - WxPusher ：免费无限，但多一个账号要维护
  - Server酱 ：免费版每天只有 5 条，单量一上来必漏
结论：**邮件一条路就够了**（无条数限制、免费、可长期留存、手机邮件 App 直接弹窗），
      可靠性交给下面三层保证，而不是靠"堆通道"。

可靠性靠这三层（不靠多通道）：
  1. **失败重试队列** —— 推失败的订单带完整快照进 pending，下一轮继续推，
     不会因为掉出 6 小时查询窗口而永久丢单
  2. **双执行体共用一份去重状态** —— 本机 3 分钟轮询 + 云端 GitHub Actions 兜底
  3. **本机语音提醒** —— 推送成功的同一秒出声，人在电脑前就不必盯手机

扩展方式：要加新通道，写一个带 `name` 和 `send(title, content) -> (ok, msg)` 的类，
在 Notifier 里注册，再把名字写进 config.json 的 `push.channel_order` 即可。
"""

import time
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("push")
TZ = timezone(timedelta(hours=8))


class RateLimiter:
    """简单的发送间隔限流器"""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0

    def acquire(self):
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            log.info(f"[限流] 等待 {wait:.1f}s（通道最小间隔 {self.min_interval}s）")
            time.sleep(wait)
        self._last = time.time()


class EmailChannel:
    """
    邮件通道 —— 本项目唯一的推送通道。

    需要 SMTP 授权码（**不是**邮箱登录密码，这是最常见的卡点）。
    投递延迟通常 1~10 秒；失败会抛异常并被上层记入 pending 队列重试。
    """

    name = "email"

    def __init__(self, host: str, port: int, user: str, password: str,
                 to_addr: str, limiter: RateLimiter):
        self.host, self.port = host, port
        self.user, self.password = user, password
        self.to_addr = to_addr or user
        self.limiter = limiter

    def send(self, title: str, content: str) -> tuple[bool, str]:
        if not (self.host and self.user and self.password):
            return False, "邮件参数未配置"
        self.limiter.acquire()
        try:
            import smtplib
            from email.mime.text import MIMEText
            from email.header import Header
            msg = MIMEText(content, "plain", "utf-8")
            msg["Subject"] = Header(title, "utf-8")
            msg["From"] = self.user
            msg["To"] = self.to_addr
            with smtplib.SMTP_SSL(self.host, int(self.port), timeout=20) as s:
                s.login(self.user, self.password)
                s.sendmail(self.user, [self.to_addr], msg.as_string())
            return True, "ok"
        except Exception as e:
            return False, str(e)[:200]


class Notifier:
    """
    统一推送入口：按 channel_order 顺序尝试，自动跳过额度用尽的通道。
    state 里维护 sent_today（按天计数），跨零点自动重置。

    当前只注册了 email 一个通道，但接口形状是多通道的 —— 将来加通道不用改调用方。
    """

    def __init__(self, cfg: dict, state: dict):
        push_cfg = cfg["push"]
        self.cfg = push_cfg
        self.state = state

        self.channels = {}

        def filled(*vals):
            """占位符（PLEASE_ 开头）视为未配置"""
            return all(v and not str(v).startswith("PLEASE_") for v in vals)

        e = push_cfg.get("email", {})
        if filled(e.get("host"), e.get("user"), e.get("password")):
            self.channels["email"] = EmailChannel(
                e["host"], e.get("port", 465), e["user"], e["password"],
                e.get("to", ""), RateLimiter(e.get("min_interval_seconds", 5)))

        order = push_cfg.get("channel_order", ["email"])
        self.order = [c for c in order if c in self.channels]
        self._reset_daily_if_needed()

    # -------- 日额度 --------
    def _reset_daily_if_needed(self):
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        if self.state.get("day") != today:
            self.state["day"] = today
            self.state["sent_today"] = {}
        self.state.setdefault("sent_today", {})

    def _left(self, name: str) -> int:
        limit = self.cfg.get("daily_limit", {}).get(name, 9999)
        used = self.state["sent_today"].get(name, 0)
        return max(0, limit - used)

    def has_budget(self) -> bool:
        return any(self._left(n) > 0 for n in self.order)

    # -------- 发送 --------
    def send(self, title: str, content: str) -> tuple[bool, str]:
        self._reset_daily_if_needed()
        errors = []
        for name in self.order:
            if self._left(name) <= 0:
                errors.append(f"{name}: 今日额度已用尽")
                continue
            ok, msg = self.channels[name].send(title, content)
            # 无论成败都计一次请求（失败请求同样占用配额）
            self.state["sent_today"][name] = self.state["sent_today"].get(name, 0) + 1
            if ok:
                log.info(f"[推送] ✅ {name} 成功: {title}（今日已用 "
                         f"{self.state['sent_today'][name]}）")
                return True, name
            errors.append(f"{name}: {msg}")
            log.warning(f"[推送] ❌ {name} 失败: {msg}")
        return False, " | ".join(errors)
