# 本地常驻版说明（双保险架构）

## v2 重构（2026-09-10）：修掉"重复 / 漏单 / 每天只 5 次"

三个问题同一个根因：**Server酱免费版 5 条/天**，第 6 单起推送失败，
而 v1 只在推送成功时才记录"已通知"，失败的单下轮重试时早已掉出 5 分钟查询窗口。

| 问题 | v1 根因 | v2 修法 |
|------|---------|---------|
| 重复推送 | 失败后反复重试；本地+云端两套状态互不知情，同一单各推一次 | 统一状态机 `pending → notified`，先占位后推送；云端改为只发日报汇总，不再逐单推 |
| 漏单 | `last_check` 每轮推进到 now，失败订单掉出 5 分钟窗口即永久丢失；只拉第 1 页 20 条 | 失败订单进 pending 队列（**带快照，无需重新拉取即可重试**）；分页拉全量（50×20 页） |
| 每天只 5 次 | Server酱免费版配额 | 主通道 PushPlus（200 条/天），Server酱降级备用；额度/限流自动切换；积压 ≥3 单自动合并成一条推送 |

推送链（按 `config.json` 的 `channel_order` 顺序自动降级）：

```
邮件（主，无限量，QQ邮箱，账号见 config.json）✅ 已打通并实测送达
        ↓ 发送失败（SMTP 异常 / 授权码失效）
    Server酱（兜底，5 条/天，个人微信）
        ↓ 额度用尽
    留在 pending 队列，次日额度恢复自动补推
```

**当前生效顺序（2026-09-10 21:39 用户指定）**：`email → serverchan`，本地与云端一致。
其他通道（WxPusher / 企业微信应用消息 / 群机器人）代码保留但**已从 channel_order 移除**，
要用时把对应 key 填好再加回顺序即可。

> 邮件建议在微信里绑定「QQ邮箱提醒」服务号（微信搜服务号 → 绑定收件邮箱 → 开启新邮件提醒），
> 这样邮件通知也会在微信弹出，等于邮件与微信二合一。

---

## 电脑语音提醒（2026-09-11 新增）：来单时本机念一句

### 为什么要做

邮件 / Server酱 属于**远程**提醒。坐在电脑前干活时手机不在手边、微信消息被折叠，
一单来了照样十几分钟后才发现。补一个**本机**提醒：推送成功的同一秒出声 + 弹横幅。

### 实现位置

| 文件 | 职责 |
|---|---|
| `local/desktop_alert.py` | `DesktopAlert` 类：`say` 念话 + `osascript` 弹横幅 + `afplay` 兜底提示音 |
| `local/monitor.py` | `push_pending(..., sent_snaps)` 收集**推送成功**的快照；`run_once` 末尾统一 `announce()` |
| `local/test_push.py --voice` | 只测语音，不出网不推送 |

### 四个必须知道的坑

1. **只有推送成功才播报**。`sent_snaps` 只在 `ok=True` 时 append —— 推失败还念一句
   等于白高兴，而且会让人误以为通知链路是通的。
2. **必须跑在 GUI(Aqua) 会话**。`start.sh` 从终端启动、或 launchd **LaunchAgent**
   都在 Aqua 会话内，有声音；换成 **LaunchDaemon(root)** 就是**完全静默**——
   这是"配置都对但没反应"最常见的原因，且没有任何报错。
3. **`say` 是同步阻塞的**（念完才返回）。所以一律 `subprocess.Popen(..., start_new_session=True)`
   分离进程 + **绝不 wait()**，只用 `poll()` 回收僵尸。否则一次播报会把 180 秒的轮询
   循环卡住几秒，多单时还会几句叠在一起谁也听不清 —— 因此多单**合并成一句**
   （"您有 3 条新订单，出单地区马来、菲律宾"）。
4. **沙箱（AI 会话）里 `say` 可能无声或失败**，这是环境限制不是配置错误。判断标准看
   **退出码**：`say -v Tingting "测试"; echo $?` 返回 0 才算真的发出去了。

### 自检

```bash
python local/test_push.py --voice          # 示例文案
python local/test_push.py --voice --real   # 用最近一条真实订单的站点播报
python local/desktop_alert.py              # 模块自带自检（单条 + 多条各念一遍）
```

离线端到端仿真（不联网、不真发邮件，验证「推送成功 → 播报」整条链路）：
mock 掉 `monitor.fetch_all_orders` 与 `monitor.Notifier`，构造新单后直接调
`monitor.run_once(cfg, state, None, DesktopAlert(cfg["desktop_alert"]))`，
日志应出现 `[语音] 🔊 播报: 您有一条新的菲律宾订单，请及时处理`。

### 已知小瑕疵（未修，影响很小）

`format_batch()` 合计金额用的是 `items[0]["currency"]`。**多站点混合**积压时
（如马来 MYR + 菲律宾 PHP）会把不同币种直接相加、再标第一个币种的符号。
当前实际业务是单站点为主，暂不处理；要修的话应按 currency 分组小计。

---

## 云端接管（2026-09-10 新增）：Mac 关机也能逐单通知

### 为什么要做

原来云端只发日报（每天 2 条汇总），Mac 关机期间拿不到逐单通知。
但**不能简单地把云端改成"每 10 分钟逐单推"**——本地和云端互不知情，
同一单会被推两次，正是之前报过的"重复通知"。

### ⚠️ 先认清现实：GitHub 的 cron 很不靠谱（2026-09-10 实测）

官方文档原文：`schedule` 事件在高负载时可延迟，**排队任务可能被直接丢弃**。
本仓库实测数据：

| 配置 | 理论频率 | 实际频率 |
|---|---|---|
| `*/5`（原配置） | 288 次/天 | 约 250 次/天；**近一周掉到 6~9 次/天**，间隔 2~5 小时 |

统计方法：拉 1548 条运行记录按日期分组（见提交记录里的排查过程）。
社区反馈一致——有人测到 7 天里 `*/5` 只跑了 97 次（理论 2016 次，**5%**），
日报任务平均延迟 **2 小时 42 分**。

**所以定位必须摆正**：

- **准时 = 本机 monitor**（180 秒轮询）。本机开着就一定是实时的
- **云端 = 宽松兜底**。Mac 关机时可能延迟几十分钟到几小时，但**不会丢**
- cron 已改为 `7,17,27,37,47,57 * * * *`，刻意避开 `:00` / `:30`
  （这两个分钟位是全 GitHub 的拥堵高峰，用冷门分钟触发率明显更高）

想要真正准时的云端兜底，只能上**外部调度器**（cron-job.org / Cloudflare Worker cron /
Google Cloud Scheduler）定时调 `workflow_dispatch` REST 接口——
因为它走实时事件通道而非批量 cron 队列。代价是要多一个第三方依赖与一处 token 托管。

### 怎么做：两个状态文件 + 时间余量

```
state/local_state.json   本机 monitor 推完订单 → 通过 GitHub Contents API 回写
state/cloud_state.json   云端推完订单 → workflow git commit 提交回仓库
```

- **本地**（`cloud_sync.py`）：每轮开工前先 `absorb()` 读 `state/cloud_state.json`，
  把云端推过的单标记为已通知 → Mac 重新开机后不会把关机期间的单重播一遍
- **云端**（`reconcile.py --cloud-fix`）：候选 = `orders − local_state − cloud_state`，
  再叠两道闸：
  - **`--min-age-minutes 15`**：下单不满 15 分钟的单先不动，留给本地（本地 3 分钟轮询）。
    本地在线时云端永远插不上话 → 零重复
  - **24 小时护栏**（`new_order_max_age_hours`）：妙手按「包裹修改时间」过滤，
    物流同步会把几个月前的旧单捞回来，不过滤就会拿旧单轰炸你
- 推完写入 `cloud_state.json`，workflow 用 `git push` 提交（带 rebase 重试）

### 自动降级（宁可少推，不可重推）

`run.py` 会检查 `state/local_state.json` 是否存在：

| 情况 | 云端行为 |
|---|---|
| 文件存在（本机 token 已配好、能回写） | `--cloud-fix` 接管模式，逐单补推 |
| 文件不存在（还没配 token） | `--report` 只发日报汇总 —— 无法可靠去重时绝不逐单推 |

所以**没配 token 也不会重复**，只是回到"每天汇总"的保守状态。

### 启用云端接管需要什么

本机 `config.json` 的 `cloud_sync.token` 填上 GitHub token。
**已于 2026-09-10 配好并实测生效**（classic token，scopes `repo` + `workflow`）。

**⚠️ 该 token 失效的隐患**：本地会继续推单但无法回写状态 →
本地日志报 `[同步] token 无效或无权限` / `回写本机状态失败`，
而云端会因 `local_state` 变旧而接管 → **可能重复**。
所以这两条日志一旦出现要立刻处理。建议设 1 年有效期，到期前换新。

### 链路自检（2026-09-10 新增）

推送失败是**静默**的——密钥写错、授权码失效、通道限流，进程都照跑不误。
所以两边都留了自检入口：

```bash
# 本地
cd ~/WorkBuddy/2026-09-10-18-17-36/miaoshou-monitor/local
/Users/jianguo/.workbuddy/binaries/python/envs/default/bin/python test_push.py
```

云端：仓库 → Actions → `妙手ERP订单通知（云端接管）` → **Run workflow** → 勾选
**「只跑链路自检」** → 它会发一条测试通知就结束，不查订单、不写状态。

自检会打印「配置的通道顺序 / 实际启用的通道 / 实际生效顺序」三者对照——
改过 Secrets 或换过授权码之后跑一次，比等漏单了才发现划算。

---

> 通道选型折腾了三轮，结论记这里避免重复踩坑：
> - **PushPlus** 免费 200 条/天，但**实名认证要收费** → 弃用
> - **企业微信自建应用** 免费无限直连微信，但 2022 起强制「企业可信IP」，
>   且未认证企业还得先配「接收消息服务器URL」（需公网 IP + 回调校验）→ 卡死，见下节
> - **WxPusher** 无 IP 白名单、无实名、无费用，微信扫码 2 分钟配好 → **备用通道（未启用）**

### ⚠️ 企业微信应用消息的坑：60020 可信 IP（2026-09-10 实测）

新建的自建应用**必须**配置「企业可信IP」才能调 API，否则报
`errcode=60020 not allow to access from your ip`。

**企业微信官方社区的答复**（[帖子](https://developer.work.weixin.qq.com/community/question/detail?content_id=16808487094259272108)）：
- 已认证企业：需先配「可信域名」（备案主体须与企业主体一致或有关联）才能配可信IP
- **未认证企业**：需先配「接收消息服务器URL」，且要求**公网 IP 形式**
  → 即必须有一台公网可达的服务器来响应企业微信的回调校验

所以这条路对个人用户基本封死：
1. 没有公网服务器就填不了 URL → 填不了可信IP → 60020 永远在
2. 就算有服务器，**家用宽带 IP 会变**，一换就静默失效（和 certifi 那次一样，不查日志发现不了）
3. **云端 GitHub Actions 用不了**——出口 IP 不固定且不可预知，加不进白名单

结论：**放弃应用消息作为主通道**，改用 WxPusher（下节）。应用消息通道代码仍保留，
若哪天配好可信IP 会自动生效，排在 WxPusher 之后。

另外还加了群机器人通道兜底（无 IP 限制）：

### 企业微信群机器人（备用通道，已内置未启用，无 IP 限制）

企业微信 App → 建个群（拉自己就行，或找同事）→ 群右上角设置 → 群机器人 → 添加机器人
→ 复制 **Webhook 地址**（形如 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxx`）
→ 把 `key=` 后面那串填进 `config.json` 的 `push.wecom_bot.key`

- 无 IP 白名单、无实名、无费用、**20 条/分钟**
- 本地和云端都能用
- 代价：消息在企业微信 App 的群里看，不是个人微信会话
- 当前**未启用**（已从 `channel_order` 移除），需要时把 key 填好再加回顺序

---

### ☆ WxPusher 配置（备用通道，已内置未启用，免费无限，微信直达）

服务实测存活（2026-09-10 探测返回 `code=1001 appToken不正确`，说明接口正常）。

1. 电脑打开 https://wxpusher.zjiecode.com/admin/ ，**微信扫码登录**（无需注册）
2. 点「创建应用」：应用名随便填（如"TK订单通知"），其他项可留空 → 拿到 **appToken**（`AT_` 开头）
3. 应用页「关注应用」，用**同一个微信**扫码关注
4. 微信里进入「WxPusher」公众号 → 底部菜单「我的」→「我的UID」→ 拿到 **UID**（`UID_` 开头）
5. 把 appToken / UID 填进 `local/config.json` 的 `push.wxpusher`，重启监控

成功返回 `code == 1000`（注意不是 0）。消息直接以公众号消息形式弹到个人微信，
内容在聊天页可见，不需要点开。云端需同步在仓库 Secrets 补 `WXPUSHER_APP_TOKEN` / `WXPUSHER_UID`。

---

### 企业微信应用消息配置（约 10 分钟，免费，个人可注册）

1. 电脑打开 https://work.weixin.qq.com 点「立即注册」
   —— **企业名称随便填**（如"我的订单通知"），行业/规模随便选，
   **不需要营业执照、不需要认证、不花钱**
2. 「应用管理 → 自建 → 创建应用」：名称填"订单通知"，可见范围选自己
   → 得到 **AgentId**，点「查看」得到 **Secret**（会发验证码到微信）
3. 「我的企业 → 企业信息」拉到最底部 → 复制 **企业ID（CorpID）**
4. **关键**：「我的企业 → 微信插件」扫码关注
   —— 这样消息直接进**个人微信**，连企业微信 App 都不用装
5. 把 CorpID / Secret / AgentId 填进 `local/config.json` 的 `push.wecom`，重启监控

**个人微信收不到时排查**：
- 微信插件页面勾选「允许成员在微信插件中接收和回复聊天消息」
- 企业微信 App「我 → 设置 → 新消息通知」关闭「仅在企业微信中接收消息」

---

本项目有两条并行的订单推送链路，互为兜底：

| 层 | 位置 | 触发方式 | 特点 |
|----|------|----------|------|
| 云端兜底 | `run.py` + `.github/workflows/monitor.yml` | GitHub Actions cron `*/5 * * * *` | Mac 关机/休眠也能跑；但免费版 schedule 有 1~3 小时限流抖动 |
| 本地常驻 | `local/miaoshou_monitor.py` | launchd 守护 + `caffeinate` 防睡眠，180 秒轮询 | 实时性最好（3 分钟内必达）；依赖本机开机在线 |

两条链路都靠 **`opOrderId` 去重**（各自维护已通知清单），所以同时开启不会重复推送同一单。

---

## 一、本地常驻版（local/）

### 文件

| 文件 | 作用 |
|------|------|
| **`monitor.py`** | **v2 主程序**：180s 轮询 + 分页全量拉取 + pending 重试队列 + 多通道降级推送 |
| **`push_channels.py`** | 通道层：邮件 / Server酱 / WxPusher / 企业微信应用 / 群机器人 + 限流 + 日额度计数与自动降级 |
| **`cloud_sync.py`** | **本地↔云端去重同步**：`absorb()` 吸收云端已推名单，`publish()` 回写本机名单 |
| **`reconcile.py`** | 对账工具：`--fix` 本地补推，`--report` 日报，`--cloud-fix` 云端接管补推 |
| **`set_gh_secrets.py`** | 把 `config.json` 里的参数加密写入 GitHub Actions Secrets（需 PAT） |
| `config.json` | 密钥与轮询参数（**已 gitignore，勿入库**） |
| `start.sh` | 启动脚本：pidfile 单例保护 + 剥离沙箱代理 + `caffeinate` 防睡眠 + **自愈循环** |
| `monitor.pid` | 运行中进程号（运行时生成，用于单例判断） |
| `cloud_sync_state.json` | 上次回写仓库的时间戳（节流用，运行时生成） |
| `MiaoshouMonitor.command` | 双击运行入口（等同 start.sh） |
| `test_connectivity.py` | 一次性自检：妙手 API 连通性 + 推送 |
| `com.miaoshou.monitor.plist.example` | launchd 守护配置模板 |
| `orders_state.json` | 状态机数据（`notified` / `pending` / 当日发送计数，勿手改） |
| `legacy_v1_miaoshou_monitor.py` | v1 旧版，仅供参考，勿运行 |

仓库根目录的 `state/` 放去重状态（**必须入库**）：`local_state.json`（本机回写）、
`cloud_state.json`（Actions 提交）。

常用命令：

```bash
cd ~/WorkBuddy/2026-09-10-18-17-36/miaoshou-monitor/local
tail -f monitor.log                       # 实时日志
/Users/jianguo/.workbuddy/binaries/python/envs/default/bin/python reconcile.py --hours 24 --quiet
                                          # 只查漏单不推送
/Users/jianguo/.workbuddy/binaries/python/envs/default/bin/python reconcile.py --hours 24 --fix
                                          # 补推漏单
/Users/jianguo/.workbuddy/binaries/python/envs/default/bin/python reconcile.py --cloud-fix --min-age-minutes 100000
                                          # 演练云端接管逻辑（不推送）
```

### 手动启动

```bash
cd /Users/jianguo/WorkBuddy/2026-09-10-18-17-36/miaoshou-monitor/local
./start.sh                 # 前台常驻（Ctrl+C 停止）
./MiaoshouMonitor.command  # 双击运行
```

### 装成开机自启（launchd）

```bash
cp local/com.miaoshou.monitor.plist.example ~/Library/LaunchAgents/com.miaoshou.monitor.plist
launchctl unload ~/Library/LaunchAgents/com.miaoshou.monitor.plist 2>/dev/null
launchctl load  ~/Library/LaunchAgents/com.miaoshou.monitor.plist
launchctl list | grep miaoshou     # 有输出即已挂载
```

停止：`launchctl unload ~/Library/LaunchAgents/com.miaoshou.monitor.plist`

### 自检

```bash
cd ~/WorkBuddy/2026-09-10-18-17-36/miaoshou-monitor/local
# 推送通道是否真的能送达（推荐，改动密钥后必跑）
/Users/jianguo/.workbuddy/binaries/python/envs/default/bin/python test_push.py
# 妙手 API 连通性
/Users/jianguo/.workbuddy/binaries/python/envs/default/bin/python test_connectivity.py
```

> ⚠️ 解释器必须用 venv 版。受管 `3.13.12/bin/python3` 缺 `certifi`，
> 所有 HTTPS 请求会静默失败（见踩坑 #17）。

---

## 二、关键实现要点（踩过的坑）

1. **签名**：`HmacSHA256(secret, secret + path + timestamp + appKey + bodyJson + secret)`，
   三个头：`x-app-key` / `x-timestamp` / `x-sign`。body 为空时不参与拼接。
2. **接口**：`POST https://openapi-erp.91miaoshou.com/open/v1/order/package/fetch/search_package_list`
3. **`gmtModifiedFrom` 过滤的是「包裹修改时间」**，不是下单时间。
   旧订单一旦物流/状态同步，也会被捞回来 → 必须用 `orderInfo.gmtOrderStart`（下单时间）
   再做一次 >= 窗口起点判断，否则会疯狂误报旧单。
4. **QPS 限流**：返回 `code = accountApiQpsRateLimit` 时要退避重试，分页之间 sleep 1.5s。
5. **回看窗口**：本地版 6 小时（`query_window_hours`）+ 24h 上限兜底；
   云端接管版 24 小时（`--hours 24`），因为 GitHub cron 高峰期可能延迟。
6. **状态裁剪**：已通知 ID 本地保留最近 1000 条，云端与同步文件同样按时间倒序裁 1000 条，
   防止状态文件无限膨胀（GitHub Contents API 单文件不宜过大）。
7. **推送文案**：标题 `您有一条新的TK 订单`（平台 TikTok→TK / Shopee→SP / Lazada→LZ）。
8. **一单多包裹**：同一轮内用 `seen_this_run` 再兜一层，避免一个订单推多次。
9. **Pending 快照**：pending 队列里存的是订单快照（订单号/金额/商品），
   重试时**不需要重新拉取订单**。这解决了 v1 最深的一个坑——
   失败订单因为掉出查询窗口而永远重试不到。
10. **PushPlus 成功返回 `code == 200`**（不是 0，写错会全部误判为失败）。
    失败请求同样计入每日额度，所以失败也要计数。
11. **频率限制**：PushPlus 免费版 1 分钟 5 条 → `min_interval_seconds = 13`；
    积压 ≥3 单时自动合并成一条推送，1 次请求覆盖多单，绕开额度与频率双重限制。
12. **WxPusher 成功返回 `code == 1000`**；**Server酱 是 `code == 0`**；**企业微信是 `errcode == 0`**；
    **PushPlus 是 `code == 200`**。四个服务的成功码都不一样，写错会把成功当失败、反复重推。
13. **企业微信 60020**：自建应用自 2022 起强制「企业可信IP」。未认证企业还要求先配
    「接收消息服务器URL」（**必须公网 IP**），家用宽带做不到 → 该通道对个人用户不可用（见上文）。
14. **QQ 邮箱 535**：SMTP 授权码错误/服务未开启时，QQ 先返回
    `535 Login fail. Account is abnormal, service is not open, password is incorrect...`，
    随后 AUTH LOGIN 回退时**直接断连**，smtplib 最终抛的是 `SMTPServerDisconnected: Connection unexpectedly closed`
    —— 别被"连接被关闭"误导成网络问题，开 `set_debuglevel(1)` 才能看到真正的 535。
    正确姿势：QQ邮箱 → 设置 → 账户 → 开启「**IMAP/SMTP服务**」→ 生成授权码（16 位），
    服务端 host 用 `smtp.qq.com`，SSL 465。
15. **单例检查别用 `pgrep -f "python.* monitor.py"`**：调用方自己的命令行里若含这段文本，
    pgrep 会匹配到调用者本身，导致误判"已在运行"而拒绝启动。改用 pidfile + `kill -0`。
16. **后台常驻进程会被会话回收**：`nohup ... & disown` 在 AI 会话里仍可能随会话结束被杀
    （现象：日志停更、无进程）。可靠做法：`subprocess.Popen(..., start_new_session=True)`
    脱离会话启动，配合 start.sh 的自愈循环。
17. **⚠️ 最隐蔽的一个坑：沙箱代理污染（2026-09-10 二次事故根因）**
    AI 会话（沙箱）会给子进程注入 `HTTP_PROXY/HTTPS_PROXY=http://127.0.0.1:<随机端口>`。
    从会话里启动的常驻进程会**继承这个代理**；等会话结束、端口关闭后，
    进程就开始报
    `ProxyError('Unable to connect to proxy', ... Connection refused)`，
    **所有 HTTPS 请求全部失败**，但进程还在跑、日志还在滚、`pgrep` 也看得见
    —— 又是典型的"静默失效"，症状和 certifi 那次一模一样。
    两次事故（certifi 缺证书、代理失效）的教训是同一条：**进程活着 ≠ 请求发得出去**，
    排查时一定要看日志里有没有"本轮获取 N 条订单"这类**成功证据**，而不是只看进程在不在。
    修法：`start.sh` 启动时剥离指向 `127.0.0.1/localhost/::1` 的代理变量（只清回环的，
    不动用户真实代理），并 `export NO_PROXY="*"`。日志会打印 `[start.sh] 已剥离回环代理 ...`。
18. **手动重启时的双实例陷阱**：`pkill caffeinate` 不会杀掉子 bash 循环，
    循环会在 10 秒内抢先拉起新的 `monitor.py`，此时若再启动一份 start.sh 就会**两份并存 → 重复推送**。
    `start.sh` 已加"孤儿清理"：启动时会把 pidfile 之外的 `monitor.py` 一并清掉。
    手动重启推荐直接 `kill $(cat monitor.pid)`，让自愈循环自己拉起来。
19. **解释器必须是 venv 版**（2026-09-10 踩坑）：
   `/Users/jianguo/.workbuddy/binaries/python/versions/3.13.12/bin/python3` 里 **没有 certifi**，
   一旦调用 requests 就报
   `OSError: Could not find a suitable TLS CA certificate bundle, invalid path: .../certifi/cacert.pem`，
   API 和微信推送会**全部静默失败**（日志只显示"轮询出错"，看上去像在跑其实一个请求都没发出去）。
   可用解释器：`/Users/jianguo/.workbuddy/binaries/python/envs/default/bin/python`（requests 2.34.2 + certifi 齐全）。
   排查命令：`grep -c "TLS CA certificate" monitor.log`。

20. **`reconcile.py` 里 `TZ_SHANGHAI` 未定义（老 bug，2026-09-10 修）**：模块顶部只定义了
    `TZ`，但正文用了 `TZ_SHANGHAI` → `--report` 一跑就 `NameError`。
    **也就是说云端日报从上线起就没成功过**。已在顶部加 `TZ_SHANGHAI = TZ` 别名兜住。
21. **云端接管必须复刻「旧单护栏」**：妙手按「包裹修改时间」过滤，物流同步会把几个月前的
    订单捞回来。本地有 `new_order_max_age_hours=24` 保护，云端 `--cloud-fix` 若不加同样的闸，
    上线第一时间就会拿 8 月的旧单轰炸。日报同理（曾把 4 条旧单算进"今日共 6 单"）。
22. **配置文件里的占位符会被当真实值用**：`config.json` 的 `cloud_sync.token` 若留着
    `PLEASE_FILL_IN_...`，每轮都会拿它去请求并刷 401。`CloudSync._clean_token()`
    统一把占位符/过短的值当空处理，并降级为只读。
23. **本地 git 工作副本会被"别人"改**（2026-09-10 踩坑）：开了云端接管之后，
    本机 monitor 通过 GitHub API 回写 `state/local_state.json`，Actions 也会提交
    `state/cloud_state.json` —— 这两者都在你本地 clone 的仓库里。
    于是本地手动 `git push` 会**被拒**（`rejected ... fetch first`）。
    解法：`git pull --rebase --autostash origin main` 再 push。
    另外**不要**用 `git add -A` 顺手提交 `state/local_state.json` ——
    工作副本里的那份可能比仓库里的旧，提交上去等于回退云端看到的状态。
24. **Actions 计费按 job 向上取整到整分钟**：监控一轮实际只跑 11 秒，但仍按 1 分钟计价。
    `*/10` 的 cron = 144 分钟/天 ≈ 4300 分钟/月，**超出私有仓库免费额度 2000**。
    Public 仓库不受此限（标准 runner 免费无限），这是当前保持 public 的主要理由。
25. **GitHub 的 cron 是尽力而为，不是定时任务**（2026-09-10 实测）：
    官方文档明确「高负载时排队任务可能被直接丢弃」。本仓库 `*/5` 配置实际只有
    **6~9 次/天**，间隔 2~5 小时。任何"每 N 分钟必达"的设计都不能建在它上面。
    详见上文「先认清现实」一节。
26. **出单地区字段**：妙手在**顶层**给了 `site`（`MY`/`PH`）和 `siteName`（`马来`/`菲律宾`），
    顶层最全；`orderInfo` 里只有 `site`，没有 `siteName`。取法：
    `order.get("site") or oi.get("site")`、`order.get("siteName") or oi.get("siteName")`。
    不要用 `consigneeInfo.countryName`（那是**收货地**，不是出单站点）。
27. **给快照加字段必须用 `.get()` 兜底**：`orders_state.json` 的 `pending` 队列里
    可能还躺着旧版本入队的快照，键不全。`region_tag()` 全部走 `.get()`，
    就是为了让老单重试时不炸 `KeyError`。

---

## 三、当前运行状态（2026-09-10 23:05 更新）

**一句话**：本地实时推、云端兜底接管，两条链路共享去重名单，全链路已实测打通。

### 已实测通过（都有证据，不是"看着没报错"）

| 验证项 | 结论 | 证据 |
|---|---|---|
| 本地 → 邮件 | ✅ 送达 | 日志 `[推送] ✅ email 成功` |
| 本地推送通道 | ✅ email / serverchan 双通道可用 | `test_push.py` 自检 |
| 本地 → 仓库回写状态 | ✅ 生效 | 仓库出现提交 `chore(state): 本机已推 12 单` |
| 仓库 → 云端去重 | ✅ 生效 | Actions 日志 `已知已推 12 单（本地 12 + 云端 0）` |
| 云端模式判定 | ✅ 正确进入接管 | `[run.py] 检测到 local_state.json，进入「接管补推」模式` |
| 云端零误推 | ✅ 无重复 | `[云端] ✅ 无需要补推的订单` |
| **云端能真发邮件** | ✅ **送达** | 自检运行：`✅ 自检通过：经由通道「email」送达` |
| 11 个 Actions Secrets | ✅ 全部写入 | `set_gh_secrets.py` → 成功 11 / 失败 0 |

### 关键状态

- ✅ **生效实例**：`local/monitor.py`（v2）常驻运行，pidfile 单例 + 孤儿清理 + 自愈循环
  （kill -9 后 10 秒自动重启，实测有效）
- ✅ **本地通道顺序（用户指定）**：`email → serverchan`
- ✅ **云端接管已启用**：`cloud_sync.token` 已填，`run.py` 走 `--cloud-fix`
- ✅ **邮件通道**：QQ 邮箱 + SMTP 授权码（值见 `config.json`），登录返回 235
- ✅ **电脑语音提醒（2026-09-11 加）**：`desktop_alert.enabled=true`，语音 `Tingting`；
  日志启动行有 `电脑语音提醒: ✅ 开（语音 Tingting，速率 190，含通知横幅）`，
  推送成功的同一秒念一句 + 弹横幅（多单合并成一句）。自检：`python local/test_push.py --voice`
- ❌ `push.wecom` 虽已填 CorpID / Secret / AgentId，但**被 60020 可信IP 拦截**
  （未认证企业需公网 IP 服务器才能配），该通道作废，代码保留
- ⏸ **旧实例** `/Users/jianguo/.workbuddy/miaoshou-monitor` 已停止
  （2026-09-10 02:20 起因 certifi 缺失挂了 16 小时，963 次报错）
- ⚠️ **launchd 开机自启待补**：当前是脱离会话拉起的常驻进程（含自愈），
  能撑到关机/注销为止。在**访达里双击 `local/MiaoshouMonitor.command`**，
  或到**终端**（不是 AI 会话）执行：

  ```bash
  cp ~/WorkBuddy/2026-09-10-18-17-36/miaoshou-monitor/local/com.miaoshou.monitor.plist.example \
     ~/Library/LaunchAgents/com.miaoshou.monitor.plist
  launchctl bootout gui/$(id -u)/com.miaoshou.monitor 2>/dev/null
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.miaoshou.monitor.plist
  launchctl list | grep miaoshou      # 有输出即成功
  ```

  AI 会话里的 shell 拿不到 Aqua session，`launchctl bootstrap` 会报
  `Bootstrap failed: 5: Input/output error`，必须在终端或双击 `.command` 执行。
- **不要同时起两份本地实例**：`start.sh` 靠 `monitor.pid` 做单例判断（只认自己这一份），
  若在别的目录再起一份，pidfile 互不可见，两份并存会导致同一订单推两次。

### 常用命令

```bash
cd ~/WorkBuddy/2026-09-10-18-17-36/miaoshou-monitor/local
tail -f monitor.log                       # 实时日志
cat monitor.pid                           # 当前进程号
kill $(cat monitor.pid)                   # 重启（让自愈循环自己拉起来；别 pkill start.sh）
grep -c "TLS CA certificate" monitor.log  # 健康检查：应为 0
grep "同步" monitor.log | tail            # 看状态回写是否正常
```

## ⚠️ 安全提醒：密钥必须轮换

README 里的明文密钥**已从代码中删除**，但仍存在于 **git 历史**（`97a0925`），
而仓库是 **public** —— 等同于已经公开过。删文件只是止血，**必须去平台重置**：

| 平台 | 操作 | 重置后 |
|---|---|---|
| 妙手开放平台 | 重置 **AppSecret** | 改 `local/config.json` → 重跑 `set_gh_secrets.py` |
| Server酱 `sct.ftqq.com` | 重置 **SendKey** | 同上 |
| QQ 邮箱授权码 | 从未进过仓库 | 无需轮换 |

**另一个可选决策**：要不要把仓库转私有？

GitHub 按「**每个 job 向上取整到整分钟**」计费（官方原文：*rounds the minutes and partial
minutes each job uses up to the nearest whole minute*），所以一轮跑 11 秒也算 1 分钟：

| cron | 轮数/天 | 计费分钟/月 | 私有免费额度 2000 |
|---|---|---|---|
| `*/10`（当前） | 144 | ≈ 4300 | ❌ 超一倍 |
| `*/15` | 96 | ≈ 2900 | ❌ 仍超 |
| `*/30` | 48 | ≈ 1440 | ✅ 够 |

**当前保持 public**：Actions 免费无限，且代码里已无任何明文凭证（值全在 Secrets）。
若转 Private，记得同步把 cron 改成 `*/30 * * * *`。
