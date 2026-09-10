# 运维备忘（2026-09-10 完成）

## 已完成

| # | 事项 | 结果 |
|---|---|---|
| 1 | GitHub Token（classic，scopes `repo` + `workflow`） | ✅ 已生成并用于以下三项 |
| 2 | Actions Secrets 写入 | ✅ 11 个全部成功（`set_gh_secrets.py`） |
| 3 | README 明文密钥整改 | ✅ 已移除，改为「Name + 说明」表格 |
| 4 | 云端链路接线（`monitor.yml` 的邮件 Secrets 映射） | ✅ 之前漏接，已补 |
| 5 | 云端接管模式（Mac 关机也能逐单通知） | ✅ 代码就绪，靠 `state/local_state.json` 激活 |
| 6 | Token 写入 `config.json` 的 `cloud_sync.token` | ✅ 本地可回写状态 |

## ⚠️ 必须尽快处理：密钥轮换

README 里的明文密钥已从代码中删除，但它仍存在于 **git 提交历史**（`97a0925`），
而仓库是 **public** —— 等同于已经公开过。删文件只是止血，**必须去平台重置**：

| 平台 | 操作 | 重置后要做 |
|---|---|---|
| 妙手开放平台 | 重置 **AppSecret** | 更新 `local/config.json` → 重跑 `set_gh_secrets.py` |
| Server酱 `sct.ftqq.com` | 重置 **SendKey** | 同上 |
| QQ 邮箱 | 授权码未进过仓库，**无需**轮换 | — |

## ⚠️ Token 有效期提醒

本机 monitor 每轮结束都要用这个 token 把「已推名单」写回仓库
（`state/local_state.json`），云端靠它判断哪些单本机已经推过。

- **失效征兆**：`monitor.log` 出现 `[同步] token 无效或无权限（HTTP 401）`
- **失效后果**：本机照常推单 → 状态写不进去 → 云端误判「本机离线」→ 接管补推 → **重复通知**
- **建议**：设 1 年有效期，到期前换新；换新后只改 `config.json` 并重启 monitor

## 日常维护命令

```bash
cd ~/WorkBuddy/2026-09-10-18-17-36/miaoshou-monitor/local

# 看实时日志
tail -f monitor.log

# 重启（务必用 kill pidfile，让自愈循环自己拉起来；不要 pkill start.sh）
kill $(cat monitor.pid)

# 只查漏单不推送
/Users/jianguo/.workbuddy/binaries/python/envs/default/bin/python reconcile.py --hours 24 --quiet

# 更新仓库 Secrets
GH_TOKEN=<token> /Users/jianguo/.workbuddy/binaries/python/envs/default/bin/python set_gh_secrets.py
```

## 云端排障

仓库 → Actions → `妙手ERP订单通知（云端接管）`：

| 现象 | 原因 |
|---|---|
| 日志说「日报」模式 | 仓库里还没有 `state/local_state.json` → 本机尚未成功回写过状态 |
| 日志说「暂缓 N 单」 | 这些单下单不足 15 分钟，按设计留给本机，**正常** |
| 日志说「跳过过旧订单」 | 物流同步捞回的旧单，被 24 小时护栏挡住，**正常** |
| 一轮都没有 | cron 被停（仓库 60 天无活动）→ 随便提交一次即可恢复 |
