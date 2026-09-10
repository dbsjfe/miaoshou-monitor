#!/usr/bin/env python3
"""
推送通道层
==========
解决的问题：
  1. Server酱免费版 5 条/天 —— 第 6 单起全部推送失败，导致漏单
  2. 单通道故障/配额耗尽 —— 没有降级，消息直接丢
  3. 频率限制（PushPlus 免费版 1 分钟 5 条）—— 高峰期会被拒

设计：
  - 多通道 + 自动降级：主通道失败或日额度用尽 → 自动切备用通道
  - 限流：按通道配置最小发送间隔
  - 日额度本地计数：跨零点自动重置，额度用尽自动切通道（而不是硬失败）
"""

import time
import logging
from datetime import datetime, timezone, timedelta

import requests

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


class PushPlusChannel:
    """PushPlus（推送加）—— 免费实名用户 200 条/天，1 分钟 5 条"""

    name = "pushplus"
    url = "https://www.pushplus.plus/send"

    def __init__(self, token: str, limiter: RateLimiter):
        self.token = token
        self.limiter = limiter

    def send(self, title: str, content: str) -> tuple[bool, str]:
        if not self.token or self.token.startswith("PLEASE_"):
            return False, "未配置 PushPlus token"
        self.limiter.acquire()
        try:
            resp = requests.post(
                self.url,
                json={
                    "token": self.token,
                    "title": title[:100],
                    "content": content,
                    "template": "txt",
                    "channel": "wechat",
                },
                timeout=20,
            )
            data = resp.json()
            # PushPlus 成功返回 code=200（注意不是 0）
            if data.get("code") == 200:
                return True, "ok"
            return False, f"code={data.get('code')} msg={data.get('msg')}"
        except Exception as e:
            return False, str(e)[:200]


class WecomAppChannel:
    """
    企业微信「自建应用」消息 —— 免费、无日条数限制、消息直达个人微信

    关键前提（缺一不可）：
      1. 企业微信后台「我的企业 → 微信插件」扫码关注，之后在**个人微信**里就能收到，
         连企业微信 App 都不用装
      2. 若个人微信收不到：微信插件页勾选「允许成员在微信插件中接收和回复聊天消息」；
         企业微信 App「我 → 设置 → 新消息通知」关闭「仅在企业微信中接收消息」

    access_token 有效期 2 小时，进程内缓存，不要每次都去换（会被限频）。
    """

    name = "wecom"
    token_url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
    send_url = "https://qyapi.weixin.qq.com/cgi-bin/message/send"

    def __init__(self, corpid: str, secret: str, agentid, touser: str,
                 limiter: RateLimiter):
        self.corpid = corpid
        self.secret = secret
        self.agentid = agentid
        self.touser = touser or "@all"
        self.limiter = limiter
        self._token = None
        self._token_expire = 0.0

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expire:
            return self._token
        try:
            resp = requests.get(self.token_url, params={
                "corpid": self.corpid, "corpsecret": self.secret}, timeout=15)
            data = resp.json()
            if data.get("errcode") != 0:
                log.error(f"[wecom] 获取 token 失败: {data}")
                return ""
            self._token = data["access_token"]
            self._token_expire = time.time() + data.get("expires_in", 7200) - 300
            return self._token
        except Exception as e:
            log.error(f"[wecom] 获取 token 异常: {e}")
            return ""

    def send(self, title: str, content: str) -> tuple[bool, str]:
        if not (self.corpid and self.secret and self.agentid):
            return False, "企业微信参数未配置"
        token = self._get_token()
        if not token:
            return False, "access_token 获取失败"

        self.limiter.acquire()
        payload = {
            "touser": self.touser,
            "msgtype": "text",
            "agentid": int(self.agentid),
            "text": {"content": f"{title}\n\n{content}"},
            "safe": 0,
        }
        try:
            resp = requests.post(self.send_url, params={"access_token": token},
                                 json=payload, timeout=20)
            data = resp.json()
            if data.get("errcode") == 0:
                return True, "ok"
            # 42001 = token 过期，清缓存让下次重新获取
            if data.get("errcode") in (42001, 40014):
                self._token = None
            return False, f"errcode={data.get('errcode')} errmsg={data.get('errmsg')}"
        except Exception as e:
            return False, str(e)[:200]


class WecomBotChannel:
    """
    企业微信「群机器人」Webhook —— 无 IP 白名单限制，本地/云端都能用

    相比自建应用消息的优势：
      - 不需要「企业可信IP」（自建应用会报 60020，且家用宽带 IP 会变、云端 IP 不可预知）
      - 不需要可信域名 / 接收消息服务器URL
      - 配置只要一个 webhook URL
    代价：消息在企业微信 App 的群里看，不是个人微信会话。
    频率：每个机器人 20 条/分钟。
    """

    name = "wecom_bot"

    def __init__(self, key: str, limiter: RateLimiter, mention_all: bool = False):
        self.url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"
        self.limiter = limiter
        self.mention_all = mention_all

    def send(self, title: str, content: str) -> tuple[bool, str]:
        if not self.url or "key=" not in self.url:
            return False, "群机器人 webhook key 未配置"
        self.limiter.acquire()
        payload = {
            "msgtype": "text",
            "text": {"content": f"{title}\n\n{content}"[:2048]},
        }
        if self.mention_all:
            payload["text"]["mentioned_list"] = ["@all"]
        try:
            resp = requests.post(self.url, json=payload, timeout=20)
            data = resp.json()
            if data.get("errcode") == 0:
                return True, "ok"
            return False, f"errcode={data.get('errcode')} errmsg={data.get('errmsg')}"
        except Exception as e:
            return False, str(e)[:200]


class WxPusherChannel:
    """
    WxPusher —— 微信公众号通道，消息直接在**个人微信**里弹出

    为什么用它替代企业微信自建应用：
      - 企业微信自建应用自 2022 起强制「企业可信IP」；未认证企业还要求先配
        「接收消息服务器URL」（需公网 IP + 回调校验），家用宽带 IP 会变、且
        GitHub Actions 出口 IP 不可预知 → 这条路对本地+云端双链路不可行
      - WxPusher 无 IP 白名单、无实名、无需企业认证、免费

    配置（约 2 分钟）：
      1. 微信扫码登录 https://wxpusher.zjiecode.com/admin/ → 创建应用 → 拿到 appToken（AT_ 开头）
      2. 在应用页「关注应用」用微信扫码关注
      3. 微信里进「WxPusher」公众号 → 我的 → 我的UID → 拿到 UID（UID_ 开头）
    成功返回 code == 1000。
    """

    name = "wxpusher"
    url = "https://wxpusher.zjiecode.com/api/send/message"

    def __init__(self, app_token: str, uid: str, limiter: RateLimiter):
        self.app_token = app_token
        # 支持逗号分隔多个 UID
        self.uids = [u.strip() for u in str(uid or "").split(",") if u.strip()]
        self.limiter = limiter

    def send(self, title: str, content: str) -> tuple[bool, str]:
        if not self.app_token or not self.uids:
            return False, "未配置 WxPusher appToken/UID"
        self.limiter.acquire()
        payload = {
            "appToken": self.app_token,
            "content": f"{title}\n\n{content}",
            "summary": title[:100],
            "contentType": 1,
            "uids": self.uids,
        }
        try:
            resp = requests.post(self.url, json=payload, timeout=20)
            data = resp.json()
            # WxPusher 成功码是 1000（不是 0）
            if data.get("code") == 1000:
                return True, "ok"
            return False, f"code={data.get('code')} msg={data.get('msg')}"
        except Exception as e:
            return False, str(e)[:200]


class EmailChannel:
    """
    邮件通道（可选第三级兜底）—— 配合微信「QQ邮箱提醒」在微信里收
    需要：SMTP 授权码（不是邮箱密码）。延迟 1~5 分钟，仅作最后兜底。
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


class ServerChanChannel:
    """Server酱 —— 免费版仅 5 条/天，仅作降级备用"""

    name = "serverchan"

    def __init__(self, send_key: str, limiter: RateLimiter):
        self.send_key = send_key
        self.limiter = limiter

    def send(self, title: str, content: str) -> tuple[bool, str]:
        if not self.send_key:
            return False, "未配置 Server酱 send_key"
        self.limiter.acquire()
        try:
            resp = requests.post(
                f"https://sctapi.ftqq.com/{self.send_key}.send",
                data={"title": title, "desp": content},
                timeout=20,
            )
            data = resp.json()
            if data.get("code") == 0:
                return True, "ok"
            return False, f"code={data.get('code')} msg={data.get('message')}"
        except Exception as e:
            return False, str(e)[:200]


class Notifier:
    """
    统一推送入口：按优先级尝试各通道，自动跳过额度用尽的通道。
    state 里维护 sent_today（按天计数），跨零点自动重置。
    """

    def __init__(self, cfg: dict, state: dict):
        push_cfg = cfg["push"]
        self.cfg = push_cfg
        self.state = state

        interval = push_cfg.get("min_interval_seconds", 13)
        self.channels = {}

        def filled(*vals):
            """占位符（PLEASE_ 开头）视为未配置"""
            return all(v and not str(v).startswith("PLEASE_") for v in vals)

        wx = push_cfg.get("wxpusher", {})
        if filled(wx.get("app_token"), wx.get("uid")):
            self.channels["wxpusher"] = WxPusherChannel(
                wx["app_token"], wx["uid"],
                RateLimiter(wx.get("min_interval_seconds", 2)))

        w = push_cfg.get("wecom", {})
        if filled(w.get("corpid"), w.get("secret"), w.get("agentid")):
            self.channels["wecom"] = WecomAppChannel(
                w["corpid"], w["secret"], w["agentid"], w.get("touser", "@all"),
                RateLimiter(w.get("min_interval_seconds", 2)))

        bot = push_cfg.get("wecom_bot", {})
        if filled(bot.get("key")):
            self.channels["wecom_bot"] = WecomBotChannel(
                bot["key"], RateLimiter(bot.get("min_interval_seconds", 4)),
                bot.get("mention_all", False))

        if push_cfg.get("pushplus", {}).get("token"):
            self.channels["pushplus"] = PushPlusChannel(
                push_cfg["pushplus"]["token"], RateLimiter(interval))

        if push_cfg.get("serverchan", {}).get("send_key"):
            self.channels["serverchan"] = ServerChanChannel(
                push_cfg["serverchan"]["send_key"], RateLimiter(3))

        e = push_cfg.get("email", {})
        if e.get("host") and e.get("user") and e.get("password"):
            self.channels["email"] = EmailChannel(
                e["host"], e.get("port", 465), e["user"], e["password"],
                e.get("to", ""), RateLimiter(e.get("min_interval_seconds", 5)))

        # WxPusher 优先（个人微信、免费无限、无 IP 限制）
        # → 企业微信应用消息（需可信IP，配好才生效）→ Server酱（5条/天）→ 邮件
        order = push_cfg.get("channel_order",
                             ["wxpusher", "wecom", "serverchan", "email"])
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
            # 无论成败都计一次请求（PushPlus 失败请求同样计入额度）
            self.state["sent_today"][name] = self.state["sent_today"].get(name, 0) + 1
            if ok:
                log.info(f"[推送] ✅ {name} 成功: {title}（今日已用 "
                         f"{self.state['sent_today'][name]}）")
                return True, name
            errors.append(f"{name}: {msg}")
            log.warning(f"[推送] ❌ {name} 失败: {msg}")
        return False, " | ".join(errors)
