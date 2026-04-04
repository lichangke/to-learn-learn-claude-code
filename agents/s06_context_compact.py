#!/usr/bin/env python3
# Harness：上下文压缩，用分层记忆清理支持超长会话持续运行。
"""
s06_context_compact.py - Compact（上下文压缩）

这个示例的核心目标是：
让 Agent 在“对话越来越长”的情况下，仍然可以持续工作，而不是被上下文窗口撑爆。

它没有试图“永远记住所有原文”，而是把记忆分成 3 层压缩策略：

    每一轮调用模型前：
    +------------------+
    | Tool call result |
    +------------------+
            |
            v
    [Layer 1: micro_compact]        （静默执行，每轮都跑）
      把很久以前的长工具输出替换成简短占位符
      只保留最近 KEEP_RECENT 个完整工具结果
            |
            v
    [Check: tokens > THRESHOLD?]
       |               |
       no              yes
       |               |
       v               v
    continue    [Layer 2: auto_compact]
                  先把完整历史落盘到 .transcripts/
                  再请求 LLM 生成“可延续工作的摘要”
                  最后用摘要替换整段历史
                        |
                        v
                [Layer 3: compact tool]
                  如果模型主动调用 compact
                  就立刻执行一次和 Layer 2 类似的摘要压缩

可以把整个脚本理解成下面这个闭环：

1. 用户输入被追加到 `history`
2. `agent_loop(history)` 开始运行
3. 每轮调模型前，先做“轻量清理”（Layer 1）
4. 如果估算 token 太多，再做“整段摘要压缩”（Layer 2）
5. 把压缩后的消息列表发给模型
6. 如果模型要调用工具，就执行工具并把结果写回消息历史
7. 如果模型调用的是 `compact`，则在本轮工具执行后立即触发 Layer 3
8. 如果模型给出最终回答而不是继续调用工具，循环结束

这份代码最重要的思想不是“压缩算法多高级”，而是：
Agent 不必死守全部原文，也可以通过分层遗忘来维持长期运行能力。
"""

import json
import os
import subprocess
import time
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

# 加载 .env，便于本地直接通过环境变量配置模型、网关地址等。
load_dotenv(override=True)

# 某些兼容 Anthropic 协议的中转服务会使用自定义 base URL。
# 如果检测到自定义网关，这里顺手移除一个可能与之冲突的认证环境变量，
# 避免请求被错误的 token 配置干扰。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# WORKDIR 既是 system prompt 中告诉模型的“工作目录”，
# 也是文件工具允许访问的根目录边界。
WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# system prompt 很克制，只告诉模型：
# “你是这个目录里的 coding agent，需要用工具完成任务。”
SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks."

# 当估算上下文超过这个阈值时，触发自动整段压缩。
THRESHOLD = 50000
# 完整历史在被摘要替换之前，会先落盘到这里，方便回溯。
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
# 只保留最近多少个 tool_result 的完整内容，其余旧结果会被 Layer 1 压缩。
KEEP_RECENT = 3


def estimate_tokens(messages: list) -> int:
    """
    粗略估算 token 数量。

    这里没有调用真实 tokenizer，而是用“4 个字符约等于 1 个 token”的经验值估算。
    优点是实现简单、运行便宜，足够用来做“是否需要压缩”的近似判断。
    缺点是并不精确，但对这个教学示例已经够用了。
    """
    return len(str(messages)) // 4


# -- Layer 1: micro_compact - replace old tool results with placeholders --
def micro_compact(messages: list) -> list:
    """
    第一层压缩：只动“旧的工具结果”，不动整体对话结构。

    这层压缩的目标很明确：
    旧的 shell 输出、文件内容、日志片段往往很长，但随着对话推进，它们的重要性会下降。
    所以这里不删除消息本身，只把老旧且很长的 `tool_result["content"]`
    替换成一个简短说明，例如：

        [Previous: used read_file]

    这样做的效果是：
    1. 保留“模型曾经调用过什么工具”的轨迹
    2. 尽量降低旧工具输出对上下文窗口的占用
    3. 不改变消息顺序，保持对话结构稳定

    注意：
    - 这个函数会原地修改 `messages` 中的字典内容
    - 同时也返回 `messages`，方便阅读和扩展
    """
    # 第一步：扫描整段消息历史，找出所有 user 侧的 tool_result。
    #
    # 在 Anthropic 的工具调用协议里：
    # - assistant 先返回 tool_use
    # - 宿主程序执行工具
    # - 然后把结果作为一个新的 user 消息回写，其 content 是一个列表，
    #   里面每个元素都是 {"type": "tool_result", ...}
    #
    # 因此这里我们只关心：
    # role == "user" 且 content 是 list 的那些消息。
    tool_results = []
    for msg_idx, msg in enumerate(messages):
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            for part_idx, part in enumerate(msg["content"]):
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    # 记录它在 messages 里的位置，后面需要回头就地修改。
                    tool_results.append((msg_idx, part_idx, part))

    # 如果总共的工具结果还不多，就没必要做微压缩，直接返回。
    if len(tool_results) <= KEEP_RECENT:
        return messages

    # 第二步：为每个 tool_result 找到“它对应的是哪个工具”。
    #
    # tool_result 自己只有 tool_use_id，没有直接保存工具名。
    # 所以要回头扫描之前 assistant 的响应块，把 tool_use_id -> tool_name 建立映射。
    tool_name_map = {}
    for msg in messages:
        if msg["role"] == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    # assistant 侧返回的 response.content 通常是 SDK block 对象，
                    # 所以这里使用属性访问而不是 dict 访问。
                    if hasattr(block, "type") and block.type == "tool_use":
                        tool_name_map[block.id] = block.name

    # 第三步：只保留最近 KEEP_RECENT 个完整工具结果，其余更早的作为“可压缩对象”。
    to_clear = tool_results[:-KEEP_RECENT]
    for _, _, result in to_clear:
        # 只压缩“字符串形式且内容较长”的工具输出。
        # 如果本来就很短，就没必要替换；那点长度节省不值得损失信息。
        if isinstance(result.get("content"), str) and len(result["content"]) > 100:
            tool_id = result.get("tool_use_id", "")
            tool_name = tool_name_map.get(tool_id, "unknown")
            result["content"] = f"[Previous: used {tool_name}]"

    return messages


# -- Layer 2: auto_compact - save transcript, summarize, replace messages --
def auto_compact(messages: list) -> list:
    """
    第二层压缩：当整体上下文太大时，直接把“整段历史”总结成摘要。

    这是比 micro_compact 更激进的一层：
    - Layer 1 只是把旧工具输出压短
    - Layer 2 则是把完整对话先存盘，再让模型生成连续性摘要，
      最后用“摘要消息”替换掉整个原始 messages

    这样做的设计取舍是：
    - 优点：压缩率高，可以把非常长的会话迅速收缩回可控体积
    - 代价：原始逐字细节不再保留在当前上下文里，只能通过磁盘 transcript 回溯
    """
    # 第一步：先把完整历史写到磁盘。
    #
    # 这一步很重要，因为后面一旦用摘要替换 messages，内存里的原始细节就没了。
    # 落盘后的 transcript 相当于“长期档案”，便于人工排查或后续检索。
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(transcript_path, "w") as f:
        for msg in messages:
            # default=str 的作用是：如果消息里有 SDK block 等 JSON 不可序列化对象，
            # 就退化成字符串写入，至少保证 transcript 能保存下来。
            f.write(json.dumps(msg, default=str) + "\n")
    print(f"[transcript saved: {transcript_path}]")

    # 第二步：把当前会话内容转成一段字符串，交给 LLM 来生成连续性摘要。
    #
    # 这里同样做了截断（[:80000]），防止“为了压缩而发出去的原文”本身过大。
    conversation_text = json.dumps(messages, default=str)[:80000]
    response = client.messages.create(
        model=MODEL,
        messages=[{
            "role": "user",
            "content":
                "Summarize this conversation for continuity. Include: "
                "1) What was accomplished, 2) Current state, 3) Key decisions made. "
                "Be concise but preserve critical details.\n\n" + conversation_text,
        }],
        max_tokens=2000,
    )
    summary = response.content[0].text

    # 第三步：直接丢弃旧 messages，换成“摘要后的最小上下文”。
    #
    # 这里返回一个全新的两条消息列表：
    # 1. user 消息：说明已经压缩，并携带 transcript 路径 + 摘要正文
    # 2. assistant 消息：给出一个承接性回复，帮助后续对话自然继续
    #
    # 这也是整个脚本最关键的“记忆重写”动作。
    return [
        {
            "role": "user",
            "content": (
                f"[Conversation compressed. Transcript: {transcript_path}]\n\n{summary}"
            ),
        },
        {
            "role": "assistant",
            "content": "Understood. I have the context from the summary. Continuing.",
        },
    ]


# -- Tool implementations --
def safe_path(p: str) -> Path:
    """
    把传入路径限制在 WORKDIR 内，防止工具越界访问。

    这是所有文件工具共享的第一道安全边界：
    - 先用 `WORKDIR / p` 拼出路径
    - 再 `resolve()` 得到真实绝对路径
    - 最后确认它仍然位于 WORKDIR 内

    如果模型试图用 `../../` 之类的方式跳出工作区，这里会直接报错。
    """
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    """
    执行 shell 命令。

    这是最通用的工具，因此也最需要基础保护：
    - 先做非常粗粒度的危险命令拦截
    - 然后在 WORKDIR 下执行
    - 把 stdout/stderr 合并后回传给模型
    - 对输出长度做截断，避免单次结果把上下文撑爆
    """
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int = None) -> str:
    """
    读取文件。

    `limit` 允许模型只读取前 N 行，常见于“先探查文件结构，再决定是否读全量”。
    最终依然会统一截断到 50000 字符以内，避免巨型文件内容直接撑爆上下文。
    """
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    """
    写文件。

    如果父目录不存在，会自动创建。
    返回值只给简短确认信息，告诉模型写入成功以及写入字节数。
    """
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    """
    做一次“精确文本替换”式编辑。

    这类编辑工具的好处是实现简单，而且模型可以明确表达：
    “把这段旧文本替换成新文本”。
    它不是 AST 级编辑器，也不会自动处理复杂冲突；
    如果 old_text 找不到，就直接返回错误。
    """
    try:
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# 这里是“工具名 -> 本地 Python 实现”的分发表。
# 模型并不会直接执行工具；模型只能返回结构化的 tool_use 请求，
# 真正执行是在宿主 Python 程序里通过这个映射完成的。
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    # `compact` 本身不负责生成摘要，它只相当于一个“信号工具”：
    # 模型一旦调用它，主循环会在本轮工具结果写回后执行 auto_compact。
    "compact":    lambda **kw: "Manual compression requested.",
}

# 这是发给模型看的工具声明列表：
# 它告诉模型“有哪些工具可用、每个工具长什么样、需要什么参数”。
# 但它只负责声明，不负责执行；执行仍然依赖上面的 TOOL_HANDLERS。
TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read file contents.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to file.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace exact text in file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "compact",
        "description": "Trigger manual conversation compression.",
        "input_schema": {
            "type": "object",
            "properties": {
                "focus": {
                    "type": "string",
                    "description": "What to preserve in the summary",
                }
            },
        },
    },
]


def agent_loop(messages: list):
    """
    Agent 的主循环。

    它和前几个示例的骨架是一致的：

        调模型 -> 看是否需要工具 -> 执行工具 -> 写回 tool_result -> 再调模型

    不同点在于，这里在“调模型之前”和“某些工具之后”插入了上下文压缩逻辑。
    所以这份脚本本质上是：
    “普通 agent loop + 三层记忆压缩机制”。
    """
    while True:
        # Layer 1：每次调模型前都先做一次微压缩。
        #
        # 这里虽然没有写成 `messages = micro_compact(messages)`，
        # 但函数内部会原地修改 messages 中的旧 tool_result，因此依然生效。
        micro_compact(messages)

        # Layer 2：如果估算发现当前上下文已经过大，就先整体压缩再继续。
        #
        # `messages[:] = ...` 的写法很关键：
        # 它不是让局部变量 messages 指向一个新列表，
        # 而是“原地替换原列表内容”，这样外部持有同一个 history 引用的地方也会同步看到更新。
        if estimate_tokens(messages) > THRESHOLD:
            print("[auto_compact triggered]")
            messages[:] = auto_compact(messages)

        # 把当前上下文发给模型，让模型决定：
        # - 直接回答
        # - 还是继续调用工具
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )

        # 无论模型是普通回答还是 tool_use，都先原样追加到历史里。
        messages.append({"role": "assistant", "content": response.content})

        # 如果这轮没有工具调用，请求到这里就结束了。
        # 最终回答已经作为最后一个 assistant 消息写进 history。
        if response.stop_reason != "tool_use":
            return

        # 如果进入这里，说明模型请求了一个或多个工具。
        results = []
        # 这个标记用于记录：本轮是否触发了“手动压缩”工具。
        manual_compact = False

        for block in response.content:
            if block.type == "tool_use":
                # `compact` 工具比较特殊：
                # 它不是直接返回真正摘要，而是先回一个简短提示，
                # 再由主循环在本轮 tool_result 回写后统一触发 auto_compact。
                if block.name == "compact":
                    manual_compact = True
                    output = "Compressing..."
                else:
                    handler = TOOL_HANDLERS.get(block.name)
                    try:
                        output = (
                            handler(**block.input)
                            if handler
                            else f"Unknown tool: {block.name}"
                        )
                    except Exception as e:
                        # 这里不让异常炸掉整个 agent loop，
                        # 而是把错误作为普通工具结果回给模型，让模型自行调整策略。
                        output = f"Error: {e}"

                # 给终端里的人一个短预览，方便观察 agent 的动作轨迹。
                print(f"> {block.name}: {str(output)[:200]}")
                # 关键步骤：把工具执行结果包装成 tool_result，并带上原始 tool_use_id。
                # 下一轮模型正是通过这些 tool_result 感知“自己刚刚行动后的世界状态”。
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output),
                })

        # 把这一轮所有工具结果作为一个新的 user turn 回写到历史。
        messages.append({"role": "user", "content": results})

        # Layer 3：如果本轮模型显式调用了 compact，就在工具结果回写后立刻做整段压缩。
        #
        # 之所以放在这里，而不是一进入 compact 就马上压缩，是因为：
        # 我们仍希望这次 compact 调用本身在历史里留下完整痕迹
        # （assistant 发起了 compact，user 返回了 “Compressing...”）。
        if manual_compact:
            print("[manual compact]")
            messages[:] = auto_compact(messages)


if __name__ == "__main__":
    # `history` 是整个 REPL 会话级别的完整上下文。
    # 每一轮用户输入、assistant 响应、tool_result 回写，都会累积在这里。
    history = []
    while True:
        try:
            # 从终端读取下一条用户输入。
            query = input("\033[36ms06 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        # 空输入 / q / exit 都视为结束会话。
        if query.strip().lower() in ("q", "exit", ""):
            break

        # 先把用户问题加入历史，再把控制权交给 agent_loop。
        history.append({"role": "user", "content": query})
        agent_loop(history)

        # agent_loop 返回时，history 的最后一项通常是 assistant 的最终回答。
        #
        # 这里之所以判断 `isinstance(response_content, list)`，
        # 是因为 Anthropic SDK 的 assistant 内容通常是 block 列表，
        # 其中既可能有 text block，也可能有 tool_use block。
        # 主循环返回时，最后一轮应该已经不是 tool_use 了，所以这里把 text 块打印出来。
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
