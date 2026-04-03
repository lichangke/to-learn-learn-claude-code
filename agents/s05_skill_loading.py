#!/usr/bin/env python3
# Harness: 按需加载知识：模型需要时再注入领域技能
"""
s05_skill_loading.py - Skills

Two-layer skill injection that avoids bloating the system prompt:

    Layer 1 (cheap): skill names in system prompt (~100 tokens/skill)
    Layer 2 (on demand): full skill body in tool_result

    skills/
      pdf/
        SKILL.md          <-- frontmatter (name, description) + body
      code-review/
        SKILL.md

    System prompt:
    +--------------------------------------+
    | You are a coding agent.              |
    | Skills available:                    |
    |   - pdf: Process PDF files...        |  <-- Layer 1: metadata only
    |   - code-review: Review code...      |
    +--------------------------------------+

    When model calls load_skill("pdf"):
    +--------------------------------------+
    | tool_result:                         |
    | <skill>                              |
    |   Full PDF processing instructions   |  <-- Layer 2: full body
    |   Step 1: ...                        |
    |   Step 2: ...                        |
    | </skill>                             |
    +--------------------------------------+

Key insight: "Don't put everything in the system prompt. Load on demand."

这一节要解决的问题是：
    如果我们把所有领域知识、最佳实践、操作说明都直接塞进 system prompt，
    prompt 会越来越长，越来越贵，而且很多知识在当前任务里根本用不到。

这份示例给出的做法是“两层注入”：

1. 第 1 层只把技能的名字、简介、标签放进 system prompt
   目的不是把知识一次性讲完，而是先告诉模型：
   “当前环境里有哪些可随时调用的专业能力”。

2. 第 2 层在模型真正需要时，再通过 `load_skill(name)` 工具按需加载完整内容
   这样只有在模型主动判断“这个技能现在有用”时，
   对应的详细说明才会进入当前轮对话上下文。

于是整体执行链路会变成：

    程序启动
        -> 扫描 skills/**/SKILL.md
        -> 解析 frontmatter，收集技能元数据
        -> 把“技能名称 + 简介”拼进 SYSTEM
        -> 模型先基于这些简要信息思考
        -> 如果模型觉得某项技能有帮助，就调用 load_skill("xxx")
        -> 宿主程序返回该技能的完整正文
        -> 模型在下一轮基于完整技能内容继续完成任务

和上一节 `s04_subagent.py` 对照着看：

- `s04` 的重点是“把任务拆给子 Agent，隔离上下文”
- `s05` 的重点是“把知识拆成元数据 + 正文，按需注入上下文”

也就是说，这一节隔离的不是“任务执行过程”，而是“知识注入时机”。
"""

import os
import re
import subprocess
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

# 和前几节一样：如果走了兼容 Anthropic 协议的自定义网关，
# 某些默认认证变量可能反而会干扰请求。
# 所以这里只要发现自定义 base URL，就顺手清掉可能冲突的 token。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 当前工作目录既是文件工具的根目录，也是 skills/ 的搜索根目录。
# 也就是说，技能文件和普通代码文件一样，都从当前仓库向下解析。
WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
# 所有技能默认都放在 skills/<skill-name>/SKILL.md 下面。
SKILLS_DIR = WORKDIR / "skills"


# -- SkillLoader: scan skills/<name>/SKILL.md with YAML frontmatter --
class SkillLoader:
    def __init__(self, skills_dir: Path):
        # `skills` 是一个内存中的索引表，结构大致是：
        # {
        #   "pdf": {
        #       "meta": {...},
        #       "body": "...",
        #       "path": "..."
        #   }
        # }
        self.skills_dir = skills_dir
        self.skills = {}
        # 在程序启动时一次性扫描所有技能文件。
        # 后续 `load_skill(...)` 查询的都是这份内存索引，
        # 而不是每次调用工具都重新扫磁盘。
        self._load_all()

    def _load_all(self):
        # 如果仓库里根本没有 skills/ 目录，就保持空表即可。
        if not self.skills_dir.exists():
            return
        # 递归查找所有名为 SKILL.md 的文件，这样每个技能可以各自放在独立目录里。
        # `sorted(...)` 让加载顺序稳定，便于调试和复现。
        for f in sorted(self.skills_dir.rglob("SKILL.md")):
            text = f.read_text()
            # 一个技能文件会被拆成两部分：
            # 1. meta: frontmatter 里的名称、描述、标签等
            # 2. body: 真正要按需注入给模型的详细说明正文
            meta, body = self._parse_frontmatter(text)
            # 如果 frontmatter 里没写 name，就退回到目录名作为技能名。
            name = meta.get("name", f.parent.name)
            self.skills[name] = {"meta": meta, "body": body, "path": str(f)}

    def _parse_frontmatter(self, text: str) -> tuple:
        """Parse YAML frontmatter between --- delimiters."""
        # 这里为了教学，故意只实现了一个“够用的极简 frontmatter 解析器”。
        # 它只识别：
        #
        # ---
        # name: pdf
        # description: Process PDF files
        # tags: documents,ocr
        # ---
        # 后面的正文内容...
        #
        # 它不是完整 YAML 解析器：
        # - 不支持复杂嵌套
        # - 不支持列表/多行字符串等高级语法
        # 但对这个示例已经足够。
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            # 如果没有 frontmatter，就把整份文件当作正文，元数据为空。
            return {}, text
        meta = {}
        for line in match.group(1).strip().splitlines():
            if ":" in line:
                # 这里只按第一个冒号分割，避免 value 里再出现冒号时被截坏。
                key, val = line.split(":", 1)
                meta[key.strip()] = val.strip()
        return meta, match.group(2).strip()

    def get_descriptions(self) -> str:
        """Layer 1: short descriptions for the system prompt."""
        # 这一层只返回“便宜”的简述信息，供 system prompt 使用。
        # 模型会先知道“有哪些技能可用”，但还没拿到完整技能内容。
        if not self.skills:
            return "(no skills available)"
        lines = []
        for name, skill in self.skills.items():
            desc = skill["meta"].get("description", "No description")
            tags = skill["meta"].get("tags", "")
            line = f"  - {name}: {desc}"
            # tags 不是必须项，但放进 system prompt 能帮助模型更快判断是否该加载此技能。
            if tags:
                line += f" [{tags}]"
            lines.append(line)
        return "\n".join(lines)

    def get_content(self, name: str) -> str:
        """Layer 2: full skill body returned in tool_result."""
        # 这是按需加载的核心：
        # 模型只有显式调用 `load_skill(name)`，才能拿到这里返回的正文。
        skill = self.skills.get(name)
        if not skill:
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
        # 用 `<skill ...>...</skill>` 包一层，有两个好处：
        # 1. 让模型更容易识别“这是一段技能文档，不是普通闲聊文本”
        # 2. 以后如果要扩展成多种知识块格式，也更容易做结构化区分
        return f"<skill name=\"{name}\">\n{skill['body']}\n</skill>"


# 注意：这里在模块加载时就会扫描一次技能目录。
# 这意味着本进程启动后新增/修改技能文件，默认不会自动热更新，
# 除非重启脚本或手动补一个重新加载机制。
SKILL_LOADER = SkillLoader(SKILLS_DIR)

# Layer 1: skill metadata injected into system prompt
# system prompt 里只注入“技能菜单”，而不注入完整技能正文。
# 这样模型一开始就知道有哪些能力可用，但不会被大量暂时无关的说明撑爆上下文。
SYSTEM = f"""You are a coding agent at {WORKDIR}.
Use load_skill to access specialized knowledge before tackling unfamiliar topics.

Skills available:
{SKILL_LOADER.get_descriptions()}"""


# -- Tool implementations --
def safe_path(p: str) -> Path:
    # 和前几节保持一致：所有文件访问都限制在当前工作区内部，
    # 防止模型通过相对路径跳出仓库边界。
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    # 示例级别的最小危险命令拦截。
    # 重点不是做完备沙箱，而是让例子运行时尽量避免明显危险操作。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        # shell 命令统一在工作区根目录执行，方便模型形成稳定预期。
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        # 截断超长输出，避免一次工具结果把上下文塞得过满。
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int = None) -> str:
    try:
        # 先做路径校验，再读文件。
        lines = safe_path(path).read_text().splitlines()
        # `limit` 允许模型先试探性查看前 N 行，而不是一口气读取整个大文件。
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        # 自动创建父目录，减少模型写文件时需要额外处理的样板步骤。
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text()
        # 这里依旧使用“精确文本替换一次”的简化编辑策略，
        # 便于教学，也能把修改范围控制得更可预测。
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# 工具分发表：
# 模型在响应里发出 `tool_use` 后，宿主 Python 程序会根据工具名路由到这里。
# 其中 `load_skill` 并不访问外部 API，它只是从启动时建立好的技能索引里取正文。
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "load_skill": lambda **kw: SKILL_LOADER.get_content(kw["name"]),
}

# 提供给模型的工具清单。
# 和 s04 不同，这里没有 `task` 之类的子 Agent 分发工具；
# 这一节唯一新增的核心工具就是 `load_skill`。
TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "load_skill", "description": "Load specialized knowledge by name.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string", "description": "Skill name to load"}}, "required": ["name"]}},
]


def agent_loop(messages: list):
    while True:
        # 第 1 步：把当前完整对话历史、system prompt 和工具列表发给模型。
        # 注意此时 system prompt 里只有技能简介，还没有技能正文。
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )

        # 第 2 步：无论这一轮返回的是普通文本还是工具调用，
        # 都先完整追加到历史里，保证上下文连续。
        messages.append({"role": "assistant", "content": response.content})

        # 第 3 步：如果模型没有请求工具，就说明它已经可以直接给出答复。
        if response.stop_reason != "tool_use":
            return

        # 第 4 步：如果模型请求了工具，就逐个执行。
        # 这里最值得关注的是 `load_skill` 的路径：
        #
        #   模型看到 system 里的技能简介
        #       -> 决定需要某项专业知识
        #       -> 发出 load_skill(name=...)
        #       -> Python 从 SKILL_LOADER 里取出完整正文
        #       -> 把正文作为 tool_result 喂回下一轮
        #       -> 模型基于“刚加载的技能内容”继续推理/执行
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                except Exception as e:
                    output = f"Error: {e}"
                # 终端里打印一个简短预览，方便人类观察代理刚刚调用了什么。
                print(f"> {block.name}: {str(output)[:200]}")
                # 关键点：
                # 工具执行结果不是直接“打印完就算了”，而是要封装成 `tool_result`
                # 再回写给模型。这样模型下一轮才能真正“看到”技能正文或文件内容。
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})

        # 第 5 步：把本轮所有工具结果作为一个新的 user turn 追加回去，
        # 然后继续 while True，让模型基于这些新信息进入下一轮决策。
        #
        # 所以 `load_skill` 的本质不是“修改 system prompt”，
        # 而是“通过 tool_result 在会话中临时注入一段外部知识”。
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    # `history` 保存的是整个主 Agent 的会话历史。
    # 和 s04 不同，这里没有额外的子 Agent 上下文，只有一个主循环。
    history = []
    while True:
        try:
            # 从终端读取用户输入。
            query = input("\033[36ms05 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        # 空输入 / q / exit 都表示结束会话。
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 先把用户问题写进历史，再交给 agent_loop 驱动“思考 -> 调工具 -> 继续思考”。
        history.append({"role": "user", "content": query})
        agent_loop(history)

        # `agent_loop` 返回时，history 最后一项通常是模型的最终回答。
        # 这里把其中的文本块打印回终端给用户看。
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
