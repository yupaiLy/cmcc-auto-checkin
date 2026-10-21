#!/usr/bin/env python3
"""中国移动统一认证会话保活（keepalive）。

背景
----
`appTokenLogin` 的 `token` 字段是一个统一认证会话 cookie
（`JSESSIONID=…; UID=…; Comment=SessionServer-unity; …`），空闲一段时间就会被
服务端回收；而主签到脚本一天只跑一次，两次运行之间会话早就凉了，于是每次都要人工
重新抓包。上游作者 2026-10-21 指出：单靠 jwt+app_token 无法解决这件事。

本脚本只做一件事：**高频（建议 15 分钟）发起一次 auth 兑换**，让统一认证会话保持
活跃、jwt 始终是最新的。不签到、不领奖、不碰任何业务接口。

用法
----
青龙定时任务（不要用 `cd &&`，必须走 task 包装器）：

    */15 * * * *   task cmcc-auto-checkin/cmcc_keepalive.py

行为
----
- 三档凭证（jwt+app_token / jwt 单独 / 纯 app_token）依次尝试，命中即止；
- 成功：静默，仅写一行日志（`--verbose` 可看到投递的档位）；
- 全部失败：推送通知（沿用 config.json 的 bark_url / serverchan_sendkey）并返回 1，
  这是唯一需要人重新抓包的场景。
"""

from __future__ import annotations

import sys
import logging
import random
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cmcc_sign as cs  # noqa: E402

log = logging.getLogger("cmcc_keepalive")


def keepalive(cfg: cs.Config, verbose: bool = False) -> tuple[bool, str]:
    """返回 (是否成功, 命中的档位/失败原因)。只做 auth，不建立活动会话。"""
    import requests

    s = requests.Session()
    s.trust_env = False
    s.mount("https://", cs.LegacyTLSAdapter())
    s.headers.update({"user-agent": cs.USER_AGENT, "accept-language": "zh-CN,zh-Hans;q=0.9"})

    r = s.get(cs.SSO_LOGIN, params={"dlwmh": "true", "actUrl": cfg.act_url}, timeout=30)
    r.raise_for_status()
    m = re.search(r"loginPath\s*=\s*'([^']+)'", r.text)
    if not m:
        return False, "登录页未返回 sid（SSO 入口可能已变更）"
    login_url = cs.BASE + "/qwhdsso" + m.group(1)

    body = {
        "provinceCode": cfg.province_code,
        "cityCode": cfg.city_code,
        "userCheckId": format(int(cfg.phone), "x"),
        "carrierOperator": cfg.carrier_operator,
        "appVersionCode": cfg.app_version_code,
        "took": random.randint(120, 900),
    }

    attempts: list[tuple[str, str | None]] = []
    cached = cs.load_jwt_cache(cfg)
    if cached:
        attempts.append(("jwt+app_token", cached[0]))
        attempts.append(("jwt 单独", cached[0]))
    attempts.append(("app_token 引导", None))

    last_err = ""
    for idx, (label, jwt_str) in enumerate(attempts):
        token = cfg.app_token if label != "jwt 单独" else ""
        payload = {**body, "jwtToken": jwt_str, "token": token}
        try:
            r = s.post(login_url, json=payload, timeout=30)
            cand = r.json()
        except Exception as e:  # 网络/解析异常：换下一档
            last_err = f"{label}: {type(e).__name__}: {e}"
            if verbose:
                log.warning("保活尝试失败 %s", last_err)
            continue
        # 上游偶发返回裸 JSON 字符串（非对象），此时不能按 code 判定，
        # 也不算凭证失效——换下一档继续试。
        if not isinstance(cand, dict):
            last_err = f"{label}: 响应非对象 {type(cand).__name__} {str(cand)[:120]!r}"
            if verbose:
                log.warning("保活尝试响应非对象 %s", last_err)
            continue
        if cand.get("code") == "SUCCESS":
            data = cand.get("data")
            data = data if isinstance(data, dict) else {}
            cs.save_jwt_cache(cfg, data.get("jwt"))
            return True, label
        last_err = f"{label}: {cand.get('code')} {cand.get('msg')}"
        if verbose:
            log.info("保活尝试未通过 %s", last_err)

    return False, last_err


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    verbose = "--verbose" in sys.argv
    cfg = cs.Config(str(Path(__file__).with_name("config.json")))
    err = cfg.validate()
    if err:
        log.error("配置不完整: %s", err)
        return 1

    try:
        ok, detail = keepalive(cfg, verbose=verbose)
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"

    if ok:
        if verbose:
            log.info("保活成功（%s）", detail)
        print("keepalive ok")
        return 0

    log.error("保活失败：三档凭证全部被拒 —— %s", detail)
    try:
        cs.notify(cfg, "中国移动凭证已失效",
                  f"保活失败：{detail}\n需要在 App 内重新抓包更新 config.json 的 app_token")
    except Exception as e:
        log.warning("通知推送失败: %s", e)
    return 1


if __name__ == "__main__":
    sys.exit(main())
