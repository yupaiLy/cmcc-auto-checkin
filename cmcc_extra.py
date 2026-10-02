#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中国移动 qwhdhub 拓展活动模块（cmcc_sign.py 的可选扩展）
=========================================================

在 mark31 主签到之外，覆盖同一 SSO 通道下的另外三类收益：

  A. 代币任务（diyTask 体系）
     GET  /qwhdhub/diyTask/list/<componentId>     任务清单（taskStage=UNDO/DONE）
     POST /qwhdhub/diyTask/finish/<taskId>        空 body 即发币
     browse / inapp_cdt / share 等类型服务端均不校验真实行为，一个端点通吃。

  B. 签到页 AI豆任务（mark/task 体系，与 diyTask 是两套独立端点）
     POST /qwhdhub/api/mark/task/taskList         任务清单（status=0 为待办）
     POST /qwhdhub/api/mark/task/finishTask       {taskId, taskType}
     POST /qwhdhub/api/mark/task/getTaskAward     领取
     服务端最小校验面 = 目标页 PV 到访 + Referer 头为该任务 jumpUrl + 停留 scanTime；
     前端 JS 计算的 sign/random 服务端并不校验，可省略。
     跳转类任务（响应提示"特殊处理"）走 hlwyxhdhub 握手，见 open_finish_task()。

  C. 抽奖消耗（diyLottery 体系）
     POST /qwhdhub/diyLottery/period/remain/<componentId>   余额与剩余次数（只读预检）
     GET  /qwhdhub/diyLottery/lotterySafely/<componentId>   执行一次抽奖，无 body

会话与凭证完全复用 cmcc_sign.exchange_session()（jwt 自续期缓存同样生效），
本模块只负责业务接口，不落任何凭证。

用法（由 cmcc_sign.py 转发）：
  python3 cmcc_sign.py --tasks     # 主签到 + 领 AI豆任务
  python3 cmcc_sign.py --games     # 三活动签到 + 代币任务 + 到窗口自动抽奖
  python3 cmcc_sign.py --games --dry-run   # 只报状态与余额，不做任何写操作
"""

import logging
import random
import re
import time
from datetime import datetime

import requests

from cmcc_sign import (
    BASE,
    api_headers,
    assert_safe_url,
    exchange_session,
)

log = logging.getLogger("cmcc_sign")

# 活动页 URL 为公开配置（非个人凭证）；若你的省份/渠道入口不同，替换 query 参数即可
ACTS = {
    "saturday": {
        "name": "周六游戏中心",
        "entry": f"{BASE}/qwhdhub/diy-client/1126086939?A_C_CODE=pXMkQhEmZ7&channelId=P00000119581",
        "sign": ("POST", "/qwhdhub/api/diySaturdayGame/clockIn", {},
                 "POST", "/qwhdhub/api/diySaturdayGame/getClockInStatus"),
        "signed": lambda d: bool(d.get("signed")) or "已签到" in (d.get("message") or ""),
        "task_component": "50HQAg0-9YaSW4St7MDT",
        # 游戏币 10 币/次，全时段可抽；周六 12:00 起奖池拓展（非周六只攒不抽），
        # 余额按 31 天周期清零，务必在清零前抽光
        "lottery": {"component": "50HQAg0-9YaSW4St7MDT", "wallet_key": "remainGameVal",
                    "cost": 10, "count_key": "remain", "weekday": 6, "start_hour": 12,
                    "label": "游戏币"},
    },
    "video": {
        "name": "追剧领福利",
        "entry": f"{BASE}/qwhdhub/diy-client/1126082530?A_C_CODE=10hep1ZhkL&channelId=P00000119581",
        "sign": ("GET", "/qwhdhub/api/diyVideoDayRedesign/sign/doSign", None,
                 "GET", "/qwhdhub/api/diyVideoDayRedesign/sign/querySignStatus"),
        "signed": lambda d: d.get("todaySignFlag") == "1",
        "task_component": "4EC96l0-9btuobajC9Yu",
        # 抽奖次数 1 次/抽；服务端 consumeRestrictionType=1，仅周日开放
        "lottery": {"component": "4EC96l0-9btuobajC9Yu", "wallet_key": "remain",
                    "cost": 1, "count_key": None, "weekday": 7,
                    "label": "次数"},
    },
    "game666": {
        "name": "玩游戏抽话费（周任务）",
        "entry": f"{BASE}/qwhdhub/diy-client/1126090514?A_C_CODE=aEvxYYAV3C&channelId=P00000119581",
        "sign": None,  # 该页无每日打卡，只有每周一刷新的任务
        "task_component": "7M6vIq0-TFMj5T-ZGjks",
    },
}

# 跳转任务握手页（hlwyxhdhub 域，会话令牌名为 HLWHD_SESSION_TOKEN）
OPEN_TASK_ENTRY = f"{BASE}/hlwyxhdhub/act-wedrecharge/index.html?pageId=1849008675699650560"


class ActConfig:
    """复用主脚本 Config 的凭证字段，仅把 act_url 换成指定活动页。

    exchange_session 按 Referer 路由签发会话，不同活动页需要各自换一次票；
    jwt 是账号级凭证，跨活动/跨 hub 通用，因此换票不会消耗新的 app_token。
    """

    def __init__(self, base_cfg, entry: str):
        self.__dict__.update(vars(base_cfg))
        self._entry = entry

    @property
    def act_url(self) -> str:
        return self._entry


def api_call(s: requests.Session, referer: str, method: str, path: str, body=None) -> dict:
    """统一 API 调用；网关偶发返回 HTML（限流/会话过期），按失败结构返回而非抛异常。"""
    headers = api_headers(referer)
    try:
        if method == "GET":
            r = s.get(BASE + path, headers=headers, timeout=30)
        else:
            r = s.post(BASE + path, json=body or {}, headers=headers, timeout=30)
        return r.json()
    except ValueError:
        return {"code": f"HTTP_{getattr(r, 'status_code', 0)}", "msg": "响应非 JSON"}
    except requests.RequestException as e:
        return {"code": "NETWORK_ERROR", "msg": str(e)}


def run_diy_tasks(s: requests.Session, referer: str, component: str, dry_run: bool) -> str:
    """代币任务：对每个 UNDO 任务 POST finish 即发币。"""
    resp = api_call(s, referer, "GET", f"/qwhdhub/diyTask/list/{component}")
    tasks = resp.get("data") or []
    if not tasks and resp.get("code") != "SUCCESS":
        return f"任务清单获取失败: {resp.get('code')} {resp.get('msg')}"
    todo = [t for t in tasks if t.get("taskStage") == "UNDO"]
    if dry_run or not todo:
        return f"代币任务 {len(tasks)} 个，待办 {len(todo)} 个"

    done = 0
    for t in todo:
        tid = t.get("taskId")
        if not tid:
            continue
        api_call(s, referer, "POST", f"/qwhdhub/diyTask/finish/{tid}", {})
        # 复查清单：完成后任务可能直接移出列表，按"消失"也算成功
        chk = api_call(s, referer, "GET", f"/qwhdhub/diyTask/list/{component}")
        cur = next((x for x in (chk.get("data") or []) if x.get("taskId") == tid), None)
        if cur is None or cur.get("taskStage") == "DONE":
            done += 1
        time.sleep(random.uniform(1, 2.5))
    return f"代币任务 {len(todo)} 个待办，完成 {done} 个"


def run_lottery(s: requests.Session, referer: str, cfg: dict, dry_run: bool) -> str:
    """抽奖消耗：只读预检余额 → 到窗口才真扣。

    幂等由服务端余额保证（余额为 0 时无事可做），重复运行不会多扣资产。
    非窗口日只报余额不消耗：游戏币攒到周六拓展池、追剧次数仅周日开放。
    """
    comp = cfg["component"]
    rem = api_call(s, referer, "POST", f"/qwhdhub/diyLottery/period/remain/{comp}", {})
    if rem.get("code") != "SUCCESS":
        return f"抽奖[{cfg['label']}] 余额查询失败: {rem.get('code')} {rem.get('msg')}"
    d = rem.get("data") or {}
    wallet = d.get(cfg["wallet_key"]) or 0
    limit = d.get(cfg["count_key"]) if cfg.get("count_key") else None
    n = wallet // cfg["cost"]
    if limit is not None:
        n = min(n, int(limit))
    head = f"抽奖[{cfg['label']}] 余额 {wallet}（{cfg['cost']}/次）→ 可抽 {n} 次"
    if dry_run or n <= 0:
        return head

    now = datetime.now()
    if (cfg.get("weekday") and now.isoweekday() != cfg["weekday"]) or \
       (cfg.get("start_hour") and now.hour < cfg["start_hour"]):
        window = f"周{cfg['weekday']}" + (f" {cfg['start_hour']}:00 起" if cfg.get("start_hour") else "")
        return f"{head}；未到窗口（{window}），本次不消耗"

    prizes = []
    for _ in range(n):
        r = api_call(s, referer, "GET", f"/qwhdhub/diyLottery/lotterySafely/{comp}")
        won = (r.get("data") or [{}])[0] if r.get("code") == "SUCCESS" else None
        if not won:
            prizes.append(f"失败({r.get('code')} {r.get('msg')})")
            break
        prizes.append(str(won.get("prizeName") or "?"))
        time.sleep(random.uniform(1.2, 2.8))
    return f"{head}，已抽 {len(prizes)} 次: {' / '.join(prizes)}"


def open_finish_task(cfg: ActConfig, task: dict) -> bool:
    """跳转类任务握手：jumpUrl 自带 taskToken → 换 hlwyxhdhub 会话 →
    getOneTaskInfo 取 cToken → 停留 scanTime → openFinish。
    成功后由调用方回主站 getTaskAward 领奖。"""
    ju = str(task.get("jumpUrl") or "")
    m = re.search(r"taskToken=([^&]+)", ju)
    if not m:
        return False
    try:
        s2, ref2 = exchange_session(ActConfig(cfg, OPEN_TASK_ENTRY))
        info = api_call(s2, ref2, "POST",
                        "/hlwyxhdhub/api/open/_pub/task/getOneTaskInfo",
                        {"jtToken": m.group(1)})
        ct = (info.get("data") or {}).get("cToken")
        scan = int((info.get("data") or {}).get("scanTime") or 0)
        if not ct:
            return False
        time.sleep(scan + 1)
        fin = api_call(s2, ref2, "POST",
                       "/hlwyxhdhub/api/open/_pub/task/openFinish", {"cToken": ct})
        return fin.get("code") == "SUCCESS"
    except Exception as e:
        log.debug("跳转任务握手失败: %s", e)
        return False


def run_mark_tasks(s: requests.Session, referer: str, cfg, dry_run: bool) -> str:
    """签到页 AI豆任务（mark/task 体系）。"""
    tl = api_call(s, referer, "POST", "/qwhdhub/api/mark/task/taskList", {})
    tasks = (tl.get("data") or {}).get("tasks") or []
    todo = [t for t in tasks if t.get("status") == 0]
    if dry_run or not todo:
        return f"AI豆任务 {len(tasks)} 个，待办 {len(todo)} 个"

    got = skipped = 0
    for t in todo:
        tid = str(t.get("taskId") or "")
        if not tid:
            continue
        ju = str(t.get("jumpUrl") or "")
        info = api_call(s, referer, "POST", "/qwhdhub/api/mark/task/taskInfo", {"taskId": tid})
        d = info.get("data") or {}
        task_type = str(d.get("taskType") or "")
        scan = int(d.get("scanTime") or 0)

        # 到访：仅 http(s) 目标页可 GET（小程序/deeplink 无页可访，直接试 finish）
        if ju.startswith("https://"):
            try:
                assert_safe_url(ju)
                s.get(ju, timeout=20)
                if scan:
                    time.sleep(scan + 1)
            except ValueError:
                log.debug("跳过非公网 jumpUrl: %s", ju)
            except requests.RequestException:
                pass

        r = api_call(s, referer, "POST", "/qwhdhub/api/mark/task/finishTask",
                     {"taskId": tid, "taskType": task_type})
        msg = r.get("msg") or ""
        # 服务端校验 Referer=目标页：默认 referer 被拒时带目标页 referer 重试一次
        if r.get("code") != "SUCCESS" and "未达到" in msg and ju.startswith("https://"):
            try:
                assert_safe_url(ju)
                rr = s.post(BASE + "/qwhdhub/api/mark/task/finishTask",
                            json={"taskId": tid, "taskType": task_type},
                            headers=api_headers(ju), timeout=30)
                r = rr.json()
            except (ValueError, requests.RequestException):
                pass
            msg = r.get("msg") or ""

        if r.get("code") != "SUCCESS" and ("特殊处理" in msg or "openFinish" in msg):
            if open_finish_task(cfg, t):
                r = api_call(s, referer, "POST", "/qwhdhub/api/mark/task/finishTask",
                             {"taskId": tid, "taskType": task_type})

        if r.get("code") == "SUCCESS":
            aw = api_call(s, referer, "POST", "/qwhdhub/api/mark/task/getTaskAward",
                          {"taskId": tid})
            n = (aw.get("data") or {}).get("awardNum") if aw.get("code") == "SUCCESS" else "?"
            log.info("[AI豆] %s +%s", t.get("taskName", tid), n)
            got += 1
        else:
            skipped += 1
        time.sleep(random.uniform(0.8, 1.8))
    return f"AI豆任务 {len(todo)} 个待办，完成 {got} 个，跳过 {skipped} 个（第三方 App/真实操作类）"


def run_games(cfg, dry_run: bool) -> list[str]:
    """跑三个拓展活动：换票 → 打卡 → 代币任务 → 抽奖。返回结果行。"""
    lines = []
    for key, act in ACTS.items():
        s, referer = exchange_session(ActConfig(cfg, act["entry"]))
        log.info("[%s] 会话建立成功", act["name"])

        if act["sign"]:
            do_method, do_path, do_body, q_method, q_path = act["sign"]
            st = api_call(s, referer, q_method, q_path, {} if q_method == "POST" else None)
            data = st.get("data") or {}
            if act["signed"](data):
                lines.append(f"{act['name']}: 今日已打卡")
            elif dry_run:
                lines.append(f"{act['name']}: [dry-run] 今日未打卡")
            else:
                r = api_call(s, referer, do_method, do_path, do_body)
                ok = r.get("code") == "SUCCESS" or "已签" in (r.get("msg") or "")
                lines.append(f"{act['name']}: 打卡{'成功' if ok else '失败 ' + str(r.get('msg'))}")

        if act.get("task_component"):
            lines.append(f"{act['name']} {run_diy_tasks(s, referer, act['task_component'], dry_run)}")
        if act.get("lottery"):
            lines.append(f"{act['name']} {run_lottery(s, referer, act['lottery'], dry_run)}")
        time.sleep(random.uniform(1.5, 4))
    return lines
