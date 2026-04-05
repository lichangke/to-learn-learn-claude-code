#!/usr/bin/env python3
# Harness：完整机制整合层，把前面章节的能力拼成一个可运行的“总控驾驶舱”。
"""
s00_full.py - 完整参考代理

这个文件把 s01-s11 中已经讲过的核心能力汇总到一个地方，方便你从“整机视角”
理解一个完整 agent 是怎样把多个机制串起来协同工作的。

这里不是按主题逐章展开教学，而是把这些部件直接放进同一个运行时里：
1. s02 的工具分发与本地执行
2. s03 的 TodoWrite 短清单
3. s04 的一次性子代理
4. s05 的技能加载
5. s06 的上下文压缩
6. s07 的文件化任务板
7. s08 的后台任务与通知队列
8. s09 / s11 的常驻队友、邮箱、idle 与自动认领
9. s10 的关机 / 计划审批相关状态结构

如果按 lead 主代理的视角来读，可以把整体流程理解成：
1. REPL 接收用户输入，把输入追加到 `history`。
2. `agent_loop(...)` 在每轮调用模型前，先做压缩、回收后台结果、读取 inbox。
3. 主模型基于当前上下文决定是直接回复，还是请求一个或多个工具。
4. 工具请求通过 `TOOL_HANDLERS` 路由到本地 Python 实现。
5. 工具结果再被回写成新的 user turn，供下一轮模型继续推理。
6. 某些工具又会进一步驱动子代理、后台线程、任务板或常驻队友。

可以把它看成“所有部件放在一起后，数据如何流动”的参考实现。

    +------------------------------------------------------------------+
    |                           完整代理                                |
    |                                                                   |
    |  系统提示词（s05 技能、任务优先、可选 todo 催办）                 |
    |                                                                   |
    |  每次调用 LLM 之前：                                               |
    |  +--------------------+  +------------------+  +--------------+  |
    |  | 微压缩（s06）      |  | 清空后台通知     |  | 检查收件箱   |  |
    |  | 自动压缩（s06）    |  | （s08）          |  | （s09）      |  |
    |  +--------------------+  +------------------+  +--------------+  |
    |                                                                   |
    |  工具分发（沿用 s02 模式）：                                       |
    |  +--------+----------+----------+---------+-----------+          |
    |  | bash   | read     | write    | edit    | TodoWrite |          |
    |  | task   | load_sk  | compress | bg_run  | bg_check  |          |
    |  | t_crt  | t_get    | t_upd    | t_list  | spawn_tm  |          |
    |  | list_tm| send_msg | rd_inbox | bcast   | shutdown  |          |
    |  | plan   | idle     | claim    |         |           |          |
    |  +--------+----------+----------+---------+-----------+          |
    |                                                                   |
    |  子代理（s04）：创建 -> 工作 -> 返回摘要                          |
    |  队友（s09）：创建 -> 工作 -> idle -> 自动认领（s11）             |
    |  关机状态（s10）：记录 request_id 并发出 shutdown_request         |
    |  计划审批（s10）：保留 review hook 供 lead 审批                    |
    +------------------------------------------------------------------+

    REPL 命令：/compact /tasks /team /inbox
"""

import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from queue import Queue

from anthropic import Anthropic
from dotenv import load_dotenv

# 从 `.env` 读取模型 ID、网关地址等配置，便于示例独立运行。
load_dotenv(override=True)
# 如果使用的是兼容 Anthropic 协议的代理网关，官方 token 环境变量有时会与之冲突，
# 这里沿用前面章节的做法：检测到自定义 base URL 时先移除它。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 所有状态都围绕当前工作区展开：
# 1. 普通文件操作只能落在 WORKDIR 内；
# 2. `.team` 保存队友配置和收件箱；
# 3. `.tasks` 是独立于对话历史之外的任务板；
# 4. `.transcripts` 用来保存压缩前的完整转录。
WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"
TASKS_DIR = WORKDIR / ".tasks"
SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOKEN_THRESHOLD = 100000
POLL_INTERVAL = 5
IDLE_TIMEOUT = 60

# 统一声明消息类型，方便 lead 和 teammate 共用同一套收件箱协议。
VALID_MSG_TYPES = {"message", "broadcast", "shutdown_request",
                   "shutdown_response", "plan_approval_response"}


# === SECTION: 基础工具（base_tools） ===
# 这一层是所有更高层能力的地基。
# 无论是主代理、子代理还是队友，最终执行文件读写或命令时都会落到这里。
def safe_path(p: str) -> Path:
    # 先把相对路径解析到 WORKDIR 内，再阻止越界访问工作区外的路径。
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    # 教学示例里只做粗粒度危险命令拦截，重点是展示“工具调用 -> 本地执行”的通路。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        # stdout / stderr 合并后统一截断，避免一条工具结果把上下文塞满。
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int = None) -> str:
    try:
        # 先做路径校验，再读文件；`limit` 用来只取前若干行，降低上下文开销。
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        # 写文件前自动补父目录，减少模型还要额外 mkdir 的负担。
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        c = fp.read_text()
        # 为了让编辑语义保持明确，这里仍采用“找到旧文本后只替换一次”的最小补丁模型。
        if old_text not in c:
            return f"Error: Text not found in {path}"
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# === SECTION: Todo 清单（s03） ===
# TodoManager 管的是“短期、会话内”的工作清单。
# 它和 `.tasks` 的区别是：todo 更轻量，偏提醒；文件任务板更持久，适合跨轮次协作。
class TodoManager:
    def __init__(self):
        self.items = []

    def update(self, items: list) -> str:
        # 每次更新都做一次完整校验，确保模型不能塞入结构不合法的 todo。
        validated, ip = [], 0
        for i, item in enumerate(items):
            content = str(item.get("content", "")).strip()
            status = str(item.get("status", "pending")).lower()
            af = str(item.get("activeForm", "")).strip()
            if not content: raise ValueError(f"Item {i}: content required")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {i}: invalid status '{status}'")
            if not af: raise ValueError(f"Item {i}: activeForm required")
            if status == "in_progress": ip += 1
            validated.append({"content": content, "status": status, "activeForm": af})
        if len(validated) > 20: raise ValueError("Max 20 todos")
        if ip > 1: raise ValueError("Only one in_progress allowed")
        self.items = validated
        return self.render()

    def render(self) -> str:
        # 渲染给模型看的文本版 todo 面板。
        # `activeForm` 只在 in_progress 时显示，提醒模型“现在正在做什么动作”。
        if not self.items: return "No todos."
        lines = []
        for item in self.items:
            m = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}.get(item["status"], "[?]")
            suffix = f" <- {item['activeForm']}" if item["status"] == "in_progress" else ""
            lines.append(f"{m} {item['content']}{suffix}")
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)

    def has_open_items(self) -> bool:
        return any(item.get("status") != "completed" for item in self.items)


# === SECTION: 子代理（s04） ===
# `task` 工具最终会调用这里。
# 这个子代理是“一次性”的：创建一段独立上下文，做完任务后只返回摘要，不长期驻留。
def run_subagent(prompt: str, agent_type: str = "Explore") -> str:
    # Explore 模式默认只有读和命令能力，避免子代理一上来就改文件。
    sub_tools = [
        {"name": "bash", "description": "Run command.",
         "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
        {"name": "read_file", "description": "Read file.",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    ]
    if agent_type != "Explore":
        # 非 Explore 模式才给写入能力，表示这个子代理可以承担实际修改工作。
        sub_tools += [
            {"name": "write_file", "description": "Write file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            {"name": "edit_file", "description": "Edit file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
        ]
    sub_handlers = {
        "bash": lambda **kw: run_bash(kw["command"]),
        "read_file": lambda **kw: run_read(kw["path"]),
        "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
        "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    }
    # 子代理维护自己的局部 messages，不污染主代理上下文。
    sub_msgs = [{"role": "user", "content": prompt}]
    resp = None
    for _ in range(30):
        # 子代理也走与主代理相同的“模型 -> 工具 -> 工具结果”闭环，只是工具面更小。
        resp = client.messages.create(model=MODEL, messages=sub_msgs, tools=sub_tools, max_tokens=8000)
        sub_msgs.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            break
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                h = sub_handlers.get(b.name, lambda **kw: "Unknown tool")
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": str(h(**b.input))[:50000]})
        sub_msgs.append({"role": "user", "content": results})
    if resp:
        # 主代理最终只拿到子代理的文字总结，而不是整段子上下文。
        return "".join(b.text for b in resp.content if hasattr(b, "text")) or "(no summary)"
    return "(subagent failed)"


# === SECTION: 技能加载（s05） ===
# SkillLoader 负责把 `skills/**/SKILL.md` 扫出来，供 system prompt 展示和按需加载。
class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills = {}
        if skills_dir.exists():
            for f in sorted(skills_dir.rglob("SKILL.md")):
                text = f.read_text()
                # 允许技能文件带一个很轻量的 front matter，便于提取 name / description。
                match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
                meta, body = {}, text
                if match:
                    for line in match.group(1).strip().splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            meta[k.strip()] = v.strip()
                    body = match.group(2).strip()
                name = meta.get("name", f.parent.name)
                self.skills[name] = {"meta": meta, "body": body}

    def descriptions(self) -> str:
        # 这个列表会拼进主 SYSTEM，告诉模型当前有哪些技能可调用。
        if not self.skills: return "(no skills)"
        return "\n".join(f"  - {n}: {s['meta'].get('description', '-')}" for n, s in self.skills.items())

    def load(self, name: str) -> str:
        # 真正加载时，把技能正文包在 `<skill>` 标签里，作为结构化上下文注入模型。
        s = self.skills.get(name)
        if not s: return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
        return f"<skill name=\"{name}\">\n{s['body']}\n</skill>"


# === SECTION: 上下文压缩（s06） ===
# 这一层处理的是“对话越来越长之后，如何继续跑下去”。
def estimate_tokens(messages: list) -> int:
    # 这里只做非常粗略的 token 估算，够用来触发阈值即可。
    return len(json.dumps(messages, default=str)) // 4

def microcompact(messages: list):
    # 微压缩不改消息结构，只清掉较旧且很长的 tool_result 文本，
    # 目的是在不打断当前会话的前提下，先做一次廉价瘦身。
    indices = []
    for i, msg in enumerate(messages):
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            for part in msg["content"]:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    indices.append(part)
    if len(indices) <= 3:
        return
    # 保留最近 3 条工具结果，较旧的长输出改成占位符。
    for part in indices[:-3]:
        if isinstance(part.get("content"), str) and len(part["content"]) > 100:
            part["content"] = "[cleared]"

def auto_compact(messages: list) -> list:
    # 自动压缩是“重做上下文”的重手段：
    # 1. 先把原始消息完整落盘到 transcript；
    # 2. 再让模型生成一段连续性摘要；
    # 3. 最后只保留“摘要 + 一个确认回复”作为新的上下文起点。
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    conv_text = json.dumps(messages, default=str)[:80000]
    resp = client.messages.create(
        model=MODEL,
        messages=[{"role": "user", "content": f"Summarize for continuity:\n{conv_text}"}],
        max_tokens=2000,
    )
    summary = resp.content[0].text
    return [
        {"role": "user", "content": f"[Compressed. Transcript: {path}]\n{summary}"},
        {"role": "assistant", "content": "Understood. Continuing with summary context."},
    ]


# === SECTION: 文件任务板（s07） ===
# 和 TodoWrite 相比，这里的任务会持久化到 `.tasks/task_<id>.json`，
# 因此更适合跨轮次、跨代理协作。
class TaskManager:
    def __init__(self):
        # 任务板目录不存在时自动创建，保证后续所有操作有统一落点。
        TASKS_DIR.mkdir(exist_ok=True)

    def _next_id(self) -> int:
        # 任务 ID 直接来自现有文件名，简单但足够直观。
        ids = [int(f.stem.split("_")[1]) for f in TASKS_DIR.glob("task_*.json")]
        return max(ids, default=0) + 1

    def _load(self, tid: int) -> dict:
        p = TASKS_DIR / f"task_{tid}.json"
        if not p.exists(): raise ValueError(f"Task {tid} not found")
        return json.loads(p.read_text())

    def _save(self, task: dict):
        (TASKS_DIR / f"task_{task['id']}.json").write_text(json.dumps(task, indent=2))

    def create(self, subject: str, description: str = "") -> str:
        # 新任务默认是 pending、无 owner、无依赖。
        task = {"id": self._next_id(), "subject": subject, "description": description,
                "status": "pending", "owner": None, "blockedBy": [], "blocks": []}
        self._save(task)
        return json.dumps(task, indent=2)

    def get(self, tid: int) -> str:
        return json.dumps(self._load(tid), indent=2)

    def update(self, tid: int, status: str = None,
               add_blocked_by: list = None, add_blocks: list = None) -> str:
        # `update(...)` 既处理状态变化，也处理任务之间的依赖关系。
        task = self._load(tid)
        if status:
            task["status"] = status
            if status == "completed":
                # 某个任务完成后，把其他任务里对它的 blockedBy 引用顺手移除。
                for f in TASKS_DIR.glob("task_*.json"):
                    t = json.loads(f.read_text())
                    if tid in t.get("blockedBy", []):
                        t["blockedBy"].remove(tid)
                        self._save(t)
            if status == "deleted":
                (TASKS_DIR / f"task_{tid}.json").unlink(missing_ok=True)
                return f"Task {tid} deleted"
        if add_blocked_by:
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
        if add_blocks:
            task["blocks"] = list(set(task["blocks"] + add_blocks))
        self._save(task)
        return json.dumps(task, indent=2)

    def list_all(self) -> str:
        # 面向终端和模型都可读的简表视图。
        tasks = [json.loads(f.read_text()) for f in sorted(TASKS_DIR.glob("task_*.json"))]
        if not tasks: return "No tasks."
        lines = []
        for t in tasks:
            m = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
            owner = f" @{t['owner']}" if t.get("owner") else ""
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            lines.append(f"{m} #{t['id']}: {t['subject']}{owner}{blocked}")
        return "\n".join(lines)

    def claim(self, tid: int, owner: str) -> str:
        # 被认领后，任务立即进入 in_progress。
        task = self._load(tid)
        task["owner"] = owner
        task["status"] = "in_progress"
        self._save(task)
        return f"Claimed task #{tid} for {owner}"


# === SECTION: 后台任务（s08） ===
# 这部分把“会阻塞当前回合的命令”改造成后台线程，让主代理可以继续对话。
class BackgroundManager:
    def __init__(self):
        # `tasks` 保存完整状态；`notifications` 只保存“需要注入主上下文的摘要通知”。
        self.tasks = {}
        self.notifications = Queue()

    def run(self, command: str, timeout: int = 120) -> str:
        # 前台只负责登记任务并拉起线程，真正执行放到 `_exec(...)`。
        tid = str(uuid.uuid4())[:8]
        self.tasks[tid] = {"status": "running", "command": command, "result": None}
        threading.Thread(target=self._exec, args=(tid, command, timeout), daemon=True).start()
        return f"Background task {tid} started: {command[:80]}"

    def _exec(self, tid: str, command: str, timeout: int):
        try:
            # 后台线程执行完成后，把结果写回状态表。
            r = subprocess.run(command, shell=True, cwd=WORKDIR,
                               capture_output=True, text=True, timeout=timeout)
            output = (r.stdout + r.stderr).strip()[:50000]
            self.tasks[tid].update({"status": "completed", "result": output or "(no output)"})
        except Exception as e:
            self.tasks[tid].update({"status": "error", "result": str(e)})
        # 无论成功失败，都推一条简短通知到队列里，等待主循环下次统一消费。
        self.notifications.put({"task_id": tid, "status": self.tasks[tid]["status"],
                                "result": self.tasks[tid]["result"][:500]})

    def check(self, tid: str = None) -> str:
        # 支持查看单个任务详情，也支持列出全部后台任务。
        if tid:
            t = self.tasks.get(tid)
            return f"[{t['status']}] {t.get('result', '(running)')}" if t else f"Unknown: {tid}"
        return "\n".join(f"{k}: [{v['status']}] {v['command'][:60]}" for k, v in self.tasks.items()) or "No bg tasks."

    def drain(self) -> list:
        # 主循环每轮会把通知队列一次性清空，然后统一注入到 messages 里。
        notifs = []
        while not self.notifications.empty():
            notifs.append(self.notifications.get_nowait())
        return notifs


# === SECTION: 邮箱消息总线（s09） ===
# lead 和 teammate 的异步协作都依赖这层。
# 设计上非常简单：每个人一个 JSONL 收件箱，发送就是追加一行，读取就是读完清空。
class MessageBus:
    def __init__(self):
        INBOX_DIR.mkdir(parents=True, exist_ok=True)

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        # `extra` 预留给协议字段，例如 request_id。
        msg = {"type": msg_type, "from": sender, "content": content,
               "timestamp": time.time()}
        if extra: msg.update(extra)
        with open(INBOX_DIR / f"{to}.jsonl", "a") as f:
            f.write(json.dumps(msg) + "\n")
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list:
        # 这里采用 drain 语义：读完后立刻清空，避免同一批消息重复消费。
        path = INBOX_DIR / f"{name}.jsonl"
        if not path.exists(): return []
        msgs = [json.loads(l) for l in path.read_text().strip().splitlines() if l]
        path.write_text("")
        return msgs

    def broadcast(self, sender: str, content: str, names: list) -> str:
        # 广播本质上仍然是对每个目标重复调用 send(...)。
        count = 0
        for n in names:
            if n != sender:
                self.send(sender, n, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"


# === SECTION: 关机 / 计划审批状态（s10） ===
# 这里保存协议相关的“对话外状态”。
# 注意：本文件主要整合的是状态结构和 lead 侧入口，完整协议往返细节可对照 s10 单独章节理解。
shutdown_requests = {}
plan_requests = {}


# === SECTION: 常驻队友（s09 / s11） ===
# TeammateManager 管的是“长期存在的队友线程”，不是一次性子代理。
# 每个队友有：
# 1. 自己的名字、角色、状态；
# 2. 自己的 inbox；
# 3. 自己独立积累的 messages；
# 4. 空闲时自动轮询新消息与可认领任务的能力。
class TeammateManager:
    def __init__(self, bus: MessageBus, task_mgr: TaskManager):
        TEAM_DIR.mkdir(exist_ok=True)
        self.bus = bus
        self.task_mgr = task_mgr
        self.config_path = TEAM_DIR / "config.json"
        # `config.json` 是持久化名册；`threads` 是当前进程里的真实线程对象。
        self.config = self._load()
        self.threads = {}

    def _load(self) -> dict:
        # 优先恢复已有团队名册；首次运行时再给一个默认空团队。
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}

    def _save(self):
        # 任何状态变化都立即落盘，避免只存在于内存里。
        self.config_path.write_text(json.dumps(self.config, indent=2))

    def _find(self, name: str) -> dict:
        # 教学场景里成员数量不大，线性查找足够直观。
        for m in self.config["members"]:
            if m["name"] == name: return m
        return None

    def spawn(self, name: str, role: str, prompt: str) -> str:
        # `spawn(...)` 兼顾两件事：
        # 1. 首次创建一个队友；
        # 2. 把之前 idle / shutdown 的队友重新唤醒。
        member = self._find(name)
        if member:
            if member["status"] not in ("idle", "shutdown"):
                return f"Error: '{name}' is currently {member['status']}"
            member["status"] = "working"
            member["role"] = role
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save()
        # 真正的队友生命周期在后台线程里运行，lead 不会阻塞等待。
        threading.Thread(target=self._loop, args=(name, role, prompt), daemon=True).start()
        return f"Spawned '{name}' (role: {role})"

    def _set_status(self, name: str, status: str):
        # 统一状态切换入口，确保每次变化都会同步写回 config.json。
        member = self._find(name)
        if member:
            member["status"] = status
            self._save()

    def _loop(self, name: str, role: str, prompt: str):
        # 每个队友都有自己的 system prompt 和独立上下文。
        team_name = self.config["team_name"]
        sys_prompt = (f"You are '{name}', role: {role}, team: {team_name}, at {WORKDIR}. "
                      f"Use idle when done with current work. You may auto-claim tasks.")
        messages = [{"role": "user", "content": prompt}]
        # 队友能用的工具比 lead 少一层编排能力，重点在“执行工作”和“接手任务”。
        tools = [
            {"name": "bash", "description": "Run command.", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
            {"name": "read_file", "description": "Read file.", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
            {"name": "write_file", "description": "Write file.", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            {"name": "edit_file", "description": "Edit file.", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
            {"name": "send_message", "description": "Send message.", "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}}, "required": ["to", "content"]}},
            {"name": "idle", "description": "Signal no more work.", "input_schema": {"type": "object", "properties": {}}},
            {"name": "claim_task", "description": "Claim task by ID.", "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
        ]
        while True:
            # -- 工作阶段 --
            # 工作阶段的职责是：基于当前上下文推进任务。
            # 只要模型还在请求工具，这个阶段就会持续小循环。
            for _ in range(50):
                # 先看 inbox。这样队友即使正在工作，也能被外部消息“插入”新上下文。
                inbox = self.bus.read_inbox(name)
                for msg in inbox:
                    # 本整合版里，shutdown_request 被简化为“收到即退出”。
                    if msg.get("type") == "shutdown_request":
                        self._set_status(name, "shutdown")
                        return
                    messages.append({"role": "user", "content": json.dumps(msg)})
                try:
                    response = client.messages.create(
                        model=MODEL, system=sys_prompt, messages=messages,
                        tools=tools, max_tokens=8000)
                except Exception:
                    # 教学实现中，线程内异常直接转成 shutdown，避免线程悬空。
                    self._set_status(name, "shutdown")
                    return
                # 不管是普通文本还是 tool_use，都先写入队友自己的历史。
                messages.append({"role": "assistant", "content": response.content})
                if response.stop_reason != "tool_use":
                    # 如果模型已经不再要工具，说明“当前工作块”暂时结束。
                    break
                results = []
                idle_requested = False
                for block in response.content:
                    if block.type == "tool_use":
                        if block.name == "idle":
                            idle_requested = True
                            output = "Entering idle phase."
                        elif block.name == "claim_task":
                            output = self.task_mgr.claim(block.input["task_id"], name)
                        elif block.name == "send_message":
                            output = self.bus.send(name, block.input["to"], block.input["content"])
                        else:
                            # 基础工具仍然复用全局实现，只是由队友的 tool schema 暴露出来。
                            dispatch = {"bash": lambda **kw: run_bash(kw["command"]),
                                        "read_file": lambda **kw: run_read(kw["path"]),
                                        "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
                                        "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"])}
                            output = dispatch.get(block.name, lambda **kw: "Unknown")(**block.input)
                        print(f"  [{name}] {block.name}: {str(output)[:120]}")
                        results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
                messages.append({"role": "user", "content": results})
                if idle_requested:
                    # 模型显式调用了 idle，说明它认为自己已完成当前阶段性工作。
                    break
            # -- 空闲阶段：轮询新消息与可认领任务 --
            # 这里体现了 s11 的关键思想：队友不是干完就消失，而是可以空闲等待下一件事。
            self._set_status(name, "idle")
            resume = False
            for _ in range(IDLE_TIMEOUT // max(POLL_INTERVAL, 1)):
                time.sleep(POLL_INTERVAL)
                # 先检查有没有新消息。有的话立即恢复到工作阶段。
                inbox = self.bus.read_inbox(name)
                if inbox:
                    for msg in inbox:
                        if msg.get("type") == "shutdown_request":
                            self._set_status(name, "shutdown")
                            return
                        messages.append({"role": "user", "content": json.dumps(msg)})
                    resume = True
                    break
                # 再扫描任务板，寻找还没人接手、也没有依赖阻塞的任务。
                unclaimed = []
                for f in sorted(TASKS_DIR.glob("task_*.json")):
                    t = json.loads(f.read_text())
                    if t.get("status") == "pending" and not t.get("owner") and not t.get("blockedBy"):
                        unclaimed.append(t)
                if unclaimed:
                    task = unclaimed[0]
                    self.task_mgr.claim(task["id"], name)
                    # 身份重注入：如果上下文因为压缩等原因变得很短，就先提醒模型“你是谁”。
                    if len(messages) <= 3:
                        messages.insert(0, {"role": "user", "content":
                            f"<identity>You are '{name}', role: {role}, team: {team_name}.</identity>"})
                        messages.insert(1, {"role": "assistant", "content": f"I am {name}. Continuing."})
                    # 自动认领的任务也会以结构化标签注入上下文，方便模型立刻进入执行状态。
                    messages.append({"role": "user", "content":
                        f"<auto-claimed>Task #{task['id']}: {task['subject']}\n{task.get('description', '')}</auto-claimed>"})
                    messages.append({"role": "assistant", "content": f"Claimed task #{task['id']}. Working on it."})
                    resume = True
                    break
            if not resume:
                # 超过 idle 超时时间仍然没有消息、也没有任务，就主动结束该队友。
                self._set_status(name, "shutdown")
                return
            # 一旦有新工作，就重新标回 working，进入下一轮 while True。
            self._set_status(name, "working")

    def list_all(self) -> str:
        # 提供给 `/team` 命令和 `list_teammates` 工具复用。
        if not self.config["members"]: return "No teammates."
        lines = [f"Team: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        return "\n".join(lines)

    def member_names(self) -> list:
        # 广播时只需要一个名字数组。
        return [m["name"] for m in self.config["members"]]


# === SECTION: 全局实例 ===
# 这些对象在整个进程生命周期里共享，相当于主代理运行时的“外部部件”。
TODO = TodoManager()
SKILLS = SkillLoader(SKILLS_DIR)
TASK_MGR = TaskManager()
BG = BackgroundManager()
BUS = MessageBus()
TEAM = TeammateManager(BUS, TASK_MGR)

# === SECTION: 主系统提示词 ===
# 这个 SYSTEM 会在每次主模型调用时重复传入，告诉模型当前有哪些高层工作习惯和工具。
SYSTEM = f"""You are a coding agent at {WORKDIR}. Use tools to solve tasks.
Prefer task_create/task_update/task_list for multi-step work. Use TodoWrite for short checklists.
Use task for subagent delegation. Use load_skill for specialized knowledge.
Skills: {SKILLS.descriptions()}"""


# === SECTION: 关机入口（s10） ===
def handle_shutdown_request(teammate: str) -> str:
    # lead 发起关机时：
    # 1. 先生成一个 request_id；
    # 2. 记录到 shutdown_requests；
    # 3. 再向目标队友发一条 shutdown_request。
    # 本整合版的队友收到后会直接退出，因此这里的 request_id 更像“跟踪记录”。
    req_id = str(uuid.uuid4())[:8]
    shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send("lead", teammate, "Please shut down.", "shutdown_request", {"request_id": req_id})
    return f"Shutdown request {req_id} sent to '{teammate}'"

# === SECTION: 计划审批入口（s10） ===
def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    # 按 request_id 找到待审批记录，更新状态，然后把结果通过 inbox 回给发起者。
    req = plan_requests.get(request_id)
    if not req: return f"Error: Unknown plan request_id '{request_id}'"
    req["status"] = "approved" if approve else "rejected"
    BUS.send("lead", req["from"], feedback, "plan_approval_response",
             {"request_id": request_id, "approve": approve, "feedback": feedback})
    return f"Plan {req['status']} for '{req['from']}'"


# === SECTION: 工具分发表（s02） ===
# 这是“模型说工具名”与“本地实际执行函数”之间的桥梁。
TOOL_HANDLERS = {
    "bash":             lambda **kw: run_bash(kw["command"]),
    "read_file":        lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file":       lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":        lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "TodoWrite":        lambda **kw: TODO.update(kw["items"]),
    "task":             lambda **kw: run_subagent(kw["prompt"], kw.get("agent_type", "Explore")),
    "load_skill":       lambda **kw: SKILLS.load(kw["name"]),
    "compress":         lambda **kw: "Compressing...",
    "background_run":   lambda **kw: BG.run(kw["command"], kw.get("timeout", 120)),
    "check_background": lambda **kw: BG.check(kw.get("task_id")),
    "task_create":      lambda **kw: TASK_MGR.create(kw["subject"], kw.get("description", "")),
    "task_get":         lambda **kw: TASK_MGR.get(kw["task_id"]),
    "task_update":      lambda **kw: TASK_MGR.update(kw["task_id"], kw.get("status"), kw.get("add_blocked_by"), kw.get("add_blocks")),
    "task_list":        lambda **kw: TASK_MGR.list_all(),
    "spawn_teammate":   lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),
    "list_teammates":   lambda **kw: TEAM.list_all(),
    "send_message":     lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    "read_inbox":       lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2),
    "broadcast":        lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),
    "shutdown_request": lambda **kw: handle_shutdown_request(kw["teammate"]),
    "plan_approval":    lambda **kw: handle_plan_review(kw["request_id"], kw["approve"], kw.get("feedback", "")),
    "idle":             lambda **kw: "Lead does not idle.",
    "claim_task":       lambda **kw: TASK_MGR.claim(kw["task_id"], "lead"),
}

# `TOOLS` 是暴露给模型看的 schema；
# `TOOL_HANDLERS` 是模型真正调到本地时的执行实现。
# 两者一一对应：前者决定模型“知道什么”，后者决定程序“怎么做”。
TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "TodoWrite", "description": "Update task tracking list.",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}, "activeForm": {"type": "string"}}, "required": ["content", "status", "activeForm"]}}}, "required": ["items"]}},
    {"name": "task", "description": "Spawn a subagent for isolated exploration or work.",
     "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}, "agent_type": {"type": "string", "enum": ["Explore", "general-purpose"]}}, "required": ["prompt"]}},
    {"name": "load_skill", "description": "Load specialized knowledge by name.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "compress", "description": "Manually compress conversation context.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "background_run", "description": "Run command in background thread.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["command"]}},
    {"name": "check_background", "description": "Check background task status.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}}},
    {"name": "task_create", "description": "Create a persistent file task.",
     "input_schema": {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}}, "required": ["subject"]}},
    {"name": "task_get", "description": "Get task details by ID.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
    {"name": "task_update", "description": "Update task status or dependencies.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "deleted"]}, "add_blocked_by": {"type": "array", "items": {"type": "integer"}}, "add_blocks": {"type": "array", "items": {"type": "integer"}}}, "required": ["task_id"]}},
    {"name": "task_list", "description": "List all tasks.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "spawn_teammate", "description": "Spawn a persistent autonomous teammate.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}},
    {"name": "list_teammates", "description": "List all teammates.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "send_message", "description": "Send a message to a teammate.",
     "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
    {"name": "read_inbox", "description": "Read and drain the lead's inbox.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "broadcast", "description": "Send message to all teammates.",
     "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
    {"name": "shutdown_request", "description": "Request a teammate to shut down.",
     "input_schema": {"type": "object", "properties": {"teammate": {"type": "string"}}, "required": ["teammate"]}},
    {"name": "plan_approval", "description": "Approve or reject a teammate's plan.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "feedback": {"type": "string"}}, "required": ["request_id", "approve"]}},
    {"name": "idle", "description": "Enter idle state.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "claim_task", "description": "Claim a task from the board.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
]


# === SECTION: 主代理循环 ===
# 这是整份文件最重要的阅读入口。
# 每一轮 while True 基本都遵循下面这个顺序：
# 1. 先做上下文维护（微压缩 / 自动压缩）；
# 2. 再回收后台结果、读取 inbox，把外部状态注入消息历史；
# 3. 调用主模型；
# 4. 如果模型请求工具，就执行工具并把结果回写；
# 5. 如果模型直接给出文本回复，则本轮结束。
def agent_loop(messages: list):
    rounds_without_todo = 0
    while True:
        # s06：压缩管线先执行，尽量在真正爆上下文前先做预处理。
        microcompact(messages)
        if estimate_tokens(messages) > TOKEN_THRESHOLD:
            print("[auto-compact triggered]")
            messages[:] = auto_compact(messages)
        # s08：把后台任务完成通知注入上下文，避免模型错过异步结果。
        notifs = BG.drain()
        if notifs:
            txt = "\n".join(f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs)
            messages.append({"role": "user", "content": f"<background-results>\n{txt}\n</background-results>"})
            messages.append({"role": "assistant", "content": "Noted background results."})
        # s09 / s10：lead 每轮都检查自己的 inbox，这样队友的消息会及时进入推理链路。
        inbox = BUS.read_inbox("lead")
        if inbox:
            messages.append({"role": "user", "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>"})
            messages.append({"role": "assistant", "content": "Noted inbox messages."})
        # 真正调用主模型。此时上下文里已经混入了用户历史、工具结果、后台结果、队友消息等。
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            # 模型没有再要工具，说明这一轮已经走到可直接返回的文本终点。
            return
        # 进入“执行工具并回写结果”的阶段。
        results = []
        used_todo = False
        manual_compress = False
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "compress":
                    # `compress` 先记一个标记，等本轮工具结果回写之后再真正压缩。
                    manual_compress = True
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                except Exception as e:
                    output = f"Error: {e}"
                print(f"> {block.name}: {str(output)[:200]}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
                if block.name == "TodoWrite":
                    used_todo = True
        # s03：如果 todo 面板里还有未完成项，但模型连续几轮没更新，就插入提醒文本。
        rounds_without_todo = 0 if used_todo else rounds_without_todo + 1
        if TODO.has_open_items() and rounds_without_todo >= 3:
            results.insert(0, {"type": "text", "text": "<reminder>Update your todos.</reminder>"})
        # 工具结果作为新的 user turn 回写，这是 agent loop 闭环的关键一步。
        messages.append({"role": "user", "content": results})
        # s06：手动压缩总是放在回写之后做，避免丢掉当前这轮刚产生的信息。
        if manual_compress:
            print("[manual compact]")
            messages[:] = auto_compact(messages)


# === SECTION: REPL 外壳 ===
# 这层只负责从终端接收输入、处理少量斜杠命令，并把普通输入送进 `agent_loop(...)`。
if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms_full >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        if query.strip() == "/compact":
            # 手动触发上下文压缩，方便观察压缩后的继续运行效果。
            if history:
                print("[manual compact via /compact]")
                history[:] = auto_compact(history)
            continue
        if query.strip() == "/tasks":
            # 直接查看文件任务板当前状态，不需要经过模型。
            print(TASK_MGR.list_all())
            continue
        if query.strip() == "/team":
            # 查看当前团队成员及其工作状态。
            print(TEAM.list_all())
            continue
        if query.strip() == "/inbox":
            # 手动读取 lead inbox，便于调试队友与 lead 的通信。
            print(json.dumps(BUS.read_inbox("lead"), indent=2))
            continue
        # 普通输入进入历史，然后交给主代理循环处理。
        history.append({"role": "user", "content": query})
        agent_loop(history)
        print()
