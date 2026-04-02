#!/usr/bin/env python3
# Harness：工具分发层，让模型从“只能调 shell”扩展到“可调多种工具”。
"""
s02_tool_use.py - Tools（工具扩展）

这个示例最重要的观察点是：

    s01 里的 Agent Loop 基本完全没变，
    只是新增了：
    1. 更多工具定义（TOOLS）
    2. 一个“工具名 -> Python处理函数”的分发表（TOOL_HANDLERS）

也就是说，Agent 的“会不会用更多能力”并不一定靠改循环，
很多时候只是给循环提供更多可调用的外部动作。

    +----------+      +-------+      +----------------------+
    |   User   | ---> |  LLM  | ---> | Tool Dispatch        |
    |  prompt  |      |       |      | {                    |
    +----------+      +---+---+      |   bash: run_bash     |
                          ^          |   read_file: run_read|
                          |          |   write_file: ...    |
                          +----------+   edit_file: ...     |
                          tool_result| }                    |
                                     +----------------------+

本文件的运行流程可以理解为：

1. 加载环境变量，初始化 `Anthropic client`、模型 ID、工作目录。
2. 定义一组工具函数，例如执行命令、读文件、写文件、局部替换文件内容。
3. 定义 `TOOLS`，把这些工具的 schema 告诉模型。
4. 定义 `TOOL_HANDLERS`，把模型返回的工具名映射到本地 Python 函数。
5. REPL 收到用户输入后，将其追加到 `history`。
6. `agent_loop(...)` 调用模型；模型可能返回普通文本，也可能返回一个或多个 `tool_use`。
7. 如果有 `tool_use`，程序根据工具名找到对应 handler 执行，并收集结果。
8. 再把这些结果包装成 `tool_result` 送回模型，让模型基于执行结果继续思考。
9. 只有当模型不再请求工具时，循环才结束，并输出最终回答。

和 s01 相比，这个版本新增的核心抽象只有一句话：

    “循环不变，工具变多；工具变多之后，再加一个分发器把名字路由到实现。”
"""

import os
import subprocess
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

# 某些兼容 Anthropic API 的服务在自定义 base URL 下，
# 认证方式可能和官方环境变量约定略有不同。
# 如果检测到自定义 base URL，这里顺手清掉一个可能造成干扰的 token 变量，
# 避免请求被错误的认证头影响。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# `WORKDIR` 是当前 Agent 允许活动的工作区根目录。
# 后面的文件工具都会以它为基准做路径拼接和安全校验。
WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# system prompt 告诉模型：它正在一个具体目录中扮演 coding agent，
# 应优先“调用工具解决问题”，而不是长篇解释。
SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. Act, don't explain."


def safe_path(p: str) -> Path:
    # 所有文件读写都先经过这里，把“用户/模型传入的相对路径”
    # 解析成工作区内的绝对路径。
    path = (WORKDIR / p).resolve()
    # 如果解析后的真实路径已经逃逸出工作区，就直接拒绝。
    # 这是最基础也最关键的沙箱边界：模型只能碰 WORKDIR 里面的文件。
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    # 先做一层非常粗粒度的危险命令拦截。
    # 这里只是演示用途，真实生产环境通常会更严格。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        # 真正执行 shell 命令，并把工作目录固定在 WORKDIR。
        # capture_output=True 让 stdout/stderr 都能回传给模型。
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        # 模型需要看到执行结果，才能决定下一步是否继续调用工具。
        out = (r.stdout + r.stderr).strip()
        # 限制输出长度，避免超长日志把上下文窗口撑爆。
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        # 超时不抛异常给上层，而是转成普通文本结果回给模型。
        # 这样模型能“知道这次尝试失败了”，并改用别的策略。
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int = None) -> str:
    try:
        # 先做路径安全检查，再读取文件内容。
        text = safe_path(path).read_text()
        lines = text.splitlines()
        # `limit` 用于只读取前 N 行，常见于“先看文件开头确认结构”。
        # 如果文件太长，就在尾部补一个提示，告诉模型还有多少行未展示。
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        # 再做一次总长度截断，避免单次 tool_result 太大。
        return "\n".join(lines)[:50000]
    except Exception as e:
        # 工具错误统一转成字符串，而不是让整个 agent loop 崩掉。
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        # 写文件前先确保父目录存在。
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        # 返回简短成功信息，让模型知道写入已经完成。
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text()
        # 这个编辑工具使用“精确文本替换”模型：
        # 只有当 old_text 在文件中出现时才执行一次替换。
        # 这种方式实现简单，也方便模型做小范围补丁。
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# -- 分发表：把模型返回的工具名，路由到本地真正执行的 Python 函数 --
# 模型只会说：“我想调用 read_file / edit_file ...”
# 真正负责落地执行的是这里。
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

# `TOOLS` 是发给模型看的“工具菜单 + 参数 schema”。
# 它决定了模型“知道有哪些工具能用、每个工具该传什么参数”。
# 但这里仍然只是声明，不会自动执行；执行仍要靠上面的 `TOOL_HANDLERS`。
TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
]


def agent_loop(messages: list):
    # 这个循环和 s01 基本一样：
    # 调模型 -> 看是否要工具 -> 执行工具 -> 把结果喂回去 -> 再调模型
    while True:
        # 第 1 步：把完整历史、system prompt、工具列表一起发给模型。
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        # 第 2 步：先把 assistant 的原始返回存入历史。
        # 这很重要，因为 response.content 里可能混合了文本块和 tool_use 块。
        messages.append({"role": "assistant", "content": response.content})

        # 第 3 步：如果这轮没有请求任何工具，就说明模型已经准备好直接回答。
        if response.stop_reason != "tool_use":
            return

        # 第 4 步：如果模型请求了工具，就逐个执行，并收集结果。
        results = []
        for block in response.content:
            if block.type == "tool_use":
                # 根据工具名找处理函数，这一步就是“工具分发”。
                handler = TOOL_HANDLERS.get(block.name)
                # 把模型给出的参数 block.input 解包给 handler。
                # 如果工具名不存在，则返回 Unknown tool，避免程序直接报错。
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                # 给终端里的人类一个简短预览，方便观察 agent 当前在做什么。
                print(f"> {block.name}: {output[:200]}")
                # `tool_use_id` 必须和原始请求对应上，这样 API 才知道
                # 这个 tool_result 是在回复哪一次工具调用。
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})

        # 第 5 步：把所有工具结果作为一个新的 user turn 追加到历史里。
        # 这就是 Agent Loop 的关键闭环：
        # 模型提出动作 -> 程序执行动作 -> 执行结果重新进入模型上下文。
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    # `history` 是整个 REPL 会话的累计上下文。
    # 每一轮用户输入、assistant 返回、tool_result 都会继续留在这里。
    history = []
    while True:
        try:
            # 从终端读取下一条用户请求。
            query = input("\033[36ms02 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        # 空输入 / q / exit 都视为结束会话。
        if query.strip().lower() in ("q", "exit", ""):
            break

        # 先把用户输入追加到历史，再交给 agent loop 处理。
        history.append({"role": "user", "content": query})
        agent_loop(history)

        # agent_loop 返回后，history 最后一项应是 assistant 的最终回复。
        # 这里把其中的文本块打印到终端给用户看。
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
