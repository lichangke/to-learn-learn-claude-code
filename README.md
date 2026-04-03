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
- Worktree Task Isolation：任务隔离与并行开发

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
| [智能体循环入门：为什么一个 while 循环就能让 AI 真正开始干活](learn_docs/智能体循环入门-为什么一个while循环就能让AI真正开始干活.md) | Agent 的关键不是会调工具，而是能把工具结果重新喂回模型形成闭环。 | [agents/s01_agent_loop.py](agents/s01_agent_loop.py) |
| [工具调用拆解：为什么给 Agent 加能力，不用重写循环](learn_docs/工具调用拆解-为什么给Agent加能力不用重写循环.md) | s02 的重点不是工具变多，而是用分发表把新增能力稳稳接进原有闭环。 | [agents/s02_tool_use.py](agents/s02_tool_use.py) |
| [待办清单驱动执行：为什么 Agent 做复杂任务时需要持续更新计划](learn_docs/待办清单驱动执行-为什么Agent做复杂任务时需要持续更新计划.md) | s03 的重点不是多一个 todo 工具，而是把任务进度变成可持续维护的结构化状态。 | [agents/s03_todo_write.py](agents/s03_todo_write.py) |



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
