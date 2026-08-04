"""
herdr agent 交互封装 —— 基于 herdr 原生 CLI 状态机（agent start/prompt/get/read）。
见 memory/herdr_orch_sop.md 场景 A（单 agent 启动 + 持续交互）。

核心用法:
    from agent_client import HerdrAgent
    ag = HerdrAgent.spawn("worker1", kind="codex", cwd="/path/to/proj")
    reply = ag.ask("请分析这段代码的问题")
    print(reply)
    ag.close()
"""
import subprocess
import json
import time


def _run(args, timeout=None):
    r = subprocess.run(["herdr"] + args, capture_output=True, text=True, timeout=timeout)
    out = r.stdout.strip()
    try:
        data = json.loads(out) if out else {}
    except json.JSONDecodeError:
        data = {"_raw": out}
    return r.returncode, data, r.stderr


class HerdrAgentError(RuntimeError):
    pass


class HerdrAgent:
    def __init__(self, pane_id, name, tab_id=None):
        self.pane_id = pane_id
        self.name = name
        self.tab_id = tab_id

    @classmethod
    def spawn(cls, name, kind, cwd, label=None, start_timeout_ms=30000):
        """新建隔离 tab+pane，并在其中拉起交互式 agent。"""
        rc, data, err = _run([
            "tab", "create", "--cwd", cwd,
            "--label", label or name, "--no-focus",
        ])
        if rc != 0:
            raise HerdrAgentError(f"tab create failed: {err or data}")
        pane_id = data["result"]["root_pane"]["pane_id"]
        tab_id = data["result"]["tab"]["tab_id"]

        rc, data, err = _run([
            "agent", "start", name, "--kind", kind,
            "--pane", pane_id, "--timeout", str(start_timeout_ms),
        ])
        if rc != 0:
            # 常见坑: codex 等 CLI 首次启动可能触发自更新重启进程，
            # 造成短暂 agent_not_found；重试一次
            time.sleep(3)
            rc, data, err = _run([
                "agent", "start", name, "--kind", kind,
                "--pane", pane_id, "--timeout", str(start_timeout_ms),
            ])
            if rc != 0:
                raise HerdrAgentError(f"agent start failed: {err or data}")
        return cls(pane_id, name, tab_id)

    def status(self):
        rc, data, err = _run(["agent", "get", self.pane_id])
        if rc != 0:
            raise HerdrAgentError(f"agent get failed: {err or data}")
        a = data["result"]["agent"]
        return a["agent_status"], a["state_change_seq"]

    def send(self, text):
        """fire-and-forget 发送 prompt，不等待。"""
        rc, data, err = _run(["agent", "prompt", self.pane_id, text])
        if rc != 0:
            raise HerdrAgentError(f"agent prompt failed: {err or data}")

    def wait_done(self, poll_interval=1.0, timeout=180):
        """轮询直到 agent_status 稳定为 done（或 idle 且非 working）。
        不使用内置 --wait/--until（对部分 agent kind 不可靠，见 SOP 坑点）。
        """
        deadline = time.time() + timeout
        last_seq = None
        stable_count = 0
        while time.time() < deadline:
            st, seq = self.status()
            if st == "working":
                stable_count = 0
            elif st in ("done", "idle"):
                if seq == last_seq:
                    stable_count += 1
                else:
                    stable_count = 1
                # 连续 2 次采样状态不变，认为已收敛
                if stable_count >= 2:
                    return st
            last_seq = seq
            time.sleep(poll_interval)
        raise HerdrAgentError(f"wait_done timeout after {timeout}s, last status={st}")

    def read(self, lines=20):
        rc, data, err = _run([
            "agent", "read", self.pane_id,
            "--source", "recent-unwrapped", "--lines", str(lines),
        ])
        if rc != 0:
            raise HerdrAgentError(f"agent read failed: {err or data}")
        return data.get("_raw", "")

    def ask(self, text, poll_interval=1.0, timeout=180, read_lines=30):
        """发送并阻塞等待完成，返回最近终端文本片段（含回复）。"""
        before_seq = None
        try:
            _, before_seq = self.status()
        except HerdrAgentError:
            pass
        self.send(text)
        self.wait_done(poll_interval=poll_interval, timeout=timeout)
        return self.read(lines=read_lines)

    def send_keys(self, *keys):
        rc, data, err = _run(["agent", "send-keys", self.pane_id, *keys])
        if rc != 0:
            raise HerdrAgentError(f"send-keys failed: {err or data}")

    def close(self):
        """关闭本 agent 所在的 tab（连带 pane 一起清理）。"""
        if self.tab_id:
            _run(["tab", "close", self.tab_id])
