# Agent Hub · 多 AI 协作评审平台（本地 MVP）

把 Kimi / 豆包 / WorkBuddy 等成熟 Agent 当作外部能力，
由本壳统一调度、归一、裁决、人工确认后落实。首个场景：**代码评审 Compare 模式**。

## 快速开始

双击 `start_agent_hub.bat`，或手动：

```powershell
cd <本目录>
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements_agent_hub.txt
python -m uvicorn agent_hub_platform:app --host 127.0.0.1 --port 8765
```

> **默认绑回环，不要图省事改成 `0.0.0.0`。** REST 面（`/skills/install` 可落库第三方
> 技能、`/compare/run` 会烧五家真实模型）只有在 `AGENT_HUB_API_KEY` 设好之后才有
> 鉴权保护（中间件 2026-09-20 已提级到 app 级）。裸绑到局域网等于把这两个接口敞开。
> 要远端部署：先设 `AGENT_HUB_API_KEY`，再放到反向代理/HTTPS 之后，并同步更新
> `agent-hub-connector/mcp.json`。

打开：<http://localhost:8765/ui>（或直接双击 `start_agent_hub.bat`，自动开浏览器）

安全默认值（开箱不越权）：

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `CR_MODE` | `fake` | fake=内置三家假 Agent；real=接真实 Kimi/豆包 |
| `ALLOW_STUB_DIFF` | `1` | 1=用内置样例 diff；0=从 GitLab/GitHub 拉真实 diff |
| `DRY_RUN_POST` | `1` | 1=人工确认后只记录不外发；0=真实发 MR 评论（需配 token） |
| `WEBHOOK_SECRET` | 空 | 设置后校验 GitLab/GitHub webhook 来源 |

## 运行冒烟测试

```powershell
python code_review_compare_mvp.py    # 引擎：归一-聚类-裁决
python code_review_agent_hub.py      # 编排：并行调度+webhook解析+人工门
```

## 接真实模型

密钥放在本目录 `.env`（已配置好，来自 `C:\Users\MVW\personal-agent\.env`），服务启动自动加载。

| 变量 | 当前值 | 状态 |
|---|---|---|
| `MOONSHOT_API_KEY` / `MOONSHOT_MODEL` | kimi-k2.7-code | ✅ 已打通（约 80s/次，慢是推理模型首包延迟，可用 `MODEL_TIMEOUT` 调） |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL` | deepseek-chat | ✅ 已打通（最快，约 20s） |
| `VOLC_ARK_API_KEY` / `VOLC_ARK_MODEL` | doubao-seed-2-1-pro-260915 | ✅ 已打通 |
| （无需配置，本机装了 hermes 即自动启用） | hermes-agent v0.21.3 | ✅ 已打通（ACP 直通，见下） |
| `WB_CLIENT_ID` / `WB_CLIENT_SECRET` | 未配置 | WorkBuddy 开放平台（见下） |

### Hermes 接入（ACP 直通通道，带原生技能库）

hermes-agent 是本机完整 agent runtime（自带 skills 目录：pdf/docx/xlsx/notion/arxiv 等 13 类），
`hermes-acp.exe` 以 ACP（Agent Client Protocol，stdio JSON-RPC）对外服务。agent-hub 经
`acp_channel.py` 驱动它：initialize → authenticate（用 hermes 自己的密钥）→ session → prompt。
与裸模型 adapter 的本质区别：**对方是完整 Agent，执行时带着它自己的技能库**。
冒烟：`python test_acp_smoke.py`；评审集成：`python test_hermes_review.py`。

**技能随行实测**（`python test_skill_invocation.py`）：场景里只有真调用技能才可能答对
（工作目录放真实 docx，金额 42 万，diff 里写死 100 万）——hermes 用 docx 技能实际打开文件、
两处 planted error 全命中，还指出代码注释"从文档提取"是虚假溯源。技能随行不是自述，是铁证。

**五家真实对比**（`python test_real_compare.py`，需 venv 跑以加载 .env）：
deepseek/豆包/hermes/kimi/千问并行 ~113s，归一-聚类-裁决链路对 hermes 与裸模型一视同仁
（SQL 注入 5/5 命中成簇）。注意：`code_review_compare_mvp._extract_json_block` 已做
非严格 JSON 容错（hermes 输出 `\'` 非法转义曾导致整家评审被丢弃）。

### WorkBuddy 接入（按官方「本地助理通道」实现）

依据官方资料（`C:\Users\MVW\WorkBuddy\2026-09-19-14-25-52\` 下的查证结论、操作手册与冒烟脚本）：
Open API 的 **本地助理通道**——向用户 PC 端的 WorkBuddy 发消息、驱动本机 Agent 评审，
数据不落云端沙箱。Base URL `https://www.workbuddy.cn/openapi/v2`，OAuth 2.1 授权。

1. 到 <https://open.workbuddy.cn/> 入驻 → 主体认证 → 「外部应用接入」创建应用，
   Scope 只勾 `user.localassistant.invokable` + `user.localassistant.readable`；
   回调地址登记 `http://127.0.0.1:8765/callback`（与 `WB_REDIRECT_URI` 字节级一致）。
2. `.env` 填入 `WB_CLIENT_ID` / `WB_CLIENT_SECRET`（`client_secret` 只明文显示一次）。
3. 首次授权（自动起本地回调服务抓授权码）：

   ```powershell
   python workbuddy_auth.py          # 授权 + 换票，凭证写 .wb_token.json
   python workbuddy_auth.py --refresh  # 之后只刷新票据
   ```

4. 评审链路：查 PC 在线 → 发评审消息 → 增量轮询收回复 → 解析 JSON。
   PC 端不在线时该 Agent 记为失败、其他三家照常出结论。

`CR_MODE=real` 时单家失败不影响其他家：评审照常出结论，失败原因记录在 run 的 `agent_errors` 里。

## MCP 端点（对外集成 / WorkBuddy 连接器）

服务同时暴露标准 MCP（Streamable HTTP）端点，供 Claude Desktop、WorkBuddy 等 MCP 客户端调用：

- 端点：`POST /mcp/`（挂在主服务同一端口）
- 鉴权：设置环境变量 `AGENT_HUB_API_KEY` 后强制校验 `Authorization: Bearer <key>`；未设置则放行（仅本地调试用）
- 工具：
  - `submit_review(diff, context="")` — 提交 diff 异步评审，立即返回 `run_id`
  - `get_review(run_id)` — 查询状态与汇总裁决（`running → pending_human → resolved`）
  - `submit_proposal(idea, context="")` — 方案共创：三家出方案+交叉互评（异步）
  - `get_proposal(proposal_id)` — 查询方案共创结果（`running → awaiting_decision → final`）
  - `decide_proposal(proposal_id, selected, comment="")` — 拍板，产出 AI 编写定稿
  - `iterate_proposal(proposal_id, instruction="")` — 三家基于定稿迭代（异步）

冒烟测试（需服务已在运行）：

```powershell
python test_mcp_smoke.py http://127.0.0.1:8765/mcp/ <你的 AGENT_HUB_API_KEY>
```

配套交付：`agent-hub-connector/` 是可直接提交 WorkBuddy 审核的连接器目录包
（connector-meta.json + mcp.json + token-schema.json + icon.svg + SKILL.md，
用户自填 Token 模式）。注意：远程接入需把服务部署到公网 HTTPS 地址，
并把 `mcp.json` 中的 `${API_BASE_URL}` 默认值替换为实际域名。

## API

- `GET /health` 健康检查
- `GET /ui` Web 控制台（代码评审 / 方案共创 两个标签页）
- `POST /compare/run` 手动触发 Compare（`diff`/`context`/`title` 均可选，缺省用内置样例 diff）
- `POST /proposal/run` 方案共创：出题 `{idea, context}` → 三家出方案 + 交叉评审
- `POST /proposal/{id}/decide` 拍板 `{selected, comment}` → 产出 AI 编写定稿
- `POST /proposal/{id}/iterate` 迭代 `{instruction}` → 三家基于定稿各出新版，回到待拍板
- `GET /proposals` / `GET /proposal/{id}` 查询
- `POST /compare/run` 手动触发 Compare（`diff`/`context`/`title` 均可选，缺省用内置样例 diff）
- `POST /events/gitlab` / `POST /events/github` MR webhook
- `GET /runs` / `GET /runs/{run_id}` 查询
- `POST /runs/{run_id}/human-decision` 人工确认 `{action, comment, actor}`，
  action ∈ `approve | comment | request_changes`

## 架构

```
GitLab/GitHub MR webhook ──▶ Orchestrator (并行调度三家 Agent)
                                   │ normalize (统一 Review JSON schema)
                                   ▼
                              Judge (聚类/裁决，非投票)
                                   │ mr_comment_draft
                                   ▼
                            人工确认（Web 控制台按钮）
                                   │ 审计记录
                                   ▼
                        外发 MR 评论（默认 DRY-RUN）
```

设计红线（来自讨论定稿）：

- 两家独立命中同一根因 → 高置信 cluster「必须修改」；独家 blocker → `needs_human`，不自动结论。
- 任何外发必须人工确认后执行，`auto_comment` 永不开默认。
- 状态只存结构化结论与 artifact 指针，原始输出留档可回放。
- WorkBuddy 只走本地助理通道，敏感代码不进云端任务沙箱。

## 后续（未包含在本 MVP）

1. 真实外发：实现 `post_comment()` 的 GitLab notes / GitHub reviews（置 `DRY_RUN_POST=0`）。
2. WorkBuddy 本地助理适配器（需官方文档字段）。
3. Postgres 替换 SQLite、secret 进 KMS/Vault、SSO/OIDC。
4. 第二个协作模式：Review-Revise（豆包提案 → WorkBuddy 评审 → Kimi 修订）。
