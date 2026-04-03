#!/usr/bin/env python3
# Harness: 上下文隔离：保持模型思路清晰
"""
s04_subagent.py - Subagents（子代理）

这一节要解决的问题是：

    当主 Agent 的上下文越来越长时，
    怎样把一个子任务“拆出去单独做”，
    又不把子任务里产生的大量中间过程塞回主上下文？

这份示例给出的答案是：引入一个 `task` 工具。

当父 Agent 觉得某件事适合委托时，它不亲自继续在当前会话里展开，
而是调用 `task(prompt=...)`，由宿主程序启动一个“子 Agent 循环”。
这个子 Agent 有 3 个关键特征：

1. 它的 `messages` 从空白开始，只收到父 Agent 给它的任务描述
2. 它和父 Agent 共享同一个工作目录，所以仍然能读写同一批文件
3. 它最终只把“总结结果”返回给父 Agent，而不是把整个思考和工具轨迹带回来

于是就形成了下面这条执行链：

    Parent agent                     Subagent
    +------------------+             +------------------+
    | messages=[...]   |             | messages=[]      |  <-- 全新上下文
    |                  |  dispatch   |                  |
    | tool: task       | ---------->| while tool_use:  |
    |   prompt="..."   |            |   call tools     |
    |   description="" |            |   append results |
    |                  |  summary   |                  |
    |   result = "..." | <--------- | return last text |
    +------------------+             +------------------+
              |
    父上下文保持干净，只收到子任务的压缩结果。
    子 Agent 内部产生的中间消息在返回后直接丢弃。

如果说：

- `s03` 重点解决的是“模型怎样显式维护 todo / 计划”
- 那么 `s04` 重点解决的就是“模型怎样把一部分工作隔离出去做”

最值得记住的一句话是：

    进程/会话隔离，本质上就带来了上下文隔离。
"""

import os
import subprocess
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

# 某些兼容 Anthropic 协议的服务会要求自定义 base URL。
# 一旦启用了自定义网关，默认的认证变量可能反而会干扰请求，
# 所以这里沿用前几节的做法：如果检测到自定义 base URL，就顺手清掉一个可能冲突的 token。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 当前 Agent 和子 Agent 都共享同一个工作目录。
# 这意味着它们虽然“消息上下文”彼此隔离，但“文件系统视角”是一致的。
WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# 父 Agent 的 system prompt 重点是：在适合时使用 `task` 工具委派工作。
# 子 Agent 的 system prompt 则更聚焦：接到任务后独立完成，并把结果总结回来。
SYSTEM = f"You are a coding agent at {WORKDIR}. Use the task tool to delegate exploration or subtasks."
SUBAGENT_SYSTEM = f"You are a coding subagent at {WORKDIR}. Complete the given task, then summarize your findings."


# -- Tool implementations shared by parent and child --
def safe_path(p: str) -> Path:
    # 所有文件类工具都先经过这里，把相对路径解析成工作区内的绝对路径。
    path = (WORKDIR / p).resolve()
    # 如果路径逃逸出工作目录，就直接拒绝，避免 Agent 访问到工作区之外的内容。
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    # 这里只做一个最基础的危险命令拦截，防止示例运行时误执行明显高风险操作。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        # 命令仍然是在共享工作目录里执行，所以父子 Agent 对 shell 的观察结果是一致的。
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        # 截断超长输出，避免把过多内容塞回模型上下文。
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int = None) -> str:
    try:
        # 先校验路径安全，再读取文件。
        lines = safe_path(path).read_text().splitlines()
        # limit 允许模型“先看前几行试探一下”，避免一次性读太多内容。
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        # 写文件前确保父目录存在，简化模型的操作负担。
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text()
        # 这里故意采用“精确替换一次”的简单编辑策略，方便教学，也降低误改范围。
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# 工具分发表。
# 无论是父 Agent 直接调基础工具，还是子 Agent 在自己的循环里调基础工具，
# 最终都会路由到这些本地 Python 函数上执行。
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

# 子 Agent 可用的工具列表。
# 它只能使用基础文件 / shell 工具，不能再继续调用 `task` 去无限套娃。
# 也就是说，这个例子里的子 Agent 是“只干活、不再继续分包”的一层结构。
CHILD_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
]


# -- Subagent: fresh context, filtered tools, summary-only return --
def run_subagent(prompt: str) -> str:
    # 子 Agent 的消息历史从零开始。
    # 父 Agent 不会把自己之前那一大串对话原样复制过来，只给它一条“任务说明”。
    sub_messages = [{"role": "user", "content": prompt}]

    # 给子 Agent 的工具循环加一个硬上限，避免异常情况下无限循环。
    for _ in range(30):
        response = client.messages.create(
            model=MODEL, system=SUBAGENT_SYSTEM, messages=sub_messages,
            tools=CHILD_TOOLS, max_tokens=8000,
        )

        # 无论这轮返回的是文本还是工具调用，都先完整记入子 Agent 自己的上下文。
        sub_messages.append({"role": "assistant", "content": response.content})

        # 如果这轮没有继续请求工具，就说明子 Agent 准备收尾并给出总结了。
        if response.stop_reason != "tool_use":
            break

        # 否则就逐个执行子 Agent 请求的工具，并把执行结果整理成 tool_result 回灌给它。
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)[:50000]})

        # 注意：这些结果只追加到 sub_messages 里，不会污染父 Agent 的 messages。
        sub_messages.append({"role": "user", "content": results})

    # 子 Agent 完成后，只把“最后一轮 assistant 文本块”拼起来返回给父 Agent。
    # 子上下文中的中间工具调用、文件读取细节、试探过程，都会在函数结束后被丢弃。
    return "".join(b.text for b in response.content if hasattr(b, "text")) or "(no summary)"


# -- Parent tools: base tools + task dispatcher --
# 父 Agent 比子 Agent 多一个 `task` 工具。
# 它并不直接在模型内部“开线程”，而是让宿主 Python 程序显式调用 `run_subagent(...)`
# 去跑一个新的、隔离的 Agent 循环。
PARENT_TOOLS = CHILD_TOOLS + [
    {"name": "task", "description": "Spawn a subagent with fresh context. It shares the filesystem but not conversation history.",
     "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}, "description": {"type": "string", "description": "Short description of the task"}}, "required": ["prompt"]}},
]


def agent_loop(messages: list):
    while True:
        # 第 1 步：父 Agent 基于自己当前完整历史，决定下一步是直接回答还是调用工具。
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=PARENT_TOOLS, max_tokens=8000,
        )

        # 第 2 步：先把 assistant 原始响应完整存起来，保证上下文连续。
        messages.append({"role": "assistant", "content": response.content})

        # 第 3 步：如果没有工具调用，说明父 Agent 已经可以直接给出最终答复。
        if response.stop_reason != "tool_use":
            return

        # 第 4 步：如果有工具请求，就逐个执行。
        results = []
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "task":
                    # `task` 是这一节的核心：
                    # 父 Agent 不自己在当前上下文里展开，而是把子任务 prompt 交给子 Agent。
                    desc = block.input.get("description", "subtask")
                    print(f"> task ({desc}): {block.input['prompt'][:80]}")
                    output = run_subagent(block.input["prompt"])
                else:
                    # 如果不是 `task`，就还是按普通基础工具执行。
                    handler = TOOL_HANDLERS.get(block.name)
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"

                # 终端里打印一个预览，方便人类观察当前发生了什么。
                print(f"  {str(output)[:200]}")

                # 把执行结果封装成 tool_result，回写给父 Agent 的下一轮。
                # 对父 Agent 来说，子 Agent 的存在被“压缩”成了一次工具调用结果。
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})

        # 第 5 步：把本轮工具结果当作一个新的 user turn 追加回去，
        # 然后继续 while True，让父 Agent 基于这些结果进入下一轮决策。
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    # `history` 只保存父 Agent 这一侧的会话历史。
    # 子 Agent 的 `sub_messages` 只存在于 `run_subagent()` 的局部作用域中。
    history = []
    while True:
        try:
            # 读取用户在终端里的下一条输入。
            query = input("\033[36ms04 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        # 空输入 / q / exit 都视为结束会话。
        if query.strip().lower() in ("q", "exit", ""):
            break

        # 先把用户问题写入父历史，再交给父 Agent loop 处理。
        history.append({"role": "user", "content": query})
        agent_loop(history)

        # `agent_loop` 返回时，history 的最后一条通常是父 Agent 的最终响应。
        # 这里把其中的文本块打印出来给用户看。
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
