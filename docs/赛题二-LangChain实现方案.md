# 赛题二 LangChain 实现指南（初学者版）

> 基于 langchain 1.4.6 / langgraph 1.2.16 实测（API 都在本机源码里核对过）。
> 你需要会：Python 基础、pip 装包、会用命令行。**不需要**懂 LangGraph、不需要懂财税知识。

## 0. 这份文档怎么读

整个系统拆开其实就一句话：

> **让大模型当"会打电话的客服"——它能听懂用户说什么，能转接查数据的同事（工具），事情办完会汇报；而我们在电话线上装了几个"检查站"（中间件），保证它不乱说、不撒谎、每一步都有记录。**

文档结构：第 1 章先弄懂 6 个概念（10 分钟）→ 第 2 章跑通最小版本（30 行代码）→ 第 3-9 章每次只加一个功能 → 第 10 章常见坑 → 附录术语表。每一节都是"加什么、为什么、代码、运行时发生了什么"。

---

## 1. 先搞懂 6 个概念

### 1.1 Agent（智能体）＝ 一个会自己决定用工具的模型

普通对话：你问一句，模型答一句，它什么也做不了。
Agent：你给模型一堆"工具"（查数据、保存记录……），它**自己判断**该调哪个、调几次，直到把事办完再回答你。

```text
用户："查一下 A 公司 9 月的发票"
  ↓ 模型思考：我需要查发票 → 调用 query_invoices 工具
  ↓ 工具返回结果
  ↓ 模型思考：结果齐了 → 组织语言回复用户
用户收到回答
```

这个"模型↔工具反复循环"的过程就叫 **agent loop**，`create_agent` 一行代码帮你建好。

### 1.2 Tool（工具）＝ 给模型装的手

模型本身只会生成文字，**它不能查数据库、不能存文件**。工具就是你写好的普通 Python 函数，加一个 `@tool` 装饰器，模型就能"调用"它——实际上是模型输出一个函数调用请求，框架替它执行，把结果喂回去。

### 1.3 Middleware（中间件）＝ 流水线上的检查站

模型每说一句话、每调一次工具，都要经过流水线。中间件就是在流水线上加的检查站：

```text
用户消息 → [检查站A: 注入企业信息] → 模型生成回复 → [检查站B: 检查有没有说谎] → 给用户
                                            ↓ 要调工具
                              [检查站C: 记录这次调用] → 执行工具 → 结果回给模型
```

四个钩子位置（记不住没关系，用到哪个讲哪个）：

| 钩子 | 时机 | 干什么用 |
| --- | --- | --- |
| `wrap_model_call` | 每次调用模型**前** | 偷偷往 prompt 里塞企业、待办、记忆 |
| `wrap_tool_call` | 每次执行工具**前后** | 记日志、算耗时 |
| `after_model` | 模型回复**后**、给你看**前** | 检查/改写回复（防说谎、去 AI 味） |
| `before_model` | 调模型前改状态 | 一般用不上，知道有就行 |

### 1.4 Checkpointer（检查点）＝ 游戏存档

Agent 每走一步，框架自动把"聊到哪了"存进 SQLite。程序崩了、电脑重启了、第二天再打开——读档继续聊。这就是赛题加分项"工具超时或程序重启后恢复任务"的免费实现。

### 1.5 thread_id ＝ 存档槽位号

同一个数据库可以存很多对话的档，`thread_id` 就是对话的唯一编号。同一个 `thread_id` 的消息自动接上上文；不同的互不干扰。我们用 `task:{任务id}` 做编号。

### 1.6 State（状态）＝ 流水线上的传送带

中间件之间传递的数据袋子，里面最重要的就是 `messages`（全部对话消息）。中间件可以往袋子里放东西（返回 dict 就是"更新袋子"）。

---

## 2. 最小可运行版本：30 行跑起来

先不管赛题的七大要求，让一个能查发票的 Agent 活起来。

### 2.1 装依赖

```bash
pip install langchain langchain-openai langgraph langgraph-checkpoint-sqlite
```

> 注意：`langgraph-checkpoint-sqlite` 很容易被漏装（名字和 langgraph 很像），缺了它第 5 节会报 `ModuleNotFoundError`。

### 2.2 代码（demo.py）

```python
from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

# ① 定义一个工具：就是一个普通函数 + @tool 装饰器 + docstring
@tool
def query_invoices(company: str, period: str) -> dict:
    """查询某公司某月份的发票列表。docstring 很重要：模型靠它决定什么时候用这个工具。"""
    return {"invoices": [
        {"id": "INV-007", "amount": 36000, "purpose": None},   # purpose=None = 缺用途
        {"id": "INV-008", "amount": 1200, "purpose": "办公用品"},
    ]}

# ② 组装 agent
llm = ChatOpenAI(
    model="你的模型名",
    base_url="https://xxx/v1",        # 赛事统一 Key 的地址，拿到后填这里
    api_key="你的key",
    temperature=0.2,                   # 财税场景要稳定，温度调低
)

agent = create_agent(llm, tools=[query_invoices])

# ③ 对话
result = agent.invoke(
    {"messages": [{"role": "user", "content": "查一下星辉贸易9月的发票，有没有缺信息的？"}]},
)
print(result["messages"][-1].content)   # 最后一条就是给用户的回复
```

### 2.3 运行时发生了什么（重点理解这段）

```text
1. 你的问题进入 agent loop
2. 模型看到消息 + 工具清单（query_invoices 的名字和 docstring）
3. 模型输出：我要调 query_invoices(company="星辉贸易", period="2026-09")
4. 框架执行函数，拿到发票数据，作为"工具结果消息"塞回对话
5. 模型看到数据，发现 INV-007 的 purpose 是 None
6. 模型生成回答："9月共2张发票，其中 INV-007（3.6万元）缺用途字段..."
7. 没有更多工具要调 → loop 结束，回复给你
```

理解了这 7 步，整个赛题二的"统一运行"你就已经会了——剩下的一切都是往这个循环上加保险。

---

## 3. 加保存功能：工具返回"三态回执"

### 3.1 为什么不能直接写个保存函数完事

赛题背景里点名的头号失败就是**"说已保存却没有真正保存"**。解法不是求模型"别说谎"，而是让保存这个动作**只能通过一个受控函数**发生，函数返回什么状态，世界就是什么状态：

### 3.2 代码

```python
import hashlib, json

def _idem_key(company_id: str, target_id: str, patch: dict) -> str:
    """幂等键：同一个公司+同一条记录+同样的修改内容 = 唯一键。
    内容相同 → 键相同 → 数据库能认出'这条已经存过了'。"""
    raw = f"{company_id}|{target_id}|{json.dumps(patch, sort_keys=True, ensure_ascii=False)}"
    return hashlib.sha256(raw.encode()).hexdigest()

@tool
def save_record(target_id: str, patch: dict) -> dict:
    """保存对发票/业务记录的修改。返回三态：success / processing / failed。"""
    company_id = get_current_company()          # 从当前会话取，见第 5 节
    key = _idem_key(company_id, target_id, patch)

    # ① 幂等检查：这条是不是已经保存过？
    if hit := db.get_success_by_idem(company_id, key):
        return {"status": "success", "receipt_id": hit.receipt_id,
                "duplicate": True}              # 重复提交 → 返回第一次的回执，不重复入库

    # ② 写前校验（对象存在吗？内容合法吗？属于本公司吗？）
    if err := pre_check(company_id, target_id, patch):
        return {"status": "failed", "error_code": err.code, "detail": err.msg}

    # ③ 单事务落库，生成回执号
    receipt = db.apply_patch(company_id, target_id, patch, key)
    return {"status": "success", "receipt_id": receipt.id, "patch_applied": patch}
```

### 3.3 初学者需要建立的直觉

- **模型永远不直接碰数据库**。它只能调 `save_record`，保存的一切规则写在这个函数里。模型再怎么"想"乱来，也没有别的入口——这就是"模型和规则的边界"。
- **回执号是关键道具**。第 7 节会讲怎么强制模型"没拿到回执号就不许说已保存"。

---

## 4. 让系统记得住事：checkpointer 和 thread_id

### 4.1 加两行代码

```python
from langgraph.checkpoint.sqlite import SqliteSaver

checkpointer = SqliteSaver.from_conn_string("data/checkpoint.db")

agent = create_agent(llm, tools=[...], checkpointer=checkpointer)

# 每次对话带上编号：
result = agent.invoke(
    {"messages": [...]},
    config={"configurable": {"thread_id": "task:1001"}},
)
```

### 4.2 这解决了什么

| 场景 | 没有 checkpointer | 有 checkpointer |
| --- | --- | --- |
| 用户第二轮说"那把它补上" | 模型不知道"它"是谁 | 自动接上第一轮的对话 |
| 用户第二天回来继续 | 一切从零开始 | 读档，接着聊 |
| 保存到一半程序崩了 | 状态丢失 | 读档，从断点继续 |

**多轮检查场景（赛题场景三）靠的就是这个**：`thread_id` 不变，什么时候回来都能续上。

---

## 5. 让上下文自动带上"企业、待办、记忆"：第一个自研中间件

### 5.1 问题

模型每轮只看到"对话记录"。但赛题要求它时刻知道：现在是哪家公司？哪个月？还有哪些事没办完？这个用户有什么偏好？

### 5.2 做法：每次调模型前，偷偷把资料夹进 system prompt

```python
from langchain.agents.middleware import AgentMiddleware

class ScopeMiddleware(AgentMiddleware):
    """检查站A：每次模型说话前，把'当前处境'注入 system prompt。"""

    def wrap_model_call(self, request, handler):
        ctx = current_scope()                       # 公司/用户/任务（会话开始时设置）
        todo = db.get_pending_items(ctx.task_id)    # 未完成事项
        mems = memory.list_active(ctx.company_id)   # 这家公司的记忆（实时查库！）
        rules = rules_db.active_rules()             # 只取启用状态的规则文档

        request = request.override(system_message=render_prompt(
            BASE_PROMPT, ctx, todo, mems, rules))
        return handler(request)                     # 继续流水线
```

然后注册进 agent：

```python
agent = create_agent(llm, tools=[...], checkpointer=checkpointer,
                     middleware=[ScopeMiddleware()])
```

### 5.3 运行时发生了什么

```text
用户："上次那事怎么样了"
  → wrap_model_call 触发：查出 task:1001 的待办 = ["INV-007 缺用途，等用户补充"]
  → system prompt 里多了一段："【当前任务】...【未完成事项】INV-007 缺用途..."
  → 模型看到的其实是很丰富的上下文，自然能答："还差 INV-007 的用途，您补一下？"
```

两个设计决定（答辩可以讲）：

1. **每轮实时查库，不缓存**。用户刚改了记忆，下一轮立刻生效（赛题要求"支持查看、更正和删除"，且更正后仍能正确处理）。
2. **待办事项放进"永不裁剪"的位置**。就算对话很长上下文被压缩，没办完的事也不会丢。

---

## 6. 让每一步都留下记录：审计中间件

```python
class AuditMiddleware(AgentMiddleware):
    """检查站C：每个工具调用前后记一笔。"""

    def wrap_tool_call(self, request, handler):
        t0 = time.monotonic()
        result = handler(request)                 # 执行工具
        db.log_tool_call(
            tool_name=request.tool_call["name"],  # 调了什么
            args=request.tool_call["args"],       # 传了什么参数
            result_summary=str(result)[:500],     # 结果摘要
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        return result                             # 原样返回，不做任何修改
```

这个中间件**只记录、不干预**。它支撑的是赛题"过程可检查"和提交物里的"操作记录"页面——`app.py` 里的"操作记录"页本质就是把这些日志渲染成表格。

---

## 7. 让它不说谎：回复守门中间件

### 7.1 问题

模型可能在数据没保存成功时说"已保存"（幻觉），或者在保存失败时含糊其辞。

### 7.2 做法：模型回复之后、给你看之前，检查一遍

```python
import re
from langchain_core.messages import AIMessage

class ReceiptGuardMiddleware(AgentMiddleware):
    """检查站B：回复里说'已保存'，就必须有回执号撑腰。"""

    def after_model(self, state, runtime):
        msg = state["messages"][-1]               # 模型刚生成的回复
        if not isinstance(msg, AIMessage):
            return None
        if re.search(r"已保存|保存成功", msg.content):
            if not db.has_receipt_this_turn():    # 本轮真有成功的回执吗？
                fixed = msg.content + "\n（更正：该修改尚未完成，请以操作记录页为准）"
                # 关键：用同一个 id 替换这条消息（LangGraph 按 id 替换，换了 id 会变成追加）
                return {"messages": [AIMessage(id=msg.id, content=fixed)]}
        return None
```

### 7.3 初学者最容易踩的坑（已替你踩过）

`after_model` 返回 `{"messages": [新消息]}` 时，**新消息必须带原消息的 `id`**。id 不同的话，框架会当作"新增一条消息"而不是"替换"，用户会看到模型说了两遍话。

---

## 8. 让它说话像人：去 AI 味中间件

赛题把"自然表达"在挑战和基础任务里各写了一遍，是明确的评分点。思路和第 7 节一样：**不靠 prompt 求模型，靠代码检查**。

```python
BAD_PATTERNS = [
    r"^好的[，!！]", r"很高兴为您", r"希望对您有帮助",
    r"如有疑问.*随时", r"^当然",
]

class StyleMiddleware(AgentMiddleware):
    def after_model(self, state, runtime):
        msg = state["messages"][-1]
        if not isinstance(msg, AIMessage):
            return None
        text = msg.content
        for p in BAD_PATTERNS:                    # 规则1：删套话
            text = re.sub(p, "", text).strip()
        text = merge_short_bullets(text)          # 规则2：少于3个信息点的列表合并成句子
        text = result_first(text)                 # 规则3：把结论句挪到开头（简单版：句序调整）
        if text != msg.content:
            return {"messages": [AIMessage(id=msg.id, content=text)]}
        return None
```

改写前后对比（这个例子可以直接放进 Demo）：

```text
模型初稿：
"好的！收到您的需求。经过查询，我为您找到了以下信息：9月星辉贸易共有发票12张，
银行收支8笔。希望以上信息对您有帮助，如有疑问请随时告诉我！"

处理输出：
"9月星辉贸易有12张发票、8笔收支，金额都能对上。"
```

信息一点没少，字数减半。剩余规则（不确定直说、不重复用户原话等）照这个模式往清单里加即可。

---

## 9. 内置中间件：两行配置拿两个加分项

```python
from langchain.agents.middleware import (
    SummarizationMiddleware,      # 加分项1：长对话自动摘要
    ToolCallLimitMiddleware,      # 防止模型死循环调工具烧钱
    HumanInTheLoopMiddleware,     # 保存前人工确认（可选开关）
)

agent = create_agent(
    llm, tools=ALL_TOOLS,
    middleware=[
        ScopeMiddleware(),
        SummarizationMiddleware(model=cfg.small_model,
                                max_tokens_before_summary=6000,
                                messages_to_keep=8),   # 超过阈值自动把早期对话压成摘要
        ToolCallLimitMiddleware(exceed="error"),       # 工具调用超限直接报错，防失控
        AuditMiddleware(),
        ReceiptGuardMiddleware(),
        StyleMiddleware(),
    ],
    checkpointer=checkpointer,
)
```

重启恢复（加分项 3）不用写任何代码——checkpointer 自动存档，程序重启后用同一个 `thread_id` 调用即可续上；工具是否重复保存由第 3 节的幂等键兜底。

---

## 10. 初学者常见坑 FAQ

| 症状 | 原因 | 解法 |
| --- | --- | --- |
| `ModuleNotFoundError: langgraph.checkpoint.sqlite` | 漏装 `langgraph-checkpoint-sqlite` | `pip install langgraph-checkpoint-sqlite` |
| 模型不调工具，直接瞎编答案 | 工具的 docstring 写得不清楚 | docstring 写"什么时候该用我"，这是给模型看的说明书 |
| 模型说两遍一样的话 | `after_model` 改写时换了 message id | 新消息必须用 `AIMessage(id=原id, ...)` |
| 改了记忆，下一轮还按旧的说 | 把记忆缓存进了全局变量 | 每轮在中间件里实时查库 |
| 工具传参总是错的 | 参数类型复杂（如 dict 套 dict） | 工具参数尽量用简单类型；复杂对象改成 JSON 字符串参数 |
| 对话越来越贵越来越慢 | 消息历史无限增长 | 上 `SummarizationMiddleware` |
| SQLite 报 `database is locked` | Streamlit 多线程共用连接 | 连接加 `check_same_thread=False`，写操作用同一把锁 |
| 模型 temperature 高导致每次答案不一样 | 默认温度不适合确定性场景 | `temperature=0.2` 或更低 |

---

## 附录 A：术语速查

| 术语 | 一句话解释 |
| --- | --- |
| agent loop | 模型↔工具反复循环，直到事情办完 |
| tool | 你写的 Python 函数，模型可以请求调用它 |
| `@tool` | 把普通函数变成工具的装饰器；docstring 是模型的说明书 |
| middleware | 流水线检查站，在模型说话/调工具前后拦截 |
| `wrap_model_call` | 钩子：调模型前（注入上下文） |
| `wrap_tool_call` | 钩子：执行工具前后（记日志） |
| `after_model` | 钩子：模型回复后（检查改写） |
| checkpointer | 游戏存档：每步自动落盘 |
| thread_id | 存档槽位号：同一编号自动接续对话 |
| interrupt | 暂停键：agent 停下来等人确认，再继续 |
| 幂等键 | "同一件事的指纹"：重复提交能被认出来 |
| 回执号 | 保存成功的凭证，没它不许说"已保存" |

## 附录 B：文件长什么样（最终形态）

```
tax-agent-harness/
├── app.py               # Streamlit：对话 / 记忆管理 / 规则库 / 操作记录 四个页面
├── harness/
│   ├── agent.py         # create_agent 组装（第 2、9 节的代码都在这）
│   ├── tools.py         # query_invoices / save_record 等（第 3 节）
│   ├── middlewares.py   # Scope / Audit / ReceiptGuard / Style（第 5-8 节）
│   ├── memory.py        # 记忆表的增删改查
│   ├── rules.py         # 规则文档 + 版本 + 检索
│   └── audit.py         # 操作日志
├── data/
│   ├── harness.db       # 业务数据（模拟）
│   └── checkpoint.db    # 对话存档
└── tests/               # 30 个测试用例
```

每个文件对应本文的一节，从上往下写就是完整作品。

## 附录 C：与深度版文档的关系

本文是"怎么写代码"；《赛题二-完整系统设计》是"为什么这样设计"（评分标准怎么对、三个场景怎么串、答辩讲什么）；《财税Agent-Harness系统设计》是"企业级完整论证"（答辩被追问深度时引用）。三层文档，一套设计。
