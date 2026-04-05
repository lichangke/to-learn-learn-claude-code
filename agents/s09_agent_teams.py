#!/usr/bin/env python3
# Harness：团队邮箱协作层，让多个模型以“固定队友”的方式通过文件互相配合。
"""
s09_agent_teams.py - Agent Teams（代理团队）

这个示例把 s04 的“一次性子代理”继续推进成“可长期存在的队友”。
两者最大的差别不在于“会不会调用模型”，而在于生命周期：

    s04 子代理：创建 -> 执行任务 -> 返回总结 -> 销毁
    s09 队友：  创建 -> 工作 -> 空闲等待 -> 再接任务 -> ... -> 关闭

这里的核心设计有 3 层：
1. `.team/config.json`
   持久化保存队伍成员、角色、状态，解决“队里有哪些人”的问题。
2. `.team/inbox/<name>.jsonl`
   给每个成员一个独立收件箱，解决“消息发给谁、谁来收”的问题。
3. `threading.Thread`
   每个队友在独立线程里跑自己的 agent loop，解决“多个成员如何并行工作”的问题。

把整体流程按 lead 视角展开，可以理解成：
1. 人类先把任务交给 lead。
2. lead 决定是自己处理，还是通过 `spawn_teammate(...)` 拉起某个长期队友。
3. lead 或其他队友通过 `send_message(...)` 把消息追加写入目标成员的 JSONL 收件箱。
4. 目标队友在线程循环里调用 `read_inbox(...)`，把自己的收件箱“读出并清空”。
5. 队友把这些新消息追加到自己的上下文里，继续思考、调用工具、必要时再发消息。
6. 当前这一轮任务结束后，队友状态从 `working` 回到 `idle`，等待下次再次被唤醒。

可以把文件结构理解成：

    .team/config.json                   .team/inbox/
    +----------------------------+      +------------------+
    | {"team_name": "default",   |      | alice.jsonl      |
    |  "members": [              |      | bob.jsonl        |
    |    {"name":"alice",        |      | lead.jsonl       |
    |     "role":"coder",        |      +------------------+
    |     "status":"idle"}       |
    |  ]}                        |      send_message("alice", "修复 bug")
    +----------------------------+        -> 追加写入 alice.jsonl

                                        read_inbox("alice")
    spawn_teammate("alice", ...)          -> 逐行解析 JSONL
         |                                -> 读完后把文件清空
         v                                -> 返回消息列表
    Thread: alice             Thread: bob
    +------------------+      +------------------+
    | agent_loop       |      | agent_loop       |
    | status: working  |      | status: idle     |
    | 处理工具与消息     |      | 等待新消息         |
    | status -> idle   |      |                  |
    +------------------+      +------------------+

文件里声明了 5 种消息类型（这里只完整处理了其中一部分）：
+-------------------------+--------------------------------------+
| message                 | 普通点对点消息                        |
| broadcast               | 发给所有队友的广播                    |
| shutdown_request        | 请求优雅关闭（给 s10 铺垫）           |
| shutdown_response       | 对关闭请求的批准 / 拒绝               |
| plan_approval_response  | 对计划审批的批准 / 拒绝               |
+-------------------------+--------------------------------------+

这个示例最值得理解的一点是：
“队友不是一次性函数调用，而是拥有名字、状态、收件箱和上下文的常驻代理。”
"""

import json
import os
import subprocess
import threading
import time
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

# 从 `.env` 加载模型和网关配置，便于本地直接运行示例。
load_dotenv(override=True)

# 如果接的是兼容 Anthropic 协议的自定义网关，官方 token 变量有时会干扰认证，
# 这里沿用前面示例的处理方式：检测到自定义 base URL 时先移除它。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 整个团队都围绕当前工作目录协作：
# 1. lead / teammate 的文件工具都只允许访问这个目录
# 2. `.team` 目录作为“对话之外的持久化状态”，保存成员名册和邮箱文件
WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"

# lead 的 system prompt 只强调两件事：
# 1. 它是团队负责人
# 2. 它可以通过“拉起队友 + 收发邮箱消息”来组织协作
SYSTEM = f"You are a team lead at {WORKDIR}. Spawn teammates and communicate via inboxes."

# 这里先把团队协议里可能出现的消息类型全集声明出来。
# s09 还没有完整用上全部类型，但先把协议边界固定下来，后续章节可以继续扩展。
VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_response",
}


# -- MessageBus：每个成员一个 JSONL 收件箱 --
class MessageBus:
    def __init__(self, inbox_dir: Path):
        # inbox_dir 对应 `.team/inbox`。
        # 其中每个 `<name>.jsonl` 文件就是一个成员的收件箱。
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        # 发消息的本质非常简单：
        # 把一条 JSON 记录追加写入目标成员的 inbox 文件末尾。
        # 这样 lead 和所有 teammate 都共用同一种通信机制。
        if msg_type not in VALID_MSG_TYPES:
            return f"Error: Invalid type '{msg_type}'. Valid: {VALID_MSG_TYPES}"
        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),
        }
        if extra:
            # `extra` 预留给更复杂的协议字段，例如后续章节可能需要的审批 ID 等。
            msg.update(extra)
        inbox_path = self.dir / f"{to}.jsonl"
        with open(inbox_path, "a") as f:
            f.write(json.dumps(msg) + "\n")
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list:
        # 收件箱采用“drain”语义：
        # 一次把所有消息读出来，然后立刻清空文件。
        # 因此 inbox 更像一次性待办队列，而不是长期聊天历史。
        inbox_path = self.dir / f"{name}.jsonl"
        if not inbox_path.exists():
            return []
        messages = []
        for line in inbox_path.read_text().strip().splitlines():
            if line:
                # JSONL 的设计就是“一行一条消息”，所以这里逐行反序列化。
                messages.append(json.loads(line))
        # 读完即清空，避免同一批消息在下一轮又被重复消费。
        inbox_path.write_text("")
        return messages

    def broadcast(self, sender: str, content: str, teammates: list) -> str:
        # 广播不是另一种独立存储结构，
        # 它只是对每个非发送者成员重复调用一次 send(...)。
        count = 0
        for name in teammates:
            if name != sender:
                self.send(sender, name, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"


# 全局消息总线：lead 和所有 teammate 线程都通过它访问 `.team/inbox`。
BUS = MessageBus(INBOX_DIR)


# -- TeammateManager：维护队伍名册、成员状态，以及每个常驻队友线程 --
class TeammateManager:
    def __init__(self, team_dir: Path):
        self.dir = team_dir
        self.dir.mkdir(exist_ok=True)
        self.config_path = self.dir / "config.json"
        # `config.json` 负责“落盘保存成员名册”，
        # `threads` 则只记录当前这个 Python 进程里实际跑起来的线程对象。
        self.config = self._load_config()
        self.threads = {}

    def _load_config(self) -> dict:
        # 优先从磁盘恢复队伍状态；磁盘没有时再创建默认空队伍。
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}

    def _save_config(self):
        # 任何成员状态变化后都立即落盘。
        # 这样即使程序退出，也能从 `.team/config.json` 看见上一次的队伍状态。
        self.config_path.write_text(json.dumps(self.config, indent=2))

    def _find_member(self, name: str) -> dict:
        # 教学示例里成员数通常很少，直接在线性列表里遍历查找即可。
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    def spawn(self, name: str, role: str, prompt: str) -> str:
        # `spawn(...)` 既负责“首次创建新成员”，也负责“重新唤醒已有但空闲的成员”。
        member = self._find_member(name)
        if member:
            # 同名成员已经存在时，不再重复创建配置项；
            # 只允许从 idle / shutdown 重新进入 working。
            if member["status"] not in ("idle", "shutdown"):
                return f"Error: '{name}' is currently {member['status']}"
            member["status"] = "working"
            member["role"] = role
        else:
            # 新成员首次出现时，同时写入名册，并立即标记为工作中。
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save_config()

        # 真正执行队友逻辑的是一个 daemon thread。
        # 线程一旦启动，就会进入自己的 `_teammate_loop(...)` 持续处理消息和工具调用。
        thread = threading.Thread(
            target=self._teammate_loop,
            args=(name, role, prompt),
            daemon=True,
        )
        self.threads[name] = thread
        thread.start()
        return f"Spawned '{name}' (role: {role})"

    def _teammate_loop(self, name: str, role: str, prompt: str):
        # 每个队友都有自己独立的 system prompt 和独立的 messages。
        # 这正是“常驻队友”和“临时子代理”的关键区别：身份稳定、上下文也持续积累。
        sys_prompt = (
            f"You are '{name}', role: {role}, at {WORKDIR}. "
            f"Use send_message to communicate. Complete your task."
        )
        # 队友的第一条 user 消息就是创建它时传入的初始任务。
        messages = [{"role": "user", "content": prompt}]
        # 队友能用的工具比 lead 少一层团队管理能力：
        # 它负责执行工作和收发消息，但不负责整体编排。
        tools = self._teammate_tools()

        # 这里限制最多 50 轮，是教学示例里的保险丝，避免异常情况下无限循环。
        for _ in range(50):
            inbox = BUS.read_inbox(name)
            for msg in inbox:
                # 新来信会被当作新的 user turn 追加进队友上下文。
                # 这里直接序列化成 JSON 字符串，让模型能看到结构化字段。
                messages.append({"role": "user", "content": json.dumps(msg)})
            try:
                response = client.messages.create(
                    model=MODEL,
                    system=sys_prompt,
                    messages=messages,
                    tools=tools,
                    max_tokens=8000,
                )
            except Exception:
                # 线程内部异常不继续向上抛，直接结束该队友本轮工作。
                break

            # 无论返回文本还是 tool_use，都先完整记录到队友自己的上下文。
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason != "tool_use":
                # 模型不再请求工具时，说明该队友当前这一轮工作暂时结束。
                break

            results = []
            for block in response.content:
                if block.type == "tool_use":
                    # 队友侧也走“工具名 -> Python 执行函数”这一路，只是工具集合更小。
                    output = self._exec(name, block.name, block.input)
                    # 打印时带上队友名字，方便在终端里区分多个线程的交错输出。
                    print(f"  [{name}] {block.name}: {str(output)[:120]}")
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(output),
                    })
            # 工具结果回写为新的 user turn，闭合当前这轮小循环。
            messages.append({"role": "user", "content": results})

        member = self._find_member(name)
        if member and member["status"] != "shutdown":
            # 只要不是显式 shutdown，队友完成当前任务后都回到 idle，可被再次唤醒。
            member["status"] = "idle"
            self._save_config()

    def _exec(self, sender: str, tool_name: str, args: dict) -> str:
        # 队友侧工具执行器。
        # 前四个基础工具沿用 s02；新增的能力是“给别人发消息”和“读取自己的收件箱”。
        if tool_name == "bash":
            return _run_bash(args["command"])
        if tool_name == "read_file":
            return _run_read(args["path"])
        if tool_name == "write_file":
            return _run_write(args["path"], args["content"])
        if tool_name == "edit_file":
            return _run_edit(args["path"], args["old_text"], args["new_text"])
        if tool_name == "send_message":
            return BUS.send(sender, args["to"], args["content"], args.get("msg_type", "message"))
        if tool_name == "read_inbox":
            # 队友只能读取自己的 inbox，因此这里直接用 sender 作为收件箱名。
            return json.dumps(BUS.read_inbox(sender), indent=2)
        return f"Unknown tool: {tool_name}"

    def _teammate_tools(self) -> list:
        # 这是暴露给队友看的工具菜单。
        # 它和 lead 的 TOOLS 很像，但少了 `spawn_teammate` / `list_teammates` / `broadcast`
        # 这类团队编排权，体现“lead 负责协调，队友负责执行”的分工。
        return [
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
            {"name": "read_file", "description": "Read file contents.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
            {"name": "write_file", "description": "Write content to file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            {"name": "edit_file", "description": "Replace exact text in file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
            {"name": "send_message", "description": "Send message to a teammate.",
             "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
            {"name": "read_inbox", "description": "Read and drain your inbox.",
             "input_schema": {"type": "object", "properties": {}}},
        ]

    def list_all(self) -> str:
        # 供 `/team` 命令和 lead 的 `list_teammates` 工具复用。
        if not self.config["members"]:
            return "No teammates."
        lines = [f"Team: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        return "\n".join(lines)

    def member_names(self) -> list:
        # 广播时只需要拿到所有成员名字。
        return [m["name"] for m in self.config["members"]]


TEAM = TeammateManager(TEAM_DIR)


# -- 基础工具实现：文件 / 命令能力，作为 lead 与 teammate 共用的执行底座 --
def _safe_path(p: str) -> Path:
    # 所有文件路径都先解析到 WORKDIR 下，再检查是否越界。
    # 这条边界和前面示例一致：模型只能操作当前工作目录里的文件。
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def _run_bash(command: str) -> str:
    # 这里仍然保留一个非常粗粒度的危险命令拦截。
    # 示例重点是代理团队协作，不是在 shell 安全上做完整防护。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(
            command, shell=True, cwd=WORKDIR,
            capture_output=True, text=True, timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def _run_read(path: str, limit: int = None) -> str:
    try:
        # 先做路径校验，再读文件，并可选地截断前 N 行。
        lines = _safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def _run_write(path: str, content: str) -> str:
    try:
        fp = _safe_path(path)
        # 写文件前自动补齐父目录，减少模型额外处理目录不存在的负担。
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"


def _run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = _safe_path(path)
        c = fp.read_text()
        # 这里采用“精确文本替换一次”的最小编辑模型，方便模型做局部补丁。
        if old_text not in c:
            return f"Error: Text not found in {path}"
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# -- lead 侧工具分发表（9 个工具） --
# 这一步负责把“模型想调用的工具名”路由到“本地真正执行的 Python 函数”。
TOOL_HANDLERS = {
    "bash":            lambda **kw: _run_bash(kw["command"]),
    "read_file":       lambda **kw: _run_read(kw["path"], kw.get("limit")),
    "write_file":      lambda **kw: _run_write(kw["path"], kw["content"]),
    "edit_file":       lambda **kw: _run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "spawn_teammate":  lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),
    "list_teammates":  lambda **kw: TEAM.list_all(),
    "send_message":    lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    "read_inbox":      lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2),
    "broadcast":       lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),
}

# 这一组工具会直接暴露给 lead 模型：
# 前 4 个是基础文件/命令能力；
# 后 5 个是团队管理与收发消息能力。
TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "spawn_teammate", "description": "Spawn a persistent teammate that runs in its own thread.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}},
    {"name": "list_teammates", "description": "List all teammates with name, role, status.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "send_message", "description": "Send a message to a teammate's inbox.",
     "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
    {"name": "read_inbox", "description": "Read and drain the lead's inbox.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "broadcast", "description": "Send a message to all teammates.",
     "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
]


def agent_loop(messages: list):
    # 这是 lead 的主循环。
    # 它和 s02 的结构很像，只是每轮在调用模型前多了一个“先收邮箱消息”的步骤。
    while True:
        inbox = BUS.read_inbox("lead")
        if inbox:
            # 来自队友的新消息不会打断当前模型推理，
            # 而是在下一轮调用模型前作为新的 user turn 注入上下文。
            messages.append({
                "role": "user",
                "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>",
            })
            messages.append({
                "role": "assistant",
                "content": "Noted inbox messages.",
            })
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        # 先把 assistant 原始返回记录进历史，保证后续工具结果能接在正确位置上。
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            # lead 不再请求工具时，本轮就结束，最终文本会在 REPL 里打印出来。
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    # lead 能调用的既有基础工具，也有“拉队友 / 发消息 / 广播”这类编排工具。
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                except Exception as e:
                    # 为了保持 loop 稳定，异常统一转成字符串返回给模型，而不是让进程中断。
                    output = f"Error: {e}"
                print(f"> {block.name}: {str(output)[:200]}")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output),
                })
        # 和前面示例一致：把 tool_result 作为新的 user turn 回写给模型，闭合这一轮 agent loop。
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    # 交互外壳：既能像前面示例一样接受自然语言任务，
    # 也额外提供两个调试命令：
    # `/team` 查看成员状态，`/inbox` 手动读取 lead 的收件箱。
    history = []
    while True:
        try:
            query = input("\033[36ms09 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        if query.strip() == "/team":
            print(TEAM.list_all())
            continue
        if query.strip() == "/inbox":
            print(json.dumps(BUS.read_inbox("lead"), indent=2))
            continue
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                # SDK 返回的是内容块列表，这里只把最终文本块打印给终端用户看。
                if hasattr(block, "text"):
                    print(block.text)
        print()
