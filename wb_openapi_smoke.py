#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wb_openapi_smoke.py —— WorkBuddy Open API「本地助理」通道 授权 + 连通性冒烟测试

仅依赖 Python 标准库（3.9+），不需要 pip 安装任何东西。

准备工作（把值换成开放平台上申请到的）：
    Windows CMD:
        set WB_CLIENT_ID=app_xxxxxxxx
        set WB_CLIENT_SECRET=sk_live_xxxxxxxx
        set WB_REDIRECT_URI=http://127.0.0.1:8765/callback
    PowerShell:
        $env:WB_CLIENT_ID="app_xxxxxxxx"
        $env:WB_CLIENT_SECRET="sk_live_xxxxxxxx"
        $env:WB_REDIRECT_URI="http://127.0.0.1:8765/callback"

用法：
    python wb_openapi_smoke.py                 # 起本地回调服务，自动抓取授权码
    python wb_openapi_smoke.py --manual        # 回调是 https 域名时，手动粘贴回调 URL
    python wb_openapi_smoke.py --refresh       # 跳过授权，用缓存 refresh_token 换新 token
    python wb_openapi_smoke.py --send "指令"    # 授权成功后向本地助理发一条消息

安全说明：
    - client_secret 只从环境变量读取，不落盘、不打印。
    - access_token / refresh_token 缓存在同目录 .wb_token.json（本地文件，勿提交版本库）。
    - 请勿把凭据写进本文件或任何 skill / 配置文件中。
"""

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

BASE = "https://www.workbuddy.cn/openapi/v2"
SCOPES = "user.localassistant.invokable user.localassistant.readable"
TOKEN_FILE = Path(__file__).resolve().with_name(".wb_token.json")
SAFETY_MARGIN = 300  # 秒；提前刷新余量。文档 expires_in 示例为 3600，勿按 24 小时设计。


def need(name):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"[x] 缺少环境变量 {name}")
    return value


def http(method, url, headers=None, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.getcode()
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except urllib.error.URLError as exc:
        return 0, {"error": str(exc.reason)}
    try:
        return status, json.loads(raw)
    except Exception:
        return status, raw


def show(tag, status, body):
    print(f"\n=== {tag} ===")
    print(f"HTTP {status}")
    if isinstance(body, (dict, list)):
        print(json.dumps(body, ensure_ascii=False, indent=2)[:2000])
    else:
        print(str(body)[:2000])
    if isinstance(body, dict) and body.get("request_id"):
        print(f"[i] request_id = {body['request_id']}（报障时请附上）")


def api_headers(token, json_body=False):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def form_headers():
    return {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }


def build_authorize_url(client_id, redirect_uri, state):
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "state": state,
        }
    )
    return f"{BASE}/authorize?{query}"


def exchange_code(client_id, client_secret, code, redirect_uri):
    data = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        }
    ).encode()
    return http("POST", f"{BASE}/token", headers=form_headers(), data=data)


def refresh_token_request(client_id, client_secret, refresh_token):
    data = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode()
    return http("POST", f"{BASE}/token", headers=form_headers(), data=data)


class _Catcher(BaseHTTPRequestHandler):
    code = None
    state = None
    err = None

    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _Catcher.code = (query.get("code") or [None])[0]
        _Catcher.state = (query.get("state") or [None])[0]
        _Catcher.err = (query.get("error") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            "<!doctype html><meta charset=utf-8>"
            "<h3>授权回调已收到，可以关闭本页回到终端。</h3>".encode("utf-8")
        )

    def log_message(self, *args):
        pass


def wait_for_code(redirect_uri, timeout=300):
    parsed = urllib.parse.urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    server = HTTPServer((host, port), _Catcher)
    server.timeout = 1
    deadline = time.time() + timeout
    print(f"[*] 已在 {host}:{port} 上监听回调，最长等待 {timeout} 秒……")
    while time.time() < deadline and _Catcher.code is None and _Catcher.err is None:
        server.handle_request()
    server.server_close()
    return _Catcher.code, _Catcher.state, _Catcher.err


def save_token(body):
    TOKEN_FILE.write_text(
        json.dumps(
            {
                "access_token": body.get("access_token"),
                "refresh_token": body.get("refresh_token"),
                "expires_at": time.time() + int(body.get("expires_in") or 3600),
                "scope": body.get("scope"),
                "open_id": body.get("open_id"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass


def load_cache():
    if not TOKEN_FILE.exists():
        return {}
    try:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def test_localassistant(access_token, message=None):
    status, body = http("GET", f"{BASE}/localassistant", headers=api_headers(access_token))
    show("本地助理在线状态", status, body)
    online = isinstance(body, dict) and bool((body.get("data") or {}).get("online"))

    if message:
        if not online:
            print("[!] 本地助理不在线，跳过发送。请确认 PC 端 WorkBuddy 已启动并登录。")
        else:
            status, body = http(
                "POST",
                f"{BASE}/localassistant/message",
                headers=api_headers(access_token, json_body=True),
                data=json.dumps({"content": message, "msg_type": "text"}).encode("utf-8"),
            )
            show("发送消息给本地助理", status, body)

    status, body = http(
        "GET", f"{BASE}/localassistant/message?limit=10", headers=api_headers(access_token)
    )
    show("消息历史（最近 10 条）", status, body)


def main():
    parser = argparse.ArgumentParser(description="WorkBuddy Open API 本地助理通道 冒烟测试")
    parser.add_argument("--manual", action="store_true", help="回调为 https 域名时，手动粘贴完整回调 URL")
    parser.add_argument("--refresh", action="store_true", help="跳过授权，用缓存的 refresh_token 换新 token")
    parser.add_argument("--send", metavar="TEXT", help="授权成功后向本地助理发送一条消息")
    parser.add_argument("--timeout", type=int, default=300, help="等待回调的秒数，默认 300")
    args = parser.parse_args()

    client_id = need("WB_CLIENT_ID")
    client_secret = need("WB_CLIENT_SECRET")
    redirect_uri = need("WB_REDIRECT_URI")

    print("[i] 若应用状态还不是「已启用」，后续调用会返回 401/403，属预期行为。")

    cache = load_cache()
    access_token = None

    if args.refresh:
        if not cache.get("refresh_token"):
            sys.exit("[x] 缓存里没有 refresh_token，请先完整跑一次授权流程")
        status, body = refresh_token_request(client_id, client_secret, cache["refresh_token"])
        show("刷新 access_token", status, body)
        if status != 200:
            sys.exit("[x] 刷新失败")
        save_token(body)
        access_token = body["access_token"]
    else:
        if cache.get("access_token") and float(cache.get("expires_at") or 0) - SAFETY_MARGIN > time.time():
            remain = int(float(cache["expires_at"]) - time.time())
            print(f"[*] 复用缓存 token，剩余约 {remain} 秒")
            access_token = cache["access_token"]
        elif cache.get("refresh_token"):
            status, body = refresh_token_request(client_id, client_secret, cache["refresh_token"])
            if status == 200:
                show("刷新 access_token（复用缓存的 refresh_token）", status, body)
                save_token(body)
                access_token = body["access_token"]

    if access_token is None:
        state = secrets.token_urlsafe(16)
        url = build_authorize_url(client_id, redirect_uri, state)
        print("\n[1/3] 请在浏览器中完成授权（回调地址须与平台注册值字节级一致）：")
        print(url)
        try:
            webbrowser.open(url)
        except Exception:
            pass

        got_state = None
        code = None
        if args.manual:
            print("\n[2/3] 授权后浏览器会跳转到回调地址（显示无法访问属正常）。")
            pasted = input("      请粘贴地址栏中的完整 URL: ").strip()
            query = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
            code = (query.get("code") or [None])[0]
            got_state = (query.get("state") or [None])[0]
        else:
            print("\n[2/3] 等待回调……")
            code, got_state, err = wait_for_code(redirect_uri, args.timeout)
            if err:
                sys.exit(f"[x] 授权被拒绝：{err}")

        if not code:
            sys.exit("[x] 未获取到授权码")
        if got_state != state:
            sys.exit("[x] state 校验失败，疑似 CSRF，已中止")
        print("[*] state 校验通过")

        status, body = exchange_code(client_id, client_secret, code, redirect_uri)
        show("[3/3] 换取 access_token", status, body)
        if status != 200:
            sys.exit("[x] 换取访问凭证失败")
        save_token(body)
        access_token = body["access_token"]
        print(f"[*] 凭证已缓存到 {TOKEN_FILE}")

    test_localassistant(access_token, args.send)
    print("\n[*] 冒烟测试结束。")


if __name__ == "__main__":
    main()
