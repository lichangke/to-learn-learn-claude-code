#!/usr/bin/env python3
# Harness：后台执行层，让模型把长耗时命令先丢出去，宿主程序等待时它还能继续推进别的步骤。
"""
s08_background_tasks.py - 后台任务（Background Tasks）

前面的示例里，模型一旦发起工具调用，通常就要原地等待工具执行结束，
然后才能拿到结果继续思考。这样做虽然简单，但遇到长耗时命令时会有两个问题：

1. 整个 Agent Loop 被这个命令“卡住”，模型不能同时处理别的事情。
2. 哪怕只是跑测试、构建、抓日志这种慢任务，也要占用当前这一轮交互。

这个示例展示的就是一种更接近真实 Agent 系统的做法：

- 快速工具继续同步执行
- 长耗时命令改为后台线程执行
- 主线程不阻塞，继续和模型推进后续步骤
- 后台任务完成后，再把结果通过“通知队列”注入回模型上下文

可以把整个流程理解成两条并行通道：

    主线程（Agent Loop）                  后台线程（任务执行）
    +-------------------------+          +-------------------------+
    | 读取 messages           |          | 接收 command            |
    | 先清空通知队列          |          | subprocess.run(...)     |
    | [调用 LLM]              | <------+ | 写回 tasks 状态         |
    | 执行同步工具            |          | enqueue(完成通知)       |
    | 把 tool_result 写回历史 |          +-------------------------+
    +-------------------------+

按时间线看，更像这样：

    Agent ----[启动任务 A]----[启动任务 B]----[继续别的工作]----
                 |                |
                 v                v
              [A 在后台跑]     [B 在后台跑]
                 |                |
                 +------ 通知队列 ------> 下一次 LLM 调用前注入结果

最关键的一点是：

    “后台任务的结果，不是立刻打断模型；
     而是在下一次进入模型前，由宿主程序统一补进上下文。”

这意味着：

- 宿主程序负责并发执行和状态保存
- 模型只需要学会两件事：
  - 用 `background_run` 把慢任务发出去
  - 用 `check_background` 主动查询，或者等宿主把完成通知带回来
"""

import os
import subprocess
import threading
import uuid
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

# 某些兼容 Anthropic 协议的服务会使用自定义 base URL，
# 这时认证变量的约定可能与官方环境不完全一致。
# 如果检测到用户配置了自定义地址，就顺手移除一个可能造成干扰的认证变量，
# 避免请求带着不合适的凭据发出去。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 整个示例始终围绕当前工作目录运行：
# 1. 文件工具只允许访问这个目录内部
# 2. shell / 后台任务也都在这个目录下执行
WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# system prompt 只做两件事：
# 1. 告诉模型自己当前扮演的是一个 coding agent
# 2. 明确提醒：遇到长耗时命令时，优先使用 background_run
SYSTEM = f"You are a coding agent at {WORKDIR}. Use background_run for long-running commands."


# -- BackgroundManager：后台任务状态表 + 完成通知队列 --
# 这是本文件真正新增的核心组件。
# 它不直接参与模型推理，而是替模型维护“对话外部”的并发执行状态。
class BackgroundManager:
    def __init__(self):
        # tasks 保存每个后台任务的完整状态。
        # 结构大致如下：
        # {
        #   task_id: {
        #       "status": "running" / "completed" / "timeout" / "error",
        #       "result": "...",
        #       "command": "..."
        #   }
        # }
        self.tasks = {}

        # _notification_queue 只存“已经完成、等待通知模型”的结果预览。
        # 它和 tasks 的区别是：
        # - tasks 是长期状态表，可供 check_background 随时查询
        # - queue 是一次性通知盒子，主线程取走后就清空
        self._notification_queue = []

        # 后台线程和主线程会同时读写通知队列，
        # 所以这里需要一把锁，保证 append / drain 的原子性。
        self._lock = threading.Lock()

    def run(self, command: str) -> str:
        """启动后台线程，并立刻返回 task_id。"""
        # 每个后台任务先生成一个短 task_id，方便模型和人类在终端里引用。
        task_id = str(uuid.uuid4())[:8]

        # 任务一创建，就先登记为 running。
        # 这样哪怕线程还没跑完，check_background 也能查到它已经存在。
        self.tasks[task_id] = {"status": "running", "result": None, "command": command}

        # 关键点：真正执行 subprocess 的工作交给 daemon 线程。
        # 主线程不会等待这里结束，因此 background_run 能“秒回”。
        thread = threading.Thread(
            target=self._execute, args=(task_id, command), daemon=True
        )
        thread.start()

        # 返回给模型的是“任务已启动”的确认，而不是任务结果。
        return f"Background task {task_id} started: {command[:80]}"

    def _execute(self, task_id: str, command: str):
        """后台线程入口：执行命令、保存结果、把完成通知塞进队列。"""
        try:
            # 这里和同步版 run_bash 很像，但它发生在独立线程中。
            # 也就是说：命令在跑，主线程 meanwhile 还能继续进行别的 LLM 循环。
            r = subprocess.run(
                command,
                shell=True,
                cwd=WORKDIR,
                capture_output=True,
                text=True,
                timeout=300,
            )
            output = (r.stdout + r.stderr).strip()[:50000]
            status = "completed"
        except subprocess.TimeoutExpired:
            # 后台任务超时，不抛出到主线程；而是把失败状态当作任务结果记录下来。
            output = "Error: Timeout (300s)"
            status = "timeout"
        except Exception as e:
            # 任何异常都被转成字符串结果，避免后台线程悄悄崩掉、主线程却毫无感知。
            output = f"Error: {e}"
            status = "error"

        # 任务跑完后，先把“完整状态”写回任务表。
        self.tasks[task_id]["status"] = status
        self.tasks[task_id]["result"] = output or "(no output)"

        # 再额外投递一份“通知摘要”到队列里。
        # 这里故意只放 500 字符预览，避免下一轮注入模型时把上下文撑得太大。
        with self._lock:
            self._notification_queue.append(
                {
                    "task_id": task_id,
                    "status": status,
                    "command": command[:80],
                    "result": (output or "(no output)")[:500],
                }
            )

    def check(self, task_id: str = None) -> str:
        """查询单个后台任务，或列出全部后台任务。"""
        # 传了 task_id，就返回单个任务的详细状态。
        if task_id:
            t = self.tasks.get(task_id)
            if not t:
                return f"Error: Unknown task {task_id}"
            return f"[{t['status']}] {t['command'][:60]}\n{t.get('result') or '(running)'}"

        # 不传 task_id，就生成总览清单，方便模型快速决定要不要继续等待/检查。
        lines = []
        for tid, t in self.tasks.items():
            lines.append(f"{tid}: [{t['status']}] {t['command'][:60]}")
        return "\n".join(lines) if lines else "No background tasks."

    def drain_notifications(self) -> list:
        """取出并清空所有待注入模型的后台完成通知。"""
        # 这是通知队列的“消费端”。
        # 主线程在每次调用 LLM 前都会来这里把已完成任务捞走。
        with self._lock:
            notifs = list(self._notification_queue)
            self._notification_queue.clear()
        return notifs


# 全局只维护一个后台任务管理器实例。
# 整个 REPL 会话中的后台任务，都共享这份状态。
BG = BackgroundManager()


# -- 工具实现层 --
# 除了 background_run / check_background 是新增的，
# 其余文件工具和同步 shell 工具仍然沿用前面示例的思路。
def safe_path(p: str) -> Path:
    # 先把相对路径解析到工作区内的绝对路径。
    path = (WORKDIR / p).resolve()

    # 如果解析后逃出了工作区，就直接拒绝。
    # 这是文件读写工具最基础的边界保护。
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    # 同步 bash 适合短命令：
    # 模型调用它时，当前这一轮会等待命令完成。
    # 如果命令很慢，就应该改用 background_run。
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

        # 工具输出最终还要重新进入模型上下文，所以这里仍然做长度截断。
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int = None) -> str:
    try:
        # 先过安全路径检查，再读取文件。
        lines = safe_path(path).read_text().splitlines()

        # limit 允许模型只看前几行，减少大文件读取的上下文成本。
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)

        # 先补齐父目录，避免模型每次写新文件都得额外创建目录。
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        c = fp.read_text()

        # 这里继续使用“精确匹配一次替换”的最小编辑策略，
        # 便于模型清楚地知道自己修改的是哪一段文本。
        if old_text not in c:
            return f"Error: Text not found in {path}"
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# 工具分发表：把模型请求的工具名路由到本地 Python 函数。
# 这里最值得注意的新增项是：
# - background_run：只负责“启动”后台线程，不等待结果
# - check_background：读取后台任务状态表
TOOL_HANDLERS = {
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "background_run": lambda **kw: BG.run(kw["command"]),
    "check_background": lambda **kw: BG.check(kw.get("task_id")),
}

# TOOLS 是发给模型看的“可调用能力说明书”。
# schema 本身没有变化魔法，真正的关键仍然是：
# 1. 模型按 schema 生成 tool_use
# 2. 本地 harness 按 TOOL_HANDLERS 执行
# 3. 再把 tool_result 喂回去形成闭环
TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command (blocking).",
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
        "name": "background_run",
        "description": "Run command in background thread. Returns task_id immediately.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "check_background",
        "description": "Check background task status. Omit task_id to list all.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
        },
    },
]


def agent_loop(messages: list):
    # 这里仍然是熟悉的 Agent Loop，
    # 但比前面多了一个非常关键的新步骤：
    # “每次调用 LLM 之前，先把后台完成通知补进上下文”。
    while True:
        # 第 1 步：先消费通知队列。
        # 这样模型在本轮思考前，就能知道有哪些后台任务已经结束。
        notifs = BG.drain_notifications()
        if notifs and messages:
            notif_text = "\n".join(
                f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs
            )

            # 这里把后台结果伪装成一条 user 消息注入历史。
            # 目的不是模拟真实用户输入，而是给模型一个稳定、统一的结果入口。
            messages.append(
                {
                    "role": "user",
                    "content": f"<background-results>\n{notif_text}\n</background-results>",
                }
            )

            # 紧跟一条 assistant acknowledgement，表示模型“已经知晓这些结果”。
            # 这和前面示例里某些宿主注入消息的做法类似，本质上是在补齐会话结构。
            messages.append({"role": "assistant", "content": "Noted background results."})

        # 第 2 步：带着更新后的上下文调用模型。
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )

        # 第 3 步：先把 assistant 原始响应完整记入历史。
        # 无论里面包含文本块还是 tool_use 块，这一步都不能省。
        messages.append({"role": "assistant", "content": response.content})

        # 第 4 步：如果没有工具调用，说明模型已经准备好在这一轮直接收尾。
        if response.stop_reason != "tool_use":
            return

        # 第 5 步：逐个执行本轮工具，并收集 tool_result。
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    # 对普通工具来说，这里会同步拿到结果。
                    # 对 background_run 来说，这里只会拿到“任务已启动”的确认字符串。
                    output = (
                        handler(**block.input) if handler else f"Unknown tool: {block.name}"
                    )
                except Exception as e:
                    # 仍然把错误压平成字符串回传给模型，而不是让主循环直接异常退出。
                    output = f"Error: {e}"

                # 给终端里的人类一个简短预览，便于观察当前动作。
                print(f"> {block.name}: {str(output)[:200]}")

                # 把工具返回值封装成 tool_result，并带回原始 tool_use_id。
                # 这样模型下一轮就能准确把“工具请求”和“工具结果”对应起来。
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": str(output)}
                )

        # 第 6 步：把本轮所有工具结果作为一个新的 user turn 追加回去。
        # 这一步仍然是 Agent Loop 的核心闭环。
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    # 一个最小 REPL：
    # - history 跨多轮保留
    # - 每次读入用户问题后，交给 agent_loop 自主运行
    history = []
    while True:
        try:
            query = input("\033[36ms08 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        # 空输入 / q / exit 都表示结束示例程序。
        if query.strip().lower() in ("q", "exit", ""):
            break

        history.append({"role": "user", "content": query})
        agent_loop(history)

        # agent_loop 返回时，history 最后一项通常就是 assistant 的最终回答。
        # 这里沿用前面示例的打印方式：只把文本块打印到终端。
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
