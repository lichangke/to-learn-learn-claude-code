#!/usr/bin/env python3
# Harness：协议层，为多个模型之间的结构化握手建立统一流程。
"""
s10_team_protocols.py - 团队协议

这个示例建立在 s09“团队邮箱协作”的基础上，但重点已经从“互相发消息”
升级为“围绕固定协议完成协作”。

这里新增了两套协议：
1. 关机协议 shutdown
   由 lead 发起，请某个 teammate 优雅退出，而不是直接把线程粗暴停掉。
2. 计划审批协议 plan approval
   由 teammate 发起，在执行较大动作前先把计划提交给 lead 审批。

两套协议虽然业务含义不同，但都复用同一个核心模式：
“先生成 request_id，再用 request_id 追踪这次请求最终是 pending / approved / rejected。”

    关机协议状态机：pending -> approved | rejected

    Lead                              Teammate
    +---------------------+          +---------------------+
    | shutdown_request     |          |                     |
    | {                    | -------> | 收到关机请求        |
    |   request_id: abc    |          | 判断是否同意        |
    | }                    |          |                     |
    +---------------------+          +---------------------+
                                             |
    +---------------------+          +-------v-------------+
    | shutdown_response    | <------- | shutdown_response   |
    | {                    |          | {                   |
    |   request_id: abc    |          |   request_id: abc   |
    |   approve: true      |          |   approve: true     |
    | }                    |          | }                   |
    +---------------------+          +---------------------+
            |
            v
    teammate.status -> "shutdown"，线程结束

    计划审批状态机：pending -> approved | rejected

    Teammate                          Lead
    +---------------------+          +---------------------+
    | plan_approval        |          |                     |
    | submit: {plan:"..."}| -------> | 阅读计划正文         |
    +---------------------+          | 决定批准或驳回       |
                                     +---------------------+
                                             |
    +---------------------+          +-------v-------------+
    | plan_approval_resp   | <------- | plan_approval       |
    | {approve: true}      |          | review: {req_id,    |
    +---------------------+          |   approve: true}     |
                                     +---------------------+

    追踪表形态：
    {request_id: {"target|from": name, "status": "pending|approved|rejected"}}

最值得理解的一点是：
“同一套 request_id 关联机制，可以承载多个不同领域的协议。”
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

# 从 `.env` 加载模型和网关配置，便于本地直接运行示例。
load_dotenv(override=True)
# 如果走的是兼容 Anthropic 协议的自定义网关，官方 token 变量有时会干扰认证；
# 这里沿用前面章节的处理方式，检测到自定义 base URL 时先移除它。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 整个协议系统都围绕当前工作目录运行：
# 1. 模型可操作的文件边界以 WORKDIR 为准；
# 2. `.team` 用来保存成员配置和收件箱，作为“对话外部的持久化状态”。
WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"

# lead 的 system prompt 这次强调的不只是“带队协作”，
# 还明确要求它使用 shutdown / plan approval 这两种协议来管理队友。
SYSTEM = f"You are a team lead at {WORKDIR}. Manage teammates with shutdown and plan approval protocols."

# 统一声明消息类型，MessageBus.send 会据此做校验。
# 其中 `plan_approval_response` 这个类型名沿用前文命名，
# 在本章里它既承载“计划提交”，也承载“审批反馈”的往返消息。
VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_response",
}

# -- 请求追踪表：用 request_id 把“发起”和“回应”关联起来 --
# shutdown_requests：lead 发起关机请求后，在这里记录目标成员和当前状态。
# plan_requests：teammate 提交计划后，在这里记录提交人、计划正文和审批状态。
shutdown_requests = {}
plan_requests = {}
_tracker_lock = threading.Lock()


# -- MessageBus：每个成员一个 JSONL 收件箱 --
class MessageBus:
    def __init__(self, inbox_dir: Path):
        # `.team/inbox/<name>.jsonl` 就是某个成员的收件箱。
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        # 发消息的本质很简单：
        # 把一条 JSON 记录追加写入目标成员的 inbox 文件末尾。
        # `extra` 用来附加协议字段，例如 request_id、approve 等。
        if msg_type not in VALID_MSG_TYPES:
            return f"Error: Invalid type '{msg_type}'. Valid: {VALID_MSG_TYPES}"
        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),
        }
        if extra:
            msg.update(extra)
        inbox_path = self.dir / f"{to}.jsonl"
        with open(inbox_path, "a") as f:
            f.write(json.dumps(msg) + "\n")
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list:
        # inbox 采用“读完即清空”的 drain 语义：
        # 这使它更像待处理消息队列，而不是永久聊天记录。
        inbox_path = self.dir / f"{name}.jsonl"
        if not inbox_path.exists():
            return []
        messages = []
        for line in inbox_path.read_text().strip().splitlines():
            if line:
                # JSONL 的一行就是一条消息，逐行反序列化即可。
                messages.append(json.loads(line))
        # 读完立即清空，避免同一批消息在下轮再次被消费。
        inbox_path.write_text("")
        return messages

    def broadcast(self, sender: str, content: str, teammates: list) -> str:
        # 广播不是单独的存储结构，本质上只是对每个队友重复 send(...)。
        count = 0
        for name in teammates:
            if name != sender:
                self.send(sender, name, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"


# 全局消息总线。lead 和所有 teammate 线程都通过它收发 `.team/inbox` 里的消息。
BUS = MessageBus(INBOX_DIR)


# -- TeammateManager：管理常驻队友，以及 shutdown / plan approval 协议 --
class TeammateManager:
    def __init__(self, team_dir: Path):
        self.dir = team_dir
        self.dir.mkdir(exist_ok=True)
        self.config_path = self.dir / "config.json"
        # `config.json` 负责把团队成员的名字、角色、状态落盘保存；
        # `threads` 只保存当前 Python 进程里真正跑起来的线程对象。
        self.config = self._load_config()
        self.threads = {}

    def _load_config(self) -> dict:
        # 优先从磁盘恢复团队名册；首次运行时再创建默认空团队。
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}

    def _save_config(self):
        # 任何成员状态变化都立刻落盘，避免进程结束后团队状态丢失。
        self.config_path.write_text(json.dumps(self.config, indent=2))

    def _find_member(self, name: str) -> dict:
        # 教学示例里成员数量很少，线性扫描已经足够。
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    def spawn(self, name: str, role: str, prompt: str) -> str:
        # 这里既负责“首次创建队友”，也负责“把已有但空闲/已关闭的队友再次唤醒”。
        member = self._find_member(name)
        if member:
            # 同名成员已存在时，不重复建档；
            # 只有 idle / shutdown 状态允许重新进入 working。
            if member["status"] not in ("idle", "shutdown"):
                return f"Error: '{name}' is currently {member['status']}"
            member["status"] = "working"
            member["role"] = role
        else:
            # 新队友第一次出现时，写入名册并标记为 working。
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save_config()
        # 真正执行队友逻辑的是一个常驻 daemon thread。
        # 线程一旦启动，就进入自己的 agent loop，直到本轮任务结束或协议要求退出。
        thread = threading.Thread(
            target=self._teammate_loop,
            args=(name, role, prompt),
            daemon=True,
        )
        self.threads[name] = thread
        thread.start()
        return f"Spawned '{name}' (role: {role})"

    def _teammate_loop(self, name: str, role: str, prompt: str):
        # 队友的 system prompt 明确告诉模型两条协议纪律：
        # 1. 做重大工作前，要先走 plan_approval；
        # 2. 收到 shutdown_request 时，要用 shutdown_response 回应。
        sys_prompt = (
            f"You are '{name}', role: {role}, at {WORKDIR}. "
            f"Submit plans via plan_approval before major work. "
            f"Respond to shutdown_request with shutdown_response."
        )
        # 每个队友都维护自己独立的上下文，初始 user 消息就是分配给它的任务。
        messages = [{"role": "user", "content": prompt}]
        tools = self._teammate_tools()
        # 如果队友在本轮工具调用里同意关机，不是立刻强退，而是在下一轮前干净退出。
        should_exit = False
        # 最多循环 50 轮，作为教学示例里的保险丝，避免异常情况下无限自旋。
        for _ in range(50):
            inbox = BUS.read_inbox(name)
            for msg in inbox:
                # 新收到的协议消息会作为新的 user turn 注入队友上下文，
                # 这样模型就能基于最新信件继续推理。
                messages.append({"role": "user", "content": json.dumps(msg)})
            if should_exit:
                break
            try:
                response = client.messages.create(
                    model=MODEL,
                    system=sys_prompt,
                    messages=messages,
                    tools=tools,
                    max_tokens=8000,
                )
            except Exception:
                # 线程内异常不向上抛出，直接结束该队友本轮执行。
                break
            # 无论返回的是文本还是工具调用，都先完整写回上下文，形成闭环历史。
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason != "tool_use":
                # 不再请求工具，说明队友这一轮已经暂时完成。
                break
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    output = self._exec(name, block.name, block.input)
                    print(f"  [{name}] {block.name}: {str(output)[:120]}")
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(output),
                    })
                    if block.name == "shutdown_response" and block.input.get("approve"):
                        # 只有明确批准关机，线程才会在本轮结束后退出并把成员状态改成 shutdown。
                        should_exit = True
            # 工具结果回写给模型，闭合这一轮“模型 -> 工具 -> 模型”的小循环。
            messages.append({"role": "user", "content": results})
        member = self._find_member(name)
        if member:
            # 线程结束时，用 should_exit 决定它是回到 idle，还是进入 shutdown。
            member["status"] = "shutdown" if should_exit else "idle"
            self._save_config()

    def _exec(self, sender: str, tool_name: str, args: dict) -> str:
        # 队友侧工具执行器。
        # 前四个基础工具沿用 s02，本章新增的是“回关机协议”和“提计划审批”。
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
            # 队友回应关机请求时，需要做两件事：
            # 1. 更新 lead 侧的 shutdown_requests 状态；
            # 2. 再发一条结构化消息到 lead 的 inbox，形成显式回执。
            req_id = args["request_id"]
            approve = args["approve"]
            with _tracker_lock:
                if req_id in shutdown_requests:
                    shutdown_requests[req_id]["status"] = "approved" if approve else "rejected"
            BUS.send(
                sender, "lead", args.get("reason", ""),
                "shutdown_response", {"request_id": req_id, "approve": approve},
            )
            return f"Shutdown {'approved' if approve else 'rejected'}"
        if tool_name == "plan_approval":
            # 队友提交计划审批时，也走同样的 request_id 模式：
            # 先登记到 plan_requests，再把计划正文和 request_id 发给 lead。
            plan_text = args.get("plan", "")
            req_id = str(uuid.uuid4())[:8]
            with _tracker_lock:
                plan_requests[req_id] = {"from": sender, "plan": plan_text, "status": "pending"}
            BUS.send(
                sender, "lead", plan_text, "plan_approval_response",
                {"request_id": req_id, "plan": plan_text},
            )
            return f"Plan submitted (request_id={req_id}). Waiting for lead approval."
        return f"Unknown tool: {tool_name}"

    def _teammate_tools(self) -> list:
        # 暴露给 teammate 模型的工具菜单。
        # 它能做执行类工作，也能参与两种协议，但不能像 lead 那样编排整个团队。
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
            {"name": "shutdown_response", "description": "Respond to a shutdown request. Approve to shut down, reject to keep working.",
             "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "reason": {"type": "string"}}, "required": ["request_id", "approve"]}},
            {"name": "plan_approval", "description": "Submit a plan for lead approval. Provide plan text.",
             "input_schema": {"type": "object", "properties": {"plan": {"type": "string"}}, "required": ["plan"]}},
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
        # 广播时只需要成员名字列表。
        return [m["name"] for m in self.config["members"]]


TEAM = TeammateManager(TEAM_DIR)


# -- 基础工具实现：沿用 s02 的文件 / 命令能力 --
def _safe_path(p: str) -> Path:
    # 所有文件路径都先落到 WORKDIR 内，再检查是否越界。
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def _run_bash(command: str) -> str:
    # 这里只保留一个教学级的粗粒度危险命令拦截。
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
        # 先做路径校验，再读文件；必要时只返回前若干行。
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
        # 仍然采用“精确匹配后替换一次”的最小编辑模型，便于演示局部补丁思路。
        if old_text not in c:
            return f"Error: Text not found in {path}"
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# -- Lead 侧协议处理函数 --
def handle_shutdown_request(teammate: str) -> str:
    # lead 发起关机时的完整流程：
    # 1. 生成 request_id；
    # 2. 记录到 shutdown_requests；
    # 3. 给指定 teammate 发 shutdown_request 消息；
    # 4. 返回 request_id，后续可轮询状态。
    req_id = str(uuid.uuid4())[:8]
    with _tracker_lock:
        shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send(
        "lead", teammate, "Please shut down gracefully.",
        "shutdown_request", {"request_id": req_id},
    )
    return f"Shutdown request {req_id} sent to '{teammate}' (status: pending)"


def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    # lead 审批计划时的流程：
    # 1. 按 request_id 找到原请求；
    # 2. 把状态改成 approved / rejected；
    # 3. 再把审批结果发回原提交者的 inbox。
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
    # 提供给 lead 的查询接口，用于查看某个关机请求目前推进到了哪一步。
    with _tracker_lock:
        return json.dumps(shutdown_requests.get(request_id, {"error": "not found"}))


# -- Lead 侧工具分发表（12 个工具） --
# 这一步负责把“模型请求的工具名”路由到“本地真正执行的 Python 函数”。
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
}

# 暴露给 lead 模型的工具列表。
# 前半部分是基础文件/命令能力，后半部分是团队编排和两种协议能力。
TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "spawn_teammate", "description": "Spawn a persistent teammate.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}},
    {"name": "list_teammates", "description": "List all teammates.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "send_message", "description": "Send a message to a teammate.",
     "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
    {"name": "read_inbox", "description": "Read and drain the lead's inbox.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "broadcast", "description": "Send a message to all teammates.",
     "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
    {"name": "shutdown_request", "description": "Request a teammate to shut down gracefully. Returns a request_id for tracking.",
     "input_schema": {"type": "object", "properties": {"teammate": {"type": "string"}}, "required": ["teammate"]}},
    {"name": "shutdown_response", "description": "Check the status of a shutdown request by request_id.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}},
    {"name": "plan_approval", "description": "Approve or reject a teammate's plan. Provide request_id + approve + optional feedback.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "feedback": {"type": "string"}}, "required": ["request_id", "approve"]}},
]


def agent_loop(messages: list):
    # lead 的主循环和前面章节类似，但这里每一轮都会先收自己的 inbox，
    # 让队友的协议消息在下一次模型推理前进入上下文。
    while True:
        inbox = BUS.read_inbox("lead")
        if inbox:
            # 队友发来的结构化消息会被包进 `<inbox>...</inbox>`，
            # 作为新的 user turn 注入 lead 的上下文。
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
        # 先记录 assistant 原始输出，再根据其中的 tool_use 去执行本地逻辑。
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            # 不再请求工具时，本轮结束，最终文本会在外层 REPL 打印。
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    # 这里统一做工具调度；异常也转成字符串返回给模型，而不是让循环崩掉。
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                except Exception as e:
                    output = f"Error: {e}"
                print(f"> {block.name}: {str(output)[:200]}")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output),
                })
        # 把 tool_result 作为新的 user turn 回写，形成“模型 -> 工具 -> 模型”的闭环。
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    # 简单的交互外壳：
    # 1. 普通输入会作为 lead 的用户任务进入 agent_loop；
    # 2. `/team` 查看当前成员状态；
    # 3. `/inbox` 手动读取 lead 的收件箱。
    history = []
    while True:
        try:
            query = input("\033[36ms10 >> \033[0m")
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
                # SDK 返回的是内容块列表；这里只把最终文本块打印给终端用户。
                if hasattr(block, "text"):
                    print(block.text)
        print()
