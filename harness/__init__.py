"""财税 Agent Harness —— 赛题二参赛作品包。

分层：
    tools.py     唯一读写入口（权限、五项校验、幂等、三态回执）
    policy.py    政策即代码（版本与阈值不进模型）
    rules.py     知识库检索（只查现行版本）
    memory.py    长期记忆 + 任务状态
    context.py   上下文装配与预算裁剪
    style.py     去 AI 味确定性管线
    middlewares.py  Scope / Audit / Expression 三个中间件
    agent.py     create_agent 接线
    cli.py       命令行操作入口
"""

__version__ = "0.1.0"
