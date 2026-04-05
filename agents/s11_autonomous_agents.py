#!/usr/bin/env python3
# Harness：自主性层，让模型在没人显式下指令时也能自己寻找下一项工作。
"""
s11_autonomous_agents.py - 自主代理

这个示例建立在 s10 的“团队协议层”之上，但关注点又往前推了一步：
前面章节解决的是“多个 agent 如何协作”，这里解决的是“agent 在空闲时如何自己找活干”。

和 s10 相比，这一章新增了 3 个关键能力：
1. idle 空闲循环
   当 teammate 暂时没有工具可调、也没有明确下一步时，它不会立刻退出，
   而是进入一个轮询阶段，周期性检查“是否收到新消息 / 是否有新任务可认领”。
2. 任务看板扫描与自动认领
   teammate 会扫描 `.tasks/task_*.json`，寻找还未被任何人接手的 pending 任务，
   如果发现可做的新任务，就会自己把任务标记成 `in_progress`，然后继续工作。
3. 身份重注入（identity re-injection）
   当上下文因为压缩、截断或长期运行而变短时，agent 可能逐渐“忘记自己是谁”。
   因此在某些恢复工作的重要时刻，会重新插入一段身份说明，提醒模型它的名字、角色和所属团队。

可以把 teammate 的完整生命周期理解成：

    teammate 生命周期：
    +-------+
    | spawn |
    +---+---+
        |
        v
    +-------+  tool_use    +-------+
    | WORK  | <----------- |  LLM  |
    +---+---+              +-------+
        |
        | stop_reason != tool_use
        v
    +--------+
    | IDLE   | 每 5 秒轮询一次，最多等 60 秒
    +---+----+
        |
        +---> 检查 inbox -> 有新消息 -> 回到 WORK
        |
        +---> 扫描 .tasks/ -> 有未认领任务 -> claim -> 回到 WORK
        |
        +---> 超时（60 秒） -> shutdown

从 lead 视角看，整体运行流程大致是：
1. lead 仍然通过和前几章类似的 agent loop 与模型交互。
2. lead 可以 `spawn_teammate(...)` 拉起一个长期存在的 teammate。
3. teammate 先进入常规工作阶段，处理消息、调用工具、推进当前任务。
4. 当模型主动调用 `idle` 工具，或这一轮工作自然停下来后，teammate 转入 idle 阶段。
5. idle 阶段里它会做两件事：看邮箱、扫任务板。
6. 只要任一渠道出现新工作，它就恢复到 working；如果长时间什么都没有，就自行 shutdown。

身份重注入的核心形式如下：
    messages = [identity_block, ...remaining...]
    "You are 'coder', role: backend, team: my-team"

最重要的观察点是：
“前面的章节让 agent 会协作；这一章让 agent 在协作之外，开始具备持续自驱的倾向。”
"""

import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)
# 如果接入的是兼容 Anthropic 协议的自定义网关，
# 官方 token 环境变量有时会干扰认证，因此沿用前面章节的处理方式先移除它。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 所有协作状态都围绕当前工作目录展开：
# 1. `.team` 保存团队成员和 inbox；
# 2. `.tasks` 作为独立于对话上下文之外的任务看板；
# 3. 这样即使单个 agent 的上下文被压缩，外部状态依然存在。
WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"
TASKS_DIR = WORKDIR / ".tasks"

# idle 阶段的轮询参数。
# 这里的设计是“短轮询 + 总超时”：
# 每隔 POLL_INTERVAL 秒看一次有没有新工作，超过 IDLE_TIMEOUT 还没有就退出。
POLL_INTERVAL = 5
IDLE_TIMEOUT = 60

# lead 的 system prompt 这次明确告诉模型：
# 它管理的是一组“自主队友”，队友不是只能等派活，而是会自己找事情做。
SYSTEM = f"You are a team lead at {WORKDIR}. Teammates are autonomous -- they find work themselves."

# 这里继续沿用 s10 的消息类型集合。
# s11 的新增重点并不在消息协议本身，而是在“空闲时如何主动获得下一份工作”。
VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_response",
}

# -- 请求跟踪表：延续 s10 的关机 / 计划审批协议 --
shutdown_requests = {}
plan_requests = {}
_tracker_lock = threading.Lock()
# 多个 teammate 可能同时扫描到同一个待领取任务。
# `_claim_lock` 用来把“读取任务 -> 标记 owner -> 写回磁盘”这段流程串行化，
# 避免两个线程几乎同时把同一个任务据为己有。
_claim_lock = threading.Lock()


# -- MessageBus：每个 teammate 一个 JSONL 收件箱 --
class MessageBus:
    def __init__(self, inbox_dir: Path):
        # `.team/inbox/<name>.jsonl` 就是某个成员的收件箱文件。
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        # 发送消息的本质就是往目标成员的 inbox 末尾追加一行 JSON。
        # 这样 lead 和 teammate 都能共用同一套通信机制。
        if msg_type not in VALID_MSG_TYPES:
            return f"Error: Invalid type '{msg_type}'. Valid: {VALID_MSG_TYPES}"
        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),
        }
        if extra:
            # `extra` 用来附加协议字段，例如 request_id、approve 等。
            msg.update(extra)
        inbox_path = self.dir / f"{to}.jsonl"
        with open(inbox_path, "a") as f:
            f.write(json.dumps(msg) + "\n")
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list:
        # inbox 使用“drain”语义：
        # 一次性读出全部消息，然后立刻清空文件，避免同一批消息被重复消费。
        inbox_path = self.dir / f"{name}.jsonl"
        if not inbox_path.exists():
            return []
        messages = []
        for line in inbox_path.read_text().strip().splitlines():
            if line:
                messages.append(json.loads(line))
        inbox_path.write_text("")
        return messages

    def broadcast(self, sender: str, content: str, teammates: list) -> str:
        # broadcast 没有单独的存储结构，
        # 它只是对每个非发送者成员重复调用一次 send(...)。
        count = 0
        for name in teammates:
            if name != sender:
                self.send(sender, name, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"


# 全局消息总线，lead 和所有 teammate 都通过它读写 `.team/inbox`。
BUS = MessageBus(INBOX_DIR)


# -- 任务看板扫描：自主代理的“找活干”入口 --
def scan_unclaimed_tasks() -> list:
    # `.tasks` 目录里的每个 `task_*.json` 都代表一个独立任务。
    # 这里只筛出三类条件都满足的任务：
    # 1. status == pending，说明还没开始；
    # 2. 没有 owner，说明还没人接手；
    # 3. 没有 blockedBy，说明当前不被别的任务阻塞。
    TASKS_DIR.mkdir(exist_ok=True)
    unclaimed = []
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        task = json.loads(f.read_text())
        if (task.get("status") == "pending"
                and not task.get("owner")
                and not task.get("blockedBy")):
            unclaimed.append(task)
    return unclaimed


def claim_task(task_id: int, owner: str) -> str:
    # 认领任务必须加锁。
    # 否则两个线程都在“看见 owner 为空”的瞬间写回，就会造成竞争条件。
    with _claim_lock:
        path = TASKS_DIR / f"task_{task_id}.json"
        if not path.exists():
            return f"Error: Task {task_id} not found"
        task = json.loads(path.read_text())
        # 认领的语义很明确：
        # owner 改成当前 teammate，状态从 pending 推进到 in_progress。
        task["owner"] = owner
        task["status"] = "in_progress"
        path.write_text(json.dumps(task, indent=2))
    return f"Claimed task #{task_id} for {owner}"


# -- 身份重注入：在上下文变短时重新提醒模型“你是谁” --
def make_identity_block(name: str, role: str, team_name: str) -> dict:
    # 这个块会被插回 messages 前部，作用不是提供新任务信息，
    # 而是恢复 agent 的自我定位，降低上下文压缩后角色漂移的概率。
    return {
        "role": "user",
        "content": f"<identity>You are '{name}', role: {role}, team: {team_name}. Continue your work.</identity>",
    }


# -- Autonomous TeammateManager：管理“会自己找事做”的队友 --
class TeammateManager:
    def __init__(self, team_dir: Path):
        self.dir = team_dir
        self.dir.mkdir(exist_ok=True)
        self.config_path = self.dir / "config.json"
        # `config.json` 负责把团队成员列表、角色和当前状态持久化到磁盘。
        # `threads` 只保存当前这个 Python 进程里实际跑起来的后台线程对象。
        self.config = self._load_config()
        self.threads = {}

    def _load_config(self) -> dict:
        # 优先从磁盘恢复团队状态；首次运行时再给一个默认空团队。
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}

    def _save_config(self):
        # 每次状态变化后立刻落盘，避免进程退出后团队状态丢失。
        self.config_path.write_text(json.dumps(self.config, indent=2))

    def _find_member(self, name: str) -> dict:
        # 教学示例里团队规模很小，线性扫描已足够。
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    def _set_status(self, name: str, status: str):
        # 状态切换统一走这个入口，确保变更后一定写回 config.json。
        member = self._find_member(name)
        if member:
            member["status"] = status
            self._save_config()

    def spawn(self, name: str, role: str, prompt: str) -> str:
        # `spawn(...)` 兼顾两种场景：
        # 1. 首次创建一个新 teammate；
        # 2. 重新唤醒一个已经存在、但当前处于 idle / shutdown 的 teammate。
        member = self._find_member(name)
        if member:
            # 如果同名成员还在 working，就不允许重复拉起，避免同一身份并发运行两份。
            if member["status"] not in ("idle", "shutdown"):
                return f"Error: '{name}' is currently {member['status']}"
            member["status"] = "working"
            member["role"] = role
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save_config()
        # 真正的 teammate 生命周期在后台线程里运行。
        # lead 只负责创建线程，不会阻塞等待这个 teammate 做完所有事。
        thread = threading.Thread(
            target=self._loop,
            args=(name, role, prompt),
            daemon=True,
        )
        self.threads[name] = thread
        thread.start()
        return f"Spawned '{name}' (role: {role})"

    def _loop(self, name: str, role: str, prompt: str):
        # 每个 teammate 都有自己专属的 system prompt。
        # 和前面的章节不同，这里专门强调：
        # “没有工作时请调用 idle 工具；进入 idle 后你会自动认领新任务。”
        team_name = self.config["team_name"]
        sys_prompt = (
            f"You are '{name}', role: {role}, team: {team_name}, at {WORKDIR}. "
            f"Use idle tool when you have no more work. You will auto-claim new tasks."
        )
        # 初始 prompt 作为该 teammate 的第一条用户消息。
        messages = [{"role": "user", "content": prompt}]
        tools = self._teammate_tools()

        while True:
            # -- 工作阶段：先像普通 agent 一样推进当前任务 --
            for _ in range(50):
                # 每次调用模型前先收件。
                # 这保证 teammate 在长期运行时不会错过 lead 或其他队友发来的新消息。
                inbox = BUS.read_inbox(name)
                for msg in inbox:
                    # 如果收到了关机请求，立即结束线程，不再继续工作循环。
                    if msg.get("type") == "shutdown_request":
                        self._set_status(name, "shutdown")
                        return
                    # 其他消息被包装成 user 内容注入上下文，让模型基于新消息继续思考。
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
                    # 这里的策略比较保守：
                    # 一旦模型调用失败，就把成员状态标记回 idle，让外界知道它当前没有在推进工作。
                    self._set_status(name, "idle")
                    return
                messages.append({"role": "assistant", "content": response.content})
                # 只要这一轮模型没有继续请求工具，就说明“当前工作回合”自然结束了，
                # 接下来转入 idle 阶段等待或自领新任务。
                if response.stop_reason != "tool_use":
                    break
                results = []
                idle_requested = False
                for block in response.content:
                    if block.type == "tool_use":
                        if block.name == "idle":
                            # `idle` 是一个非常关键的自我调度信号：
                            # 它表示“我暂时没有下一步了，请把我送入空闲轮询阶段”。
                            idle_requested = True
                            output = "Entering idle phase. Will poll for new tasks."
                        else:
                            output = self._exec(name, block.name, block.input)
                        # 给宿主程序打印一个简短预览，方便观察这个 teammate 正在做什么。
                        print(f"  [{name}] {block.name}: {str(output)[:120]}")
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(output),
                        })
                # 关键步骤：无论调用的是 bash、发消息还是 idle，
                # 都要把结果包装成 tool_result 回送给模型，保持工具调用闭环。
                messages.append({"role": "user", "content": results})
                if idle_requested:
                    # 如果模型显式表示“我要 idle”，就提前结束工作阶段，进入下面的轮询逻辑。
                    break

            # -- 空闲阶段：周期性检查邮箱和任务板，等待新工作自己出现 --
            self._set_status(name, "idle")
            resume = False
            polls = IDLE_TIMEOUT // max(POLL_INTERVAL, 1)
            for _ in range(polls):
                time.sleep(POLL_INTERVAL)
                # 通道 1：先看 inbox。
                # 如果有人直接给它发消息，这是最高优先级的新工作来源。
                inbox = BUS.read_inbox(name)
                if inbox:
                    for msg in inbox:
                        if msg.get("type") == "shutdown_request":
                            self._set_status(name, "shutdown")
                            return
                        messages.append({"role": "user", "content": json.dumps(msg)})
                    resume = True
                    break
                # 通道 2：没人发消息时，主动扫任务板，看看有没有没人接手的任务。
                unclaimed = scan_unclaimed_tasks()
                if unclaimed:
                    # 这里只取第一个可用任务。
                    # 这让策略保持简单明确：一次只接一项，先认领再干。
                    task = unclaimed[0]
                    claim_task(task["id"], name)
                    task_prompt = (
                        f"<auto-claimed>Task #{task['id']}: {task['subject']}\n"
                        f"{task.get('description', '')}</auto-claimed>"
                    )
                    if len(messages) <= 3:
                        # 当消息历史已经很短时，说明上下文可能压缩过、或队友刚被重新唤醒。
                        # 这时把身份块重新插回前面，提醒模型它的角色与归属。
                        messages.insert(0, make_identity_block(name, role, team_name))
                        messages.insert(1, {"role": "assistant", "content": f"I am {name}. Continuing."})
                    # 把自动认领到的任务像一条新用户消息一样塞进上下文，
                    # 这样模型会把它视为“下一件要处理的事”。
                    messages.append({"role": "user", "content": task_prompt})
                    messages.append({"role": "assistant", "content": f"Claimed task #{task['id']}. Working on it."})
                    resume = True
                    break

            if not resume:
                # 到达超时时间还没有任何新工作，就主动关闭这个 teammate。
                # 这避免线程无限常驻空转。
                self._set_status(name, "shutdown")
                return
            # 只要收到了新消息或成功认领到任务，就重新切回 working，外层 while True 会继续跑工作阶段。
            self._set_status(name, "working")

    def _exec(self, sender: str, tool_name: str, args: dict) -> str:
        # 这里是 teammate 侧的工具分发器。
        # 前四个基础文件工具延续自 s02，后面的消息 / 审批 / 认领任务工具则来自团队协作场景。
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
            return json.dumps(BUS.read_inbox(sender), indent=2)
        if tool_name == "shutdown_response":
            # teammate 对 lead 发起的 shutdown_request 给出批准 / 拒绝反馈。
            req_id = args["request_id"]
            with _tracker_lock:
                if req_id in shutdown_requests:
                    shutdown_requests[req_id]["status"] = "approved" if args["approve"] else "rejected"
            BUS.send(
                sender, "lead", args.get("reason", ""),
                "shutdown_response", {"request_id": req_id, "approve": args["approve"]},
            )
            return f"Shutdown {'approved' if args['approve'] else 'rejected'}"
        if tool_name == "plan_approval":
            # teammate 可以在执行大动作前，先把计划提交给 lead 审批。
            plan_text = args.get("plan", "")
            req_id = str(uuid.uuid4())[:8]
            with _tracker_lock:
                plan_requests[req_id] = {"from": sender, "plan": plan_text, "status": "pending"}
            BUS.send(
                sender, "lead", plan_text, "plan_approval_response",
                {"request_id": req_id, "plan": plan_text},
            )
            return f"Plan submitted (request_id={req_id}). Waiting for approval."
        if tool_name == "claim_task":
            return claim_task(args["task_id"], sender)
        return f"Unknown tool: {tool_name}"

    def _teammate_tools(self) -> list:
        # 发给 teammate 模型看的工具菜单。
        # 相比 lead，它没有 `spawn_teammate` / `broadcast` 这类团队管理工具，
        # 但多了 `idle`，因为它需要主动声明“我现在进入空闲轮询阶段”。
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
            {"name": "shutdown_response", "description": "Respond to a shutdown request.",
             "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "reason": {"type": "string"}}, "required": ["request_id", "approve"]}},
            {"name": "plan_approval", "description": "Submit a plan for lead approval.",
             "input_schema": {"type": "object", "properties": {"plan": {"type": "string"}}, "required": ["plan"]}},
            {"name": "idle", "description": "Signal that you have no more work. Enters idle polling phase.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "claim_task", "description": "Claim a task from the task board by ID.",
             "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
        ]

    def list_all(self) -> str:
        # 供 `/team` 和 lead 工具使用的人类可读团队概览。
        if not self.config["members"]:
            return "No teammates."
        lines = [f"Team: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        return "\n".join(lines)

    def member_names(self) -> list:
        # broadcast 时只需要名字列表，不需要完整成员对象。
        return [m["name"] for m in self.config["members"]]


TEAM = TeammateManager(TEAM_DIR)


# -- 基础工具实现：沿用 s02 的文件与命令能力 --
def _safe_path(p: str) -> Path:
    # 所有文件读写都必须限制在 WORKDIR 之内，防止路径逃逸。
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def _run_bash(command: str) -> str:
    # 这里只做一层演示级的危险命令拦截。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        # 工具输出会回流给模型，因此同时采集 stdout 和 stderr。
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
        # 读取时按行切分，便于在需要时做前 N 行裁剪。
        lines = _safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def _run_write(path: str, content: str) -> str:
    try:
        fp = _safe_path(path)
        # 自动补父目录，减少模型写文件时的样板操作。
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"


def _run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = _safe_path(path)
        c = fp.read_text()
        # edit 使用“精确匹配后替换一次”的策略，
        # 让模型做小范围补丁时更可控。
        if old_text not in c:
            return f"Error: Text not found in {path}"
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# -- Lead 侧协议处理器：延续 s10 的审批 / 关机握手 --
def handle_shutdown_request(teammate: str) -> str:
    # lead 发起关机时先生成 request_id，并把状态记入 shutdown_requests。
    req_id = str(uuid.uuid4())[:8]
    with _tracker_lock:
        shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send(
        "lead", teammate, "Please shut down gracefully.",
        "shutdown_request", {"request_id": req_id},
    )
    return f"Shutdown request {req_id} sent to '{teammate}'"


def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    # lead 审批 teammate 提交的计划：
    # 先更新追踪表，再把审批结果回发给原提交者。
    with _tracker_lock:
        req = plan_requests.get(request_id)
    if not req:
        return f"Error: Unknown plan request_id '{request_id}'"
    with _tracker_lock:
        req["status"] = "approved" if approve else "rejected"
    BUS.send(
        "lead", req["from"], feedback, "plan_approval_response",
        {"request_id": request_id, "approve": approve, "feedback": feedback},
    )
    return f"Plan {req['status']} for '{req['from']}'"


def _check_shutdown_status(request_id: str) -> str:
    # 供 lead 查询某次关机请求目前是 pending / approved / rejected。
    with _tracker_lock:
        return json.dumps(shutdown_requests.get(request_id, {"error": "not found"}))


# -- Lead 工具分发表（14 个工具） --
TOOL_HANDLERS = {
    "bash":              lambda **kw: _run_bash(kw["command"]),
    "read_file":         lambda **kw: _run_read(kw["path"], kw.get("limit")),
    "write_file":        lambda **kw: _run_write(kw["path"], kw["content"]),
    "edit_file":         lambda **kw: _run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "spawn_teammate":    lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),
    "list_teammates":    lambda **kw: TEAM.list_all(),
    "send_message":      lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    "read_inbox":        lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2),
    "broadcast":         lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),
    "shutdown_request":  lambda **kw: handle_shutdown_request(kw["teammate"]),
    "shutdown_response": lambda **kw: _check_shutdown_status(kw.get("request_id", "")),
    "plan_approval":     lambda **kw: handle_plan_review(kw["request_id"], kw["approve"], kw.get("feedback", "")),
    "idle":              lambda **kw: "Lead does not idle.",
    "claim_task":        lambda **kw: claim_task(kw["task_id"], "lead"),
}

# 发给 lead 模型的工具菜单。
# 它既保留了基础文件工具，也包含团队管理和协议相关工具。
TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "spawn_teammate", "description": "Spawn an autonomous teammate.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}},
    {"name": "list_teammates", "description": "List all teammates.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "send_message", "description": "Send a message to a teammate.",
     "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
    {"name": "read_inbox", "description": "Read and drain the lead's inbox.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "broadcast", "description": "Send a message to all teammates.",
     "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
    {"name": "shutdown_request", "description": "Request a teammate to shut down.",
     "input_schema": {"type": "object", "properties": {"teammate": {"type": "string"}}, "required": ["teammate"]}},
    {"name": "shutdown_response", "description": "Check shutdown request status.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}},
    {"name": "plan_approval", "description": "Approve or reject a teammate's plan.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "feedback": {"type": "string"}}, "required": ["request_id", "approve"]}},
    {"name": "idle", "description": "Enter idle state (for lead -- rarely used).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "claim_task", "description": "Claim a task from the board by ID.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
]


def agent_loop(messages: list):
    # lead 的主循环与前几章结构基本一致，
    # 但每轮调用模型前都会先检查 inbox，把队友的最新消息注入上下文。
    while True:
        inbox = BUS.read_inbox("lead")
        if inbox:
            messages.append({
                "role": "user",
                "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>",
            })
            messages.append({
                "role": "assistant",
                "content": "Noted inbox messages.",
            })
        # 把完整历史、系统提示和可用工具一起发给 lead 模型。
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            # 只要这一轮没有工具调用，lead 就把当前回答交回给 REPL。
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                except Exception as e:
                    output = f"Error: {e}"
                print(f"> {block.name}: {str(output)[:200]}")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output),
                })
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    # 一个最小 REPL：
    # 1. 普通输入交给 lead agent；
    # 2. `/team` 看团队状态；
    # 3. `/inbox` 看 lead 收件箱；
    # 4. `/tasks` 查看任务板当前有哪些任务及其 owner。
    history = []
    while True:
        try:
            query = input("\033[36ms11 >> \033[0m")
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
        if query.strip() == "/tasks":
            TASKS_DIR.mkdir(exist_ok=True)
            for f in sorted(TASKS_DIR.glob("task_*.json")):
                t = json.loads(f.read_text())
                # 用简短标记把状态展示成人类更容易扫读的列表。
                marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
                owner = f" @{t['owner']}" if t.get("owner") else ""
                print(f"  {marker} #{t['id']}: {t['subject']}{owner}")
            continue
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                # 最后一条 assistant 内容可能是内容块列表，逐块打印可见文本。
                if hasattr(block, "text"):
                    print(block.text)
        print()
