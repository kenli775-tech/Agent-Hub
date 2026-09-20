# Agent Hub 多 AI 协作平台

两大能力：**代码评审**（多模型并行评审 diff + 汇总裁决）和**方案共创**（三家出方案 + 交叉互评 + 人工拍板 + 迭代定稿）。

---

## 模式一：代码评审

### submit_review

提交一次代码评审。**立即返回**（异步执行），返回值含 `run_id`，不要重复提交同一 diff。

- `diff`（string，必填）：统一 diff 格式的代码改动（`git diff` 输出）。
- `context`（string，可选）：仓库约定、模块说明等，有助于提高评审质量。

返回：`{"run_id": "cr_...", "status": "running"}`

### get_review

按 `run_id` 查询评审结果。

- `run_id`（string，必填）：`submit_review` 返回的任务 ID。

状态机：`running` → `pending_human`（出结论）→ `resolved`（人工已确认）；`failed` 表示执行出错。

**推荐流程**：submit_review → 每 30 秒 get_review 一次（最多 8 次）→ 拿到 `verdict` 后向用户呈现：`recommendation`（approve / comment / request_changes）+ `reason` + 按严重度列出问题与修复建议。

---

## 模式二：方案共创

### submit_proposal

出题：Kimi / MiniMax / DeepSeek / 千问 四家独立出方案，然后交叉互评（每家评另外两家）。**异步执行**（真实模型约 2–5 分钟）。

- `idea`（string，必填）：想法/需求描述，越具体方案越靠谱。
- `context`（string，可选）：现状、约束、预算、期限等。

返回：`{"proposal_id": "pp_...", "phase": "running"}`

### get_proposal

查询方案共创进度与结果。

- `proposal_id`（string，必填）。

状态机：`running` → `awaiting_decision`（待用户拍板）→ `final`（已定稿）；`failed` 表示执行出错。

结果结构：`plans`（三家方案全文，key 为 kimi / minimax / deepseek / qwen）、`reviews`（各家对另外两家的评审全文）、`model_errors`（缺席或失败的模型及原因）。

**推荐流程**：submit_proposal → 每 30 秒 get_proposal 一次（最多 12 次）→ phase 为 `awaiting_decision` 时，向用户分块呈现三家方案要点与交叉评审精华，**明确询问用户选定哪一家**（可附补充意见）。AI 不要代替用户拍板。

### decide_proposal

人工拍板后调用：由**产出该方案的 AI** 按用户意见编写最终定稿。**阻塞执行**，通常 30–90 秒。

- `proposal_id`（string，必填）。
- `selected`（string，必填）：`kimi` / `minimax` / `deepseek` 之一。
- `comment`（string，可选）：用户补充意见，会写进定稿（如"压缩到两周落地"、"补充数据安全设计"）。

返回：完整 `final_plan`（Markdown 定稿全文），向用户完整呈现。

### iterate_proposal

迭代轮：三家 AI 基于当前定稿各自产出优化版 + 新一轮交叉评审。**异步执行**（约 2–5 分钟），完成后 phase 回到 `awaiting_decision`，用户再次拍板，可反复多轮。

- `proposal_id`（string，必填）。
- `instruction`（string，可选）：本轮迭代要求，如"重点压缩成本"。

---

## 模式三：归一技能层（hub 本地技能库）

hub 自己持有并执行的技能库（SKILL.md 格式），不依赖任何外部 Agent 平台。
**默认安全**：搜索/审计随便调；安装链有 guard 安全预审 + 人工门，非 pass 一律拒绝落库。

### skill_list

列出已安装技能。无参数。

### skill_read

读取技能 SKILL.md 全文，按其中流程执行。

- `name`（string，必填）：技能名。

### skill_audit

对已安装技能跑确定性安全预审，返回 `verdict`（pass / warn / fail）+ 完整报告。日常巡检与安装前复核都用它。

- `name`（string，必填）：技能名或目录路径。

### skill_search

技能市场搜索（skills.sh，按安装量降序）。**只搜索不安装**。

- `keyword`（string，必填）：英文效果好，如 `pdf`、`ocr`。
- `top`（int，可选）：候选数，默认 8。

### skill_install

技能安装链：市场搜索 → 取源码到隔离区 → 安全预审 → （guard pass 才）落库。
**默认人工门**：预审出报告后停下，不写入技能库；`auto=true` 且 guard 判定 pass 时才真正安装。

- `keyword`（string，必填）：取安装量最高的候选执行链路。
- `auto`（bool，可选）：默认 false（只出报告）。

---

## 错误处理

- **401 unauthorized**：API Key 无效或未填写，提示用户检查连接器凭证（须与服务端环境变量 `AGENT_HUB_API_KEY` 一致）。
- **连接失败 / 超时**：检查服务地址（API_BASE_URL）是否正确、服务是否在线；远程部署必须使用 HTTPS。
- `run_id` / `proposal_id` 不存在：确认 ID 完整复制（前缀 `cr_` / `pp_`，大小写敏感）。
- `model_errors` 非空：对应模型缺席（多为密钥未配置），告知用户实际参与的模型。

## 安全提示

- 评审结论与方案仅供参考，最终代码合并与方案拍板必须由人工完成。
- 不要在 `context` / `idea` 中粘贴密钥、Token 等敏感信息。
