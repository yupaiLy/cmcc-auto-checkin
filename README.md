# 中国移动 App 自动签到（签到领流量/话费）

基于抓包逆向的中国移动 App「网签领流量」H5 活动自动签到脚本。
单文件 Python，仅依赖 `requests`。

## 文件说明

| 文件 | 说明 |
|------|------|
| `cmcc_sign.py` | 主脚本 |
| `cmcc_extra.py` | 可选拓展模块：代币任务 / AI豆任务 / 抽奖消耗（`--tasks` / `--games`） |
| `config.example.json` | 配置模板，复制为 `config.json` 后填写 |
| `.cmcc_jwt_cache_<尾号>.json` | 运行后按账号自动生成的 jwt 凭证缓存（勿外传） |
| `images/app-token-capture.png` | app_token 抓包位置示例图 |

## 快速开始

```bash
pip3 install requests
cp config.example.json config.json   # 填入自己的 app_token 和手机号
python3 cmcc_sign.py                 # 签到
python3 cmcc_sign.py --dry-run       # 只查状态
python3 cmcc_sign.py --claim         # 签到后顺带尝试领连签奖励
python3 cmcc_sign.py --delay 600     # 随机延迟 0~600 秒执行（防风控）
python3 cmcc_sign.py --tasks         # 签到 + 顺带领签到页 AI豆任务
python3 cmcc_sign.py --games         # 三拓展活动：打卡 + 代币任务 + 到窗口自动抽奖
python3 cmcc_sign.py --games --dry-run   # 只报各活动状态与余额，零消耗
```

`--tasks` / `--games` 依赖同目录的 `cmcc_extra.py`（缺失时自动跳过并提示，不影响主签到）。

配置也可用环境变量覆盖（适合 CI）：`CMCC_APP_TOKEN`、`CMCC_PHONE`、
`CMCC_PROVINCE_CODE`、`CMCC_CITY_CODE`、`CMCC_ACTIVITY_ID` 等。

## 如何获取 app_token（凭证）

1. 手机装抓包工具（Proxyman / Charles / Stream 等），对 App 开启 SSL 抓包；
2. 打开中国移动 App → 我的 → 签到领流量 进入签到页；
3. 在抓包记录中找到 `wx.10086.cn/qwhdsso/appTokenLogin` 这条 POST 请求；
4. 复制请求体里 `token` 字段的完整值（形如
   `JSESSIONID=xxxx; UID=xxxx; Comment=...; ticketID=NingBo`）填入配置。
   尾部的 `Secure`/`Path` 等 Cookie 属性标记可留可删，服务端只解析 `JSESSIONID`/`UID` 名值对。

![appTokenLogin 抓包示例](images/app-token-capture.png)

省编码 `provinceCode`、市编码 `cityCode` 也在同一条请求体里，一并照抄。
`app_token` 属于账号登录凭证，**只在本地使用，不要提交到公开仓库**。

### jwt 续期（抓一次包即可长期使用）

首次运行会用 `app_token` 引导登录，服务端同时签发一个账号级 jwt 并缓存到
`.cmcc_jwt_cache_<尾号>.json`。之后每次运行脚本**优先用 jwt 续期**
（实测 jwt 不受 App 内切换登录影响，app_token 失效后依然可用），
`app_token` 仅在 jwt 缺失或失效时作兜底引导。

因此日常无需反复抓包；只有当脚本同时报「jwt 续期失败」和
「appTokenLogin 失败」并推送通知时，才需要重新抓包更新 `app_token`。
缓存按手机尾号隔离并校验归属，多账号/换号配置不会串用凭证。

## 定时执行

### macOS launchd / crontab

```bash
crontab -e
# 每天早上 8 点 23 分执行（避开整点）
23 8 * * * cd /path/to/cmcc-auto-checkin && /usr/bin/python3 cmcc_sign.py --delay 1800 >> sign.log 2>&1
```

### GitHub Actions

`.github/workflows/sign.yml`（注意：在 App 内切换账号登录会使 `app_token` 失效，
使用云上定时方案时请留意凭证状态）：

```yaml
name: cmcc-sign
on:
  schedule:
    - cron: "37 0 * * *"   # UTC 时间，对应北京时间 8:37（分钟避开整点）
  workflow_dispatch:
jobs:
  sign:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install requests
      - run: python cmcc_sign.py --delay 3600
        env:
          CMCC_APP_TOKEN: ${{ secrets.CMCC_APP_TOKEN }}
          CMCC_PHONE: ${{ secrets.CMCC_PHONE }}
          CMCC_BARK_URL: ${{ secrets.CMCC_BARK_URL }}  # 可选
```

## 通知（可选）

- **Server酱**：填 `serverchan_sendkey`，签到结果推送到微信；
- **Bark**（iOS）：填 `bark_url`（形如 `https://api.day.app/你的key`）。

## 拓展活动（可选，`cmcc_extra.py`）

同一 SSO 通道（wx.10086.cn / qwhdhub）下还有三类收益，凭证与会话完全复用主脚本
（jwt 续期缓存同样生效），通过 `--tasks` / `--games` 开启：

| 体系 | 内容 | 开关 |
|------|------|------|
| mark/task | 签到页 AI豆任务（到访 + finish + 领奖，57 项中约 1/3 可纯 HTTP 完成） | `--tasks` |
| diyTask | 三活动页代币任务（browse/share 等类型服务端不校验真实行为） | `--games` |
| diyLottery | 抽奖消耗：周六游戏中心（游戏币 10/次，周六 12:00 起奖池拓展）、追剧领福利（次数 1/抽，仅周日开放） | `--games` |

抽奖内置**窗口闸门**：非窗口日只报余额不消耗（游戏币攒到周六拓展池一次抽光，
币按 31 天周期清零），到窗口才真扣；幂等由服务端余额保证，重复运行不多扣资产。
`--dry-run` 下全部只读，零消耗。

活动页 URL / 组件 ID 写在 `cmcc_extra.py` 的 `ACTS` 注册表里，属公开配置；
不同省份入口不同时替换 query 参数即可。

## 实现说明（接口链路）

```
GET  /qwhdsso/login?actUrl=<活动页>          → 提取一次性 sid
POST /qwhdsso/appTokenLogin?sid=...          → {token:App票据,...} 换取跳转 URL
GET  <活动页?token=QWHDSSOD...>              → Set-Cookie: QWHD_SESSION_TOKEN(30分钟)
POST /qwhdhub/api/mark/mark31/markstatus {}  → 查签到状态（幂等）
POST /qwhdhub/api/mark/mark31/domark         → {"date":"YYYYMMDD"} 执行签到
POST /qwhdhub/api/mark/mark31/taskAward/<id> → 领连签奖励（--claim）
```

拓展活动（`cmcc_extra.py`）：

```
POST /qwhdhub/api/mark/task/taskList         → AI豆任务清单（--tasks）
POST /qwhdhub/api/mark/task/finishTask       → 完成（服务端最小校验=到访+Referer，前端 sign 不校验）
GET  /qwhdhub/diyTask/list/<componentId>     → 代币任务清单
POST /qwhdhub/diyTask/finish/<taskId>        → 空 body 即发币
POST /qwhdhub/diyLottery/period/remain/<id>  → 抽奖余额预检（只读）
GET  /qwhdhub/diyLottery/lotterySafely/<id>  → 抽奖一次（无 body，Referer=活动页）
```

已知坑（脚本内已处理）：

- **TLS 套件**：wx.10086.cn 网关只接受老式 TLS 套件（ECDHE-RSA-AES128-SHA），
  Python 默认现代套件会握手失败，脚本挂载了自定义 SSL 适配器；
- **系统代理**：本机开着抓包/代理工具时证书会被 MITM，脚本已禁用代理继承直连；
- **请求头**：UA 需含 `leadeon`，API 需带 `login-check: 1` 与 `x-requested-with`；
- `domark` 返回 `code=SUCCESS` + `status=PRIZE_NO_CONFIG` 表示签到成功、当日无单日奖品；
- 重复签到服务端返回 `HAVE_MARKED`，脚本视为幂等成功。

仅供个人号码自动化签到使用，请勿高频调用或用于批量账号。
