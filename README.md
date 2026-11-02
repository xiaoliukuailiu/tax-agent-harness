# 财税 Agent Harness

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![Tests](https://img.shields.io/badge/tests-24%20passed-brightgreen)
![LangChain](https://img.shields.io/badge/LangChain-1.x-1C3C3C)
![LangGraph](https://img.shields.io/badge/LangGraph-Checkpoint-00A3E0)
![Model](https://img.shields.io/badge/model-DeepSeek%20%2F%20OpenAI%E5%85%BC%E5%AE%B9-4D6CFE)

一个能**查金额**、**补发票用途**、**多轮接着办**的财税助手。核心不是让模型更聪明，而是：

> **把会犯错的模型，放进不会犯错的流程里。**
> 模型负责理解与表达，程序负责计算与裁决——金额、权限、规则版本、写入校验，一个都不交给模型。

---

## 核心设计：模型与程序的边界

| 环节 | 谁做 | 为什么 |
| --- | --- | --- |
| 理解用户这句话想干什么 | 模型 | 规则写不出来，关键词匹配是反模式 |
| 决定先查数据还是先追问 | 模型 | 需要结合上下文判断 |
| 把结果说成人话 | 模型 | 语言质量 |
| 金额求和、收支分向统计 | 程序 | 模型算数会错，且必须可复算 |
| 该用哪一版规则 | 程序（生效日期） | 模型会拿旧版算新账 |
| 门槛多少、是否必填 | 程序（常量表） | 政策参数不能进提示词 |
| 能不能保存、能不能改这个字段 | 程序（五项校验） | 模型没有第二个入口 |
| 说「已保存」 | 程序（回执）+ 守门 | 无回执不得声称完成 |

一句话总结：**模型只能提议调哪个工具、填什么参数；不能执行、不能改校验、不能加权限。**

---

## 一次真实对话（DeepSeek 实测）

```text
> 帮我看下 9 月的发票和银行收支

9 月共 10 张发票，价税合计 9622.06 元；银行支出 7180.74 元、收入 13000.50 元。
另有 5 张发票尚未填写用途，依据《甲企业发票用途规则》2.0 版需要补齐。

> 把 F004 的用途填成 购买办公用品

这张发票的票面项目是「住宿服务」，与拟填用途不一致，确认要这样保存吗？

> 确认，保存

已保存，回执号 R-0021。再次提交同一修改不会产生第二条记录。
```

金额全部来自 SQL 聚合；发现矛盾主动确认而不擅自推断；保存必须出回执——这些都不是提示词的「希望」，是代码的保证。

---

## 快速开始

```bash
# 1) 装依赖（Python 3.11+）
pip install -r requirements.txt

# 2) 一键演示（不需要模型 Key）
python demo.py

# 3) 跑陷阱回归测试
pytest -q          # 24 passed

# 4) 命令行入口
python -m harness.cli
```

数据库 `data/harness.db` 自动从 `data/official/` 的 Excel 生成，`demo.py` 每次重置初始数据。
**注意**：出于版权考虑，官方材料包（Excel / 规则 docx）**不随仓库分发**——克隆后需将材料包放入 `data/official/`，再运行 `python scripts/import_seed.py` 生成数据库，然后即可运行 `demo.py` 与 `pytest`。
接入大模型见文末「接入模型」。

---

## 架构

```
用户（中文自然语言）
   │
   ▼
CLI ── LLM（DeepSeek / 任意 OpenAI 兼容接口）
   │      理解意图、决定调用顺序、组织语言
   ▼
tools.py ── 唯一读写入口
   │      五项校验 · 幂等键 · 三态回执 · 保存后回库核对
   │      ▲
   │      └─ middlewares：上下文注入 / 工具全量留痕 / 表达守门
   ▼
SQLite（WAL）
   invoice · bank_txn · rule_doc · acl
   memory · task_state · tool_log · audit_event（只追加）
```

```
├── demo.py                  一键演示（不需要模型）
├── harness/
│   ├── config.py            路径/模型/预算配置
│   ├── db.py                连接、权限查询、事务、缺库自动初始化
│   ├── policy.py            政策即代码：版本选择 + 规则检索 + 金额阈值（整数分）
│   ├── memory.py            长期记忆 + 任务状态（每用户每企业最多 1 个未完成）
│   ├── tools.py             唯一读写入口：权限 / 五项校验 / 幂等 / 三态回执 / 重查核对
│   ├── context.py           上下文装配与预算裁剪
│   ├── style.py             去 AI 味 7 条确定性规则
│   ├── middlewares.py       Scope / Audit / Expression 三个中间件
│   ├── agent.py             create_agent 接线（可选）
│   └── cli.py               命令行入口
├── scripts/import_seed.py   官方 Excel → SQLite，含金额自检
├── scripts/package.py       打包分发（排除 .env / 缓存 / wal）
├── tests/test_traps.py      24 条回归测试（15 类陷阱全覆盖）
└── data/
    ├── official/            官方材料包（版权原因不随仓库分发，需自行放置）
    └── harness.db           生成的数据库（不入库，`import_seed.py` 可随时重建）
```

---

## 测试覆盖了哪些「坑」

`pytest -q`，24 个用例对应 15 类业务陷阱，每条都是先写失败再修：

- **金额口径**：价税合计求和；收支分向统计不互抵；空月份返回 0.00 + 说明而非报错
- **权限**：无权限 ≠ 没数据（不出现 0.00 误导）；只读用户保存被拒
- **规则版本**：按生效日期选现行版；反证测试——误用停用版会从应报 5 张漏到 1 张（**漏报 80%**）；备注不能替代用途
- **写操作**：只读字段拦截、空内容拦截、保存后回库核对、重复提交只落一条且回执相同、跨企业同号发票不串
- **记忆与任务**：每用户每企业唯一未完成任务；记忆按企业隔离、可更正软删
- **表达守门**：无回执时禁止说「已保存」；AI 套话黑名单命中为 0

---

## 关键实现选择

1. **金额用整数「分」**。官方汇总表本身就有 `9622.060000000001` 这类浮点尾差，用 float 做相等判断必然出错。
2. **幂等键 + 部分唯一索引双保险**。`UNIQUE(company_id, idem_key) WHERE status='success'`，应用层写错也重复保存不了。
3. **保存后重新查询核对**。返回库里真实值而非调用方的 patch，不一致标 `VERIFY_MISMATCH`。
4. **规则版本由日期决定，不由模型决定**。某月没有现行规则时返回 `not_applicable` 并说明原因，而不是硬套旧版。
5. **未完成事项永不裁剪**。上下文压到预算极限时，Agent 仍知道手里还剩哪几张票。
6. **去 AI 味是代码不是提示词**。可写单测断言黑名单命中为 0，模型做不到。

---

## 能力对照

| 能力 | 实现 | 位置 |
| --- | --- | --- |
| 对话与决策 | Agent 主循环 + 系统提示（模型驱动，无关键词分支） | `agent.py` / `context.SYSTEM_PROMPT` |
| 记忆管理 | `memory` 表 CRUD + 按企业/用户隔离 + 单未完成任务唯一索引 | `memory.py` |
| 知识库管理 | `(doc_id, version)` 主键 + `status` + 生效日选择 + 检索 | `policy.py` |
| 模型输入 | 7 段装配 + 预算裁剪 + 永不裁剪项 | `context.py` / `middlewares.ScopeMiddleware` |
| 数据操作 | 五项校验 + 三态回执 + 幂等键 + 部分唯一索引 + 保存后重查 | `tools.save_patch` |
| 运行记录 | `audit_event`（只追加）+ `tool_log`（参数/结果/耗时/回执） | `audit.py` / `tools._track` |
| 自然表达 | 7 条确定性规则（套话/结论前置/列点/无回执/空转/长度） | `style.py` |

---

## 接入模型

```bash
cp .env.example .env      # 填 BASE_URL / API_KEY / MODEL（任意 OpenAI 兼容接口）
python -m harness.cli --llm
```

```ini
HARNESS_BASE_URL=https://api.deepseek.com
HARNESS_API_KEY=sk-xxxx
HARNESS_MODEL=deepseek-chat
```

模型侧只多一行：`create_agent(model, tools, system_prompt, middleware=[...], checkpointer=...)`。
同一套工具、同一套校验，只是由模型决定调用顺序；换模型只改 `.env` 三个变量，业务代码零改动。
没有 Key 时不影响 `demo.py` 与测试。

---

## 与同类开源项目的关系

「agent harness」已是成熟品类（如 `langchain-ai/deepagents`），通用骨架是**模型接入 / 工具分发 / 安全层 / 会话记忆 / 可观测性 / 扩展点**，本项目按此组织。

发票类开源项目多集中在 **OCR + 字段抽取 + RAG**；而本项目数据已是结构化表格，真正的难点是**权限、幂等、版本化规则、可审计**——这部分没有现成方案可抄，是本项目的主要投入。

---

## 状态

主链路已可运行：查询 / 检查 / 保存 / 重启恢复 / 审计 / 记忆 / 规则版本选择，24 条陷阱测试全绿。
未完成：长对话自动摘要、Web 界面（当前为命令行入口）、新数据集适配。

配套文档（`docs/`）：[资料包解析与 15 个陷阱](docs/赛题二-资料包解析.md) · [完整系统设计](docs/赛题二-完整系统设计.md) · [初学者版设计文档](docs/赛题二-设计文档-初学者版.md)

## 打包分发

```bash
python scripts/package.py                  # 生成 dist/tax-agent-harness-v0.1.0.zip
python scripts/package.py --no-rebuild     # 不重建数据库，直接用现有库
```

不会进包：`__pycache__`、`.pytest_cache`、`.env`（只带 `.env.example`）、SQLite 的 `-wal/-shm`。
解压即用：包内含重建后的初始数据库，`python demo.py` 与 `pytest -q` 直接可用。
