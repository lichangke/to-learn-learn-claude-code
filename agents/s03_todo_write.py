#!/usr/bin/env python3
# Harness：规划与进度跟踪层，让模型在自主行动时别“做着做着就忘了计划”。
"""
s03_todo_write.py - TodoWrite（任务规划与进度记录）

如果说：

- `s01` 解决的是“Agent 如何形成最基本的闭环”
- `s02` 解决的是“如何给 Agent 增加更多可调用工具”

那么 `s03` 解决的就是另一个非常实际的问题：

    当任务开始变复杂、步骤开始变多时，
    怎么让模型不仅会做事，还能持续维护自己的任务清单？

这个版本相对 `s02` 的核心新增点有两层：

1. 增加一个 `TodoManager`，专门保存“当前有哪些任务、哪一个在做、哪一些已经完成”
2. 增加一个 `todo` 工具，让模型可以自己更新这份结构化状态

另外，这里还多加了一条“提醒机制”：

    如果模型连续 3 轮都没有更新 todo，
    宿主程序就主动往上下文里塞一个 reminder，
    提醒它把任务清单同步一下。

也就是说，`s03` 不再只是“模型会不会调用工具”，
而是开始进入“模型能不能边做边管理自己的工作流”。

    +----------+      +-------+      +---------+
    |   User   | ---> |  LLM  | ---> | Tools   |
    |  prompt  |      |       |      | + todo  |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+
                                |
                    +-----------+-----------+
                    | TodoManager state     |
                    | [ ] task A            |
                    | [>] task B <- doing   |
                    | [x] task C            |
                    +-----------------------+
                                |
                    if rounds_since_todo >= 3:
                      inject <reminder>

整个运行过程可以概括为：

1. 初始化和 `s02` 类似：加载环境变量、创建 client、定义普通工具。
2. 额外创建一个 `TodoManager`，它是“任务清单的真实状态容器”。
3. 通过 `todo` 工具把“任务更新权”交给模型，但更新规则仍由 Python 代码约束。
4. 每一轮模型都可以像 `s02` 那样调用文件工具或 shell 工具。
5. 如果模型调用 `todo`，就会刷新当前任务状态，并把格式化后的结果回传给模型。
6. 如果模型连续几轮都没更新任务清单，程序就插入提醒，逼它重新关注计划。
7. 最终形成的不是单纯的“工具执行闭环”，而是“工具执行 + 任务跟踪”的双层闭环。

最值得记住的一句话是：

    s02 是“给模型更多手”；
    s03 是“让模型在动手时，别忘了自己正在做哪一步”。
"""

import os
import subprocess
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

# 某些兼容 Anthropic 协议的服务在自定义 base URL 下，
# 认证方式可能和官方默认环境变量约定不同。
# 如果检测到用户配置了自定义 base URL，就顺手移除一个可能造成干扰的认证变量，
# 避免请求被错误的认证头污染。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# `WORKDIR` 是当前 Agent 的工作区根目录。
# 后面的文件读写工具仍然和 `s02` 一样，只允许在这个目录内活动。
WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# system prompt 在这里比 `s02` 多了一层要求：
# 不只是“优先使用工具解决问题”，还要求模型：
# 1. 遇到多步骤任务时，要主动使用 todo 工具规划
# 2. 开始做某一步前，先把它标成 in_progress
# 3. 做完后，再标成 completed
# 也就是说，程序开始显式要求模型“边做边汇报进度”。
SYSTEM = f"""You are a coding agent at {WORKDIR}.
Use the todo tool to plan multi-step tasks. Mark in_progress before starting, completed when done.
Prefer tools over prose."""


# -- TodoManager：模型可写入的结构化任务状态 --
# `s02` 里模型虽然也能“自己记住计划”，但那只是散落在自然语言上下文里。
# 到了 `s03`，我们把“计划/进度”抽成一个独立的、结构化的数据容器，
# 让模型通过 todo 工具显式维护它。
class TodoManager:
    def __init__(self):
        # `items` 保存当前任务清单。
        # 每一项都是形如 {"id": "...", "text": "...", "status": "..."} 的字典。
        self.items = []

    def update(self, items: list) -> str:
        # 每次 todo 工具调用，都会把一整份任务列表传进来。
        # 这里先做校验，再决定是否真正覆盖当前状态。
        if len(items) > 20:
            # 限制任务数量，避免模型一口气塞太多碎任务，导致列表失控。
            raise ValueError("Max 20 todos allowed")
        validated = []
        in_progress_count = 0
        for i, item in enumerate(items):
            # 统一做字段清洗和默认值处理。
            text = str(item.get("text", "")).strip()
            status = str(item.get("status", "pending")).lower()
            item_id = str(item.get("id", str(i + 1)))

            # `text` 必填，否则这条 todo 没有实际意义。
            if not text:
                raise ValueError(f"Item {item_id}: text required")
            # 只允许 3 种状态，避免模型发明出新的状态名。
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {item_id}: invalid status '{status}'")
            if status == "in_progress":
                in_progress_count += 1
            validated.append({"id": item_id, "text": text, "status": status})

        # 限制同一时刻只能有一项任务处于进行中。
        # 这是一个很重要的约束：它逼着模型聚焦当前步骤，而不是同时“假装在做很多事”。
        if in_progress_count > 1:
            raise ValueError("Only one task can be in_progress at a time")

        # 校验通过后，再整体替换内部状态。
        self.items = validated
        # 返回渲染后的任务清单字符串，让模型和人类都能看见当前进度。
        return self.render()

    def render(self) -> str:
        # 如果还没有任何 todo，就返回一个简短占位信息。
        if not self.items:
            return "No todos."
        lines = []
        for item in self.items:
            # 用简洁的符号把 3 种状态可视化：
            # [ ] 待办
            # [>] 进行中
            # [x] 已完成
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}[item["status"]]
            lines.append(f"{marker} #{item['id']}: {item['text']}")
        # 在底部补一个完成度汇总，方便快速看整体进展。
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)


# 全局只维护一个 TodoManager 实例。
# 它相当于这次 REPL 会话中的“共享任务面板”。
TODO = TodoManager()


# -- 工具实现层 --
# 这里的 `safe_path / bash / read / write / edit` 基本延续 `s02` 的设计。
# 真正新增的是最后的 `todo` 工具，它不是操作文件，而是操作“任务状态”。
def safe_path(p: str) -> Path:
    # 先把外部传入的路径解析到工作区内的真实绝对路径。
    path = (WORKDIR / p).resolve()
    # 如果路径逃逸出工作区，则直接拒绝。
    # 这是文件工具最基本的安全边界。
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    # 简单拦截几类明显危险的命令。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        # 在工作区内执行 shell 命令，并同时抓取 stdout/stderr。
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        # 截断过长输出，避免污染上下文窗口。
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        # 超时也作为普通文本结果返回给模型。
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int = None) -> str:
    try:
        # 先确保路径安全，再读取文件内容。
        lines = safe_path(path).read_text().splitlines()
        # 如果设置了 `limit`，就只返回前几行，并补一个剩余行数提示。
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        # 写入前先确保父目录存在。
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        # 返回简短确认信息即可。
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text()
        # 仍然采用“精确文本替换一次”的简单编辑策略。
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# 工具分发表：把模型返回的工具名路由到本地处理函数。
# 和 `s02` 相比，这里唯一的重要新增就是 `"todo": lambda **kw: TODO.update(...)`
# 这意味着：模型现在除了能操作文件和 shell，还能操作“任务清单状态”。
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "todo":       lambda **kw: TODO.update(kw["items"]),
}

# `TOOLS` 是发给模型看的工具菜单。
# 相对 `s02`，新增的 `todo` schema 很关键：
# 它强迫模型以结构化数组的形式汇报任务，而不是随手写一段自然语言计划。
TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "todo", "description": "Update task list. Track progress on multi-step tasks.",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["id", "text", "status"]}}}, "required": ["items"]}},
]


# -- 带“催更提醒”的 Agent Loop --
# `s02` 的循环只关心“要不要继续调用工具”。
# `s03` 的循环除了执行工具，还多维护一个附加状态：
#     rounds_since_todo = 距离上一次更新 todo 已经过去了几轮
# 这让宿主程序可以在模型忘记更新计划时，主动做一点点节奏纠偏。
def agent_loop(messages: list):
    # 会话开始时，默认还没有“长时间未更新 todo”的问题。
    rounds_since_todo = 0
    while True:
        # 第 1 步：和 `s02` 一样，把完整历史 + system prompt + 工具菜单发给模型。
        # 模型会决定这一轮是直接回答，还是继续发起一个或多个工具调用。
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )

        # 第 2 步：先把 assistant 的原始返回完整存进历史。
        # 这样无论响应里混有文本块还是 tool_use 块，都不会丢失上下文。
        messages.append({"role": "assistant", "content": response.content})

        # 第 3 步：如果这轮没有工具调用，说明模型已经准备好结束当前任务。
        if response.stop_reason != "tool_use":
            return

        # 第 4 步：逐个执行本轮的工具请求，并收集 tool_result。
        results = []
        # `used_todo` 用来记录：这一轮模型有没有更新任务清单。
        # 这会影响后面的提醒计数器。
        used_todo = False
        for block in response.content:
            if block.type == "tool_use":
                # 通过分发表找到对应处理函数。
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    # 把模型给出的参数 block.input 解包给 handler。
                    # 对 todo 工具来说，这一步会触发 TodoManager.update(...)。
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                except Exception as e:
                    # 即使工具执行失败，也转成普通字符串结果返回给模型，
                    # 而不是让整个循环崩掉。
                    output = f"Error: {e}"
                # 给终端里的人类一个简短预览，方便观察当前步骤。
                print(f"> {block.name}: {str(output)[:200]}")
                # 把结果包装成 tool_result，稍后统一回写进 messages。
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
                if block.name == "todo":
                    # 只要本轮调用过 todo，就说明模型有维护进度。
                    used_todo = True

        # 第 5 步：根据这一轮是否更新过 todo，刷新“未更新轮数”计数器。
        # - 如果用了 todo，计数器清零
        # - 如果没用，计数器 +1
        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1

        # 第 6 步：如果模型连续 3 轮没碰 todo，就注入一个提醒文本。
        # 注意这里不是强行替模型改 todo，而只是把提醒作为额外上下文塞回去，
        # 让模型在下一轮自行决定如何修正计划。
        if rounds_since_todo >= 3:
            results.insert(0, {"type": "text", "text": "<reminder>Update your todos.</reminder>"})

        # 第 7 步：把工具结果（以及可能的 reminder）作为一个新的 user turn 追加到历史里。
        # 这和 `s02` 的闭环结构一致，只是现在回写的除了 tool_result，
        # 还可能包含宿主程序注入的“流程提醒”。
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    # `history` 保存整个 REPL 会话的完整上下文。
    # 用户输入、assistant 响应、tool_result、todo 更新结果都会累计在这里。
    history = []
    while True:
        try:
            # 从终端读取下一条用户请求。
            query = input("\033[36ms03 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        # 空输入 / q / exit 视为结束会话。
        if query.strip().lower() in ("q", "exit", ""):
            break

        # 先把用户输入追加到历史，再交给 agent loop 处理。
        history.append({"role": "user", "content": query})
        agent_loop(history)

        # agent_loop 返回后，history 最后一项通常是 assistant 的最终回复。
        # 这里把其中的文本块打印出来给用户看。
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
