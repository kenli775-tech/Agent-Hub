# -*- coding: utf-8 -*-
"""
WorkBuddy 授权助手 —— 官方冒烟脚本 wb_openapi_smoke.py 的薄封装。

差异只有两点：
1. 自动加载同目录 .env（官方脚本只读环境变量）；
2. 缺 WB_CLIENT_ID 时给出清晰的申请指引，而不是直接退出。

用法（与官方脚本一致）：
    python workbuddy_auth.py                 # 起本地回调服务，自动抓取授权码
    python workbuddy_auth.py --manual        # 手动粘贴回调 URL
    python workbuddy_auth.py --refresh       # 用缓存 refresh_token 换新 token
    python workbuddy_auth.py --send "指令"   # 授权成功后向本地助理发一条消息

授权成功后，凭证写入 .wb_token.json，平台（RealWorkBuddyAdapter）直接复用。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# 1. 加载 .env（不覆盖已有环境变量）
env_file = BASE_DIR / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())

# 2. 校验凭据
missing = [k for k in ("WB_CLIENT_ID", "WB_CLIENT_SECRET", "WB_REDIRECT_URI")
           if not os.environ.get(k)]
if missing:
    print("缺少环境变量: " + ", ".join(missing))
    print()
    print("获取路径（详见 WorkBuddy 目录《应用申请与凭据获取-操作手册v1.0.md》）：")
    print("  1. 打开 https://open.workbuddy.cn/ 入驻并完成主体认证")
    print("  2. 控制台 → 业务类型「外部应用接入」→ 创建应用")
    print("  3. Scope 只勾 user.localassistant.invokable + user.localassistant.readable")
    print("  4. OAuth 回调地址填 http://127.0.0.1:8765/callback（与 WB_REDIRECT_URI 一致）")
    print("  5. 应用「已启用」后，把 client_id / client_secret 写进 .env")
    print()
    print("注意：client_secret 只明文展示一次，拿到立刻写入 .env 并备份。")
    sys.exit(1)

# 3. 交给官方冒烟脚本
sys.path.insert(0, str(BASE_DIR))
sys.argv = ["wb_openapi_smoke.py"] + sys.argv[1:]

import runpy
runpy.run_path(str(BASE_DIR / "wb_openapi_smoke.py"), run_name="__main__")
