#!/usr/bin/env python3
"""
本地 ↔ 云端 状态同步（消除重复推送的关键）
==========================================
问题：本地和云端互不知情 → 同一单可能被推两次（用户报过的"重复通知"）。

方案：把"谁推过哪些单"收敛到仓库里的两个文件，双方都读写：

    state/local_state.json   ← 本机 monitor 推完就写（本文件负责）
    state/cloud_state.json   ← 云端 Actions 推完才写（workflow 负责 git push）

两边的规则：
  * 本地：每轮开始前先**吸收** cloud_state（云端推过的单，本地标记为已通知、不再推）
  * 云端：只处理"下单时间 ≥ N 分钟前"且**两边状态里都没有**的单
    （留出 N 分钟余量，等本地先推 —— 本地是主通道，延迟 3 分钟）

用法：
    from cloud_sync import CloudSync
    cs = CloudSync(cfg.get("cloud_sync", {}))
    cs.absorb(state)        # 吸收云端已推名单（在判断新单之前调用）
    ...
    cs.publish(state)       # 把本地已推名单写回仓库（推送后调用，自带节流）
"""
import base64
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger("cloud_sync")

SCRIPT_DIR = Path(__file__).resolve().parent
THROTTLE_FILE = SCRIPT_DIR / "cloud_sync_state.json"

API = "https://api.github.com"
LOCAL_STATE_PATH = "state/local_state.json"
CLOUD_STATE_PATH = "state/cloud_state.json"
MAX_ENTRIES = 1000            # 状态文件最多保留多少条，防止无限膨胀
DEFAULT_THROTTLE = 300        # 本地写回仓库的最小间隔（秒）
TZ = timezone(timedelta(hours=8))


class CloudSync:
    def __init__(self, cs_cfg: dict):
        self.enabled = bool(cs_cfg.get("enabled"))
        self.repo = cs_cfg.get("repo", "")
        self.token = self._clean_token(cs_cfg.get("token"))
        self.throttle = int(cs_cfg.get("min_interval_seconds", DEFAULT_THROTTLE))
        self._last_publish = self._load_throttle()

    @staticmethod
    def _clean_token(raw) -> str:
        """占位符/明显无效的值一律当"没有 token"，避免每轮刷 401"""
        t = (raw or "").strip()
        if not t or t.startswith("PLEASE_FILL_IN") or len(t) < 20:
            return ""
        return t

    # ---------------- 节流 ----------------
    def _load_throttle(self) -> float:
        try:
            return json.loads(THROTTLE_FILE.read_text(encoding="utf-8")).get("last_publish", 0)
        except Exception:
            return 0

    def _save_throttle(self, ts: float):
        try:
            THROTTLE_FILE.write_text(json.dumps({"last_publish": ts}), encoding="utf-8")
        except Exception as e:
            log.warning(f"节流文件写入失败: {e}")

    # ---------------- 底层 HTTP ----------------
    def _headers(self):
        h = {"Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _get_file(self, path: str):
        """返回 (内容 dict | None, sha | None)。404 表示文件还不存在。

        有 token → 走 API（新鲜、无 CDN 缓存）；无 token → 走 raw（公开仓库可读，
        但 CDN 最多缓存 5 分钟，仅用于降级场景）。
        """
        if not self.token:
            url = f"https://raw.githubusercontent.com/{self.repo}/main/{path}"
            try:
                r = requests.get(url, timeout=20)
            except Exception as e:
                log.warning(f"[同步] 读取 {path} 失败: {e}")
                return None, None
            if r.status_code != 200:
                return None, None
            try:
                return r.json(), None
            except Exception:
                return None, None

        try:
            r = requests.get(f"{API}/repos/{self.repo}/contents/{path}",
                             headers=self._headers(), timeout=20)
        except Exception as e:
            log.warning(f"[同步] 读取 {path} 失败: {e}")
            return None, None
        if r.status_code == 404:
            return None, None
        if r.status_code in (401, 403):
            if not getattr(self, "_auth_warned", False):
                log.error(f"[同步] token 无效或无权限（HTTP {r.status_code}）"
                          f"，本机状态无法回写 → 云端不会接管。请检查 config.json 里的 token")
                self._auth_warned = True
            return None, None
        if r.status_code != 200:
            log.warning(f"[同步] 读取 {path} HTTP {r.status_code}: {r.text[:120]}")
            return None, None
        data = r.json()
        try:
            raw = base64.b64decode(data["content"]).decode("utf-8")
            return json.loads(raw), data.get("sha")
        except Exception as e:
            log.warning(f"[同步] 解析 {path} 失败: {e}")
            return None, data.get("sha")

    def _put_file(self, path: str, payload: dict, sha, message: str) -> bool:
        body = {
            "message": message,
            "content": base64.b64encode(
                json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")).decode("utf-8"),
        }
        if sha:
            body["sha"] = sha
        for attempt in range(3):
            try:
                r = requests.put(f"{API}/repos/{self.repo}/contents/{path}",
                                 headers=self._headers(), json=body, timeout=25)
            except Exception as e:
                log.warning(f"[同步] 写 {path} 异常({attempt + 1}/3): {e}")
                time.sleep(2)
                continue
            if r.status_code in (200, 201):
                return True
            if r.status_code in (409, 422):
                # sha 过期（对方刚写过），重新取 sha 再试
                _, body["sha"] = self._get_file(path)
                time.sleep(1)
                continue
            log.warning(f"[同步] 写 {path} HTTP {r.status_code}: {r.text[:160]}")
            return False
        return False

    # ---------------- 对外接口 ----------------
    def absorb(self, state: dict) -> int:
        """
        吸收云端已推名单：云端推过的单，本地标记为已通知（silent），不再重复推。
        返回吸收条数。必须在判断"哪些是新单"之前调用。

        没有 token 时直接跳过：云端状态文件只有云端会写，而云端在本机成功回写
        之前一直处于"日报模式"（不逐单推），所以此时云端名单必然为空，读了也没用。
        """
        if not self.enabled or not self.repo or not self.token:
            return 0
        remote, _ = self._get_file(CLOUD_STATE_PATH)
        if not remote:
            return 0
        got = 0
        for oid, info in (remote.get("pushed") or {}).items():
            oid = str(oid)
            if oid and oid not in state["notified"]:
                state["notified"][oid] = {"sn": info.get("sn", ""),
                                          "ts": info.get("ts", ""), "from": "cloud"}
                got += 1
        if got:
            log.info(f"[同步] 从云端吸收 {got} 条已推订单，本轮不会重复推送")
        return got

    def publish(self, state: dict, force: bool = False) -> bool:
        """把本地已推名单写回仓库。节流：默认 5 分钟内只写一次。"""
        if not self.enabled or not self.repo or not self.token:
            return False
        now = time.time()
        if not force and now - self._last_publish < self.throttle:
            return False

        items = list(state["notified"].items())
        # 只保留最近的 MAX_ENTRIES 条，按时间倒序裁剪
        items.sort(key=lambda kv: kv[1].get("ts", ""), reverse=True)
        pushed = {oid: {"sn": v.get("sn", ""), "ts": v.get("ts", "")}
                  for oid, v in items[:MAX_ENTRIES]}
        payload = {"updated_at": datetime.now(TZ).isoformat(), "pushed": pushed}

        remote, sha = self._get_file(LOCAL_STATE_PATH)
        # 名单没变化就不写。否则每 5 分钟（节流间隔）都会产生一个空提交，
        # 一天刷近 300 个 commit，还会连带触发云端 checkout 的噪声。
        if remote is not None and (remote.get("pushed") or {}) == pushed:
            self._last_publish = now
            self._save_throttle(now)
            return False
        ok = self._put_file(LOCAL_STATE_PATH, payload, sha,
                            f"chore(state): 本机已推 {len(pushed)} 单")
        if ok:
            self._last_publish = now
            self._save_throttle(now)
            log.info(f"[同步] 已回写本机状态（{len(pushed)} 条）→ {LOCAL_STATE_PATH}")
        else:
            log.warning("[同步] 回写本机状态失败，下轮重试")
        return ok
