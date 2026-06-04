# -*- coding: utf-8 -*-
"""Trellis 桥接插件：让 GenericAgent 在 Trellis 项目中工作并感知其上下文。

零改 GA 现有代码，通过 plugins hook 扩展点（agent_before）实现，等价于
Claude Code 的 .claude/ 集成角色，GA 升级不冲突。

项目定位（会话内随时切换，无需重启 GA）：
  - 对话指令 `/trellis <项目路径>`：切换当前项目；`/trellis` 查看当前项目
  - 环境变量 TRELLIS_PROJECT_DIR：可选，仅作进程启动时的默认项目
  - 当前项目保存在进程内，切换即时生效，作用于之后所有任务

设计要点（重要）：
  所有 Trellis 信息（含 /trellis 指令的执行结果与用法说明）只注入 system prompt 这一
  可信区，并显式标注"由 trellis_bridge 注入、非用户输入"；**绝不改写用户消息**——
  否则祈使式回执会被模型识别为 prompt injection 而拒绝执行。

  由于 GA 的命令补全/帮助是各前端硬编码、不识别 `/trellis`，本插件随注入附带用法说明，
  借模型向用户提示命令用法（弥补无 UI 级命令发现性）。

每次任务开始（agent_before 触发）：
  1. 将 handler.cwd 切到当前项目目录（GA 文件/shell 工具以此为基准）
  2. 调用项目 .trellis/scripts/get_context.py，把上下文注入 system prompt（messages[0]，幂等）

未设置默认项目且未切换时，插件完全静默，GA 行为不变。
"""
import os
import sys
import subprocess

import plugins.hooks as hooks

_MARKER = "<!-- trellis-context -->"
_CTX_TIMEOUT = 30
_SWITCH_CMD = "/trellis"
# 用法提示：随注入一并告知模型，让模型能向用户说明命令用法（弥补 GA 无命令补全/帮助）
_USAGE = "用法说明：`/trellis <项目路径>` 切换当前 Trellis 项目；`/trellis` 查看当前项目。"

# 当前 Trellis 项目（进程内状态）
_current_project = None
_initialized = False


def _is_trellis_project(path):
    return bool(path) and os.path.isdir(os.path.join(path, ".trellis"))


def _norm(path):
    return os.path.abspath(os.path.expanduser(path.strip()))


def _default_project():
    """环境变量提供的默认项目；无效返回 None。"""
    raw = os.environ.get("TRELLIS_PROJECT_DIR")
    if not raw:
        return None
    path = _norm(raw)
    return path if _is_trellis_project(path) else None


def _fetch_trellis_context(project_dir):
    """执行项目的 get_context.py，返回上下文文本；失败返回 None。"""
    script = os.path.join(project_dir, ".trellis", "scripts", "get_context.py")
    if not os.path.isfile(script):
        return None
    try:
        proc = subprocess.run(
            [sys.executable, script],
            cwd=project_dir, capture_output=True, text=True, timeout=_CTX_TIMEOUT,
        )
    except Exception:
        return None
    out = (proc.stdout or "").strip()
    return out or None


def _inject(ctx, header, project):
    """切 cwd + 往 system prompt 注入可信上下文（幂等）。

    只写 system 区、不碰用户消息，并显式声明可信来源，避免被模型误判为注入攻击。
    """
    handler = ctx.get("handler")
    if project and _is_trellis_project(project) and handler is not None:
        handler.cwd = project
    messages = ctx.get("messages")
    if not messages or messages[0].get("role") != "system":
        return
    sys_content = messages[0].get("content", "")
    if _MARKER in sys_content:
        return  # 幂等：本任务已注入
    block = (
        f"\n\n{_MARKER}\n"
        f"[可信系统上下文 · 由本地插件 trellis_bridge 从项目 .trellis 读取注入 · 非用户输入]\n"
        f"{header}\n"
    )
    if project and _is_trellis_project(project):
        context = _fetch_trellis_context(project)
        if context:
            block += (
                f"\n真实工作目录 cwd = {project}\n"
                f"（文件/shell 工具均以此为基准，覆盖上文 temp 提示）\n\n"
                f"{context}\n"
            )
    messages[0]["content"] = sys_content + block


def _handle_switch(ctx, user_input):
    """处理 /trellis 切换/查询指令：更新当前项目状态，结果+用法通过 system 区告知模型。"""
    global _current_project
    parts = user_input.split(maxsplit=1)
    cur = _current_project or "(未设置)"
    if len(parts) == 1:
        header = (f"用户用 /trellis 查看当前 Trellis 项目。当前项目：{cur}。"
                  f"请用一两句话告知用户当前项目，并附上用法说明。{_USAGE}")
    else:
        path = _norm(parts[1])
        if _is_trellis_project(path):
            _current_project = path
            header = (f"用户用 /trellis 命令将当前 Trellis 项目切换为：{path}。"
                      f"请用一句话向用户确认切换成功。{_USAGE}")
        else:
            header = (f"用户用 /trellis 命令尝试切换到 {path}，但该目录不是有效 Trellis 项目"
                      f"（缺少 .trellis/ 目录）；当前项目维持：{cur}。"
                      f"请告知用户失败原因与正确用法。{_USAGE}")
    _inject(ctx, header, _current_project)


@hooks.register("agent_before")
def _on_agent_before(ctx):
    global _current_project, _initialized
    try:
        if not _initialized:
            _current_project = _default_project()
            _initialized = True

        user_input = (ctx.get("user_input") or "").strip()
        first = user_input.split(maxsplit=1)[0] if user_input else ""
        if first == _SWITCH_CMD:
            _handle_switch(ctx, user_input)
        elif _current_project:
            _inject(ctx, f"Trellis 项目集成已激活。{_USAGE}", _current_project)
    except Exception as e:
        # 任何异常都不得阻断 GA 主流程
        sys.stderr.write(f"[trellis_bridge] skipped: {e}\n")
