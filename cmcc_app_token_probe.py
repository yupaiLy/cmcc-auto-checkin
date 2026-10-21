#!/usr/bin/env python3
"""app_token 单独存活探针（独立于主保活流程）。

背景
----
2026-10-21 实测：服务端「jwt 单独续期」的口子会**开开关关**（15:08 放开、15:15
又关闭、17:14 仍关闭）。三档策略（jwt+app_token → jwt 单独 → 纯 app_token 引导）
对两种状态都成立，所以主保活流程不受影响。

但有个副作用：后台「开」时主保活第一档就命中，永远走不到第三档，于是
**测不出 app_token 本身什么时候死**。

而「app_token 是空闲超时还是绝对过期」这个判断，直接决定保活间隔该设
15 分钟还是 2 小时。目前的数据只覆盖「一直保活」这一侧，缺「停多久会死」。

本脚本补上这一侧：**只发纯 app_token，完全不带 jwt**，独立记录每次结果。
它不参与主保活、不影响保活成功率，只是旁路采集数据。

用法（青龙定时任务）
--------------------
    0 * * * *   task cmcc-auto-checkin/cmcc_app_token_probe.py

输出
----
- 结果 append 到同目录 .cmcc_app_token_probe.jsonl（每行一条 JSON）
- 成功：静默
- 连续失败 N 次（默认 3）：推送通知，因为这通常意味着 app_token 真的到期了，
  需要在 App 内重新抓包

为什么单独发而不复用 keepalive
------------------------------
keepalive 第一档「jwt+app_token」在后台开着时必然成功，会掩盖 app_token 自身的
存活状态。要测「app_token 单独能撑多久」，必须绕过 jwt、只发 app_token。
"""

from __future__ import annotations

import json
import logging
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests  # noqa: E402

import cmcc_sign as cs  # noqa: E402

log = logging.getLogger("cmcc_app_token_probe")

LOG_PATH = Path(__file__).resolve().parent / ".cmcc_app_token_probe.jsonl"
FAIL_ALERT_THRESHOLD = 3


def _probe(cfg: cs.Config) -> dict:
    """发一次纯 app_token 请求（jwtToken 置空），返回结果字典。"""
    s = requests.Session()
    s.trust_env = False
    s.mount("https://", cs.LegacyTLSAdapter())
    s.headers.update({"user-agent": cs.USER_AGENT, "accept-language": "zh-CN,zh-Hans;q=0.9"})

    r = s.get(cs.SSO_LOGIN, params={"dlwmh": "true", "actUrl": cfg.act_url}, timeout=30)
    r.raise_for_status()
    m = re.search(r"loginPath\s*=\s*'([^']+)'", r.text)
    if not m:
        return {"ok": False, "err": "SSO 入口未返回 loginPath（入口可能已变更）"}
    login_url = cs.BASE + "/qwhdsso" + m.group(1)
    sid = m.group(1)

    body = {
        "provinceCode": cfg.province_code,
        "cityCode": cfg.city_code,
        "userCheckId": format(int(cfg.phone), "x"),
        "carrierOperator": cfg.carrier_operator,
        "appVersionCode": cfg.app_version_code,
        "took": random.randint(120, 900),
    }
    # 关键：jwtToken 置空，token 只带 app_token —— 测的是 app_token 自身
    payload = {**body, "jwtToken": "", "token": cfg.app_token}

    r = s.post(login_url, json=payload, timeout=30)
    try:
        cand = r.json()
    except ValueError:
        return {"ok": False, "err": f"响应非 JSON (HTTP {r.status_code})", "sid": sid}

    if not isinstance(cand, dict):
        return {"ok": False, "err": f"响应非对象 {type(cand).__name__}", "sid": sid,
                "raw": str(cand)[:160]}

    out = {"ok": cand.get("code") == "SUCCESS", "code": cand.get("code"),
           "msg": (cand.get("msg") or "")[:160], "sid": sid}
    d = cand.get("data")
    out["jwt_issued"] = bool(d.get("jwt")) if isinstance(d, dict) else False
    return out


def _read_history(limit: int = 200) -> list[dict]:
    if not LOG_PATH.exists():
        return []
    rows: list[dict] = []
    try:
        for line in LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return []
    return rows[-limit:]


def _consecutive_failures() -> int:
    n = 0
    for row in reversed(_read_history()):
        if row.get("ok"):
            break
        n += 1
    return n


def _notify(cfg: cs.Config, text: str) -> None:
    """复用 cmcc_sign.notify（自带 URL 校验与异常兜底）。失败不影响脚本退出码。"""
    try:
        cs.notify(cfg, "中国移动 app_token 探针", text)
    except Exception as e:  # 通知失败无所谓
        log.warning("推送失败: %s", e)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    cfg = cs.Config(str(Path(__file__).with_name("config.json")))
    err = cfg.validate()
    if err:
        log.error("配置不完整: %s", err)
        return 1

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        res = _probe(cfg)
    except Exception as e:
        res = {"ok": False, "err": f"{type(e).__name__}: {e}"}

    cached = cs.load_jwt_cache(cfg)
    chain = round((time.time() - cached[1]) / 86400, 2) if cached else None

    row = {"ts": ts, "mode": "app_token_only", "chain_days": chain, **res}
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

    if res.get("ok"):
        # 成功时静默，避免每小时刷屏
        if res.get("jwt_issued"):
            # 纯 app_token 就能换出新 jwt —— 这是 app_token 自身有效的最强证据
            pass
        return 0

    fails = _consecutive_failures()
    log.warning("app_token 单独探测失败(%s) 连续第 %d 次: %s",
                ts, fails, res.get("msg") or res.get("err"))
    if fails >= FAIL_ALERT_THRESHOLD:
        _notify(cfg, f"app_token 单独探测连续 {fails} 次失败，疑似已到期。\n"
                     f"最后一次: {res.get('msg') or res.get('err')}\n"
                     f"请在 App 内重新抓包更新 config.json 的 app_token。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
