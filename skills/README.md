# agent-hub 本地技能库（归一技能层）

hub 统一持有、统一执行的 SKILL.md 技能目录。每个技能一个子目录，内含 `SKILL.md`（必需）与可选资源文件。

## 与其他"skills"目录的区别

| 目录 | 归属 | 用途 |
|---|---|---|
| `agent-hub/skills/`（本目录） | hub 自己 | 归一技能层：hub 的模型调用前注入 SKILL.md，由 hub 编排执行 |
| `agent-hub-connector/skills/` | WorkBuddy | 出站 connector 打包：WorkBuddy 的 Agent 读它来了解如何调用 hub 的 MCP |
| 各 Agent 平台自带技能目录（如豆包 `.skills`） | 对应平台 | hub 不直接驱动；如需使用，通过 `skill_install` 市场链路移植到本目录 |

## 生命周期

```powershell
# 搜索市场（skills.sh，按安装量降序）
python skill_install.py search pdf --top 8
# 取源码到隔离区（不落库）+ 安装前预审
python skill_install.py fetch owner/repo --skill <slug>
python skills_guard.py <隔离区目录>
# 过审后安装（唯一落点 = 本目录）
python skill_install.py install <隔离区目录> --auto
```

也可经 MCP 工具调用（WorkBuddy 等客户端可达）：`skill_list` / `skill_read` /
`skill_audit` / `skill_search` / `skill_install`（默认人工门，`auto=true` 且 guard pass 才落库）。

## 安全

- `skills_guard.py` 确定性预审：危险 API（窃取浏览器数据/凭证/反弹连接等）一票否决
- guard 判定 warn/fail 的技能**不会**进入本目录（除非人工 `--force` 并留档）
- 来源不明的 SKILL.md 请先用 `skills_guard.py` 审计再手工放入
