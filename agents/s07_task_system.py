#!/usr/bin/env python3
# Harness：任务系统，把跨轮次目标持久化到对话外部，避免上下文压缩后任务状态丢失。
"""
s07_task_system.py - Tasks（任务系统）

这个示例要解决的问题是：
普通对话式 Agent 很容易“只记得当前聊天窗口里的事”，
一旦上下文被压缩、清理，之前规划过的任务、依赖关系、完成状态就可能一起丢掉。

这里的做法不是把所有任务状态继续塞进对话历史，
而是把任务单独落盘到工作区的 `.tasks/` 目录里：

    .tasks/
      task_1.json  {"id":1, "subject":"...", "status":"completed", ...}
      task_2.json  {"id":2, "blockedBy":[1], "status":"pending", ...}
      task_3.json  {"id":3, "blockedBy":[2], "blocks":[], ...}

这样一来，任务就变成了“对话外部状态”：
1. 会话再长，任务信息也不会因为压缩上下文而消失
2. Agent 可以在不同轮对话之间继续读取、更新同一批任务
3. 任务之间的依赖关系也能稳定保留下来

这个脚本的核心可以理解成两层：

第一层：TaskManager
- 负责把任务保存成 JSON 文件
- 负责读取、修改、列出任务
- 负责维护依赖关系 `blockedBy` / `blocks`

第二层：agent loop
- 用户发出请求
- 模型决定是否调用任务工具或普通文件工具
- Python 宿主程序执行工具
- 工具结果写回消息历史
- 模型再根据结果继续规划下一步

其中最值得注意的设计点是：
“任务状态不再依赖对话历史本身，而是依赖磁盘中的任务文件。”
这就是它能跨越上下文压缩持续工作的关键。
"""

import json
import os
import subprocess
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

# 加载本地环境变量，方便从 .env 中读取模型 ID、网关地址等配置。
load_dotenv(override=True)

# 如果当前走的是兼容 Anthropic 协议的自定义网关，
# 这里移除一个可能冲突的认证环境变量，避免请求被错误配置干扰。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# WORKDIR 是整个脚本的工作根目录：
# 1. system prompt 会告诉模型当前在哪个目录工作
# 2. 文件读写工具只能在这个目录范围内活动
# 3. `.tasks/` 任务数据也保存在这里
WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
TASKS_DIR = WORKDIR / ".tasks"

# 这里的 system prompt 很简短，只强调两件事：
# - 你是当前目录下的 coding agent
# - 你可以使用任务工具来规划和跟踪工作
SYSTEM = f"You are a coding agent at {WORKDIR}. Use task tools to plan and track work."


# -- TaskManager：把任务以 JSON 文件形式持久化，并维护依赖图 --
class TaskManager:
    def __init__(self, tasks_dir: Path):
        # 初始化时先确保 `.tasks/` 目录存在。
        self.dir = tasks_dir
        self.dir.mkdir(exist_ok=True)

        # `_next_id` 不是简单从 1 开始，而是扫描现有任务文件后续号。
        # 这样即使脚本重启，也不会和旧任务 ID 冲突。
        self._next_id = self._max_id() + 1

    def _max_id(self) -> int:
        """
        扫描当前任务目录，找出已经存在的最大任务 ID。

        文件命名约定是 `task_<id>.json`，
        所以这里直接从文件名中拆出编号即可。
        """
        ids = [int(f.stem.split("_")[1]) for f in self.dir.glob("task_*.json")]
        return max(ids) if ids else 0

    def _load(self, task_id: int) -> dict:
        """
        按任务 ID 读取单个任务文件。

        这是所有读操作的基础入口：
        `get()`、`update()` 以及依赖同步都会先走这里。
        """
        path = self.dir / f"task_{task_id}.json"
        if not path.exists():
            raise ValueError(f"Task {task_id} not found")
        return json.loads(path.read_text())

    def _save(self, task: dict):
        """
        把任务字典写回对应的 JSON 文件。

        任务文件名由任务自身的 `id` 决定，
        因此调用方只要保证 `task["id"]` 正确即可。
        """
        path = self.dir / f"task_{task['id']}.json"
        path.write_text(json.dumps(task, indent=2))

    def create(self, subject: str, description: str = "") -> str:
        """
        创建一个新任务，并立刻持久化到 `.tasks/`。

        新任务的默认状态：
        - `status = pending`
        - 没有前置依赖 `blockedBy`
        - 也没有后续依赖 `blocks`
        """
        task = {
            "id": self._next_id,
            "subject": subject,
            "description": description,
            "status": "pending",
            "blockedBy": [],
            "blocks": [],
            "owner": "",
        }
        self._save(task)
        self._next_id += 1
        return json.dumps(task, indent=2)

    def get(self, task_id: int) -> str:
        """
        读取单个任务详情，并以格式化 JSON 字符串返回给模型。
        """
        return json.dumps(self._load(task_id), indent=2)

    def update(
        self,
        task_id: int,
        status: str = None,
        add_blocked_by: list = None,
        add_blocks: list = None,
    ) -> str:
        """
        更新任务状态或依赖关系。

        这是任务系统最核心的入口，因为它同时承担三类修改：
        1. 改状态，例如 pending -> in_progress -> completed
        2. 给当前任务补充前置依赖 `blockedBy`
        3. 给当前任务补充它会阻塞的后续任务 `blocks`

        其中第 3 点还会顺手做“双向同步”：
        如果 A 的 `blocks` 里加了 B，
        那么 B 的 `blockedBy` 里也会自动补上 A。
        """
        task = self._load(task_id)

        if status:
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Invalid status: {status}")
            task["status"] = status

            # 一个任务被标记完成后，它就不应该再继续阻塞别人。
            # 所以这里会扫描其它任务，把它们 `blockedBy` 中的当前任务 ID 移除。
            if status == "completed":
                self._clear_dependency(task_id)

        if add_blocked_by:
            # 使用 set 去重，避免重复追加同一个前置依赖。
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))

        if add_blocks:
            # 当前任务声明“我会阻塞哪些任务”。
            task["blocks"] = list(set(task["blocks"] + add_blocks))

            # 关键点：这里会把“正向关系”同步成“反向关系”。
            # 也就是 A.blocks += [B] 后，同时写入 B.blockedBy += [A]。
            # 这样依赖图无论从哪一端看，都是一致的。
            for blocked_id in add_blocks:
                try:
                    blocked = self._load(blocked_id)
                    if task_id not in blocked["blockedBy"]:
                        blocked["blockedBy"].append(task_id)
                        self._save(blocked)
                except ValueError:
                    # 如果被依赖的任务并不存在，这里选择静默跳过。
                    # 这个示例更关注机制演示，没有做更严格的事务回滚。
                    pass

        self._save(task)
        return json.dumps(task, indent=2)

    def _clear_dependency(self, completed_id: int):
        """
        当某个任务完成后，把它从其它任务的 `blockedBy` 列表中移除。

        可以把这一步理解成：
        “解锁所有曾经被这个任务卡住的后续任务”。
        """
        for f in self.dir.glob("task_*.json"):
            task = json.loads(f.read_text())
            if completed_id in task.get("blockedBy", []):
                task["blockedBy"].remove(completed_id)
                self._save(task)

    def list_all(self) -> str:
        """
        列出所有任务的简要状态，方便模型快速浏览当前任务面板。

        这里返回的不是完整 JSON，而是更适合阅读的摘要文本，例如：
        `[>] #2: 实现登录 (blocked by: [1])`
        """
        tasks = []
        for f in sorted(self.dir.glob("task_*.json")):
            tasks.append(json.loads(f.read_text()))
        if not tasks:
            return "No tasks."

        lines = []
        for t in tasks:
            marker = {
                "pending": "[ ]",
                "in_progress": "[>]",
                "completed": "[x]",
            }.get(t["status"], "[?]")
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            lines.append(f"{marker} #{t['id']}: {t['subject']}{blocked}")
        return "\n".join(lines)


# 全局共享一个任务管理器实例，供任务工具直接调用。
TASKS = TaskManager(TASKS_DIR)


# -- 基础工具实现：文件、命令行，以及任务系统的本地执行层 --
def safe_path(p: str) -> Path:
    """
    把路径限制在工作区内部，防止模型通过相对路径越界访问文件。

    例如模型如果尝试传入 `../../secret.txt`，
    `resolve()` 后就会发现它已经不在 WORKDIR 里，随后直接报错。
    """
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    """
    执行 shell 命令。

    这是最通用、也最危险的工具，所以这里只做了一个很轻量的黑名单拦截，
    避免明显危险的命令直接运行。它不是完整沙箱，只是教学示例中的最低限度保护。
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
    读取文件内容。

    `limit` 允许模型先只看前几行，适合做文件结构侦察；
    最终结果仍会统一截断，避免超长文件内容挤占太多上下文。
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
    写入文件。

    如果父目录不存在，会一并创建。
    返回值只给出简短确认信息，告诉模型写入是否成功。
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
    对文件做一次精确文本替换。

    这个工具的定位不是复杂语义编辑器，
    而是让模型能表达“把这段旧文本替换成新文本”这种直接操作。
    如果旧文本找不到，就明确返回错误，而不是猜测性修改。
    """
    try:
        fp = safe_path(path)
        c = fp.read_text()
        if old_text not in c:
            return f"Error: Text not found in {path}"
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# 这是“工具名 -> 本地 Python 实现”的映射表。
# 模型本身不会真的执行工具，它只会返回结构化的 `tool_use` 请求；
# 真正落地执行的是宿主 Python 程序，然后把结果再回写给模型。
TOOL_HANDLERS = {
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "task_create": lambda **kw: TASKS.create(kw["subject"], kw.get("description", "")),
    "task_update": lambda **kw: TASKS.update(
        kw["task_id"],
        kw.get("status"),
        kw.get("addBlockedBy"),
        kw.get("addBlocks"),
    ),
    "task_list": lambda **kw: TASKS.list_all(),
    "task_get": lambda **kw: TASKS.get(kw["task_id"]),
}

# 这是发给模型看的“工具说明书”。
# 它只负责声明有哪些工具、参数长什么样，
# 不负责实际执行；实际执行仍然依赖上面的 TOOL_HANDLERS。
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
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
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
        "name": "task_create",
        "description": "Create a new task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["subject"],
        },
    },
    {
        "name": "task_update",
        "description": "Update a task's status or dependencies.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed"],
                },
                "addBlockedBy": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
                "addBlocks": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "task_list",
        "description": "List all tasks with status summary.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "task_get",
        "description": "Get full details of a task by ID.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
        },
    },
]


def agent_loop(messages: list):
    """
    Agent 的主循环。

    整个执行节奏可以概括成：
    1. 把当前历史发给模型
    2. 如果模型直接回答，就结束这一轮
    3. 如果模型请求调用工具，就执行工具
    4. 把工具结果包装成 `tool_result` 写回历史
    5. 再把更新后的历史发回模型，继续下一轮

    和前面几个示例相比，这里的特殊点在于：
    模型除了可以操作文件、跑命令，还能直接操作 `.tasks/` 中的持久化任务。
    """
    while True:
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )

        # 不论这次返回的是普通文本还是工具调用，都先原样记入历史。
        messages.append({"role": "assistant", "content": response.content})

        # 如果 stop_reason 不是 `tool_use`，说明模型已经给出了最终回答，
        # 当前这轮 agent loop 就可以退出。
        if response.stop_reason != "tool_use":
            return

        # 走到这里说明模型请求了一个或多个工具。
        # 我们需要逐个执行，并把结果收集起来统一回写。
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                except Exception as e:
                    # 这里不让单个工具异常直接炸掉整个对话循环，
                    # 而是把错误作为工具结果返回给模型，让模型自己决定如何恢复。
                    output = f"Error: {e}"

                # 给终端中的人一个简短预览，方便观察 agent 当前在做什么。
                print(f"> {block.name}: {str(output)[:200]}")

                # 关键动作：把工具执行结果封装成 `tool_result`。
                # 下一轮模型会依据这些结果判断后续要不要继续推进任务。
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(output),
                    }
                )

        # Anthropic 工具协议里，工具结果是作为新的 user 消息回写的。
        # 这样下一轮模型就能把它视作“外部世界对我刚才行动的反馈”。
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    # `history` 是整个 REPL 会话级别的完整消息历史。
    # 每次用户输入、assistant 输出、tool_result 回写，都会依次累积在这里。
    history = []
    while True:
        try:
            query = input("\033[36ms07 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        # 空输入、`q`、`exit` 都表示结束会话。
        if query.strip().lower() in ("q", "exit", ""):
            break

        # 先把用户问题放进历史，再交给 agent_loop 推进。
        history.append({"role": "user", "content": query})
        agent_loop(history)

        # agent_loop 返回时，history 的最后一项通常是 assistant 的最终回复。
        # Anthropic SDK 的消息内容一般是 block 列表，因此这里遍历其中的 text block 打印。
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
