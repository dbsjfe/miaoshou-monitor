# 妙手ERP → 订单通知（邮件 + 微信）

两条链路互为兜底，**共用一份去重状态**，不会重复推送：

| 链路 | 位置 | 触发 | 作用 |
|------|------|------|------|
| **本机常驻版** | `local/` | 每 180 秒轮询 | 实时逐单通知（主通道） |
| **云端 Actions 版** | 仓库根目录 | 每 10 分钟 | **Mac 关机时接管**逐单通知 |

去重靠仓库里的两个状态文件（双方都读写）：

```
state/local_state.json   本机推完订单后通过 GitHub API 回写
state/cloud_state.json   云端推完订单后由 workflow 提交回仓库
```

加上「下单满 15 分钟云端才动手」的时间余量 → 本机在线时云端永不插话，
本机离线时云端接管，两边都不会重复。

推送通道按顺序自动降级：**邮件 → Server酱**。

## 📦 一键安装包（要分享给别人用）

把整套东西打包成**一个自解压文件**，对方不需要懂命令：

```bash
python3 tools/build_installer.py        # 生成 dist/妙手ERP-TK订单通知-安装包.py
```

把生成的这个 `.py` 文件发给对方（连同 `docs/安装前准备清单.md`），他只要：

```bash
python3 妙手ERP-TK订单通知-安装包.py --check      # 检查环境
python3 妙手ERP-TK订单通知-安装包.py --install    # 释放到 ~/miaoshou-monitor
python3 妙手ERP-TK订单通知-安装包.py --guide      # 完整安装说明
```

或者直接把文件丢给 AI 助手说「读取这个文件，按里面的说明在本机安装」——
安装包顶部 docstring 里写好了《给 AI 助手的执行指令》，AI 会照着做完。

安装包内含全部源码 + 脱敏的 `config.example.json` + 按目标目录生成的 launchd plist，
**不含任何密钥**；同名文件会先备份成 `*.bak-<时间戳>`，不会静默覆盖。
配好通道后 `test_push.py` 会一条条验证，避免"装完却一条通知都收不到"。

## 推送长什么样

标题（出单地区在最前，一眼看清是哪个站点）：

```
【菲律宾 PH】您有一条新的TK 订单
```

正文：

```
★ 出单地区: 菲律宾 PH
平台: TikTok | 店铺: SoftTots PH
订单号: 585996945371334366
状态: 待发货 | 金额: 394.46 PHP
收货地: 菲律宾 / Metro Manila / Manila

商品:
  - 3D-Printed Dragon Flame Book Nook Night Light — Fantasy LED Decor ×1  394.46 PHP

⏰ 下单 2026-09-10 15:43:09
```

积压 ≥3 单时会合并成一条（一次推送覆盖多单，省额度）：

```
共 3 单，合计 1183.38 PHP

1. 【菲律宾 PH】585996945371334366 | 394.46 PHP | 菲律宾
2. 【菲律宾 PH】585989885238478095 | 394.46 PHP | 菲律宾
3. 【菲律宾 PH】585970089818687040 | 394.46 PHP | 菲律宾
```

预览真实文案（不写状态、不影响去重）：

```bash
python local/test_push.py --real          # 最近 1 单
python local/test_push.py --real --count 3  # 走合并推送格式
```

> 私有说明（含通道选型踩坑、沙箱代理污染、单例陷阱等）见 [LOCAL.md](./LOCAL.md)

## 🔊 电脑语音提醒（只在本机 Mac 上生效）

邮件和 Server酱 都是**远程**提醒 —— 人在电脑前、手机不在手边时，来单可能要十几分钟后才看到。
本机语音提醒会在**推送成功的同一秒**念一句，并弹一条通知中心横幅，眼耳双保险。

```
🔊 您有一条新的菲律宾订单，请及时处理          ← 单条
🔊 您有 3 条新订单，出单地区马来、菲律宾，请及时处理   ← 多单合并成一句，避免重叠听不清
```

只用 macOS 自带命令（`say` / `osascript` / `afplay`），**零第三方依赖**。配置在
`local/config.json` 的 `desktop_alert` 段：

```json
"desktop_alert": {
  "enabled": true,
  "voice": "Tingting",        // 中文女声，系统自带；留空用系统默认
  "rate": 190,                // 语速
  "sound": "Glass",           // 通知横幅提示音，留空则静音
  "notify_center": true,      // 是否弹通知中心横幅
  "max_seconds": 15,
  "template_one": "您有一条新的{region}订单，请及时处理",
  "template_many": "您有{count}条新订单，出单地区{regions}，请及时处理"
}
```

自检（不出网、不推送，只念一句 + 弹横幅）：

```bash
python local/test_push.py --voice
python local/test_push.py --voice --real   # 用最近一条真实订单的站点播报
```

**只在推送成功后才播报** —— 推送失败还念一句只会白高兴一场。云端 Actions 没有音频设备，
`run.py` 一律不启用语音，只有本机 `monitor.py` 会用。

排障：

| 现象 | 原因 |
|---|---|
| 只有横幅没声音 | 系统设置 → 声音 静音了，或音量 0 |
| 只有声音没横幅 | 系统设置 → 通知 → 允许「终端 / 脚本编辑器」发通知 |
| 完全没有反应 | 进程跑在 **LaunchDaemon(root)** 下，不在 GUI 会话里；改用 LaunchAgent 或 `start.sh` 从终端启动 |
| 声音念的是英文/怪音 | `voice` 名字不存在，日志会有 `[语音] 声音「xxx」不存在，改用系统默认音色` |

## ⚠️ 云端 cron 是"尽力而为"，别指望它准时

GitHub 官方文档写明：`schedule` 事件在高负载时可延迟，**排队任务甚至可能被直接丢弃**。
实测本仓库（2026-09-10）：

| 配置 | 理论 | 实际 |
|---|---|---|
| `*/5` | 288 次/天 | 约 250 次/天，后期掉到 **6~9 次/天**，间隔 2~5 小时 |

所以正确的心智模型是：

- **准时靠本机 monitor**（每 180 秒轮询）—— 本机开着就一定是实时的
- **云端只当宽松兜底** —— Mac 关机时，通知可能延迟几十分钟到几小时，但**不会丢**
- cron 刻意避开 `:00` / `:30`（全 GitHub 的拥堵高峰），用 `7,17,27,37,47,57`

## 部署步骤

### 1. 创建 GitHub 仓库

**建议 Private 私有仓库**——代码与文档任何人都能看，一旦把密钥写进 README / 代码就等于公开泄露。

⚠️ **但私有仓库有 Actions 计费限制**：GitHub 按「每个 job 向上取整到整分钟」计费
（官方原文：*GitHub rounds the minutes and partial minutes each job uses up to the nearest whole minute*），
所以一轮哪怕只跑 11 秒也算 1 分钟：

| cron 频率 | 每天轮数 | 折合计费分钟/月 | 私有仓库免费额度 2000 够吗 |
|---|---|---|---|
| `*/10`（当前） | 144 | ≈ 4300 | ❌ 超一倍 |
| `*/15` | 96 | ≈ 2900 | ❌ 仍超 |
| `*/30` | 48 | ≈ 1440 | ✅ 够 |

**Public 仓库的 Actions 完全免费无限**（标准 runner，不受分钟数限制）。

本项目**当前用 public**：代码里已经没有任何明文凭证，值全部走 Secrets。
如果你更在意隐私，把仓库转 Private 后请同步把 cron 改成 `*/30 * * * *`。

### 2. 设置 Secrets

仓库 → Settings → Secrets and variables → Actions → New repository secret。

**不要把密钥写进任何提交的文件里**，只放 Secrets：

| Name | 说明 |
|------|------|
| `MIAOSHOU_APP_KEY` | 妙手 ERP 开放平台 AppKey |
| `MIAOSHOU_APP_SECRET` | 妙手 ERP 开放平台 AppSecret |
| `SMTP_HOST` | 如 `smtp.qq.com` |
| `SMTP_PORT` | 如 `465`（SSL） |
| `SMTP_USER` | 发件邮箱 |
| `SMTP_PASSWORD` | 邮箱 SMTP **授权码**（不是登录密码） |
| `SMTP_TO` | 收件邮箱 |
| `SERVERCHAN_SEND_KEY` | Server酱 SendKey（可选，兜底通道） |

批量写入可用 `local/set_gh_secrets.py`（读取本地 `config.json`，加密后写入）：

```bash
GH_TOKEN=<你的token> python local/set_gh_secrets.py dbsjfe/miaoshou-monitor
```

### 3. 推送代码

```bash
git init
git add .
git commit -m "init: 妙手ERP订单通知"
git branch -M main
git remote add origin https://github.com/你的用户名/仓库名.git
git push -u origin main
```

`config.json`、`orders_state.json`、`*.log` 已在 `.gitignore` 中，不会被提交。

### 4. 验证

仓库 → Actions → 选 `妙手ERP订单通知（云端接管）` → **Run workflow**。

**推荐先勾「只跑链路自检」**——它会发一条测试通知就结束（不查订单、不写状态），
用来确认 Secrets 配对了、消息真的送得出去。推送失败是静默的，值得先验一次。

不勾则跑正式流程，日志开头会打印当前模式：

- `进入「接管补推」模式` —— 本机已成功回写过状态，云端会补推漏单
- `降级为「日报」模式` —— 本机还没回写（token 未配），云端只发一条当日汇总，**不会重复推**

## 费用

按 GitHub 官方计费规则（每个 job 向上取整到整分钟）：

- **Public 仓库**：标准 runner 免费无限 —— 本项目当前的选择
- **Private 仓库**：免费额度 2000 分钟/月。`*/10` 频率约 4300 分钟/月会超，
  需把 cron 降到 `*/30`（≈1440 分钟/月）

## 本地测试

```bash
export MIAOSHOU_APP_KEY="你的AppKey"
export MIAOSHOU_APP_SECRET="你的AppSecret"
export SMTP_USER="你的邮箱"
export SMTP_PASSWORD="你的SMTP授权码"
export SMTP_TO="收件邮箱"
pip install requests
python run.py
```

手动跑一次对账（只打印不推送）：

```bash
python local/reconcile.py --cloud-fix --hours 24 --min-age-minutes 100000
```

## ⚠️ 安全须知

密钥一旦被提交进 Git，**即使后续删除也仍留在历史记录里**。如果曾经误提交：

1. 先去平台**重置密钥**（妙手后台重置 AppSecret、Server酱重置 SendKey）——这一步最关键
2. 再把仓库转为 Private / 清理文件中的明文
3. 之后一律只通过 Secrets 注入
