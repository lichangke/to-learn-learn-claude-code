# to-learn-learn-claude-code

这是一个基于 [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 搭建的学习型仓库，用来系统梳理和实践 Agent Harness / Claude Code 风格智能体工程。

仓库当前目标不是“直接交付一个完整产品”，而是围绕智能体工程中的关键能力，边学边拆、边读边练，逐步沉淀自己的理解、实验代码和学习笔记。

## 学习目标

通过这个仓库，重点学习和实践以下内容：

- Agent Loop：智能体如何接收任务、规划步骤并持续推进
- Tool Use：模型如何调用工具与外部环境交互
- Todo / Task Write：如何把复杂问题拆成可执行任务
- Sub Agent：如何让多个子代理协作
- Skill Loading：如何加载和组织技能
- Context Compact：如何控制上下文长度与压缩信息
- Task System：如何定义稳定的任务执行机制
- Background Tasks：如何处理后台任务
- Agent Teams：如何设计多智能体团队
- Team Protocols：团队协作时的协议与约束
- Autonomous Agents：更高自治度的智能体执行模式

## 仓库结构

当前仓库以学习骨架为主，下面的结构已忽略 `.gitignore` 中的文件和目录：

```text
.
├─ agents/          # 练习脚本、实验代码、最小可运行示例
├─ learn_docs/      # 个人学习记录、补充资料、阶段总结
├─ .env.example     # 环境变量模板
├─ LICENSE          # 开源许可证
├─ README.md        # 项目说明
└─ requirements.txt # Python 依赖
```

说明：`docs/`、`venv/`、`chat.md` 和 `.env` 已在 `.gitignore` 中忽略，因此没有展示在上面的结构里。

## 学习总结表

后续可以在这里持续维护学习总结文档、对应说明和相关代码：

| 总结文档 | 说明 | 代码 |
| --- | --- | --- |
| [从 Agent Loop 到 Agent Harness：一篇讲透智能体工程的架构、流程与设计取舍](learn_docs/从AgentLoop到AgentHarness-一篇讲透智能体工程的架构流程与设计取舍.md) | 这篇总览把 s01-s11 串成一套完整的 Agent Harness，并补上设计思路、架构思路与工程取舍。 | [agents/s00_full.py](agents/s00_full.py) |
| [智能体循环入门：为什么一个 while 循环就能让 AI 真正开始干活](learn_docs/智能体循环入门-为什么一个while循环就能让AI真正开始干活.md) | s01 的关键不是会调工具，而是能把工具结果重新喂回模型形成闭环。 | [agents/s01_agent_loop.py](agents/s01_agent_loop.py) |
| [工具调用拆解：为什么给 Agent 加能力，不用重写循环](learn_docs/工具调用拆解-为什么给Agent加能力不用重写循环.md) | s02 的重点不是工具变多，而是用分发表把新增能力稳稳接进原有闭环。 | [agents/s02_tool_use.py](agents/s02_tool_use.py) |
| [待办清单驱动执行：为什么 Agent 做复杂任务时需要持续更新计划](learn_docs/待办清单驱动执行-为什么Agent做复杂任务时需要持续更新计划.md) | s03 的重点不是多一个 todo 工具，而是把任务进度变成可持续维护的结构化状态。 | [agents/s03_todo_write.py](agents/s03_todo_write.py) |
| [子代理拆分任务：为什么要用上下文隔离保护 Agent 的思路清晰](learn_docs/子代理拆分任务-为什么要用上下文隔离保护Agent的思路清晰.md) | s04 的关键不是多一个 task 工具，而是把探索过程隔离到子上下文里，只把结果摘要带回主循环。 | [agents/s04_subagent.py](agents/s04_subagent.py) |
| [技能按需加载：为什么不要把所有知识都塞进 System Prompt](learn_docs/技能按需加载-为什么不要把所有知识都塞进SystemPrompt.md) | s05 的重点不是多一个 load_skill 工具，而是把知识注入从默认全量改成先挂目录、再按需加载。 | [agents/s05_skill_loading.py](agents/s05_skill_loading.py) |
| [上下文压缩设计：为什么 Agent 想长期工作，必须学会分层遗忘](learn_docs/上下文压缩设计-为什么Agent想长期工作必须学会分层遗忘.md) | s06 的关键不是简单删历史，而是把活跃上下文、摘要记忆和磁盘归档拆成三层，让 Agent 能长期工作。 | [agents/s06_context_compact.py](agents/s06_context_compact.py) |
| [任务系统设计：为什么 Agent 不能只靠聊天记录推进长期工作](learn_docs/任务系统设计-为什么Agent不能只靠聊天记录推进长期工作.md) | s07 的关键不是把 Todo 换个存储位置，而是把任务状态和依赖关系迁到对话外部，让 Agent 在压缩或重启后还能继续推进。 | [agents/s07_task_system.py](agents/s07_task_system.py) |
| [后台任务设计：为什么 Agent 遇到慢命令时不该原地干等](learn_docs/后台任务设计-为什么Agent遇到慢命令时不该原地干等.md) | s08 的关键不是多一个后台工具，而是把等待长命令完成这件事从模型思考链路里拆出去，让 Agent 能边等边继续推进。 | [agents/s08_background_tasks.py](agents/s08_background_tasks.py) |
| [智能体团队协作设计：为什么 Agent 真正像团队一样工作，离不开持久队友和文件邮箱](learn_docs/智能体团队协作设计-为什么Agent真正像团队一样工作离不开持久队友和文件邮箱.md) | s09 的关键不是多开几个模型，而是给每个队友稳定身份、状态和收件箱，让协作脱离一次性调用。 | [agents/s09_agent_teams.py](agents/s09_agent_teams.py) |
| [团队协议设计：为什么多智能体协作不能只靠发消息](learn_docs/团队协议设计-为什么多智能体协作不能只靠发消息.md) | s10 的关键不是多几个握手工具，而是把协作约定变成带 request_id 的结构化协议。 | [agents/s10_team_protocols.py](agents/s10_team_protocols.py) |
| [自主代理设计：为什么 Agent 空闲时不该只是等下一条指令](learn_docs/自主代理设计-为什么Agent空闲时不该只是等下一条指令.md) | s11 的关键不是多一个 idle 工具，而是让 teammate 在空闲时通过邮箱和任务板自己找到下一份工作。 | [agents/s11_autonomous_agents.py](agents/s11_autonomous_agents.py) |



## 环境准备

建议使用 Python 虚拟环境：

```bash
python -m venv venv
```

Windows:

```bash
venv\Scripts\activate
```

macOS / Linux:

```bash
source venv/bin/activate
```

安装依赖：

```bash
pip install -r requirements.txt
```

复制环境变量模板：

```bash
cp .env.example .env
```

如果你在 Windows PowerShell 中操作，也可以使用：

```powershell
Copy-Item .env.example .env
```

然后编辑 `.env`，填写你的模型配置，例如：

- `ANTHROPIC_API_KEY`
- `MODEL_ID`
- `ANTHROPIC_BASE_URL`（可选，兼容 Anthropic 协议的服务商时使用）


## 致谢

本仓库的学习主线参考并受益于：

- [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)

如果你也在学习 Agent 工程，欢迎把这个仓库当作自己的实验场，边阅读、边实现、边沉淀。
