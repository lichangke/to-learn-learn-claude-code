#!/usr/bin/env python3
# Harness：循环本身，就是模型第一次真正接触外部世界的入口。
"""
s01_agent_loop.py - Agent Loop（智能体循环）

一个 AI Coding Agent 的核心秘密，浓缩成一个模式就是：

    while stop_reason == "tool_use":
        response = LLM(messages, tools)
        execute tools
        append results

    +----------+      +-------+      +---------+
    |   User   | ---> |  LLM  | ---> |  Tool   |
    |  prompt  |      |       |      | execute |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+
                          (loop continues)

这就是最核心的循环：把工具执行结果继续喂回模型，
直到模型自己决定停止。真实生产环境中的 Agent，
通常会在这一层之上继续叠加策略控制、钩子机制和生命周期管理。

这个示例的运行流程如下：

1. 加载环境变量，并创建 Anthropic client。
2. 启动一个简单的 REPL，把用户输入累计到 `history`。
3. 将完整对话历史和工具定义一起发送给模型。
4. 如果模型返回 `tool_use`，就执行对应的 shell 命令。
5. 把命令输出包装成 `tool_result`，再追加回对话历史。
6. 使用更新后的历史再次调用模型。
7. 只有当模型返回普通回答，而不是继续调用工具时，循环才结束。
"""

import os
import subprocess

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

# 某些兼容 Anthropic 协议的提供商，认证方式和官方接口略有差异。
# 如果配置了自定义 base URL，这里会顺手清理一个可能残留的认证环境变量，
# 避免它干扰这些兼容服务。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# client 和 model id 在程序启动时初始化一次，后续每一轮对话循环都复用它们。
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# system prompt 用来告诉模型：它现在扮演什么角色，以及需要采取行动时，
# 应优先使用什么工具。
SYSTEM = f"You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain."

# 工具 schema 会在每次请求模型时一并发送过去。
# 模型本身并不会真的执行工具，它只会返回符合该 schema 的结构化调用请求。
TOOLS = [{
    "name": "bash",
    "description": "Run a shell command.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]


def run_bash(command: str) -> str:
    # 这个示例会先拦截几类明显危险的命令，再决定是否交给本地 shell。
    # 真实 Agent 的安全策略通常会比这里严格得多。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        # `subprocess.run(...)` 就是“模型意图”通向“真实世界动作”的桥。
        # 我们同时捕获 stdout 和 stderr，这样模型才能看到命令到底发生了什么，
        # 并据此决定下一步。
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=True, timeout=120)
        # 工具输出会重新进入模型上下文，所以这里要限制长度，
        # 避免某些高噪声命令把上下文撑爆。
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        # 超时也作为普通文本结果返回给模型，让模型可以感知失败并调整策略。
        return "Error: Timeout (120s)"


# -- 核心模式：不断调用工具，直到模型自己决定停止 --
def agent_loop(messages: list):
    # `messages` 是完整的运行中对话历史。
    # 它会随着用户输入、assistant 回复、tool_result 不断增长。
    while True:
        # 第 1 步：把完整历史和可用工具列表发给模型，
        # 让模型判断下一步该做什么。
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        # 第 2 步：把 assistant 的原始返回完整保存下来。
        # 这里很重要，因为返回内容里可能同时包含文本和工具调用。
        messages.append({"role": "assistant", "content": response.content})

        # 第 3 步：如果这次返回里没有工具调用，说明循环结束。
        # 最终回答此时已经写进 `messages` 里了。
        if response.stop_reason != "tool_use":
            return

        # 第 4 步：执行本轮 assistant 返回中的所有工具调用。
        # 一次响应里可能不止一个 `tool_use` 块。
        results = []
        for block in response.content:
            if block.type == "tool_use":
                # 把命令打印到终端，方便人类观察 Agent 正在做什么。
                print(f"\033[33m$ {block.input['command']}\033[0m")
                output = run_bash(block.input["command"])
                # 终端里只打印一个简短预览给人看；
                # 真正传给模型的是 `run_bash` 返回的完整结果（已在函数内截断）。
                print(output[:200])
                # 每个 tool_result 都必须带上原始 tool_use_id，
                # 这样 API 才能把“工具结果”和“工具请求”正确对应起来。
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output})

        # 第 5 步：把工具结果作为一个新的 user turn 追加回去。
        # 这正是 Agent Loop 最关键的技巧：
        # 工具执行结果重新进入模型上下文，模型因此能够“看到”自己动作带来的效果。
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    # `history` 会在这个 REPL 会话中持续存在，
    # 所以模型可以保留同一轮终端会话里的上下文。
    history = []
    while True:
        try:
            # 从终端读取下一条用户请求。
            query = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        # 输入空字符串，或显式输入退出命令时，结束示例程序。
        if query.strip().lower() in ("q", "exit", ""):
            break

        # 先把用户新输入存入历史，再把控制权交给 agent_loop。
        history.append({"role": "user", "content": query})
        agent_loop(history)

        # `agent_loop(...)` 返回时，history 的最后一项应当是
        # assistant 在这一轮中的最终回答，这里把其中的文本块打印出来。
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
