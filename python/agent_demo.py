import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import hashlib
import math
from typing import Any, Dict, List, Optional, Set, Tuple
import torch
import torch.nn as nn
import torch.optim as optim


def load_env_file(path: str) -> None:
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        if raw.startswith("export "):
            raw = raw[7:].strip()
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


class ToolServerClient:
    def __init__(self, proc: subprocess.Popen, debug_enabled: bool = False):
        self.proc = proc
        self._next_id = 1
        self.debug_enabled = debug_enabled

    def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        req_id = str(self._next_id)
        self._next_id += 1
        payload = {"id": req_id, "method": method, "params": params or {}}
        if self.debug_enabled:
            print(f"\n========== TOOLSERVER 请求 (id={req_id}) ==========", file=sys.stderr)
            print(
                json.dumps(
                    {"method": method, "params": params or {}},
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
        self.proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("toolserver exited")
        resp = json.loads(line.decode("utf-8"))
        if self.debug_enabled:
            print(f"\n========== TOOLSERVER 响应 (id={req_id}) ==========", file=sys.stderr)
            print(json.dumps(resp, ensure_ascii=False, indent=2), file=sys.stderr)
        if resp.get("id") != req_id:
            raise RuntimeError(f"mismatched response id: expected {req_id}, got {resp.get('id')}")
        if resp.get("error"):
            raise RuntimeError(resp["error"]["message"])
        return resp.get("result")

    def list_tools(self) -> Any:
        return self.request("list_tools")

    def call_tool(self, name: str, input_obj: Dict[str, Any]) -> Any:
        return self.request("call_tool", {"name": name, "input": input_obj})


Message = Dict[str, Any]
ToolCall = Dict[str, Any]


class TokenUsage:
    def __init__(self, input_tokens: int = 0, output_tokens: int = 0, total_tokens: int = 0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.total_tokens = total_tokens

    def add(self, other: "TokenUsage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.total_tokens += other.total_tokens


DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"
GLM_DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
GLM_DEFAULT_MODEL = "glm-4-plus"


class ProviderResponse:
    def __init__(self, message: Message, tool_calls: List[ToolCall], raw: Any, usage: TokenUsage):
        self.message = message
        self.tool_calls = tool_calls
        self.raw = raw
        self.usage = usage


class ProviderEvent:
    def __init__(
        self,
        event_type: str,
        content: str = "",
        tool_call: Optional[ToolCall] = None,
        response: Optional[ProviderResponse] = None,
        error: Optional[Exception] = None,
    ):
        self.event_type = event_type
        self.content = content
        self.tool_call = tool_call
        self.response = response
        self.error = error


SPEED_WINDOW_MS = 2000


def _format_timestamp(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def _normalize_task_status(status: Any) -> Optional[str]:
    if not isinstance(status, str):
        return None
    if status in {"pending", "not_started"}:
        return "pending"
    if status in {"in_progress", "running"}:
        return "in_progress"
    if status in {"completed", "complete", "done"}:
        return "completed"
    return None


def _extract_tool_target(tool_name: str, input_obj: Dict[str, Any]) -> Optional[str]:
    name = tool_name.lower()
    if name in {"read", "write", "edit"}:
        target = input_obj.get("file_path") or input_obj.get("path")
        return str(target) if isinstance(target, str) else None
    if name in {"glob", "grep"}:
        target = input_obj.get("pattern")
        return str(target) if isinstance(target, str) else None
    if name == "bash":
        cmd = input_obj.get("command")
        if not isinstance(cmd, str):
            return None
        return cmd[:30] + ("..." if len(cmd) > 30 else "")
    return None


class SessionActivityTracker:
    def __init__(self) -> None:
        self.tools_by_id: Dict[str, Dict[str, Any]] = {}
        self.tool_order: List[str] = []
        self.agents_by_id: Dict[str, Dict[str, Any]] = {}
        self.agent_order: List[str] = []
        self.todos: List[Dict[str, str]] = []
        self.task_id_to_index: Dict[str, int] = {}

    def _resolve_task_index(self, task_id: Any) -> Optional[int]:
        if isinstance(task_id, (str, int)):
            key = str(task_id)
            if key in self.task_id_to_index:
                return self.task_id_to_index[key]
            if key.isdigit():
                idx = int(key) - 1
                if 0 <= idx < len(self.todos):
                    return idx
        return None

    def _apply_todo_write(self, input_obj: Dict[str, Any]) -> None:
        todos = input_obj.get("todos")
        if not isinstance(todos, list):
            return
        self.todos = []
        self.task_id_to_index = {}
        for item in todos:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            status = item.get("status")
            if not isinstance(content, str):
                continue
            normalized = _normalize_task_status(status) or "pending"
            self.todos.append({"content": content, "status": normalized})

    def _apply_task_create(self, call_id: str, input_obj: Dict[str, Any]) -> None:
        subject = input_obj.get("subject") if isinstance(input_obj.get("subject"), str) else ""
        description = input_obj.get("description") if isinstance(input_obj.get("description"), str) else ""
        content = subject or description or "Untitled task"
        status = _normalize_task_status(input_obj.get("status")) or "pending"
        self.todos.append({"content": content, "status": status})
        task_id_raw = input_obj.get("taskId")
        if isinstance(task_id_raw, (str, int)):
            task_id = str(task_id_raw)
        else:
            task_id = call_id
        if task_id:
            self.task_id_to_index[task_id] = len(self.todos) - 1

    def _apply_task_update(self, input_obj: Dict[str, Any]) -> None:
        idx = self._resolve_task_index(input_obj.get("taskId"))
        if idx is None:
            return
        status = _normalize_task_status(input_obj.get("status"))
        if status:
            self.todos[idx]["status"] = status
        subject = input_obj.get("subject") if isinstance(input_obj.get("subject"), str) else ""
        description = input_obj.get("description") if isinstance(input_obj.get("description"), str) else ""
        content = subject or description
        if content:
            self.todos[idx]["content"] = content

    def start_tool(self, call_id: str, name: str, input_obj: Dict[str, Any], ts: float) -> None:
        if not call_id:
            return
        if name == "Task":
            if call_id not in self.agents_by_id:
                self.agent_order.append(call_id)
            self.agents_by_id[call_id] = {
                "id": call_id,
                "type": input_obj.get("subagent_type") if isinstance(input_obj.get("subagent_type"), str) else "unknown",
                "model": input_obj.get("model") if isinstance(input_obj.get("model"), str) else "",
                "description": input_obj.get("description") if isinstance(input_obj.get("description"), str) else "",
                "status": "running",
                "start_time": _format_timestamp(ts),
            }
            return
        if name == "TodoWrite":
            self._apply_todo_write(input_obj)
            return
        if name == "TaskCreate":
            self._apply_task_create(call_id, input_obj)
            return
        if name == "TaskUpdate":
            self._apply_task_update(input_obj)
            return
        if call_id not in self.tools_by_id:
            self.tool_order.append(call_id)
        self.tools_by_id[call_id] = {
            "id": call_id,
            "name": name,
            "target": _extract_tool_target(name, input_obj) or "",
            "status": "running",
            "start_time": _format_timestamp(ts),
        }

    def finish_tool(self, call_id: str, is_error: bool, ts: float) -> None:
        tool = self.tools_by_id.get(call_id)
        if tool:
            tool["status"] = "error" if is_error else "completed"
            tool["end_time"] = _format_timestamp(ts)
        agent = self.agents_by_id.get(call_id)
        if agent:
            agent["status"] = "completed"
            agent["end_time"] = _format_timestamp(ts)

    def snapshot(self) -> Dict[str, Any]:
        tools = [self.tools_by_id[k] for k in self.tool_order if k in self.tools_by_id][-20:]
        agents = [self.agents_by_id[k] for k in self.agent_order if k in self.agents_by_id][-10:]
        return {
            "tools": tools,
            "agents": agents,
            "todos": list(self.todos),
        }


def _read_speed_cache(cache_path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        output_tokens = data.get("output_tokens")
        timestamp = data.get("timestamp")
        if not isinstance(output_tokens, (int, float)) or not isinstance(timestamp, (int, float)):
            return None
        return {"output_tokens": float(output_tokens), "timestamp": float(timestamp)}
    except Exception:
        return None


def _write_speed_cache(cache_path: str, output_tokens: float, timestamp: float) -> None:
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"output_tokens": output_tokens, "timestamp": timestamp}, f, ensure_ascii=False)
    except Exception:
        return


def get_output_speed(output_tokens: int, cache_path: str, now_ms: Optional[float] = None) -> Optional[float]:
    if not isinstance(output_tokens, int):
        return None
    now = float(now_ms if now_ms is not None else (time.time() * 1000.0))
    previous = _read_speed_cache(cache_path)
    speed = None
    if previous and float(output_tokens) >= previous["output_tokens"]:
        delta_tokens = float(output_tokens) - previous["output_tokens"]
        delta_ms = now - previous["timestamp"]
        if delta_tokens > 0 and delta_ms > 0 and delta_ms <= SPEED_WINDOW_MS:
            speed = delta_tokens / (delta_ms / 1000.0)
    _write_speed_cache(cache_path, float(output_tokens), now)
    return speed


def _build_activity_export(activity: Optional[Dict[str, Any]], output_speed: Optional[float]) -> Optional[Dict[str, Any]]:
    if not activity and output_speed is None:
        return None
    export: Dict[str, Any] = {}
    if isinstance(activity, dict):
        tools = activity.get("tools")
        agents = activity.get("agents")
        todos = activity.get("todos")
        export["tools"] = tools if isinstance(tools, list) else []
        export["agents"] = agents if isinstance(agents, list) else []
        export["todos"] = todos if isinstance(todos, list) else []
    else:
        export["tools"] = []
        export["agents"] = []
        export["todos"] = []
    if output_speed is not None:
        export["output_speed"] = {"tokens_per_second": round(output_speed, 2)}
    return export


def _truncate_text(value: str, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    return value[: max_len - 3] + "..."


def _safe_json(value: Any, max_len: int = 3000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        text = str(value)
    return _truncate_text(text, max_len)


def _debug_log_block(debug_enabled: bool, title: str, payload: Any, max_len: int = 3000) -> None:
    if not debug_enabled:
        return
    print(f"\n========== {title} ==========", file=sys.stderr)
    if isinstance(payload, str):
        print(_truncate_text(payload, max_len), file=sys.stderr)
        return
    print(_safe_json(payload, max_len=max_len), file=sys.stderr)


def _terminal_width() -> int:
    try:
        return int(shutil.get_terminal_size((100, 20)).columns)
    except Exception:
        return 100


def _hud_style_value(hud_style: str) -> str:
    style = hud_style.strip().lower()
    if style in {"compact", "full"}:
        return style
    return "full"


def _ansi_enabled() -> bool:
    return sys.stderr.isatty()


def _color(text: str, code: str, colored: bool) -> str:
    if not colored:
        return text
    return f"\033[{code}m{text}\033[0m"


def _status_icon(status: str) -> str:
    if status == "running":
        return "◐"
    if status == "completed":
        return "✓"
    if status == "error":
        return "✗"
    return "•"


def _is_meta_todo_content(content: str) -> bool:
    text = content.strip().lower()
    if not text:
        return True
    blocked = [
        "hud",
        "ui",
        "展示格式",
        "输出格式",
        "终端展示",
        "样式优化",
        "美化",
        "渲染样式",
    ]
    return any(token in text for token in blocked)


def _extract_prompt_terms(prompt: str) -> List[str]:
    terms = re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9_]{3,}", prompt.lower())
    unique: List[str] = []
    for term in terms:
        if term not in unique:
            unique.append(term)
    return unique[:12]


def _todo_relevance_score(content: str, prompt_terms: List[str]) -> int:
    text = content.lower()
    score = 0
    for term in prompt_terms:
        if term in text:
            score += 1
    return score


def _normalize_todo_items(todos_obj: Any) -> List[Dict[str, str]]:
    if not isinstance(todos_obj, list):
        return []
    valid: List[Dict[str, str]] = []
    for item in todos_obj:
        if not isinstance(item, dict):
            continue
        content = item.get("content") if isinstance(item.get("content"), str) else ""
        status = item.get("status") if isinstance(item.get("status"), str) else ""
        valid.append({"content": content, "status": status})
    return valid


def _select_focus_todo(valid_todos: List[Dict[str, str]], prompt: str) -> Optional[Dict[str, str]]:
    if not valid_todos:
        return None
    prompt_terms = _extract_prompt_terms(prompt)
    meaningful: List[Any] = []
    for item in valid_todos:
        content = item.get("content", "")
        if _is_meta_todo_content(content):
            continue
        score = _todo_relevance_score(content, prompt_terms)
        meaningful.append((score, item))
    in_progress_meaningful = [pair for pair in meaningful if pair[1].get("status") == "in_progress"]
    if in_progress_meaningful:
        return sorted(in_progress_meaningful, key=lambda pair: pair[0], reverse=True)[0][1]
    if meaningful:
        return sorted(meaningful, key=lambda pair: pair[0], reverse=True)[0][1]
    return None


def _render_tools_line(activity: Dict[str, Any], width: int, colored: bool) -> Optional[str]:
    tools = activity.get("tools")
    if not isinstance(tools, list) or not tools:
        return None
    running = [t for t in tools if isinstance(t, dict) and t.get("status") == "running"]
    completed = [t for t in tools if isinstance(t, dict) and t.get("status") in {"completed", "error"}]
    parts: List[str] = []
    for tool in running[-2:]:
        name = tool.get("name") if isinstance(tool.get("name"), str) else ""
        target = tool.get("target") if isinstance(tool.get("target"), str) else ""
        target_text = f": {_truncate_text(target, max(16, min(28, width // 6)))}" if target else ""
        parts.append(_color(f"◐ {name}{target_text}", "33", colored))
    counts: Dict[str, int] = {}
    for tool in completed:
        name = tool.get("name") if isinstance(tool.get("name"), str) else ""
        if not name:
            continue
        counts[name] = counts.get(name, 0) + 1
    top = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:4]
    for name, count in top:
        parts.append(_color(f"✓ {name} ×{count}", "32", colored))
    if not parts:
        return None
    return " | ".join(parts)


def _render_agents_lines(activity: Dict[str, Any], width: int, colored: bool, compact: bool) -> List[str]:
    agents = activity.get("agents")
    if not isinstance(agents, list) or not agents:
        return []
    running = [a for a in agents if isinstance(a, dict) and a.get("status") == "running"]
    completed = [a for a in agents if isinstance(a, dict) and a.get("status") == "completed"]
    to_show = (running + completed[-2:])[-2:] if compact else (running + completed[-2:])[-4:]
    lines: List[str] = []
    for agent in to_show:
        status = agent.get("status") if isinstance(agent.get("status"), str) else ""
        icon = _status_icon(status)
        agent_type = agent.get("type") if isinstance(agent.get("type"), str) else "unknown"
        model = agent.get("model") if isinstance(agent.get("model"), str) else ""
        desc = agent.get("description") if isinstance(agent.get("description"), str) else ""
        model_text = f" [{model}]" if model else ""
        desc_max = max(18, min(72, width // 2))
        desc_text = f": {_truncate_text(desc, desc_max)}" if desc else ""
        color_code = "33" if status == "running" else ("31" if status == "error" else "36")
        lines.append(_color(f"{icon} {agent_type}{model_text}{desc_text}", color_code, colored))
    return lines


def _render_todos_line(activity: Dict[str, Any], width: int, colored: bool, prompt: str) -> Optional[str]:
    todos = activity.get("todos")
    if not isinstance(todos, list) or not todos:
        goal = _truncate_text(prompt.strip(), max(24, min(80, width - 24)))
        return _color(f"▸ {goal} (agent goal)", "35", colored) if goal else None
    valid = _normalize_todo_items(todos)
    if not valid:
        goal = _truncate_text(prompt.strip(), max(24, min(80, width - 24)))
        return _color(f"▸ {goal} (agent goal)", "35", colored) if goal else None
    in_progress = _select_focus_todo(valid, prompt)
    completed = sum(1 for t in valid if t.get("status") == "completed")
    total = len(valid)
    if in_progress:
        content = in_progress.get("content", "")
        max_len = max(24, min(80, width - 24))
        return _color(f"▸ {_truncate_text(content, max_len)} ({completed}/{total})", "35", colored)
    if completed == total and total > 0:
        return _color(f"✓ All todos complete ({completed}/{total})", "32", colored)
    goal = _truncate_text(prompt.strip(), max(24, min(80, width - 24)))
    return _color(f"▸ {goal} (agent goal, {completed}/{total})", "35", colored) if goal else None


def emit_activity_hud(
    activity: Dict[str, Any],
    output_speed: Optional[float],
    step_index: int,
    hud_style: str,
    prompt: str,
) -> None:
    width = _terminal_width()
    style = _hud_style_value(hud_style)
    compact = style == "compact"
    colored = _ansi_enabled()
    tools = activity.get("tools") if isinstance(activity.get("tools"), list) else []
    agents = activity.get("agents") if isinstance(activity.get("agents"), list) else []
    todos = activity.get("todos") if isinstance(activity.get("todos"), list) else []
    summary = f"tools {len(tools)} | agents {len(agents)} | todos {len(todos)}"
    if output_speed is not None:
        summary = f"{summary} | speed {output_speed:.2f} tok/s"
    title = f" HUD · step {step_index} "
    bar_len = max(12, min(30, width - len(title) - 4))
    left_bar = "═" * (bar_len // 2)
    right_bar = "═" * (bar_len - (bar_len // 2))
    header = f"{left_bar}{title}{right_bar}"
    lines: List[str] = [_color(header, "96", colored), _color(summary, "90", colored)]
    tools_line = _render_tools_line(activity, width, colored)
    if tools_line:
        lines.append(f"Tools  {tools_line}")
    agent_lines = _render_agents_lines(activity, width, colored, compact)
    lines.extend(agent_lines)
    todos_line = _render_todos_line(activity, width, colored, prompt)
    if todos_line:
        lines.append(f"Todos  {todos_line}")
    if not compact:
        done_tools = sum(1 for t in tools if isinstance(t, dict) and t.get("status") in {"completed", "error"})
        running_tools = sum(1 for t in tools if isinstance(t, dict) and t.get("status") == "running")
        footer = f"status running_tools={running_tools} done_tools={done_tools}"
        lines.append(_color(footer, "90", colored))
    if len(lines) <= 2:
        return
    print("\n".join(lines), file=sys.stderr)


class DeepSeekProvider:
    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    def _build_payload(self, messages: List[Message], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }

    def _build_stream_payload(self, messages: List[Message], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        payload = self._build_payload(messages, tools)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        return payload

    def _request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = self.base_url + "/chat/completions"
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        req = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method="POST",
        )
        return self._request_with_retries(req)

    def _request_with_retries(self, req: urllib.request.Request) -> Dict[str, Any]:
        attempts = 0
        while True:
            attempts += 1
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code not in {429, 500}:
                    raise
                if attempts >= 8:
                    raise
                retry_after = e.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    sleep_ms = int(retry_after) * 1000
                else:
                    backoff = 2000 * (2 ** (attempts - 1))
                    jitter = int(backoff * random.uniform(0.0, 0.2))
                    sleep_ms = backoff + jitter
                time.sleep(sleep_ms / 1000.0)
            except urllib.error.URLError:
                if attempts >= 8:
                    raise
                backoff = 2000 * (2 ** (attempts - 1))
                jitter = int(backoff * random.uniform(0.0, 0.2))
                time.sleep((backoff + jitter) / 1000.0)

    def _parse_tool_calls(self, assistant_msg: Message) -> List[ToolCall]:
        tool_calls = []
        raw_calls = assistant_msg.get("tool_calls") or []
        for call in raw_calls:
            function = call.get("function") or {}
            tool_calls.append(
                {
                    "id": call.get("id", ""),
                    "name": function.get("name", ""),
                    "arguments": function.get("arguments", "{}"),
                }
            )
        return tool_calls

    def request(self, messages: List[Message], tools: List[Dict[str, Any]]) -> ProviderResponse:
        payload = self._build_payload(messages, tools)
        result = self._request(payload)
        choice = result["choices"][0]
        assistant_msg = choice["message"]
        tool_calls = self._parse_tool_calls(assistant_msg)
        usage = extract_token_usage(result)
        return ProviderResponse(assistant_msg, tool_calls, result, usage)

    def stream(self, messages: List[Message], tools: List[Dict[str, Any]]) -> List[ProviderEvent]:
        payload = self._build_stream_payload(messages, tools)
        url = self.base_url + "/chat/completions"
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        req = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method="POST",
        )
        attempts = 0
        while True:
            attempts += 1
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return self._parse_stream(resp)
            except urllib.error.HTTPError as e:
                if e.code not in {429, 500}:
                    raise
                if attempts >= 8:
                    raise
                retry_after = e.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    sleep_ms = int(retry_after) * 1000
                else:
                    backoff = 2000 * (2 ** (attempts - 1))
                    jitter = int(backoff * random.uniform(0.0, 0.2))
                    sleep_ms = backoff + jitter
                time.sleep(sleep_ms / 1000.0)
            except urllib.error.URLError:
                if attempts >= 8:
                    raise
                backoff = 2000 * (2 ** (attempts - 1))
                jitter = int(backoff * random.uniform(0.0, 0.2))
                time.sleep((backoff + jitter) / 1000.0)

    def _parse_stream(self, resp: Any) -> List[ProviderEvent]:
        events: List[ProviderEvent] = []
        content = ""
        finish_reason = ""
        usage = TokenUsage()
        tool_calls_by_index: Dict[int, ToolCall] = {}
        while True:
            line = resp.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            if not line.startswith(b"data:"):
                continue
            data = line[len(b"data:") :].strip()
            if data == b"[DONE]":
                break
            try:
                chunk = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            chunk_usage = chunk.get("usage")
            if isinstance(chunk_usage, dict):
                usage.add(
                    TokenUsage(
                        int(chunk_usage.get("prompt_tokens") or 0),
                        int(chunk_usage.get("completion_tokens") or 0),
                        int(chunk_usage.get("total_tokens") or 0),
                    )
                )
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                delta_content = delta.get("content")
                if delta_content:
                    events.append(ProviderEvent("content_delta", content=delta_content))
                    content += delta_content
                if "tool_calls" in delta:
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        current = tool_calls_by_index.get(idx, {"id": "", "name": "", "arguments": ""})
                        if tc.get("id") and not current.get("id"):
                            current["id"] = tc.get("id")
                            events.append(ProviderEvent("tool_use_start", tool_call=current.copy()))
                        func = tc.get("function") or {}
                        if func.get("name") and current.get("name") != func.get("name"):
                            current["name"] = func.get("name")
                            events.append(ProviderEvent("tool_use_start", tool_call=current.copy()))
                        if "arguments" in func and func.get("arguments"):
                            current["arguments"] = current.get("arguments", "") + func.get("arguments")
                            events.append(ProviderEvent("tool_use_delta", tool_call=current.copy()))
                        tool_calls_by_index[idx] = current
                if choice.get("finish_reason"):
                    finish_reason = choice.get("finish_reason") or finish_reason
        tool_calls: List[ToolCall] = []
        for idx in sorted(tool_calls_by_index.keys()):
            tc = tool_calls_by_index[idx]
            if not tc.get("id"):
                tc["id"] = f"call_{idx}"
            tool_calls.append(tc)
            events.append(ProviderEvent("tool_use_stop", tool_call=tc.copy()))
        response = ProviderResponse({"content": content}, tool_calls, {"finish_reason": finish_reason}, usage)
        events.append(ProviderEvent("complete", response=response))
        return events


def normalize_provider_name(raw_provider: str) -> str:
    value = raw_provider.strip().lower()
    if value in {"", "deepseek"}:
        return "deepseek"
    if value in {"glm", "zhipu", "zhipuai", "bigmodel"}:
        return "glm"
    return value


def resolve_provider_config(provider_name: str) -> Dict[str, str]:
    if provider_name == "deepseek":
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY is required when LLM_PROVIDER=deepseek.")
        model = os.environ.get("DEEPSEEK_MODEL", DEEPSEEK_DEFAULT_MODEL).strip() or DEEPSEEK_DEFAULT_MODEL
        base_url = os.environ.get("DEEPSEEK_BASE_URL", "").strip() or DEEPSEEK_DEFAULT_BASE_URL
        return {
            "provider": provider_name,
            "api_key": api_key,
            "model": model,
            "base_url": base_url,
            "model_label": f"{provider_name}/{model}",
        }
    if provider_name == "glm":
        api_key = os.environ.get("GLM_API_KEY", "").strip()
        if not api_key:
            api_key = os.environ.get("ZHIPUAI_API_KEY", "").strip()
        if not api_key:
            raise ValueError("GLM_API_KEY or ZHIPUAI_API_KEY is required when LLM_PROVIDER=glm.")
        model = os.environ.get("GLM_MODEL", GLM_DEFAULT_MODEL).strip() or GLM_DEFAULT_MODEL
        base_url = os.environ.get("GLM_BASE_URL", "").strip() or GLM_DEFAULT_BASE_URL
        return {
            "provider": provider_name,
            "api_key": api_key,
            "model": model,
            "base_url": base_url,
            "model_label": f"{provider_name}/{model}",
        }
    raise ValueError(f"Unsupported LLM_PROVIDER: {provider_name}. Supported values: deepseek, glm.")


def build_tool_schema(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    schema = []
    for tool in tools:
        schema.append(
            {
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": {
                        "type": "object",
                        "properties": tool.get("parameters", {}) or {},
                        "required": tool.get("required", []) or [],
                    },
                },
            }
        )
    return schema


def build_tool_prompt_catalog(tools: List[Dict[str, Any]]) -> str:
    description_map = {
        "ls": "列出目录内容，用于查看项目结构与文件清单。",
        "glob": "按文件名模式快速匹配文件路径。",
        "grep": "按关键词或正则在文件内容中搜索。",
        "view": "读取文件内容或指定行区间。",
        "write": "写入或创建文件内容。",
        "edit": "按定位规则修改现有文件内容。",
        "patch": "以补丁方式批量更新文件。",
        "fetch": "抓取指定 URL 的网页或数据内容。",
        "bash": "执行命令行命令。",
        "diagnostics": "获取代码诊断信息（如编译或语法错误）。",
        "skill_search": "搜索可用技能元数据，定位最相关技能。",
        "skill_load": "加载技能正文与资源，供后续执行使用。",
    }
    lines: List[str] = []
    for tool in tools:
        name = str(tool.get("name", "")).strip()
        if not name:
            continue
        zh_description = description_map.get(name)
        if not zh_description:
            zh_description = f"用于执行 {name} 相关任务。"
        lines.append(f"- {name}：{zh_description}")
    return "\n".join(lines)

def extract_token_usage(result: Any) -> TokenUsage:
    usage = result.get("usage") if isinstance(result, dict) else None
    if not isinstance(usage, dict):
        return TokenUsage()
    input_tokens = usage.get("prompt_tokens")
    output_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")
    return TokenUsage(
        int(input_tokens or 0),
        int(output_tokens or 0),
        int(total_tokens or 0),
    )


def normalize_arguments(arguments: Any) -> Dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if not isinstance(arguments, str):
        return {}
    try:
        args_obj = json.loads(arguments)
        if isinstance(args_obj, dict):
            return args_obj
    except Exception:
        return {}
    return {}


def _resolve_path(value: str, base_dir: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return os.path.abspath(base_dir)
    if os.path.isabs(raw):
        return os.path.abspath(raw)
    return os.path.abspath(os.path.join(base_dir, raw))


def parse_first_json_object(raw: str) -> Dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, flags=re.IGNORECASE)
    if fenced:
        inner = fenced.group(1).strip()
        try:
            data = json.loads(inner)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        data = json.loads(candidate)
                        if isinstance(data, dict):
                            return data
                    except Exception:
                        break
        start = text.find("{", start + 1)
    return {}


def _memory_normalize_text(text: str, max_len: int = 800) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 3] + "..."


def _memory_tokens(text: str) -> Set[str]:
    tokens = set(re.findall(r"[a-zA-Z0-9_]{2,}|[\u4e00-\u9fff]{1,6}", text.lower()))
    if not tokens:
        return set()
    return tokens


def _memory_overlap_score(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    denom = max(len(a), len(b))
    if denom <= 0:
        return 0.0
    return float(inter) / float(denom)


def _memory_focus_overlap(query_tokens: Set[str], candidate_tokens: Set[str]) -> float:
    if not query_tokens or not candidate_tokens:
        return 0.0
    query_focus = {tok for tok in query_tokens if len(tok) >= 3}
    if not query_focus:
        query_focus = set(query_tokens)
    if not query_focus:
        return 0.0
    inter = len(query_focus & candidate_tokens)
    if inter <= 0:
        return 0.0
    return float(inter) / float(max(len(query_focus), 1))


def _memory_env_float(name: str, default: float, min_value: float = 0.0, max_value: float = 1.0) -> float:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except Exception:
        return default
    if value < min_value:
        return min_value
    if value > max_value:
        return max_value
    return value


def _memory_env_int(name: str, default: int, min_value: int = 0, max_value: int = 1000000) -> int:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except Exception:
        return default
    if value < min_value:
        return min_value
    if value > max_value:
        return max_value
    return value


def _memory_embedding(text: str, dim: int = 192, profile: str = "retrieval") -> List[float]:
    normalized = _memory_normalize_text(text, max_len=1600).lower()
    tokens = sorted(_memory_tokens(normalized))
    compact = re.sub(r"\s+", "", normalized)
    units: List[Tuple[str, float]] = [(token, 1.0) for token in tokens]
    bigram_weight = 0.6
    trigram_weight = 0.45
    if str(profile or "").strip().lower() == "update":
        bigram_weight = 0.45
        trigram_weight = 0.30
    if compact:
        length = len(compact)
        for n in (2, 3):
            if length >= n:
                for i in range(length - n + 1):
                    units.append((compact[i : i + n], bigram_weight if n == 2 else trigram_weight))
    if not units:
        return [0.0] * dim
    vec = [0.0] * dim
    for token, base_weight in units:
        digest = hashlib.blake2b(token.encode("utf-8", errors="ignore"), digest_size=16).digest()
        idx = int.from_bytes(digest[:4], "little", signed=False) % dim
        sign = -1.0 if (digest[4] & 1) else 1.0
        jitter = (int.from_bytes(digest[5:7], "little", signed=False) % 100) / 500.0
        weight = base_weight * (1.0 + jitter)
        vec[idx] += sign * weight
    norm = math.sqrt(sum(x * x for x in vec))
    if norm <= 1e-12:
        return [0.0] * dim
    return [x / norm for x in vec]


def _memory_cosine_similarity(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 1e-12 or norm_b <= 1e-12:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


def _memory_extract_lines(raw_text: str, limit: int = 8) -> List[str]:
    raw_lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
    cleaned: List[str] = []
    for ln in raw_lines:
        ln = re.sub(r"^\d+\s*[→:]\s*", "", ln)
        ln = re.sub(r"^[-*]\s*", "", ln)
        if ln.startswith("<") and ln.endswith(">"):
            continue
        if ln:
            cleaned.append(ln)
        if len(cleaned) >= limit:
            break
    return cleaned


def _memory_split_fixed_length(text: str, chunk_size: int, chunk_overlap: int) -> List[str]:
    normalized = _memory_normalize_text(text)
    if not normalized:
        return []
    safe_chunk_size = max(64, int(chunk_size))
    safe_overlap = max(0, int(chunk_overlap))
    if safe_overlap >= safe_chunk_size:
        safe_overlap = max(0, safe_chunk_size // 4)
    if len(normalized) <= safe_chunk_size:
        return [normalized]
    chunks: List[str] = []
    start = 0
    step = max(1, safe_chunk_size - safe_overlap)
    while start < len(normalized):
        end = min(len(normalized), start + safe_chunk_size)
        chunk = normalized[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(normalized):
            break
        start += step
    return chunks


def _memory_split_paragraph(text: str) -> List[str]:
    normalized = str(text or "")
    if not normalized.strip():
        return []
    raw_parts = [part.strip() for part in re.split(r"\n\s*\n+", normalized) if part.strip()]
    if raw_parts:
        return [_memory_normalize_text(part) for part in raw_parts if _memory_normalize_text(part)]
    fallback = _memory_extract_lines(normalized, limit=48)
    return [_memory_normalize_text(item) for item in fallback if _memory_normalize_text(item)]


class PPOActionPolicy:
    def __init__(self):
        self.action_types = ["NOOP", "INSERT", "UPDATE", "DELETE"]
        self.logits: Dict[str, float] = {k: 0.0 for k in self.action_types}
        self.lr = _memory_env_float("AGENT_MEMORY_PPO_LR", 0.06, min_value=0.0001, max_value=1.0)
        self.clip_epsilon = _memory_env_float("AGENT_MEMORY_PPO_CLIP_EPSILON", 0.20, min_value=0.01, max_value=0.5)
        self.entropy_coef = _memory_env_float("AGENT_MEMORY_PPO_ENTROPY_COEF", 0.01, min_value=0.0, max_value=0.2)
        self.epochs = _memory_env_int("AGENT_MEMORY_PPO_EPOCHS", 3, min_value=1, max_value=20)
        self.batch_size = _memory_env_int("AGENT_MEMORY_PPO_BATCH_SIZE", 12, min_value=2, max_value=512)
        self.minibatch_size = _memory_env_int("AGENT_MEMORY_PPO_MINIBATCH_SIZE", 6, min_value=0, max_value=512)
        self.top_k = _memory_env_int("AGENT_MEMORY_PPO_TOP_K", 2, min_value=1, max_value=4)
        self.sample_top_k = str(os.environ.get("AGENT_MEMORY_PPO_SAMPLE_TOP_K", "1")).strip().lower() not in {
            "0",
            "false",
            "no",
        }
        self.topk_temperature = _memory_env_float("AGENT_MEMORY_PPO_TOPK_TEMPERATURE", 1.0, min_value=0.1, max_value=5.0)
        self.min_prob = _memory_env_float("AGENT_MEMORY_PPO_MIN_PROB", 0.10, min_value=0.01, max_value=0.9)
        self.gamma = _memory_env_float("AGENT_MEMORY_PPO_GAMMA", 0.96, min_value=0.5, max_value=0.999)
        self.gae_lambda = _memory_env_float("AGENT_MEMORY_PPO_GAE_LAMBDA", 0.90, min_value=0.5, max_value=1.0)
        self.value_lr = _memory_env_float("AGENT_MEMORY_PPO_VALUE_LR", 0.12, min_value=0.001, max_value=1.0)
        self.adam_beta1 = _memory_env_float("AGENT_MEMORY_PPO_ADAM_BETA1", 0.9, min_value=0.0, max_value=0.9999)
        self.adam_beta2 = _memory_env_float("AGENT_MEMORY_PPO_ADAM_BETA2", 0.999, min_value=0.0, max_value=0.99999)
        self.adam_eps = _memory_env_float("AGENT_MEMORY_PPO_ADAM_EPS", 1e-8, min_value=1e-12, max_value=1e-3)
        self.adam_weight_decay = _memory_env_float("AGENT_MEMORY_PPO_ADAM_WEIGHT_DECAY", 0.0, min_value=0.0, max_value=1.0)
        self.vf_clip = _memory_env_float("AGENT_MEMORY_PPO_VF_CLIP", 0.15, min_value=0.0, max_value=2.0)
        self.target_kl = _memory_env_float("AGENT_MEMORY_PPO_TARGET_KL", 0.03, min_value=0.0, max_value=1.0)
        self.max_grad_norm = _memory_env_float("AGENT_MEMORY_PPO_MAX_GRAD_NORM", 1.0, min_value=0.0, max_value=20.0)
        self.update_every_episodes = _memory_env_int("AGENT_MEMORY_PPO_UPDATE_EVERY_EPISODES", 1, min_value=1, max_value=64)
        self.advantage_normalize = str(os.environ.get("AGENT_MEMORY_PPO_ADVANTAGE_NORMALIZE", "1")).strip().lower() not in {
            "0",
            "false",
            "no",
        }
        self.advantage_clip = _memory_env_float("AGENT_MEMORY_PPO_ADVANTAGE_CLIP", 5.0, min_value=0.0, max_value=20.0)
        self.baseline = 0.0
        self.baseline_momentum = _memory_env_float("AGENT_MEMORY_PPO_BASELINE_MOMENTUM", 0.9, min_value=0.0, max_value=0.999)
        self.reward_mean = 0.0
        self.advantage_mean = 0.0
        self.update_steps = 0
        self.episodes_since_update = 0
        self.flush_calls = 0
        self.flush_force_calls = 0
        self.flush_skipped_updates = 0
        self.flush_triggered_updates = 0
        self.buffer: List[Dict[str, Any]] = []
        self.value_table: Dict[str, float] = {k: 0.0 for k in self.action_types}
        self.context_feature_keys = [
            "candidate_count",
            "candidate_score_top",
            "candidate_score_mean",
            "tool_line_count",
            "has_target",
            "ambiguous",
            "blocked_ratio",
            "query_memory_similarity",
            "query_memory_dispersion",
            "fusion_mass",
            "recent_query_similarity",
            "chunk_density",
        ]
        self.feature_keys = ["bias"] + list(self.context_feature_keys) + [
            "is_noop",
            "is_insert",
            "is_update",
            "is_delete",
            "candidate_retrieval_score",
            "candidate_rank_score",
            "candidate_position_prob",
            "candidate_selection_cond_prob",
            "selected_flag",
        ]
        self.action_weights: Dict[str, Dict[str, float]] = {
            action_type: {k: 0.0 for k in self.feature_keys} for action_type in self.action_types
        }
        self.value_weights: Dict[str, float] = {k: 0.0 for k in self.feature_keys}
        self.value_bias = 0.0
        self.topk_entropy_mean = 0.0
        self.topk_mass_mean = 0.0
        self.topk_bin_entropy_mean = 0.0
        self.optimizer_step_policy = 0
        self.optimizer_step_value = 0
        self.opt_m_logits: Dict[str, float] = {k: 0.0 for k in self.action_types}
        self.opt_v_logits: Dict[str, float] = {k: 0.0 for k in self.action_types}
        self.opt_m_action_weights: Dict[str, Dict[str, float]] = {
            action_type: {k: 0.0 for k in self.feature_keys} for action_type in self.action_types
        }
        self.opt_v_action_weights: Dict[str, Dict[str, float]] = {
            action_type: {k: 0.0 for k in self.feature_keys} for action_type in self.action_types
        }
        self.opt_m_value_weights: Dict[str, float] = {k: 0.0 for k in self.feature_keys}
        self.opt_v_value_weights: Dict[str, float] = {k: 0.0 for k in self.feature_keys}
        self.opt_m_value_bias = 0.0
        self.opt_v_value_bias = 0.0
        self.last_update_info: Dict[str, float] = {
            "policy_loss": 0.0,
            "value_loss": 0.0,
            "entropy": 0.0,
            "approx_kl": 0.0,
            "clip_frac": 0.0,
            "explained_variance": 0.0,
            "value_mean": 0.0,
            "return_mean": 0.0,
            "advantage_mean": 0.0,
            "advantage_std": 0.0,
            "advantage_clip_frac": 0.0,
            "buffer_size": 0.0,
            "policy_items": 0.0,
            "policy_skipped_items": 0.0,
            "n_updates": 0.0,
            "early_stop": 0.0,
        }
        self.state_dim = len(self.context_feature_keys) + 1
        self.op_dim = len(self.feature_keys)
        self.hidden_dim = _memory_env_int("AGENT_MEMORY_PPO_HIDDEN_DIM", 64, min_value=8, max_value=512)
        self.value_coef = _memory_env_float("AGENT_MEMORY_PPO_VALUE_COEF", 0.5, min_value=0.0, max_value=5.0)
        self.device = "cpu"
        self.state_net = nn.Sequential(
            nn.Linear(self.state_dim, self.hidden_dim),
            nn.ReLU(),
            nn.Linear(self.hidden_dim, self.hidden_dim),
            nn.ReLU(),
        ).to(self.device)
        self.op_net = nn.Sequential(
            nn.Linear(self.op_dim, self.hidden_dim),
            nn.ReLU(),
            nn.Linear(self.hidden_dim, self.hidden_dim),
            nn.ReLU(),
        ).to(self.device)
        self.actor_head = nn.Sequential(
            nn.Linear(self.hidden_dim * 2, self.hidden_dim),
            nn.ReLU(),
            nn.Linear(self.hidden_dim, 1),
        ).to(self.device)
        self.critic_head = nn.Sequential(
            nn.Linear(self.hidden_dim, self.hidden_dim),
            nn.ReLU(),
            nn.Linear(self.hidden_dim, 1),
        ).to(self.device)
        mem_controller_params = (
            list(self.state_net.parameters())
            + list(self.op_net.parameters())
            + list(self.actor_head.parameters())
            + list(self.critic_head.parameters())
        )
        self.mem_controller_optimizer = optim.Adam(
            mem_controller_params,
            lr=self.lr,
            betas=(self.adam_beta1, self.adam_beta2),
            eps=self.adam_eps,
            weight_decay=self.adam_weight_decay,
        )
        self._mem_controller_param_shapes: List[Tuple[int, ...]] = [tuple(param.shape) for param in mem_controller_params]
        self._mem_controller_param_numel: List[int] = [int(param.numel()) for param in mem_controller_params]
        self.episode_flush_only = str(os.environ.get("AGENT_MEMORY_PPO_EPISODE_FLUSH_ONLY", "1")).strip().lower() not in {
            "0",
            "false",
            "no",
        }

    def _softmax_indexed(self, score_pairs: List[Tuple[int, float]]) -> Dict[int, float]:
        if not score_pairs:
            return {}
        max_score = max(score for _, score in score_pairs)
        exps: Dict[int, float] = {}
        total = 0.0
        for idx, score in score_pairs:
            value = math.exp(score - max_score)
            exps[idx] = value
            total += value
        if total <= 1e-12:
            uniform = 1.0 / float(max(len(score_pairs), 1))
            return {idx: uniform for idx, _ in score_pairs}
        return {idx: exps[idx] / total for idx, _ in score_pairs}

    def _normalize_candidate_snapshot(self, candidates: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
        if not isinstance(candidates, list) or not candidates:
            return None
        packed: List[Dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            action_type = str(candidate.get("action_type", "")).strip().upper()
            if action_type not in self.logits:
                continue
            raw_features = candidate.get("features")
            candidate_features = {k: 0.0 for k in self.feature_keys}
            candidate_features["bias"] = 1.0
            if isinstance(raw_features, dict):
                for name in self.feature_keys:
                    raw_val = raw_features.get(name)
                    if isinstance(raw_val, (int, float)):
                        candidate_features[name] = float(raw_val)
            raw_selected_order = candidate.get("selected_order", -1)
            selected_order_value = int(raw_selected_order) if isinstance(raw_selected_order, (int, float)) else -1
            packed.append(
                {
                    "action_type": action_type,
                    "features": candidate_features,
                    "selected_order": selected_order_value,
                }
            )
        if not packed:
            return None
        return packed

    def _state_vector_from_context(self, context: Optional[Dict[str, Any]]) -> List[float]:
        ctx = self._normalize_context(context)
        vec = [1.0]
        for key in self.context_feature_keys:
            vec.append(float(ctx.get(key, 0.0)))
        return vec

    def _op_vector_from_features(self, features: Optional[Dict[str, Any]]) -> List[float]:
        vec: List[float] = []
        if not isinstance(features, dict):
            return [0.0 for _ in self.feature_keys]
        for key in self.feature_keys:
            raw = features.get(key, 0.0)
            vec.append(float(raw) if isinstance(raw, (int, float)) else 0.0)
        return vec

    def _forward_logits_value(
        self,
        state_vec: List[float],
        op_vecs: List[List[float]],
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        state_t = torch.tensor([state_vec], dtype=torch.float32, device=self.device)
        op_t = torch.tensor(op_vecs, dtype=torch.float32, device=self.device)
        if op_t.ndim != 2 or op_t.shape[0] <= 0:
            op_t = torch.zeros((1, self.op_dim), dtype=torch.float32, device=self.device)
        state_h = self.state_net(state_t)
        op_h = self.op_net(op_t.unsqueeze(0))
        state_expand = state_h.unsqueeze(1).expand(-1, op_h.shape[1], -1)
        combined = torch.cat([state_expand, op_h], dim=-1)
        logits = self.actor_head(combined).squeeze(-1).squeeze(0)
        value = self.critic_head(state_h).squeeze(-1).squeeze(0)
        return logits, value

    def _candidate_probs_from_feature_list(
        self,
        feature_list: List[Dict[str, float]],
        context: Optional[Dict[str, Any]],
    ) -> Dict[int, float]:
        if not feature_list:
            return {}
        state_vec = self._state_vector_from_context(context)
        op_vecs = [self._op_vector_from_features(features) for features in feature_list]
        with torch.no_grad():
            logits, _ = self._forward_logits_value(state_vec, op_vecs)
            probs_t = torch.softmax(logits, dim=-1)
            probs = probs_t.detach().cpu().tolist()
        return {idx: float(p) for idx, p in enumerate(probs)}

    def _encode_mem_controller_params(self) -> List[float]:
        params = (
            list(self.state_net.parameters())
            + list(self.op_net.parameters())
            + list(self.actor_head.parameters())
            + list(self.critic_head.parameters())
        )
        values: List[float] = []
        for param in params:
            values.extend(float(v) for v in param.detach().view(-1).cpu().tolist())
        return values

    def _decode_mem_controller_params(self, values: List[float]) -> None:
        if not isinstance(values, list):
            return
        expected = sum(self._mem_controller_param_numel)
        if len(values) != expected:
            return
        params = (
            list(self.state_net.parameters())
            + list(self.op_net.parameters())
            + list(self.actor_head.parameters())
            + list(self.critic_head.parameters())
        )
        cursor = 0
        for param, shape, numel in zip(params, self._mem_controller_param_shapes, self._mem_controller_param_numel):
            chunk = values[cursor : cursor + numel]
            cursor += numel
            tensor = torch.tensor(chunk, dtype=torch.float32, device=self.device).view(shape)
            with torch.no_grad():
                param.copy_(tensor)

    def _candidate_probs(self, candidates: List[Dict[str, Any]]) -> Dict[int, float]:
        valid_indices: List[int] = []
        feature_list: List[Dict[str, float]] = []
        for idx, candidate in enumerate(candidates):
            action_type = str(candidate.get("action_type", "")).strip().upper()
            if action_type not in self.logits:
                continue
            raw_features = candidate.get("features")
            if not isinstance(raw_features, dict):
                continue
            features = {k: float(raw_features.get(k, 0.0)) for k in self.feature_keys}
            valid_indices.append(idx)
            feature_list.append(features)
        local_probs = self._candidate_probs_from_feature_list(feature_list, context=None)
        return {global_idx: float(local_probs.get(local_idx, 0.0)) for local_idx, global_idx in enumerate(valid_indices)}

    def _adam_update_scalar(
        self,
        param_value: float,
        grad_value: float,
        m_value: float,
        v_value: float,
        lr: float,
        step: int,
    ) -> Tuple[float, float, float]:
        grad = float(grad_value)
        if self.adam_weight_decay > 0.0:
            grad += self.adam_weight_decay * float(param_value)
        m = self.adam_beta1 * m_value + (1.0 - self.adam_beta1) * grad
        v = self.adam_beta2 * v_value + (1.0 - self.adam_beta2) * (grad * grad)
        bias_correction_1 = 1.0 - (self.adam_beta1 ** step)
        bias_correction_2 = 1.0 - (self.adam_beta2 ** step)
        m_hat = m / max(bias_correction_1, 1e-12)
        v_hat = v / max(bias_correction_2, 1e-12)
        updated = float(param_value) + lr * m_hat / (math.sqrt(max(v_hat, 0.0)) + self.adam_eps)
        return updated, m, v

    def _sample_index_from_probs(self, index_prob_pairs: List[Tuple[int, float]]) -> int:
        if not index_prob_pairs:
            return -1
        r = random.random()
        running = 0.0
        for idx, prob in index_prob_pairs:
            running += max(float(prob), 0.0)
            if r <= running:
                return int(idx)
        return int(index_prob_pairs[-1][0])

    def _select_topk_indices(
        self,
        indices: List[int],
        probs: Dict[int, float],
        k: int,
    ) -> Tuple[List[int], List[float], float]:
        selected_order: List[int] = []
        selected_cond_probs: List[float] = []
        remaining = list(indices)
        keep_count = min(max(int(k), 0), len(remaining))
        joint_log_prob = 0.0
        for _ in range(keep_count):
            weights: List[Tuple[int, float]] = []
            weight_total = 0.0
            for idx in remaining:
                base_prob = max(float(probs.get(idx, 0.0)), 1e-12)
                weight = base_prob ** (1.0 / self.topk_temperature)
                weights.append((idx, weight))
                weight_total += weight
            if weight_total <= 1e-12:
                uniform_prob = 1.0 / float(max(len(remaining), 1))
                normalized = [(idx, uniform_prob) for idx in remaining]
            else:
                normalized = [(idx, weight / weight_total) for idx, weight in weights]
            if self.sample_top_k:
                chosen_idx = self._sample_index_from_probs(normalized)
                if chosen_idx < 0:
                    break
            else:
                normalized.sort(key=lambda x: x[1], reverse=True)
                chosen_idx = int(normalized[0][0])
            chosen_prob = 0.0
            for idx, prob in normalized:
                if idx == chosen_idx:
                    chosen_prob = max(float(prob), 1e-8)
                    break
            selected_order.append(chosen_idx)
            selected_cond_probs.append(chosen_prob)
            joint_log_prob += math.log(chosen_prob)
            remaining = [idx for idx in remaining if idx != chosen_idx]
            if not remaining:
                break
        return selected_order, selected_cond_probs, float(joint_log_prob)

    def _normalize_context(self, context: Optional[Dict[str, Any]]) -> Dict[str, float]:
        values = {k: 0.0 for k in self.context_feature_keys}
        if not isinstance(context, dict):
            return values
        for key in self.context_feature_keys:
            raw = context.get(key, 0.0)
            if isinstance(raw, (int, float)):
                values[key] = float(raw)
        return values

    def _build_action_features(
        self,
        action_type: str,
        context: Optional[Dict[str, Any]],
        action: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, float]:
        key = str(action_type or "").strip().upper()
        ctx = self._normalize_context(context)
        features = {k: 0.0 for k in self.feature_keys}
        features["bias"] = 1.0
        for name in self.context_feature_keys:
            features[name] = float(ctx.get(name, 0.0))
        if key == "NOOP":
            features["is_noop"] = 1.0
        elif key == "INSERT":
            features["is_insert"] = 1.0
        elif key == "UPDATE":
            features["is_update"] = 1.0
        elif key == "DELETE":
            features["is_delete"] = 1.0
        if isinstance(action, dict):
            retrieval_score = action.get("__ppo_retrieval_score")
            if isinstance(retrieval_score, (int, float)):
                features["candidate_retrieval_score"] = float(retrieval_score)
            rank_score = action.get("__ppo_rank_score")
            if isinstance(rank_score, (int, float)):
                features["candidate_rank_score"] = float(rank_score)
            position_prob = action.get("__ppo_position_prob")
            if isinstance(position_prob, (int, float)):
                features["candidate_position_prob"] = float(position_prob)
            selection_cond_prob = action.get("__ppo_selection_cond_prob")
            if isinstance(selection_cond_prob, (int, float)):
                features["candidate_selection_cond_prob"] = float(selection_cond_prob)
            selected = action.get("__ppo_selected")
            if isinstance(selected, (int, float, bool)):
                features["selected_flag"] = 1.0 if bool(selected) else 0.0
        return features

    def _score(self, action_type: str, features: Dict[str, float]) -> float:
        key = str(action_type or "").strip().upper()
        score = float(self.logits.get(key, 0.0))
        weights = self.action_weights.get(key) or {}
        for name in self.feature_keys:
            score += float(weights.get(name, 0.0)) * float(features.get(name, 0.0))
        return score

    def _feature_map_for_context(self, context: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
        return {action_type: self._build_action_features(action_type, context) for action_type in self.action_types}

    def _probs(self, context: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
        feature_list = [self._build_action_features(action_type, context) for action_type in self.action_types]
        local_probs = self._candidate_probs_from_feature_list(feature_list, context)
        total = sum(float(local_probs.get(i, 0.0)) for i in range(len(self.action_types)))
        if total <= 1e-12:
            uniform = 1.0 / float(len(self.action_types))
            return {k: uniform for k in self.action_types}
        return {
            action_type: float(local_probs.get(idx, 0.0)) / total
            for idx, action_type in enumerate(self.action_types)
        }

    def action_prob(self, action_type: str, context: Optional[Dict[str, Any]] = None) -> float:
        key = str(action_type or "").strip().upper()
        probs = self._probs(context)
        return float(probs.get(key, 0.0))

    def _estimate_value(self, features: Dict[str, float]) -> float:
        context = {k: float(features.get(k, 0.0)) for k in self.context_feature_keys}
        state_vec = self._state_vector_from_context(context)
        op_vecs = [self._op_vector_from_features(features)]
        with torch.no_grad():
            _, value_t = self._forward_logits_value(state_vec, op_vecs)
        return float(value_t.detach().cpu().item())

    def prepare_feedback(
        self,
        actions: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None,
        policy_log_probs_by_pos: Optional[List[float]] = None,
        policy_probs_by_pos: Optional[List[float]] = None,
        policy_selected_cond_probs_by_pos: Optional[List[float]] = None,
    ) -> List[Dict[str, Any]]:
        prepared: List[Dict[str, Any]] = []
        for idx, action in enumerate(actions):
            if not isinstance(action, dict):
                continue
            action_type = str(action.get("type", "")).strip().upper()
            if action_type not in self.logits:
                continue
            features = self._build_action_features(action_type, context, action)
            policy_prob = None
            if isinstance(action, dict):
                selected_cond_prob = action.get("__ppo_selection_cond_prob")
                if isinstance(selected_cond_prob, (int, float)) and float(selected_cond_prob) > 0.0:
                    policy_prob = max(float(selected_cond_prob), 1e-8)
            if policy_prob is None and isinstance(policy_selected_cond_probs_by_pos, list) and idx < len(policy_selected_cond_probs_by_pos):
                raw_selected_prob = policy_selected_cond_probs_by_pos[idx]
                if isinstance(raw_selected_prob, (int, float)) and float(raw_selected_prob) > 0.0:
                    policy_prob = max(float(raw_selected_prob), 1e-8)
            if policy_prob is None and isinstance(policy_probs_by_pos, list) and idx < len(policy_probs_by_pos):
                raw_prob = policy_probs_by_pos[idx]
                if isinstance(raw_prob, (int, float)):
                    policy_prob = max(float(raw_prob), 1e-8)
            if policy_prob is None:
                policy_prob = max(float(self.action_prob(action_type, context)), 1e-8)
            old_log_prob = None
            if isinstance(policy_log_probs_by_pos, list) and idx < len(policy_log_probs_by_pos):
                raw_log_prob = policy_log_probs_by_pos[idx]
                if isinstance(raw_log_prob, (int, float)):
                    old_log_prob = float(raw_log_prob)
            if old_log_prob is None:
                old_log_prob = math.log(policy_prob)
            old_joint_log_prob = None
            if isinstance(action, dict):
                raw_joint = action.get("__ppo_selection_joint_log_prob")
                if isinstance(raw_joint, (int, float)):
                    old_joint_log_prob = float(raw_joint)
            selected_group_size = 1
            if isinstance(action, dict):
                raw_group_size = action.get("__ppo_selected_group_size")
                if isinstance(raw_group_size, (int, float)):
                    selected_group_size = max(1, int(raw_group_size))
            selected_order = -1
            if isinstance(action, dict):
                raw_selected_order = action.get("__ppo_selected_order")
                if isinstance(raw_selected_order, (int, float)):
                    selected_order = int(raw_selected_order)
            value_estimate = self._estimate_value(features)
            prepared.append(
                {
                    "action_type": action_type,
                    "old_log_prob": old_log_prob,
                    "old_joint_log_prob": old_joint_log_prob,
                    "selected_group_size": selected_group_size,
                    "selected_order": selected_order,
                    "selection_prob": policy_prob,
                    "features": features,
                    "value_estimate": value_estimate,
                }
            )
        return prepared

    def rerank_actions(
        self,
        actions: List[Dict[str, Any]],
        candidate_map: Dict[int, int],
        candidate_scores_by_seq: Dict[int, float],
        candidate_scores_by_index: Dict[int, float],
        context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        scored: List[Tuple[float, int, Dict[str, Any]]] = []
        blocked_indices: List[int] = []
        position_probs: Dict[int, float] = {}
        position_log_probs: Dict[int, float] = {}
        retrieval_scores_by_idx: Dict[int, float] = {}
        rank_scores_by_idx: Dict[int, float] = {}
        candidate_features_by_idx: Dict[int, Dict[str, float]] = {}
        keep_count = max(self.top_k, 1)
        for idx, action in enumerate(actions):
            if not isinstance(action, dict):
                blocked_indices.append(idx)
                continue
            action_type = str(action.get("type", "")).strip().upper()
            if action_type == "NOOP":
                action_features = self._build_action_features(action_type, context)
                candidate_score = self._score(action_type, action_features)
                action_features["candidate_retrieval_score"] = 0.0
                action_features["candidate_rank_score"] = float(candidate_score)
                scored.append((candidate_score, idx, action))
                rank_scores_by_idx[idx] = candidate_score
                retrieval_scores_by_idx[idx] = 0.0
                candidate_features_by_idx[idx] = dict(action_features)
                continue
            if action_type not in {"INSERT", "UPDATE", "DELETE"}:
                blocked_indices.append(idx)
                continue
            action_features = self._build_action_features(action_type, context)
            type_score = self._score(action_type, action_features)
            retrieval_score = 0.45
            if action_type in {"UPDATE", "DELETE"}:
                try:
                    seq_index = int(action.get("memory_index"))
                except Exception:
                    seq_index = -1
                if seq_index in candidate_scores_by_seq:
                    retrieval_score = candidate_scores_by_seq.get(seq_index, 0.0)
                else:
                    mapped = candidate_map.get(seq_index, seq_index)
                    retrieval_score = candidate_scores_by_index.get(mapped, 0.0)
            final_score = type_score + retrieval_score * 0.7
            action_features["candidate_retrieval_score"] = float(retrieval_score)
            action_features["candidate_rank_score"] = float(final_score)
            scored.append((final_score, idx, action))
            rank_scores_by_idx[idx] = final_score
            retrieval_scores_by_idx[idx] = retrieval_score
            candidate_features_by_idx[idx] = dict(action_features)
        ordered_indices = [idx for _, idx, _ in scored]
        ordered_features = [candidate_features_by_idx.get(idx, self._build_action_features("NOOP", context)) for idx in ordered_indices]
        local_probs = self._candidate_probs_from_feature_list(ordered_features, context)
        candidate_probs = {idx: float(local_probs.get(pos, 0.0)) for pos, idx in enumerate(ordered_indices)}
        for idx, prob in candidate_probs.items():
            position_probs[idx] = float(prob)
            position_log_probs[idx] = math.log(max(float(prob), 1e-8))
        blocked_set = set(blocked_indices)
        for score_value, idx, action in scored:
            _ = score_value
            action_type = str(action.get("type", "")).strip().upper()
            policy_prob = float(position_probs.get(idx, 0.0))
            if policy_prob < self.min_prob and action_type in {"UPDATE", "DELETE"}:
                blocked_indices.append(idx)
                continue
        blocked_set = set(blocked_indices)
        scored = [item for item in scored if item[1] not in blocked_set]
        if not scored:
            return [{"type": "NOOP"}], {
                "blocked_indices": blocked_indices,
                "selected": [0],
                "top_k": self.top_k,
                "probs_by_pos": [1.0],
                "log_probs_by_pos": [0.0],
                "selected_cond_probs_by_pos": [1.0],
                "selected_joint_log_prob": 0.0,
                "selected_order": [0],
                "topk_entropy": 0.0,
                "topk_mass": 1.0,
                "topk_bin_entropy": 0.0,
            }
        scored.sort(key=lambda x: position_probs.get(x[1], 0.0), reverse=True)
        keep_count = min(keep_count, len(scored))
        candidate_indices = [int(item[1]) for item in scored]
        selected_order, selected_cond_probs, selected_joint_log_prob = self._select_topk_indices(
            candidate_indices,
            position_probs,
            keep_count,
        )
        selected_indices = set(selected_order)
        selected_cond_prob_by_idx: Dict[int, float] = {}
        for idx, cond_prob in zip(selected_order, selected_cond_probs):
            selected_cond_prob_by_idx[int(idx)] = float(cond_prob)
        selected_rank_by_idx: Dict[int, int] = {}
        for order_idx, selected_idx in enumerate(selected_order):
            selected_rank_by_idx[int(selected_idx)] = int(order_idx)
        reranked: List[Dict[str, Any]] = []
        for idx, action in enumerate(actions):
            if idx in selected_indices:
                action_copy = dict(action) if isinstance(action, dict) else {"type": "NOOP"}
                action_copy["__ppo_retrieval_score"] = float(retrieval_scores_by_idx.get(idx, 0.0))
                action_copy["__ppo_rank_score"] = float(rank_scores_by_idx.get(idx, 0.0))
                action_copy["__ppo_position_prob"] = float(position_probs.get(idx, 0.0))
                action_copy["__ppo_selected"] = 1.0
                action_copy["__ppo_selection_cond_prob"] = float(selected_cond_prob_by_idx.get(idx, 0.0))
                action_copy["__ppo_selection_joint_log_prob"] = float(selected_joint_log_prob)
                action_copy["__ppo_selected_group_size"] = int(len(selected_order))
                action_copy["__ppo_selected_order"] = int(selected_rank_by_idx.get(idx, -1))
                reranked.append(action_copy)
            else:
                blocked_indices.append(idx)
                reranked.append(
                    {
                        "type": "NOOP",
                        "__ppo_retrieval_score": float(retrieval_scores_by_idx.get(idx, 0.0)),
                        "__ppo_rank_score": float(rank_scores_by_idx.get(idx, 0.0)),
                        "__ppo_position_prob": float(position_probs.get(idx, 0.0)),
                        "__ppo_selected": 0.0,
                        "__ppo_selection_cond_prob": 0.0,
                        "__ppo_selection_joint_log_prob": 0.0,
                        "__ppo_selected_group_size": 0,
                        "__ppo_selected_order": -1,
                    }
                )
        selected_probs = [float(position_probs.get(idx, 0.0)) for idx in sorted(selected_indices)]
        topk_mass = sum(selected_probs)
        if topk_mass > 1e-8 and selected_probs:
            normalized = [max(p / topk_mass, 1e-8) for p in selected_probs]
            topk_entropy = -sum(p * math.log(p) for p in normalized)
        else:
            topk_entropy = 0.0
        mass = max(1e-8, min(1.0 - 1e-8, topk_mass))
        topk_bin_entropy = -(mass * math.log(mass) + (1.0 - mass) * math.log(1.0 - mass))
        self.topk_entropy_mean = self.topk_entropy_mean * 0.95 + topk_entropy * 0.05
        self.topk_mass_mean = self.topk_mass_mean * 0.95 + topk_mass * 0.05
        self.topk_bin_entropy_mean = self.topk_bin_entropy_mean * 0.95 + topk_bin_entropy * 0.05
        probs_by_pos = [float(position_probs.get(i, 0.0)) for i in range(len(actions))]
        log_probs_by_pos = [float(position_log_probs.get(i, math.log(1e-8))) for i in range(len(actions))]
        selected_cond_probs_by_pos = [float(selected_cond_prob_by_idx.get(i, 0.0)) for i in range(len(actions))]
        return reranked, {
            "blocked_indices": sorted(set(blocked_indices)),
            "selected": sorted(selected_indices),
            "selected_order": selected_order,
            "top_k": self.top_k,
            "probs_by_pos": probs_by_pos,
            "log_probs_by_pos": log_probs_by_pos,
            "selected_cond_probs_by_pos": selected_cond_probs_by_pos,
            "selected_joint_log_prob": float(selected_joint_log_prob),
            "topk_entropy": topk_entropy,
            "topk_mass": topk_mass,
            "topk_bin_entropy": topk_bin_entropy,
        }

    def observe(
        self,
        action_type: str,
        old_log_prob: float,
        reward: float,
        done: bool = False,
        features: Optional[Dict[str, Any]] = None,
        value_estimate: Optional[float] = None,
        old_joint_log_prob: Optional[float] = None,
        selected_group_size: int = 1,
        step_group_id: Optional[str] = None,
        selected_order: int = -1,
        step_candidates: Optional[List[Dict[str, Any]]] = None,
        group_candidates: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        key = str(action_type or "").strip().upper()
        if key not in self.logits:
            return
        self.reward_mean = self.reward_mean * 0.95 + float(reward) * 0.05
        action_features = self._build_action_features(key, None)
        if isinstance(features, dict):
            for name in self.feature_keys:
                raw = features.get(name)
                if isinstance(raw, (int, float)):
                    action_features[name] = float(raw)
        if isinstance(value_estimate, (int, float)):
            value_estimate_value = float(value_estimate)
        else:
            value_estimate_value = self._estimate_value(action_features)
        normalized_step_candidates = self._normalize_candidate_snapshot(step_candidates)
        normalized_group_candidates = self._normalize_candidate_snapshot(group_candidates)
        selected_candidate_idx = -1
        if isinstance(normalized_step_candidates, list) and int(selected_order) >= 0:
            for idx, candidate in enumerate(normalized_step_candidates):
                if int(candidate.get("selected_order", -1)) == int(selected_order):
                    selected_candidate_idx = idx
                    break
        self.buffer.append(
            {
                "action_idx": float(self.action_types.index(key)),
                "old_log_prob": float(old_log_prob),
                "reward": float(reward),
                "value": value_estimate_value,
                "done": 1.0 if done else 0.0,
                "features": action_features,
                "old_joint_log_prob": float(old_joint_log_prob) if isinstance(old_joint_log_prob, (int, float)) else None,
                "selected_group_size": max(1, int(selected_group_size)),
                "step_group_id": str(step_group_id) if isinstance(step_group_id, str) and step_group_id else "",
                "selected_order": int(selected_order),
                "step_candidates": normalized_step_candidates,
                "selected_candidate_idx": int(selected_candidate_idx),
                "group_candidates": normalized_group_candidates,
            }
        )
        if (not self.episode_flush_only) and len(self.buffer) >= self.batch_size:
            self.update()

    def adjust_last_reward(self, delta: float) -> None:
        if not self.buffer:
            return
        self.buffer[-1]["reward"] = float(self.buffer[-1].get("reward", 0.0)) + float(delta)

    def distribute_episode_reward(
        self,
        final_reward: float,
        redistribute: bool,
        redistribution_decay: float,
        final_reward_last_ratio: float,
        selected_only: bool,
    ) -> None:
        if not self.buffer:
            return
        if abs(float(final_reward)) <= 1e-12:
            return
        selected_indices: List[int] = []
        for idx, item in enumerate(self.buffer):
            if not selected_only:
                selected_indices.append(idx)
                continue
            raw_features = item.get("features")
            selected_flag = 0.0
            if isinstance(raw_features, dict):
                raw_selected = raw_features.get("selected_flag")
                if isinstance(raw_selected, (int, float, bool)):
                    selected_flag = float(raw_selected)
            if selected_flag >= 0.5:
                selected_indices.append(idx)
        if not selected_indices:
            selected_indices = [len(self.buffer) - 1]
        if len(selected_indices) <= 1:
            target_idx = selected_indices[-1]
            self.buffer[target_idx]["reward"] = float(self.buffer[target_idx].get("reward", 0.0)) + float(final_reward)
            return
        if not redistribute:
            target_idx = selected_indices[-1]
            self.buffer[target_idx]["reward"] = float(self.buffer[target_idx].get("reward", 0.0)) + float(final_reward)
            return
        last_ratio = max(0.0, min(1.0, float(final_reward_last_ratio)))
        head_ratio = max(0.0, 1.0 - last_ratio)
        decay = max(0.1, min(1.0, float(redistribution_decay)))
        weights: List[float] = []
        weight_total = 0.0
        selected_count = len(selected_indices)
        for order_idx in range(selected_count):
            w = decay ** float(selected_count - 1 - order_idx)
            weights.append(w)
            weight_total += w
        if weight_total <= 1e-12:
            weight_total = 1.0
        for order_idx, buffer_idx in enumerate(selected_indices):
            add_value = float(final_reward) * head_ratio * (weights[order_idx] / weight_total)
            self.buffer[buffer_idx]["reward"] = float(self.buffer[buffer_idx].get("reward", 0.0)) + add_value
        last_idx = selected_indices[-1]
        self.buffer[last_idx]["reward"] = (
            float(self.buffer[last_idx].get("reward", 0.0)) + float(final_reward) * last_ratio
        )

    def flush_episode(self, force_update: bool = True) -> bool:
        if not self.buffer:
            return False
        self.flush_calls += 1
        if force_update:
            self.flush_force_calls += 1
        self.buffer[-1]["done"] = 1.0
        self.episodes_since_update += 1
        should_update = force_update or (self.episodes_since_update >= self.update_every_episodes)
        if not should_update:
            self.flush_skipped_updates += 1
            return False
        self.update()
        self.flush_triggered_updates += 1
        self.episodes_since_update = 0
        return True

    def _apply_transition(
        self,
        action_idx: int,
        old_log_prob: float,
        advantage: float,
        return_value: float,
        features: Optional[Dict[str, Any]],
        old_value_pred: Optional[float] = None,
        old_joint_log_prob: Optional[float] = None,
        selected_group_size: int = 1,
        apply_policy_update: bool = True,
        joint_ratio_override: Optional[float] = None,
        joint_approx_kl_override: Optional[float] = None,
        step_candidates: Optional[List[Dict[str, Any]]] = None,
        selected_candidate_idx: int = -1,
    ) -> Dict[str, float]:
        action_type = self.action_types[action_idx]
        action_features = self._build_action_features(action_type, None)
        if isinstance(features, dict):
            for name in self.feature_keys:
                raw = features.get(name)
                if isinstance(raw, (int, float)):
                    action_features[name] = float(raw)
        context = {k: float(action_features.get(k, 0.0)) for k in self.context_feature_keys}
        candidate_features: List[Dict[str, float]] = []
        if isinstance(step_candidates, list) and step_candidates:
            for candidate in step_candidates:
                if not isinstance(candidate, dict):
                    continue
                raw_features = candidate.get("features")
                if not isinstance(raw_features, dict):
                    continue
                features_map = {k: float(raw_features.get(k, 0.0)) for k in self.feature_keys}
                candidate_features.append(features_map)
        if not candidate_features:
            candidate_features = [self._build_action_features(t, context) for t in self.action_types]
            selected_candidate_idx = max(0, min(int(action_idx), len(candidate_features) - 1))
        if selected_candidate_idx < 0 or selected_candidate_idx >= len(candidate_features):
            selected_candidate_idx = max(0, min(int(action_idx), len(candidate_features) - 1))
        state_vec = self._state_vector_from_context(context)
        op_vecs = [self._op_vector_from_features(f) for f in candidate_features]
        logits_t, value_t = self._forward_logits_value(state_vec, op_vecs)
        probs_t = torch.softmax(logits_t, dim=-1)
        selected_prob_t = torch.clamp(probs_t[selected_candidate_idx], min=1e-8)
        new_log_prob_t = torch.log(selected_prob_t)
        new_prob = float(selected_prob_t.detach().cpu().item())
        new_log_prob = float(new_log_prob_t.detach().cpu().item())
        old_prob = max(math.exp(old_log_prob), 1e-8)
        approx_kl = old_log_prob - new_log_prob
        ratio = new_prob / old_prob
        selected_group_size = max(1, int(selected_group_size))
        ratio_t = torch.exp(new_log_prob_t - float(old_log_prob))
        if apply_policy_update and isinstance(joint_ratio_override, (int, float)):
            ratio = float(joint_ratio_override)
            ratio_t = torch.tensor(float(ratio), dtype=torch.float32, device=self.device)
            if isinstance(joint_approx_kl_override, (int, float)):
                approx_kl = float(joint_approx_kl_override)
            else:
                approx_kl = -math.log(max(ratio, 1e-8))
        clipped_ratio = min(max(ratio, 1.0 - self.clip_epsilon), 1.0 + self.clip_epsilon)
        clipped_ratio_t = torch.clamp(ratio_t, 1.0 - self.clip_epsilon, 1.0 + self.clip_epsilon)
        advantage_t = torch.tensor(float(advantage), dtype=torch.float32, device=self.device)
        policy_loss_t = -torch.min(ratio_t * advantage_t, clipped_ratio_t * advantage_t)
        entropy_t = -(probs_t * torch.log(torch.clamp(probs_t, min=1e-8))).sum()
        entropy = float(entropy_t.detach().cpu().item())
        current_state_value = float(value_t.detach().cpu().item())
        if isinstance(old_value_pred, (int, float)):
            old_state_value = float(old_value_pred)
        else:
            old_state_value = current_state_value
        return_t = torch.tensor(float(return_value), dtype=torch.float32, device=self.device)
        old_state_t = torch.tensor(float(old_state_value), dtype=torch.float32, device=self.device)
        value_loss_unclipped_t = (value_t - return_t) ** 2
        value_loss_t = value_loss_unclipped_t
        if self.vf_clip > 0.0:
            value_clipped_t = old_state_t + torch.clamp(value_t - old_state_t, -self.vf_clip, self.vf_clip)
            value_loss_clipped_t = (value_clipped_t - return_t) ** 2
            value_loss_t = torch.max(value_loss_unclipped_t, value_loss_clipped_t)
        total_loss_t = policy_loss_t + self.value_coef * 0.5 * value_loss_t - self.entropy_coef * entropy_t
        if apply_policy_update:
            self.mem_controller_optimizer.zero_grad()
            total_loss_t.backward()
            if self.max_grad_norm > 0.0:
                torch.nn.utils.clip_grad_norm_(
                    list(self.state_net.parameters())
                    + list(self.op_net.parameters())
                    + list(self.actor_head.parameters())
                    + list(self.critic_head.parameters()),
                    self.max_grad_norm,
                )
            self.mem_controller_optimizer.step()
            self.optimizer_step_policy += 1
            self.optimizer_step_value += 1
        old_value = float(self.value_table.get(action_type, 0.0))
        self.value_table[action_type] = old_value + self.value_lr * (return_value - old_value)
        value_loss = 0.5 * float(value_loss_t.detach().cpu().item())
        clip_frac = 1.0 if (apply_policy_update and abs(ratio - 1.0) > self.clip_epsilon) else 0.0
        policy_loss = float(policy_loss_t.detach().cpu().item()) if apply_policy_update else 0.0
        return {
            "policy_loss": float(policy_loss),
            "value_loss": float(value_loss),
            "entropy": float(entropy if apply_policy_update else 0.0),
            "approx_kl": float(approx_kl if apply_policy_update else 0.0),
            "clip_frac": float(clip_frac),
            "value_pred": float(old_state_value),
        }

    def _compute_returns_advantages(self) -> Tuple[List[float], List[float]]:
        n = len(self.buffer)
        if n <= 0:
            return [], []
        rewards = [float(item.get("reward", 0.0)) for item in self.buffer]
        values = [float(item.get("value", 0.0)) for item in self.buffer]
        dones = [float(item.get("done", 0.0)) for item in self.buffer]
        returns = [0.0] * n
        advantages = [0.0] * n
        gae = 0.0
        next_value = 0.0
        for t in range(n - 1, -1, -1):
            mask = 1.0 - dones[t]
            delta = rewards[t] + self.gamma * next_value * mask - values[t]
            gae = delta + self.gamma * self.gae_lambda * mask * gae
            advantages[t] = gae
            returns[t] = gae + values[t]
            next_value = values[t]
        return returns, advantages

    def _compute_group_joint_log_prob(self, group_indices: List[int]) -> Optional[Tuple[float, float]]:
        if not group_indices:
            return None
        ordered_items: List[Tuple[int, int, str, Dict[str, float]]] = []
        old_joint_log_prob: Optional[float] = None
        for idx in group_indices:
            if idx < 0 or idx >= len(self.buffer):
                continue
            item = self.buffer[idx]
            action_idx = int(item.get("action_idx", 0))
            if action_idx < 0 or action_idx >= len(self.action_types):
                continue
            action_type = self.action_types[action_idx]
            selected_order = int(item.get("selected_order", -1))
            action_features = self._build_action_features(action_type, None)
            raw_features = item.get("features")
            if isinstance(raw_features, dict):
                for name in self.feature_keys:
                    raw = raw_features.get(name)
                    if isinstance(raw, (int, float)):
                        action_features[name] = float(raw)
            ordered_items.append((selected_order, idx, action_type, action_features))
            raw_old_joint = item.get("old_joint_log_prob")
            if old_joint_log_prob is None and isinstance(raw_old_joint, (int, float)):
                old_joint_log_prob = float(raw_old_joint)
        if not ordered_items or not isinstance(old_joint_log_prob, float):
            return None
        ordered_items.sort(key=lambda x: (x[0] if x[0] >= 0 else 10**9, x[1]))
        remaining_prob = 1.0
        new_joint_log_prob = 0.0
        for _, _, action_type, action_features in ordered_items:
            context = {k: float(action_features.get(k, 0.0)) for k in self.context_feature_keys}
            probs = self._probs(context)
            p_i = max(float(probs.get(action_type, 0.0)), 1e-8)
            denom = max(remaining_prob, 1e-8)
            new_joint_log_prob += math.log(p_i) - math.log(denom)
            remaining_prob = max(remaining_prob - p_i, 1e-8)
        return old_joint_log_prob, new_joint_log_prob

    def _compute_group_joint_log_prob_from_candidates(self, group_candidates: List[Dict[str, Any]]) -> Optional[float]:
        if not isinstance(group_candidates, list) or not group_candidates:
            return None
        valid_indices: List[int] = []
        feature_list: List[Dict[str, float]] = []
        selected: List[Tuple[int, int]] = []
        for idx, candidate in enumerate(group_candidates):
            if not isinstance(candidate, dict):
                continue
            action_type = str(candidate.get("action_type", "")).strip().upper()
            if action_type not in self.logits:
                continue
            raw_features = candidate.get("features")
            features = {k: 0.0 for k in self.feature_keys}
            features["bias"] = 1.0
            if isinstance(raw_features, dict):
                for name in self.feature_keys:
                    raw = raw_features.get(name)
                    if isinstance(raw, (int, float)):
                        features[name] = float(raw)
            valid_indices.append(idx)
            feature_list.append(features)
            raw_order = candidate.get("selected_order", -1)
            order_idx = int(raw_order) if isinstance(raw_order, (int, float)) else -1
            if order_idx >= 0:
                selected.append((order_idx, idx))
        if len(valid_indices) <= 1 or len(selected) <= 1:
            return None
        local_probs = self._candidate_probs_from_feature_list(feature_list, context=None)
        probs = {global_idx: float(local_probs.get(local_idx, 0.0)) for local_idx, global_idx in enumerate(valid_indices)}
        selected.sort(key=lambda x: x[0])
        remaining_prob = 1.0
        joint_log_prob = 0.0
        for _, idx in selected:
            p_i = max(float(probs.get(idx, 0.0)), 1e-8)
            denom = max(remaining_prob, 1e-8)
            joint_log_prob += math.log(p_i) - math.log(denom)
            remaining_prob = max(remaining_prob - p_i, 1e-8)
        return float(joint_log_prob)

    def update(self) -> None:
        if not self.buffer:
            return
        self.last_update_info = {
            "policy_loss": 0.0,
            "value_loss": 0.0,
            "entropy": 0.0,
            "approx_kl": 0.0,
            "clip_frac": 0.0,
            "explained_variance": 0.0,
            "value_mean": 0.0,
            "return_mean": 0.0,
            "advantage_mean": 0.0,
            "advantage_std": 0.0,
            "advantage_clip_frac": 0.0,
            "buffer_size": float(len(self.buffer)),
            "policy_items": 0.0,
            "policy_skipped_items": 0.0,
            "n_updates": 0.0,
            "early_stop": 0.0,
        }
        returns, advantages = self._compute_returns_advantages()
        rewards = [float(item.get("reward", 0.0)) for item in self.buffer]
        batch_avg = sum(rewards) / float(len(rewards))
        self.baseline = self.baseline * self.baseline_momentum + batch_avg * (1.0 - self.baseline_momentum)
        value_preds = [float(item.get("value", 0.0)) for item in self.buffer]
        raw_mean_adv = 0.0
        raw_std_adv = 0.0
        if advantages:
            raw_mean_adv = sum(advantages) / float(len(advantages))
            raw_var_adv = sum((x - raw_mean_adv) * (x - raw_mean_adv) for x in advantages) / float(len(advantages))
            raw_std_adv = math.sqrt(max(raw_var_adv, 1e-8))
            self.advantage_mean = raw_mean_adv
        self.last_update_info["advantage_mean"] = float(self.advantage_mean)
        self.last_update_info["advantage_std"] = float(raw_std_adv)
        if self.advantage_normalize and len(advantages) > 1:
            advantages = [(x - raw_mean_adv) / max(raw_std_adv, 1e-8) for x in advantages]
        clip_frac_adv = 0.0
        if self.advantage_clip > 0.0 and advantages:
            clip_limit = float(self.advantage_clip)
            clipped_count = 0
            clipped_adv: List[float] = []
            for value in advantages:
                clipped = max(-clip_limit, min(clip_limit, float(value)))
                if abs(clipped - float(value)) > 1e-12:
                    clipped_count += 1
                clipped_adv.append(clipped)
            advantages = clipped_adv
            clip_frac_adv = float(clipped_count) / float(len(advantages))
        self.last_update_info["advantage_clip_frac"] = float(clip_frac_adv)
        if not returns:
            returns = list(rewards)
        n = len(self.buffer)
        if len(returns) > 1:
            mean_ret = sum(returns) / float(len(returns))
            var_ret = sum((x - mean_ret) * (x - mean_ret) for x in returns) / float(len(returns))
            var_err = sum((returns[i] - value_preds[i]) * (returns[i] - value_preds[i]) for i in range(len(returns))) / float(len(returns))
            self.last_update_info["explained_variance"] = float(1.0 - (var_err / (var_ret + 1e-8)))
        self.last_update_info["value_mean"] = float(sum(value_preds) / float(max(len(value_preds), 1)))
        self.last_update_info["return_mean"] = float(sum(returns) / float(max(len(returns), 1)))
        group_to_indices: Dict[str, List[int]] = {}
        for idx, item in enumerate(self.buffer):
            group_id = item.get("step_group_id")
            if not isinstance(group_id, str) or not group_id:
                continue
            group_size = int(item.get("selected_group_size", 1) or 1)
            if group_size <= 1:
                continue
            group_to_indices.setdefault(group_id, []).append(idx)
        group_leader: Dict[str, int] = {}
        group_advantage: Dict[str, float] = {}
        for group_id, indices in group_to_indices.items():
            unique_indices = sorted(set(indices))
            if not unique_indices:
                continue
            group_leader[group_id] = unique_indices[0]
            group_advantage[group_id] = float(sum(float(advantages[i]) for i in unique_indices) / float(len(unique_indices)))
        mb_size = self.minibatch_size if self.minibatch_size > 0 else n
        if mb_size <= 0:
            mb_size = n
        mb_size = min(max(1, mb_size), n)
        n_updates = 0
        early_stop = False
        policy_items = 0
        policy_skipped_items = 0
        for _ in range(self.epochs):
            if early_stop:
                break
            order = list(range(n))
            random.shuffle(order)
            epoch_kl_sum = 0.0
            epoch_mb_count = 0
            for start in range(0, n, mb_size):
                batch_indices = order[start : start + mb_size]
                if not batch_indices:
                    continue
                mb_kl = 0.0
                for idx in batch_indices:
                    item = self.buffer[idx]
                    apply_policy_update = True
                    policy_advantage = float(advantages[idx])
                    old_joint_log_prob = item.get("old_joint_log_prob")
                    selected_group_size = int(item.get("selected_group_size", 1) or 1)
                    joint_ratio_override: Optional[float] = None
                    joint_approx_kl_override: Optional[float] = None
                    group_id = item.get("step_group_id")
                    if (
                        isinstance(group_id, str)
                        and group_id
                        and selected_group_size > 1
                        and group_id in group_leader
                    ):
                        if idx == group_leader[group_id]:
                            policy_advantage = float(group_advantage.get(group_id, policy_advantage))
                            group_joint_precise = None
                            raw_group_candidates = item.get("group_candidates")
                            if isinstance(raw_group_candidates, list):
                                group_joint_precise = self._compute_group_joint_log_prob_from_candidates(raw_group_candidates)
                            if isinstance(group_joint_precise, (int, float)) and isinstance(old_joint_log_prob, (int, float)):
                                old_joint = float(old_joint_log_prob)
                                new_joint = float(group_joint_precise)
                                joint_ratio_override = math.exp(max(-20.0, min(20.0, new_joint - old_joint)))
                                joint_approx_kl_override = old_joint - new_joint
                            else:
                                group_joint = self._compute_group_joint_log_prob(group_to_indices.get(group_id, []))
                                if isinstance(group_joint, tuple):
                                    old_joint, new_joint = group_joint
                                    joint_ratio_override = math.exp(max(-20.0, min(20.0, new_joint - old_joint)))
                                    joint_approx_kl_override = old_joint - new_joint
                        else:
                            apply_policy_update = False
                            policy_advantage = 0.0
                            old_joint_log_prob = None
                            selected_group_size = 1
                    if apply_policy_update:
                        policy_items += 1
                    else:
                        policy_skipped_items += 1
                    info = self._apply_transition(
                        int(item["action_idx"]),
                        float(item["old_log_prob"]),
                        policy_advantage,
                        float(returns[idx]),
                        item.get("features"),
                        old_value_pred=float(item.get("value", 0.0)),
                        old_joint_log_prob=old_joint_log_prob,
                        selected_group_size=selected_group_size,
                        apply_policy_update=apply_policy_update,
                        joint_ratio_override=joint_ratio_override,
                        joint_approx_kl_override=joint_approx_kl_override,
                        step_candidates=item.get("step_candidates"),
                        selected_candidate_idx=int(item.get("selected_candidate_idx", -1) or -1),
                    )
                    self.last_update_info["policy_loss"] += float(info.get("policy_loss", 0.0))
                    self.last_update_info["value_loss"] += float(info.get("value_loss", 0.0))
                    self.last_update_info["entropy"] += float(info.get("entropy", 0.0))
                    self.last_update_info["approx_kl"] += float(info.get("approx_kl", 0.0))
                    self.last_update_info["clip_frac"] += float(info.get("clip_frac", 0.0))
                    mb_kl += float(info.get("approx_kl", 0.0))
                n_updates += 1
                epoch_mb_count += 1
                mb_avg_kl = mb_kl / float(len(batch_indices))
                epoch_kl_sum += mb_avg_kl
            if self.target_kl > 0.0 and epoch_mb_count > 0:
                epoch_avg_kl = epoch_kl_sum / float(epoch_mb_count)
                if epoch_avg_kl > self.target_kl:
                    early_stop = True
        if n_updates > 0:
            self.last_update_info["policy_loss"] /= float(n_updates)
            self.last_update_info["value_loss"] /= float(n_updates)
            self.last_update_info["entropy"] /= float(n_updates)
            self.last_update_info["approx_kl"] /= float(n_updates)
            self.last_update_info["clip_frac"] /= float(n_updates)
        self.last_update_info["n_updates"] = float(n_updates)
        self.last_update_info["early_stop"] = 1.0 if early_stop else 0.0
        self.last_update_info["policy_items"] = float(policy_items)
        self.last_update_info["policy_skipped_items"] = float(policy_skipped_items)
        self.buffer = []
        self.update_steps += 1

    def export_state(self) -> Dict[str, Any]:
        return {
            "logits": dict(self.logits),
            "baseline": self.baseline,
            "reward_mean": self.reward_mean,
            "advantage_mean": self.advantage_mean,
            "update_steps": self.update_steps,
            "episodes_since_update": self.episodes_since_update,
            "flush_stats": {
                "calls": self.flush_calls,
                "force_calls": self.flush_force_calls,
                "skipped_updates": self.flush_skipped_updates,
                "triggered_updates": self.flush_triggered_updates,
            },
            "value_table": dict(self.value_table),
            "action_weights": {action_type: dict(weights) for action_type, weights in self.action_weights.items()},
            "value_weights": dict(self.value_weights),
            "value_bias": self.value_bias,
            "topk_entropy_mean": self.topk_entropy_mean,
            "topk_mass_mean": self.topk_mass_mean,
            "topk_bin_entropy_mean": self.topk_bin_entropy_mean,
            "optimizer_step_policy": self.optimizer_step_policy,
            "optimizer_step_value": self.optimizer_step_value,
            "opt_m_logits": dict(self.opt_m_logits),
            "opt_v_logits": dict(self.opt_v_logits),
            "opt_m_action_weights": {
                action_type: dict(weights) for action_type, weights in self.opt_m_action_weights.items()
            },
            "opt_v_action_weights": {
                action_type: dict(weights) for action_type, weights in self.opt_v_action_weights.items()
            },
            "opt_m_value_weights": dict(self.opt_m_value_weights),
            "opt_v_value_weights": dict(self.opt_v_value_weights),
            "opt_m_value_bias": self.opt_m_value_bias,
            "opt_v_value_bias": self.opt_v_value_bias,
            "last_update": dict(self.last_update_info),
            "mem_controller_hidden_dim": self.hidden_dim,
            "mem_controller_params": self._encode_mem_controller_params(),
            "buffer": list(self.buffer),
            "config": {
                "lr": self.lr,
                "clip_epsilon": self.clip_epsilon,
                "entropy_coef": self.entropy_coef,
                "epochs": self.epochs,
                "batch_size": self.batch_size,
                "minibatch_size": self.minibatch_size,
                "target_kl": self.target_kl,
                "max_grad_norm": self.max_grad_norm,
                "vf_clip": self.vf_clip,
                "episode_flush_only": self.episode_flush_only,
                "top_k": self.top_k,
                "min_prob": self.min_prob,
                "gamma": self.gamma,
                "gae_lambda": self.gae_lambda,
                "value_lr": self.value_lr,
                "update_every_episodes": self.update_every_episodes,
                "advantage_normalize": self.advantage_normalize,
                "advantage_clip": self.advantage_clip,
                "adam_beta1": self.adam_beta1,
                "adam_beta2": self.adam_beta2,
                "adam_eps": self.adam_eps,
                "adam_weight_decay": self.adam_weight_decay,
                "value_coef": self.value_coef,
            },
        }

    def load_state(self, data: Dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        logits = data.get("logits")
        if isinstance(logits, dict):
            for key in self.action_types:
                value = logits.get(key)
                if isinstance(value, (int, float)):
                    self.logits[key] = float(value)
        baseline = data.get("baseline")
        if isinstance(baseline, (int, float)):
            self.baseline = float(baseline)
        reward_mean = data.get("reward_mean")
        if isinstance(reward_mean, (int, float)):
            self.reward_mean = float(reward_mean)
        advantage_mean = data.get("advantage_mean")
        if isinstance(advantage_mean, (int, float)):
            self.advantage_mean = float(advantage_mean)
        update_steps = data.get("update_steps")
        if isinstance(update_steps, (int, float)):
            self.update_steps = int(update_steps)
        episodes_since_update = data.get("episodes_since_update")
        if isinstance(episodes_since_update, (int, float)):
            self.episodes_since_update = max(0, int(episodes_since_update))
        flush_stats_raw = data.get("flush_stats")
        if isinstance(flush_stats_raw, dict):
            raw_calls = flush_stats_raw.get("calls")
            if isinstance(raw_calls, (int, float)):
                self.flush_calls = max(0, int(raw_calls))
            raw_force_calls = flush_stats_raw.get("force_calls")
            if isinstance(raw_force_calls, (int, float)):
                self.flush_force_calls = max(0, int(raw_force_calls))
            raw_skipped_updates = flush_stats_raw.get("skipped_updates")
            if isinstance(raw_skipped_updates, (int, float)):
                self.flush_skipped_updates = max(0, int(raw_skipped_updates))
            raw_triggered_updates = flush_stats_raw.get("triggered_updates")
            if isinstance(raw_triggered_updates, (int, float)):
                self.flush_triggered_updates = max(0, int(raw_triggered_updates))
        buffer_raw = data.get("buffer")
        if isinstance(buffer_raw, list):
            restored_buffer: List[Dict[str, Any]] = []
            for item in buffer_raw:
                if isinstance(item, dict):
                    restored_buffer.append(dict(item))
            self.buffer = restored_buffer
        value_table = data.get("value_table")
        if isinstance(value_table, dict):
            for key in self.action_types:
                value = value_table.get(key)
                if isinstance(value, (int, float)):
                    self.value_table[key] = float(value)
        action_weights = data.get("action_weights")
        if isinstance(action_weights, dict):
            for action_type in self.action_types:
                raw_weights = action_weights.get(action_type)
                if not isinstance(raw_weights, dict):
                    continue
                for name in self.feature_keys:
                    value = raw_weights.get(name)
                    if isinstance(value, (int, float)):
                        self.action_weights[action_type][name] = float(value)
        value_weights = data.get("value_weights")
        if isinstance(value_weights, dict):
            for name in self.feature_keys:
                value = value_weights.get(name)
                if isinstance(value, (int, float)):
                    self.value_weights[name] = float(value)
        value_bias = data.get("value_bias")
        if isinstance(value_bias, (int, float)):
            self.value_bias = float(value_bias)
        topk_entropy_mean = data.get("topk_entropy_mean")
        if isinstance(topk_entropy_mean, (int, float)):
            self.topk_entropy_mean = float(topk_entropy_mean)
        topk_mass_mean = data.get("topk_mass_mean")
        if isinstance(topk_mass_mean, (int, float)):
            self.topk_mass_mean = float(topk_mass_mean)
        topk_bin_entropy_mean = data.get("topk_bin_entropy_mean")
        if isinstance(topk_bin_entropy_mean, (int, float)):
            self.topk_bin_entropy_mean = float(topk_bin_entropy_mean)
        optimizer_step_policy = data.get("optimizer_step_policy")
        if isinstance(optimizer_step_policy, (int, float)):
            self.optimizer_step_policy = max(0, int(optimizer_step_policy))
        optimizer_step_value = data.get("optimizer_step_value")
        if isinstance(optimizer_step_value, (int, float)):
            self.optimizer_step_value = max(0, int(optimizer_step_value))
        opt_m_logits = data.get("opt_m_logits")
        if isinstance(opt_m_logits, dict):
            for key in self.action_types:
                value = opt_m_logits.get(key)
                if isinstance(value, (int, float)):
                    self.opt_m_logits[key] = float(value)
        opt_v_logits = data.get("opt_v_logits")
        if isinstance(opt_v_logits, dict):
            for key in self.action_types:
                value = opt_v_logits.get(key)
                if isinstance(value, (int, float)):
                    self.opt_v_logits[key] = float(value)
        opt_m_action_weights = data.get("opt_m_action_weights")
        if isinstance(opt_m_action_weights, dict):
            for action_type in self.action_types:
                raw_weights = opt_m_action_weights.get(action_type)
                if not isinstance(raw_weights, dict):
                    continue
                for name in self.feature_keys:
                    value = raw_weights.get(name)
                    if isinstance(value, (int, float)):
                        self.opt_m_action_weights[action_type][name] = float(value)
        opt_v_action_weights = data.get("opt_v_action_weights")
        if isinstance(opt_v_action_weights, dict):
            for action_type in self.action_types:
                raw_weights = opt_v_action_weights.get(action_type)
                if not isinstance(raw_weights, dict):
                    continue
                for name in self.feature_keys:
                    value = raw_weights.get(name)
                    if isinstance(value, (int, float)):
                        self.opt_v_action_weights[action_type][name] = float(value)
        opt_m_value_weights = data.get("opt_m_value_weights")
        if isinstance(opt_m_value_weights, dict):
            for name in self.feature_keys:
                value = opt_m_value_weights.get(name)
                if isinstance(value, (int, float)):
                    self.opt_m_value_weights[name] = float(value)
        opt_v_value_weights = data.get("opt_v_value_weights")
        if isinstance(opt_v_value_weights, dict):
            for name in self.feature_keys:
                value = opt_v_value_weights.get(name)
                if isinstance(value, (int, float)):
                    self.opt_v_value_weights[name] = float(value)
        opt_m_value_bias = data.get("opt_m_value_bias")
        if isinstance(opt_m_value_bias, (int, float)):
            self.opt_m_value_bias = float(opt_m_value_bias)
        opt_v_value_bias = data.get("opt_v_value_bias")
        if isinstance(opt_v_value_bias, (int, float)):
            self.opt_v_value_bias = float(opt_v_value_bias)
        last_update = data.get("last_update")
        if isinstance(last_update, dict):
            for key, value in last_update.items():
                if isinstance(value, (int, float)):
                    self.last_update_info[str(key)] = float(value)
        mem_controller_params = data.get("mem_controller_params")
        if not isinstance(mem_controller_params, list):
            legacy_mem_controller_params = data.get("controller_params")
            if isinstance(legacy_mem_controller_params, list):
                mem_controller_params = legacy_mem_controller_params
        if isinstance(mem_controller_params, list):
            self._decode_mem_controller_params(mem_controller_params)
        config = data.get("config")
        if isinstance(config, dict):
            update_every_episodes = config.get("update_every_episodes")
            if isinstance(update_every_episodes, (int, float)):
                self.update_every_episodes = max(1, int(update_every_episodes))
            advantage_normalize = config.get("advantage_normalize")
            if isinstance(advantage_normalize, bool):
                self.advantage_normalize = advantage_normalize
            advantage_clip = config.get("advantage_clip")
            if isinstance(advantage_clip, (int, float)):
                self.advantage_clip = max(0.0, float(advantage_clip))


class MemoryRecord:
    def __init__(
        self,
        content: str,
        source: str,
        timestamp: float,
        memory_type: str = "task",
        tool_name: str = "",
        created_at: Optional[float] = None,
        content_history: Optional[List[str]] = None,
        operation_history: Optional[List[str]] = None,
        retrieval_embedding: Optional[List[float]] = None,
        update_embedding: Optional[List[float]] = None,
        embedding: Optional[List[float]] = None,
    ):
        self.content = content
        self.source = source
        memory_type_key = str(memory_type or "task").strip().lower()
        if memory_type_key not in {"task", "tool"}:
            memory_type_key = "task"
        self.memory_type = memory_type_key
        self.tool_name = str(tool_name or "").strip().lower()
        self.tokens = _memory_tokens(content)
        base_embedding = list(embedding) if isinstance(embedding, list) else None
        if isinstance(retrieval_embedding, list):
            self.retrieval_embedding = list(retrieval_embedding)
        elif base_embedding is not None:
            self.retrieval_embedding = list(base_embedding)
        else:
            self.retrieval_embedding = _memory_embedding(content, profile="retrieval")
        if isinstance(update_embedding, list):
            self.update_embedding = list(update_embedding)
        elif base_embedding is not None:
            self.update_embedding = list(base_embedding)
        else:
            self.update_embedding = _memory_embedding(content, profile="update")
        self.embedding = list(self.retrieval_embedding)
        self.updated_at = timestamp
        self.created_at = timestamp if created_at is None else float(created_at)
        self.last_accessed = timestamp
        self.access_count = 0
        self.content_history = list(content_history or [])
        self.operation_history = list(operation_history or [])
        self.hits = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "content": self.content,
            "source": self.source,
            "memory_type": self.memory_type,
            "tool_name": self.tool_name,
            "updated_at": self.updated_at,
            "created_at": self.created_at,
            "last_accessed": self.last_accessed,
            "access_count": self.access_count,
            "hits": self.hits,
            "content_history": list(self.content_history),
            "operation_history": list(self.operation_history),
            "embedding": list(self.retrieval_embedding),
            "retrieval_embedding": list(self.retrieval_embedding),
            "update_embedding": list(self.update_embedding),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryRecord":
        content = str(data.get("content") or "")
        source = str(data.get("source") or "")
        updated_at = float(data.get("updated_at") or time.time())
        record = cls(
            content=content,
            source=source,
            timestamp=updated_at,
            memory_type=str(data.get("memory_type") or "task"),
            tool_name=str(data.get("tool_name") or ""),
            created_at=data.get("created_at"),
            content_history=data.get("content_history") if isinstance(data.get("content_history"), list) else [],
            operation_history=data.get("operation_history") if isinstance(data.get("operation_history"), list) else [],
            retrieval_embedding=data.get("retrieval_embedding")
            if isinstance(data.get("retrieval_embedding"), list)
            else None,
            update_embedding=data.get("update_embedding") if isinstance(data.get("update_embedding"), list) else None,
            embedding=data.get("embedding") if isinstance(data.get("embedding"), list) else None,
        )
        record.last_accessed = float(data.get("last_accessed") or updated_at)
        record.access_count = int(data.get("access_count") or 0)
        record.hits = int(data.get("hits") or 1)
        return record


class LightweightMemoryBank:
    def __init__(self, max_items: int = 80, default_top_k: int = 3, mode: str = "controller"):
        self.max_items = max(10, int(max_items))
        self.default_top_k = max(1, int(default_top_k))
        self.mode = "controller"
        self.records: List[MemoryRecord] = []
        self.tool_records: Dict[str, List[MemoryRecord]] = {}
        self.retrieve_min_score = _memory_env_float("AGENT_MEMORY_RETRIEVE_THRESHOLD", 0.08)
        self.action_min_score = _memory_env_float("AGENT_MEMORY_ACTION_THRESHOLD", 0.16)
        self.action_min_margin = _memory_env_float("AGENT_MEMORY_ACTION_MARGIN", 0.06)
        self.upsert_match_threshold = _memory_env_float("AGENT_MEMORY_UPSERT_THRESHOLD", 0.86)
        self.update_consistency_threshold = _memory_env_float("AGENT_MEMORY_UPDATE_CONSISTENCY_THRESHOLD", 0.28)
        self.action_stats: Dict[str, int] = {
            "insert": 0,
            "update": 0,
            "delete": 0,
            "noop": 0,
            "invalid": 0,
            "fallback_rule": 0,
        }
        self.ppo_policy = PPOActionPolicy()
        self.ppo_reward_redistribute = str(os.environ.get("AGENT_MEMORY_PPO_REWARD_REDISTRIBUTE", "1")).strip().lower() not in {"0", "false", "no"}
        self.ppo_reward_selected_only = str(os.environ.get("AGENT_MEMORY_PPO_REWARD_SELECTED_ONLY", "1")).strip().lower() not in {"0", "false", "no"}
        self.ppo_reward_decay = _memory_env_float("AGENT_MEMORY_PPO_REWARD_DECAY", 0.90, min_value=0.1, max_value=1.0)
        self.ppo_final_reward_last_ratio = _memory_env_float("AGENT_MEMORY_PPO_FINAL_REWARD_LAST_RATIO", 0.70, min_value=0.0, max_value=1.0)
        self.ppo_episode_reward_redistribute = str(os.environ.get("AGENT_MEMORY_PPO_EPISODE_REWARD_REDISTRIBUTE", "1")).strip().lower() not in {"0", "false", "no"}
        self.ppo_episode_reward_selected_only = str(os.environ.get("AGENT_MEMORY_PPO_EPISODE_REWARD_SELECTED_ONLY", "1")).strip().lower() not in {"0", "false", "no"}
        self.ppo_episode_reward_decay = _memory_env_float("AGENT_MEMORY_PPO_EPISODE_REWARD_DECAY", 0.95, min_value=0.1, max_value=1.0)
        self.ppo_episode_final_reward_last_ratio = _memory_env_float("AGENT_MEMORY_PPO_EPISODE_FINAL_REWARD_LAST_RATIO", 0.0, min_value=0.0, max_value=1.0)
        self.ppo_episode_success_reward = _memory_env_float("AGENT_MEMORY_PPO_EPISODE_SUCCESS_REWARD", 0.06, min_value=-1.0, max_value=1.0)
        self.ppo_episode_error_reward = _memory_env_float("AGENT_MEMORY_PPO_EPISODE_ERROR_REWARD", -0.05, min_value=-1.0, max_value=1.0)
        self.ppo_episode_max_steps_reward = _memory_env_float("AGENT_MEMORY_PPO_EPISODE_MAX_STEPS_REWARD", -0.03, min_value=-1.0, max_value=1.0)
        self.chunk_mode = str(os.environ.get("AGENT_MEMORY_CHUNK_MODE", "paragraph")).strip().lower()
        if self.chunk_mode not in {"none", "paragraph", "fixed-length", "full-session"}:
            self.chunk_mode = "paragraph"
        self.chunk_size = _memory_env_int("AGENT_MEMORY_CHUNK_SIZE", 900, min_value=64, max_value=16000)
        self.chunk_overlap = _memory_env_int("AGENT_MEMORY_CHUNK_OVERLAP", 120, min_value=0, max_value=8000)
        if self.chunk_overlap >= self.chunk_size:
            self.chunk_overlap = max(0, self.chunk_size // 4)
        self.chunk_max_parts = _memory_env_int("AGENT_MEMORY_CHUNK_MAX_PARTS", 4, min_value=1, max_value=16)
        self.fusion_mode = str(os.environ.get("AGENT_MEMORY_FUSION_MODE", "sim_weighted")).strip().lower()
        if self.fusion_mode not in {"mean", "sim_weighted"}:
            self.fusion_mode = "sim_weighted"
        self.fusion_tau = _memory_env_float("AGENT_MEMORY_FUSION_TAU", 1.0, min_value=0.1, max_value=10.0)
        self.query_history_size = _memory_env_int("AGENT_MEMORY_QUERY_HISTORY_SIZE", 8, min_value=1, max_value=64)
        self.query_history_embeddings: List[List[float]] = []

    def _chunk_text_for_memory(self, text: str, lines: Optional[List[str]] = None) -> List[str]:
        mode = self.chunk_mode
        if mode == "none":
            if lines:
                return [" | ".join(lines[:8])]
            normalized = _memory_normalize_text(text)
            return [normalized] if normalized else []
        if mode == "full-session":
            normalized = _memory_normalize_text(text)
            return [normalized] if normalized else []
        if mode == "fixed-length":
            chunks = _memory_split_fixed_length(text, self.chunk_size, self.chunk_overlap)
        else:
            chunks = _memory_split_paragraph(text)
            if not chunks:
                chunks = _memory_split_fixed_length(text, self.chunk_size, self.chunk_overlap)
        if not chunks and lines:
            merged = " | ".join(lines[:12]).strip()
            if merged:
                chunks = [merged]
        return chunks[: self.chunk_max_parts]

    def _register_query_embedding(self, query_embedding: List[float]) -> None:
        if not isinstance(query_embedding, list) or not query_embedding:
            return
        if not any(abs(x) > 0 for x in query_embedding):
            return
        self.query_history_embeddings.append(list(query_embedding))
        if len(self.query_history_embeddings) > self.query_history_size:
            self.query_history_embeddings = self.query_history_embeddings[-self.query_history_size :]

    def _fuse_memory_embeddings(self, query_embedding: List[float], memory_embeddings: List[List[float]]) -> Tuple[float, float, float]:
        if not memory_embeddings:
            return 0.0, 0.0, 0.0
        sims: List[float] = []
        for emb in memory_embeddings:
            sims.append(_memory_cosine_similarity(query_embedding, emb))
        if not sims:
            return 0.0, 0.0, 0.0
        mean_sim = sum(sims) / float(len(sims))
        variance = sum((x - mean_sim) * (x - mean_sim) for x in sims) / float(len(sims))
        dispersion = math.sqrt(max(variance, 0.0))
        if self.fusion_mode == "mean":
            weights = [1.0 / float(len(memory_embeddings)) for _ in memory_embeddings]
        else:
            tau = max(1e-6, float(self.fusion_tau))
            shifted = [(sim / tau) for sim in sims]
            max_score = max(shifted)
            exps = [math.exp(v - max_score) for v in shifted]
            denom = sum(exps)
            if denom <= 1e-12:
                weights = [1.0 / float(len(memory_embeddings)) for _ in memory_embeddings]
            else:
                weights = [value / denom for value in exps]
        fused = [0.0 for _ in query_embedding]
        for emb, weight in zip(memory_embeddings, weights):
            if len(emb) != len(fused):
                continue
            for idx, value in enumerate(emb):
                fused[idx] += float(value) * float(weight)
        fused_sim = _memory_cosine_similarity(query_embedding, fused)
        return float(fused_sim), float(dispersion), float(max(weights) if weights else 0.0)

    def _build_ppo_context(
        self,
        tool_name: str,
        target: str,
        lines: List[str],
        candidate_scores_by_seq: Dict[int, float],
        query_embedding: Optional[List[float]] = None,
        memory_embeddings: Optional[List[List[float]]] = None,
        chunk_count: int = 0,
        blocked_ratio: float = 0.0,
    ) -> Dict[str, float]:
        scores = [float(v) for v in candidate_scores_by_seq.values()]
        candidate_count = float(len(scores))
        if scores:
            top_score = max(scores)
            mean_score = sum(scores) / float(len(scores))
            sorted_scores = sorted(scores, reverse=True)
            ambiguous = len(sorted_scores) >= 2 and (sorted_scores[0] - sorted_scores[1]) < self.action_min_margin
        else:
            top_score = 0.0
            mean_score = 0.0
            ambiguous = False
        line_count = float(len(lines))
        query_memory_similarity = 0.0
        query_memory_dispersion = 0.0
        fusion_mass = 0.0
        if isinstance(query_embedding, list) and isinstance(memory_embeddings, list) and memory_embeddings:
            fused_sim, dispersion, mass = self._fuse_memory_embeddings(query_embedding, memory_embeddings)
            query_memory_similarity = max(0.0, min(1.0, (fused_sim + 1.0) * 0.5))
            query_memory_dispersion = max(0.0, min(1.0, dispersion))
            fusion_mass = max(0.0, min(1.0, mass))
        recent_query_similarity = 0.0
        if isinstance(query_embedding, list) and self.query_history_embeddings:
            latest = self.query_history_embeddings[-1]
            recent_query_similarity = _memory_cosine_similarity(query_embedding, latest)
            recent_query_similarity = max(0.0, min(1.0, (recent_query_similarity + 1.0) * 0.5))
        chunk_density = 0.0
        if chunk_count > 0:
            chunk_density = max(0.0, min(1.0, float(chunk_count) / float(max(int(line_count), 1))))
        context = {
            "candidate_count": min(candidate_count, 8.0) / 8.0,
            "candidate_score_top": max(0.0, min(1.0, top_score)),
            "candidate_score_mean": max(0.0, min(1.0, mean_score)),
            "tool_line_count": min(line_count, 16.0) / 16.0,
            "has_target": 1.0 if str(target or "").strip() else 0.0,
            "ambiguous": 1.0 if ambiguous else 0.0,
            "blocked_ratio": max(0.0, min(1.0, float(blocked_ratio))),
            "query_memory_similarity": query_memory_similarity,
            "query_memory_dispersion": query_memory_dispersion,
            "fusion_mass": fusion_mass,
            "recent_query_similarity": recent_query_similarity,
            "chunk_density": chunk_density,
        }
        return context

    def _build_policy_rewards(
        self,
        action_feedback: List[Dict[str, float]],
        changed: bool,
        guard_meta: Dict[str, Any],
        stats_before: Dict[str, int],
        stats_after: Dict[str, int],
    ) -> List[float]:
        if not action_feedback:
            return []
        blocked_count = len(guard_meta.get("blocked_indices") or [])
        invalid_delta = int(stats_after.get("invalid", 0)) - int(stats_before.get("invalid", 0))
        selected_mask: List[bool] = []
        for item in action_feedback:
            is_selected = True
            if self.ppo_reward_selected_only:
                raw_features = item.get("features")
                if isinstance(raw_features, dict):
                    raw_flag = raw_features.get("selected_flag", 0.0)
                    if isinstance(raw_flag, (int, float, bool)):
                        is_selected = float(raw_flag) >= 0.5
                    else:
                        is_selected = False
                else:
                    is_selected = False
            selected_mask.append(bool(is_selected))
        if not any(selected_mask):
            selected_mask = [True] * len(action_feedback)
        process_rewards: List[float] = []
        for idx, item in enumerate(action_feedback):
            if not selected_mask[idx]:
                process_rewards.append(0.0)
                continue
            action_type = str(item.get("action_type", "")).strip().upper()
            reward = 0.0
            if action_type == "NOOP":
                reward -= 0.04
                if not changed:
                    reward += 0.06
            else:
                reward += 0.38 if changed else -0.22
                if action_type in {"UPDATE", "DELETE"} and blocked_count > 0:
                    reward -= 0.08
                if invalid_delta > 0:
                    reward -= 0.18
            process_rewards.append(reward)
        final_reward = 0.34 if changed else -0.16
        if blocked_count > 0:
            final_reward -= min(0.12, 0.03 * float(blocked_count))
        if invalid_delta > 0:
            final_reward -= min(0.25, 0.07 * float(invalid_delta))
        count = len(action_feedback)
        selected_indices = [idx for idx, flag in enumerate(selected_mask) if flag]
        selected_count = len(selected_indices)
        if selected_count <= 1:
            rewards = list(process_rewards)
            target_idx = selected_indices[-1] if selected_indices else (count - 1)
            rewards[target_idx] += final_reward
            return rewards
        if not self.ppo_reward_redistribute:
            rewards = list(process_rewards)
            rewards[selected_indices[-1]] += final_reward
            return rewards
        last_ratio = self.ppo_final_reward_last_ratio
        head_ratio = max(0.0, 1.0 - last_ratio)
        weights: List[float] = []
        total = 0.0
        for order_idx in range(selected_count):
            w = self.ppo_reward_decay ** float(selected_count - 1 - order_idx)
            weights.append(w)
            total += w
        if total <= 1e-12:
            total = 1.0
        redistributed = [0.0] * count
        for order_idx, action_idx in enumerate(selected_indices):
            redistributed[action_idx] += final_reward * head_ratio * (weights[order_idx] / total)
        redistributed[selected_indices[-1]] += final_reward * last_ratio
        rewards: List[float] = []
        for idx in range(count):
            rewards.append(process_rewards[idx] + redistributed[idx])
        return rewards

    def finalize_episode(
        self,
        status: str,
        qa_reward: Optional[float] = None,
        qa_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.ppo_policy.buffer:
            return
        status_key = str(status or "").strip().lower()
        delta = 0.0
        if status_key == "success":
            delta = self.ppo_episode_success_reward
        elif status_key == "max_steps_reached":
            delta = self.ppo_episode_max_steps_reward
        elif status_key == "error":
            delta = self.ppo_episode_error_reward
        final_reward = delta
        if isinstance(qa_reward, (int, float)):
            final_reward = float(qa_reward) + delta * 0.2
        if abs(final_reward) > 1e-12:
            self.ppo_policy.distribute_episode_reward(
                final_reward=final_reward,
                redistribute=self.ppo_episode_reward_redistribute,
                redistribution_decay=self.ppo_episode_reward_decay,
                final_reward_last_ratio=self.ppo_episode_final_reward_last_ratio,
                selected_only=self.ppo_episode_reward_selected_only,
            )
        self.ppo_policy.flush_episode(force_update=False)

    def flush_pending_policy_update(self) -> None:
        if not self.ppo_policy.buffer:
            return
        self.ppo_policy.flush_episode(force_update=True)

    def _trim_overflow(self) -> None:
        for tool_name, items in list(self.tool_records.items()):
            if not items:
                self.tool_records.pop(tool_name, None)
                continue
            if len(items) <= self.max_items:
                continue
            items.sort(key=lambda x: (x.hits, x.updated_at), reverse=True)
            self.tool_records[tool_name] = items[: self.max_items]
        if len(self.records) > self.max_items:
            self.records.sort(key=lambda x: (x.hits, x.updated_at), reverse=True)
            self.records = self.records[: self.max_items]

    def _resolve_scope(self, source: str, memory_type: str = "", tool_name: str = "") -> Tuple[str, str]:
        scope = str(memory_type or "").strip().lower()
        tool_key = str(tool_name or "").strip().lower()
        if scope not in {"task", "tool"}:
            src = str(source or "").strip().lower()
            if src.startswith("tool:"):
                scope = "tool"
                if not tool_key:
                    tool_key = src.split(":", 1)[1].strip().lower()
            else:
                scope = "task"
        if scope == "tool" and not tool_key:
            tool_key = "generic"
        return scope, tool_key

    def _select_records(self, memory_type: str, tool_name: str) -> List[MemoryRecord]:
        if memory_type == "tool":
            return self.tool_records.setdefault(tool_name, [])
        return self.records

    def _all_records(self) -> List[MemoryRecord]:
        merged = list(self.records)
        for items in self.tool_records.values():
            merged.extend(items)
        return merged

    def upsert(self, content: str, source: str, memory_type: str = "", tool_name: str = "") -> None:
        normalized = _memory_normalize_text(content)
        if not normalized:
            return
        now = time.time()
        scope, tool_key = self._resolve_scope(source, memory_type, tool_name)
        target_records = self._select_records(scope, tool_key)
        incoming_tokens = _memory_tokens(normalized)
        if not incoming_tokens:
            return
        incoming_retrieval_embedding = _memory_embedding(normalized, profile="retrieval")
        incoming_update_embedding = _memory_embedding(normalized, profile="update")
        best_match: Optional[MemoryRecord] = None
        best_score = 0.0
        for record in target_records:
            score = _memory_cosine_similarity(incoming_update_embedding, record.update_embedding)
            if score > best_score:
                best_score = score
                best_match = record
        if best_match is not None and best_score >= self.upsert_match_threshold:
            if len(normalized) >= len(best_match.content):
                best_match.content = normalized
                best_match.tokens = incoming_tokens
                best_match.retrieval_embedding = incoming_retrieval_embedding
                best_match.update_embedding = incoming_update_embedding
                best_match.embedding = list(best_match.retrieval_embedding)
            best_match.source = source
            best_match.updated_at = now
            best_match.hits += 1
            return
        target_records.append(
            MemoryRecord(
                normalized,
                source,
                now,
                memory_type=scope,
                tool_name=tool_key,
            )
        )
        self._trim_overflow()

    def _rank_records(self, query: str, top_k: Optional[int] = None) -> List[Tuple[float, int, MemoryRecord]]:
        records = self._all_records()
        if not records:
            return []
        query_embedding = _memory_embedding(query, profile="retrieval")
        if not any(abs(x) > 0 for x in query_embedding):
            return []
        limit = self.default_top_k if top_k is None else max(1, int(top_k))
        query_tokens = _memory_tokens(query)
        scored: List[Tuple[float, int, MemoryRecord]] = []
        for idx, record in enumerate(records):
            semantic = _memory_cosine_similarity(query_embedding, record.retrieval_embedding)
            lexical = _memory_overlap_score(query_tokens, record.tokens)
            focus_overlap = _memory_focus_overlap(query_tokens, record.tokens)
            score = semantic * 0.74 + lexical * 0.14 + focus_overlap * 0.12
            if score <= self.retrieve_min_score:
                continue
            score += min(record.hits, 6) * 0.02
            age_seconds = max(0.0, time.time() - float(record.updated_at))
            recency_decay = math.exp(-age_seconds / 259200.0)
            score = score * (0.85 + 0.15 * recency_decay)
            scored.append((score, idx, record))
        if not scored:
            return []
        scored.sort(key=lambda x: x[0], reverse=True)
        scores = [item[0] for item in scored]
        top_score = scores[0]
        mean_score = sum(scores) / float(len(scores))
        variance = sum((s - mean_score) * (s - mean_score) for s in scores) / float(len(scores))
        std_score = math.sqrt(max(variance, 0.0))
        dynamic_floor = max(self.retrieve_min_score, top_score * 0.72, mean_score + std_score * 0.20)
        filtered = [item for item in scored if item[0] >= dynamic_floor]
        if not filtered:
            filtered = scored[:1]
        return filtered[:limit]

    def retrieve_with_indices(self, query: str, top_k: Optional[int] = None) -> Tuple[List[str], List[int]]:
        ranked = self._rank_records(query, top_k=top_k)
        contents = [item.content for _, _, item in ranked]
        indices = [idx for _, idx, _ in ranked]
        return contents, indices

    def retrieve(self, query: str, top_k: Optional[int] = None) -> List[str]:
        contents, _ = self.retrieve_with_indices(query, top_k=top_k)
        return contents

    def retrieve_toolmem(self, tool_name: str, query: str, top_k: Optional[int] = None) -> List[Tuple[float, int, MemoryRecord]]:
        tool_key = str(tool_name or "").strip().lower()
        records = list(self.tool_records.get(tool_key) or [])
        if not records:
            return []
        query_embedding = _memory_embedding(query, profile="retrieval")
        if not any(abs(x) > 0 for x in query_embedding):
            return []
        limit = self.default_top_k if top_k is None else max(1, int(top_k))
        scored: List[Tuple[float, int, MemoryRecord]] = []
        for idx, record in enumerate(records):
            score = _memory_cosine_similarity(query_embedding, record.retrieval_embedding)
            lexical = _memory_overlap_score(_memory_tokens(query), record.tokens)
            score = score * 0.88 + lexical * 0.12
            if score <= self.retrieve_min_score:
                continue
            score += min(record.hits, 6) * 0.02
            scored.append((score, idx, record))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:limit]

    def retrieve_taskmem_full(self) -> List[MemoryRecord]:
        return list(self.records)

    def _guard_actions(
        self,
        actions: List[Dict[str, Any]],
        candidate_map: Dict[int, int],
        candidate_scores_by_seq: Dict[int, float],
        candidate_scores_by_index: Dict[int, float],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        def _noop_with_meta(source_action: Dict[str, Any]) -> Dict[str, Any]:
            noop_action: Dict[str, Any] = {"type": "NOOP"}
            if isinstance(source_action, dict):
                for key in ("__ppo_retrieval_score", "__ppo_rank_score", "__ppo_position_prob", "__ppo_selected"):
                    if key in source_action:
                        noop_action[key] = source_action[key]
                for key in ("__ppo_selection_cond_prob", "__ppo_selection_joint_log_prob", "__ppo_selected_group_size", "__ppo_selected_order"):
                    if key in source_action:
                        noop_action[key] = source_action[key]
            noop_action["__ppo_selected"] = 0.0
            return noop_action

        guarded: List[Dict[str, Any]] = []
        blocked_indices: List[int] = []
        blocked_reasons: List[str] = []
        top_scores = sorted(candidate_scores_by_seq.values(), reverse=True)
        ambiguous = len(top_scores) >= 2 and (top_scores[0] - top_scores[1]) < self.action_min_margin
        for action_idx, action in enumerate(actions):
            action_type = str(action.get("type", "")).strip().upper()
            if action_type not in {"UPDATE", "DELETE"}:
                guarded.append(action)
                continue
            score = 0.0
            raw_idx = action.get("memory_index")
            resolved_seq: Optional[int] = None
            resolved_index = -1
            try:
                seq_index = int(raw_idx)
                if seq_index in candidate_scores_by_seq:
                    score = candidate_scores_by_seq.get(seq_index, 0.0)
                    resolved_seq = seq_index
                    resolved_index = candidate_map.get(seq_index, -1)
                else:
                    resolved_index = self._resolve_memory_index(seq_index, candidate_map)
                    score = candidate_scores_by_index.get(resolved_index, 0.0)
            except Exception:
                score = 0.0
                resolved_index = -1
            if ambiguous or score < self.action_min_score:
                blocked_indices.append(action_idx)
                blocked_reasons.append("ambiguous_or_low_retrieval_score")
                self.action_stats["noop"] += 1
                guarded.append(_noop_with_meta(action))
                continue
            if action_type == "UPDATE":
                content = _memory_normalize_text(str(action.get("content", "")).strip())
                if (
                    resolved_index < 0
                    or resolved_index >= len(self.records)
                    or not content
                    or not self.records[resolved_index].update_embedding
                ):
                    blocked_indices.append(action_idx)
                    blocked_reasons.append("invalid_update_target_or_content")
                    self.action_stats["noop"] += 1
                    guarded.append(_noop_with_meta(action))
                    continue
                proposed_update_embedding = _memory_embedding(content, profile="update")
                update_similarity = _memory_cosine_similarity(
                    proposed_update_embedding,
                    self.records[resolved_index].update_embedding,
                )
                update_overlap = _memory_overlap_score(
                    _memory_tokens(content),
                    self.records[resolved_index].tokens,
                )
                update_consistency = update_similarity * 0.85 + update_overlap * 0.15
                if update_consistency < self.update_consistency_threshold:
                    blocked_indices.append(action_idx)
                    blocked_reasons.append("low_update_consistency")
                    self.action_stats["noop"] += 1
                    guarded.append(_noop_with_meta(action))
                    continue
            if resolved_seq is not None:
                action["memory_index"] = resolved_seq
            guarded.append(action)
        return guarded, {
            "blocked_indices": blocked_indices,
            "blocked_reasons": blocked_reasons,
            "ambiguous": ambiguous,
        }

    def _build_rule_summary(self, tool_name: str, input_obj: Dict[str, Any], tool_content: str) -> str:
        name = str(tool_name or "").strip()
        text = str(tool_content or "")
        lines = _memory_extract_lines(text, limit=12)
        if not lines:
            return ""
        target = _extract_tool_target(name, input_obj) or ""
        if name in {"ls", "glob", "grep"}:
            payload = " | ".join(lines[:8])
        elif name in {"view", "read"}:
            payload = " | ".join(lines[:6])
        else:
            payload = " | ".join(lines[:4])
        return f"[{name}] {target} => {payload}".strip()

    def _resolve_memory_index(self, idx: Any, candidate_map: Dict[int, int]) -> int:
        try:
            value = int(idx)
        except Exception:
            return -1
        mapped = candidate_map.get(value)
        if mapped is not None:
            return mapped
        if 0 <= value < len(self.records):
            return value
        if 1 <= value <= len(self.records):
            return value - 1
        return -1

    def _apply_actions(self, actions: List[Dict[str, Any]], candidate_map: Dict[int, int], source: str) -> bool:
        changed = False
        for action in actions:
            if not isinstance(action, dict):
                self.action_stats["invalid"] += 1
                continue
            action_type = str(action.get("type", "")).strip().upper()
            if not action_type:
                self.action_stats["invalid"] += 1
                continue
            if action_type == "NOOP":
                self.action_stats["noop"] += 1
                continue
            if action_type == "INSERT":
                content = str(action.get("content", "")).strip()
                if not content:
                    self.action_stats["invalid"] += 1
                    continue
                self.upsert(content, source)
                self.action_stats["insert"] += 1
                changed = True
                continue
            if action_type in {"UPDATE", "DELETE"}:
                record_index = self._resolve_memory_index(action.get("memory_index"), candidate_map)
                if record_index < 0 or record_index >= len(self.records):
                    self.action_stats["invalid"] += 1
                    continue
                if action_type == "DELETE":
                    self.records.pop(record_index)
                    self.action_stats["delete"] += 1
                    changed = True
                    continue
                content = str(action.get("content", "")).strip()
                normalized = _memory_normalize_text(content)
                if not normalized:
                    self.action_stats["invalid"] += 1
                    continue
                record = self.records[record_index]
                record.content = normalized
                record.tokens = _memory_tokens(normalized)
                record.retrieval_embedding = _memory_embedding(normalized, profile="retrieval")
                record.update_embedding = _memory_embedding(normalized, profile="update")
                record.embedding = list(record.retrieval_embedding)
                record.source = source
                record.updated_at = time.time()
                record.hits += 1
                self.action_stats["update"] += 1
                changed = True
                continue
            self.action_stats["invalid"] += 1
        self._trim_overflow()
        return changed

    def _resolve_memory_ref(self, idx: Any, candidate_ref_map: Dict[int, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        try:
            seq = int(idx)
        except Exception:
            return None
        ref = candidate_ref_map.get(seq)
        if isinstance(ref, dict):
            return ref
        return None

    def _record_by_ref(self, ref: Dict[str, Any]) -> Optional[MemoryRecord]:
        scope = str(ref.get("scope") or "task").strip().lower()
        index = int(ref.get("index", -1))
        if scope == "tool":
            tool_key = str(ref.get("tool_name") or "").strip().lower()
            bucket = self.tool_records.get(tool_key) or []
            if 0 <= index < len(bucket):
                return bucket[index]
            return None
        if 0 <= index < len(self.records):
            return self.records[index]
        return None

    def _build_mem_controller_actions(
        self,
        tool_name: str,
        target: str,
        lines: List[str],
        memory_chunks: List[str],
        candidate_ref_map: Dict[int, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        compact = " | ".join(lines[:4]).strip()
        actions: List[Dict[str, Any]] = [{"type": "NOOP"}]
        tool_chunks = [chunk for chunk in memory_chunks if str(chunk or "").strip()]
        if not tool_chunks and compact:
            tool_chunks = [compact]
        for idx, chunk in enumerate(tool_chunks[: self.chunk_max_parts], start=1):
            actions.append(
                {
                    "type": "INSERT",
                    "memory_type": "tool",
                    "tool_name": tool_name,
                    "content": f"[{tool_name}#{idx}] {target} => {chunk}".strip(),
                }
            )
        if tool_chunks:
            task_payload = " | ".join(tool_chunks[: min(2, len(tool_chunks))]).strip()
            if task_payload:
                actions.append(
                    {
                        "type": "INSERT",
                        "memory_type": "task",
                        "content": f"[task] {tool_name} {target} => {task_payload}".strip(),
                    }
                )
        for seq in sorted(candidate_ref_map.keys()):
            ref = candidate_ref_map.get(seq) or {}
            record = self._record_by_ref(ref)
            if record is None:
                continue
            updated_content = _memory_normalize_text(f"{record.content} | {compact}".strip(" |"))
            actions.append({"type": "UPDATE", "memory_index": seq, "content": updated_content})
            actions.append({"type": "DELETE", "memory_index": seq})
        return actions

    def _guard_actions_v2(
        self,
        actions: List[Dict[str, Any]],
        candidate_ref_map: Dict[int, Dict[str, Any]],
        candidate_scores_by_seq: Dict[int, float],
        candidate_scores_by_index: Dict[int, float],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        guarded: List[Dict[str, Any]] = []
        blocked_indices: List[int] = []
        blocked_reasons: List[str] = []
        top_scores = sorted(candidate_scores_by_seq.values(), reverse=True)
        ambiguous = len(top_scores) >= 2 and (top_scores[0] - top_scores[1]) < self.action_min_margin
        for action_idx, action in enumerate(actions):
            action_type = str(action.get("type", "")).strip().upper()
            if action_type not in {"UPDATE", "DELETE"}:
                guarded.append(action)
                continue
            seq = int(action.get("memory_index") or -1)
            score = float(candidate_scores_by_seq.get(seq, candidate_scores_by_index.get(seq, 0.0)))
            ref = self._resolve_memory_ref(seq, candidate_ref_map)
            if ref is None:
                blocked_indices.append(action_idx)
                blocked_reasons.append("invalid_target")
                guarded.append({"type": "NOOP"})
                continue
            record = self._record_by_ref(ref)
            if record is None or ambiguous or score < self.action_min_score:
                blocked_indices.append(action_idx)
                blocked_reasons.append("ambiguous_or_low_retrieval_score")
                guarded.append({"type": "NOOP"})
                continue
            if action_type == "UPDATE":
                content = _memory_normalize_text(str(action.get("content", "")).strip())
                if not content:
                    blocked_indices.append(action_idx)
                    blocked_reasons.append("invalid_update_content")
                    guarded.append({"type": "NOOP"})
                    continue
                proposed_update_embedding = _memory_embedding(content, profile="update")
                update_similarity = _memory_cosine_similarity(proposed_update_embedding, record.update_embedding)
                update_overlap = _memory_overlap_score(_memory_tokens(content), record.tokens)
                if update_similarity * 0.85 + update_overlap * 0.15 < self.update_consistency_threshold:
                    blocked_indices.append(action_idx)
                    blocked_reasons.append("low_update_consistency")
                    guarded.append({"type": "NOOP"})
                    continue
            guarded.append(action)
        return guarded, {"blocked_indices": blocked_indices, "blocked_reasons": blocked_reasons, "ambiguous": ambiguous}

    def _apply_actions_v2(self, actions: List[Dict[str, Any]], candidate_ref_map: Dict[int, Dict[str, Any]], source: str) -> bool:
        changed = False
        for action in actions:
            if not isinstance(action, dict):
                self.action_stats["invalid"] += 1
                continue
            action_type = str(action.get("type", "")).strip().upper()
            if action_type == "NOOP":
                self.action_stats["noop"] += 1
                continue
            if action_type == "INSERT":
                content = str(action.get("content", "")).strip()
                if not content:
                    self.action_stats["invalid"] += 1
                    continue
                memory_type = str(action.get("memory_type") or "").strip().lower()
                tool_name = str(action.get("tool_name") or "").strip().lower()
                self.upsert(content, source, memory_type=memory_type, tool_name=tool_name)
                self.action_stats["insert"] += 1
                changed = True
                continue
            if action_type in {"UPDATE", "DELETE"}:
                ref = self._resolve_memory_ref(action.get("memory_index"), candidate_ref_map)
                record = self._record_by_ref(ref or {})
                if ref is None or record is None:
                    self.action_stats["invalid"] += 1
                    continue
                if action_type == "DELETE":
                    scope = str(ref.get("scope") or "task").strip().lower()
                    index = int(ref.get("index", -1))
                    if scope == "tool":
                        tool_key = str(ref.get("tool_name") or "").strip().lower()
                        bucket = self.tool_records.get(tool_key) or []
                        if 0 <= index < len(bucket):
                            bucket.pop(index)
                            if not bucket:
                                self.tool_records.pop(tool_key, None)
                    else:
                        if 0 <= index < len(self.records):
                            self.records.pop(index)
                    self.action_stats["delete"] += 1
                    changed = True
                    continue
                normalized = _memory_normalize_text(str(action.get("content", "")).strip())
                if not normalized:
                    self.action_stats["invalid"] += 1
                    continue
                record.content = normalized
                record.tokens = _memory_tokens(normalized)
                record.retrieval_embedding = _memory_embedding(normalized, profile="retrieval")
                record.update_embedding = _memory_embedding(normalized, profile="update")
                record.embedding = list(record.retrieval_embedding)
                record.source = source
                record.updated_at = time.time()
                record.hits += 1
                self.action_stats["update"] += 1
                changed = True
                continue
            self.action_stats["invalid"] += 1
        self._trim_overflow()
        return changed

    def _mem_controller_decide_actions(
        self,
        tool_name: str,
        input_obj: Dict[str, Any],
        tool_content: str,
        debug_enabled: bool = False,
        log_title_suffix: str = "",
    ) -> bool:
        target = _extract_tool_target(tool_name, input_obj) or ""
        text = str(tool_content or "")
        lines = _memory_extract_lines(text, limit=14)
        if not lines:
            return False
        query = f"{tool_name}\n{target}\n" + "\n".join(lines[:8])
        query_embedding = _memory_embedding(query, profile="retrieval")
        memory_chunks = self._chunk_text_for_memory(text, lines=lines)
        ranked_tool_candidates = self.retrieve_toolmem(tool_name, query, top_k=self.default_top_k)
        taskmem_records = self.retrieve_taskmem_full()
        candidate_ref_map: Dict[int, Dict[str, Any]] = {}
        candidate_scores_by_seq: Dict[int, float] = {}
        candidate_scores_by_index: Dict[int, float] = {}
        memory_embeddings: List[List[float]] = []
        candidate_lines: List[str] = []
        seq = 1
        for score, idx, record in ranked_tool_candidates:
            candidate_ref_map[seq] = {"scope": "tool", "tool_name": str(tool_name or "").strip().lower(), "index": idx}
            candidate_scores_by_seq[seq] = float(score)
            candidate_scores_by_index[seq] = float(score)
            memory_embeddings.append(list(record.retrieval_embedding))
            candidate_lines.append(f"{seq}. [tool] (score={score:.3f}) {record.content}")
            seq += 1
        for idx, record in enumerate(taskmem_records):
            if seq > self.max_items:
                break
            score = _memory_cosine_similarity(_memory_embedding(query, profile="retrieval"), record.retrieval_embedding)
            candidate_ref_map[seq] = {"scope": "task", "index": idx}
            candidate_scores_by_seq[seq] = float(score)
            candidate_scores_by_index[seq] = float(score)
            memory_embeddings.append(list(record.retrieval_embedding))
            candidate_lines.append(f"{seq}. [task] (score={score:.3f}) {record.content}")
            seq += 1
        actions = self._build_mem_controller_actions(tool_name, target, lines, memory_chunks, candidate_ref_map)
        ppo_context = self._build_ppo_context(
            tool_name=tool_name,
            target=target,
            lines=lines,
            candidate_scores_by_seq=candidate_scores_by_seq,
            query_embedding=query_embedding,
            memory_embeddings=memory_embeddings,
            chunk_count=len(memory_chunks),
            blocked_ratio=0.0,
        )
        guarded_actions, guard_meta = self._guard_actions_v2(
            actions,
            candidate_ref_map,
            candidate_scores_by_seq,
            candidate_scores_by_index,
        )
        blocked = len(guard_meta.get("blocked_indices") or [])
        guarded_context = self._build_ppo_context(
            tool_name=tool_name,
            target=target,
            lines=lines,
            candidate_scores_by_seq=candidate_scores_by_seq,
            query_embedding=query_embedding,
            memory_embeddings=memory_embeddings,
            chunk_count=len(memory_chunks),
            blocked_ratio=(float(blocked) / float(max(len(guarded_actions), 1))),
        )
        action_feedback = self.ppo_policy.prepare_feedback(
            guarded_actions,
            context=guarded_context,
        )
        stats_before = dict(self.action_stats)
        changed = self._apply_actions_v2(guarded_actions, candidate_ref_map, source=f"tool:{tool_name}")
        stats_after = dict(self.action_stats)
        rewards = self._build_policy_rewards(action_feedback, changed, guard_meta, stats_before, stats_after)
        for item, reward in zip(action_feedback, rewards):
            self.ppo_policy.observe(
                str(item.get("action_type") or ""),
                float(item.get("old_log_prob") or 0.0),
                reward,
                done=False,
                features=item.get("features"),
                value_estimate=item.get("value_estimate"),
            )
        _debug_log_block(
            debug_enabled,
            f"记忆控制器决策{log_title_suffix}",
            {
                "tool_name": tool_name,
                "tool_target": target,
                "toolmem_candidates": candidate_lines,
                "mem_controller_actions": actions,
                "guarded_actions": guarded_actions,
                "stats": self.stats(),
            },
            max_len=12000,
        )
        self._register_query_embedding(query_embedding)
        return changed

    def observe_tool_result(
        self,
        tool_name: str,
        input_obj: Dict[str, Any],
        tool_content: str,
        is_error: bool,
        provider: Optional["DeepSeekProvider"] = None,
        debug_enabled: bool = False,
        log_title_suffix: str = "",
    ) -> None:
        if is_error:
            return
        name = str(tool_name or "").strip()
        if not name:
            return
        try:
            self._mem_controller_decide_actions(
                name,
                input_obj,
                tool_content,
                debug_enabled=debug_enabled,
                log_title_suffix=log_title_suffix,
            )
        except Exception:
            self.action_stats["invalid"] += 1

    def _build_ppo_summary(self) -> Dict[str, Any]:
        ppo_state = self.ppo_policy.export_state()
        config = ppo_state.get("config")
        if not isinstance(config, dict):
            config = {}
        last_update = ppo_state.get("last_update")
        if not isinstance(last_update, dict):
            last_update = {}
        flush_stats = ppo_state.get("flush_stats")
        if not isinstance(flush_stats, dict):
            flush_stats = {}
        pending_buffer_raw = ppo_state.get("buffer")
        pending_buffer_size = len(pending_buffer_raw) if isinstance(pending_buffer_raw, list) else 0
        policy_items = float(last_update.get("policy_items", 0.0) or 0.0)
        policy_skipped_items = float(last_update.get("policy_skipped_items", 0.0) or 0.0)
        policy_total_items = policy_items + policy_skipped_items
        flush_calls = float(flush_stats.get("calls", 0.0) or 0.0)
        flush_skipped_updates = float(flush_stats.get("skipped_updates", 0.0) or 0.0)
        flush_skip_ratio = (flush_skipped_updates / flush_calls) if flush_calls > 0.0 else 0.0
        policy_skip_ratio = (policy_skipped_items / policy_total_items) if policy_total_items > 0.0 else 0.0
        return {
            "update_steps": int(ppo_state.get("update_steps", 0) or 0),
            "episodes_since_update": int(ppo_state.get("episodes_since_update", 0) or 0),
            "pending_buffer_size": int(pending_buffer_size),
            "process_reward_mean": float(ppo_state.get("reward_mean", 0.0) or 0.0),
            "topk_entropy_mean": float(ppo_state.get("topk_entropy_mean", 0.0) or 0.0),
            "topk_mass_mean": float(ppo_state.get("topk_mass_mean", 0.0) or 0.0),
            "topk_bin_entropy_mean": float(ppo_state.get("topk_bin_entropy_mean", 0.0) or 0.0),
            "config": {
                "update_every_episodes": int(config.get("update_every_episodes", 1) or 1),
                "advantage_normalize": bool(config.get("advantage_normalize", True)),
                "advantage_clip": float(config.get("advantage_clip", 0.0) or 0.0),
                "target_kl": float(config.get("target_kl", 0.0) or 0.0),
            },
            "flush": {
                "calls": int(flush_calls),
                "force_calls": int(float(flush_stats.get("force_calls", 0.0) or 0.0)),
                "triggered_updates": int(float(flush_stats.get("triggered_updates", 0.0) or 0.0)),
                "skipped_updates": int(flush_skipped_updates),
                "skip_ratio": float(flush_skip_ratio),
            },
            "last_update": {
                "policy_loss": float(last_update.get("policy_loss", 0.0) or 0.0),
                "value_loss": float(last_update.get("value_loss", 0.0) or 0.0),
                "entropy": float(last_update.get("entropy", 0.0) or 0.0),
                "approx_kl": float(last_update.get("approx_kl", 0.0) or 0.0),
                "clip_frac": float(last_update.get("clip_frac", 0.0) or 0.0),
                "explained_variance": float(last_update.get("explained_variance", 0.0) or 0.0),
                "advantage_mean": float(last_update.get("advantage_mean", 0.0) or 0.0),
                "advantage_std": float(last_update.get("advantage_std", 0.0) or 0.0),
                "advantage_clip_frac": float(last_update.get("advantage_clip_frac", 0.0) or 0.0),
                "value_mean": float(last_update.get("value_mean", 0.0) or 0.0),
                "return_mean": float(last_update.get("return_mean", 0.0) or 0.0),
                "policy_items": int(policy_items),
                "policy_skipped_items": int(policy_skipped_items),
                "policy_skip_ratio": float(policy_skip_ratio),
                "n_updates": int(float(last_update.get("n_updates", 0.0) or 0.0)),
                "early_stop": bool(float(last_update.get("early_stop", 0.0) or 0.0) >= 0.5),
            },
        }

    def stats(self) -> Dict[str, Any]:
        tool_items = sum(len(items) for items in self.tool_records.values())
        return {
            "memory_items": len(self.records) + tool_items,
            "taskmem_items": len(self.records),
            "toolmem_items": tool_items,
            "toolmem_tools": sorted(self.tool_records.keys()),
            "max_items": self.max_items,
            "top_k": self.default_top_k,
            "mode": self.mode,
            "thresholds": {
                "retrieve_min_score": self.retrieve_min_score,
                "action_min_score": self.action_min_score,
                "action_min_margin": self.action_min_margin,
                "upsert_match_threshold": self.upsert_match_threshold,
                "update_consistency_threshold": self.update_consistency_threshold,
            },
            "chunking": {
                "mode": self.chunk_mode,
                "chunk_size": self.chunk_size,
                "chunk_overlap": self.chunk_overlap,
                "chunk_max_parts": self.chunk_max_parts,
            },
            "fusion": {
                "mode": self.fusion_mode,
                "tau": self.fusion_tau,
                "query_history_size": self.query_history_size,
                "query_history_len": len(self.query_history_embeddings),
            },
            "actions": dict(self.action_stats),
            "ppo_summary": self._build_ppo_summary(),
            "ppo": self.ppo_policy.export_state(),
        }


class MemSkillLocalMemoryBank(LightweightMemoryBank):
    def __init__(
        self,
        max_items: int = 80,
        default_top_k: int = 3,
        mode: str = "controller",
        storage_path: str = "",
    ):
        super().__init__(max_items=max_items, default_top_k=default_top_k, mode=mode)
        self.storage_path = str(storage_path or "").strip()
        self.load_success = False
        self.last_persist_error = ""
        if self.storage_path:
            self._load_from_file()

    def _serialize(self) -> Dict[str, Any]:
        tool_records_payload: Dict[str, List[Dict[str, Any]]] = {}
        for tool_name, records in self.tool_records.items():
            tool_records_payload[tool_name] = [record.to_dict() for record in records]
        return {
            "version": 2,
            "mode": self.mode,
            "max_items": self.max_items,
            "top_k": self.default_top_k,
            "actions": dict(self.action_stats),
            "ppo_policy": self.ppo_policy.export_state(),
            "records": [record.to_dict() for record in self.records],
            "task_records": [record.to_dict() for record in self.records],
            "tool_records": tool_records_payload,
        }

    def _save_to_file(self) -> None:
        if not self.storage_path:
            return
        try:
            parent = os.path.dirname(self.storage_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            temp_path = self.storage_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(self._serialize(), f, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.storage_path)
            self.last_persist_error = ""
        except Exception as e:
            self.last_persist_error = str(e)

    def _load_from_file(self) -> None:
        if not self.storage_path or not os.path.exists(self.storage_path):
            return
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            records_raw = payload.get("records")
            task_records_raw = payload.get("task_records")
            records_source = task_records_raw if isinstance(task_records_raw, list) else records_raw
            if isinstance(records_source, list):
                loaded: List[MemoryRecord] = []
                for item in records_source:
                    if not isinstance(item, dict):
                        continue
                    record = MemoryRecord.from_dict(item)
                    if record.content:
                        record.memory_type = "task"
                        record.tool_name = ""
                        loaded.append(record)
                self.records = loaded
            tool_records_raw = payload.get("tool_records")
            if isinstance(tool_records_raw, dict):
                loaded_tools: Dict[str, List[MemoryRecord]] = {}
                for raw_tool_name, raw_items in tool_records_raw.items():
                    tool_key = str(raw_tool_name or "").strip().lower()
                    if not tool_key or not isinstance(raw_items, list):
                        continue
                    bucket: List[MemoryRecord] = []
                    for item in raw_items:
                        if not isinstance(item, dict):
                            continue
                        record = MemoryRecord.from_dict(item)
                        if not record.content:
                            continue
                        record.memory_type = "tool"
                        record.tool_name = tool_key
                        bucket.append(record)
                    if bucket:
                        loaded_tools[tool_key] = bucket
                self.tool_records = loaded_tools
            actions_raw = payload.get("actions")
            if isinstance(actions_raw, dict):
                for key in self.action_stats.keys():
                    self.action_stats[key] = int(actions_raw.get(key) or 0)
            ppo_policy_raw = payload.get("ppo_policy")
            if isinstance(ppo_policy_raw, dict):
                self.ppo_policy.load_state(ppo_policy_raw)
            self._trim_overflow()
            self.load_success = True
        except Exception as e:
            self.last_persist_error = str(e)

    def _apply_actions(self, actions: List[Dict[str, Any]], candidate_map: Dict[int, int], source: str) -> bool:
        changed = False
        pending_updates: List[Tuple[int, str]] = []
        pending_deletes: List[int] = []
        pending_inserts: List[str] = []
        for action in actions:
            if not isinstance(action, dict):
                self.action_stats["invalid"] += 1
                continue
            action_type = str(action.get("type", "")).strip().upper()
            if not action_type:
                self.action_stats["invalid"] += 1
                continue
            if action_type == "NOOP":
                self.action_stats["noop"] += 1
                continue
            if action_type == "INSERT":
                content = _memory_normalize_text(str(action.get("content", "")).strip())
                if not content:
                    self.action_stats["invalid"] += 1
                    continue
                pending_inserts.append(content)
                self.action_stats["insert"] += 1
                continue
            if action_type == "UPDATE":
                record_index = self._resolve_memory_index(action.get("memory_index"), candidate_map)
                content = _memory_normalize_text(str(action.get("content", "")).strip())
                if record_index < 0 or not content:
                    self.action_stats["invalid"] += 1
                    continue
                pending_updates.append((record_index, content))
                self.action_stats["update"] += 1
                continue
            if action_type == "DELETE":
                record_index = self._resolve_memory_index(action.get("memory_index"), candidate_map)
                if record_index < 0:
                    self.action_stats["invalid"] += 1
                    continue
                pending_deletes.append(record_index)
                self.action_stats["delete"] += 1
                continue
            self.action_stats["invalid"] += 1

        for record_index, normalized in pending_updates:
            if record_index >= len(self.records):
                self.action_stats["invalid"] += 1
                continue
            record = self.records[record_index]
            record.content_history.append(record.content)
            record.content = normalized
            record.tokens = _memory_tokens(normalized)
            record.retrieval_embedding = _memory_embedding(normalized, profile="retrieval")
            record.update_embedding = _memory_embedding(normalized, profile="update")
            record.embedding = list(record.retrieval_embedding)
            record.source = source
            record.updated_at = time.time()
            record.last_accessed = record.updated_at
            record.access_count += 1
            record.hits += 1
            record.operation_history.append("update")
            changed = True

        applied_deletes: Set[int] = set()
        for record_index in sorted(pending_deletes, reverse=True):
            if record_index in applied_deletes:
                continue
            if record_index < 0 or record_index >= len(self.records):
                self.action_stats["invalid"] += 1
                continue
            self.records.pop(record_index)
            applied_deletes.add(record_index)
            changed = True

        for content in pending_inserts:
            self.upsert(content, source)
            if self.records:
                self.records[-1].operation_history.append("insert")
            changed = True

        self._trim_overflow()
        return changed

    def observe_tool_result(
        self,
        tool_name: str,
        input_obj: Dict[str, Any],
        tool_content: str,
        is_error: bool,
        provider: Optional["DeepSeekProvider"] = None,
        debug_enabled: bool = False,
        log_title_suffix: str = "",
    ) -> None:
        super().observe_tool_result(
            tool_name=tool_name,
            input_obj=input_obj,
            tool_content=tool_content,
            is_error=is_error,
            provider=provider,
            debug_enabled=debug_enabled,
            log_title_suffix=log_title_suffix,
        )
        self._save_to_file()

    def finalize_episode(
        self,
        status: str,
        qa_reward: Optional[float] = None,
        qa_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().finalize_episode(status, qa_reward=qa_reward, qa_meta=qa_meta)
        self._save_to_file()

    def flush_pending_policy_update(self) -> None:
        super().flush_pending_policy_update()
        self._save_to_file()

    def stats(self) -> Dict[str, Any]:
        data = super().stats()
        data["backend"] = "memskill"
        data["storage_path"] = self.storage_path
        data["load_success"] = self.load_success
        if self.last_persist_error:
            data["persist_error"] = self.last_persist_error
        return data


def create_mem_store(
    memory_enabled: bool,
    memory_top_k: int,
    memory_max_items: int,
    memory_mode: str,
    memory_backend: str,
    memory_file: str,
) -> Optional[LightweightMemoryBank]:
    if not memory_enabled:
        return None
    backend = str(memory_backend or "").strip().lower()
    if backend == "memskill":
        return MemSkillLocalMemoryBank(
            max_items=memory_max_items,
            default_top_k=memory_top_k,
            mode=memory_mode,
            storage_path=memory_file,
        )
    return LightweightMemoryBank(max_items=memory_max_items, default_top_k=memory_top_k, mode=memory_mode)


def strip_tool_order_instructions(prompt: str) -> str:
    start = prompt.find("1) 用 ls 查看 contract 目录")
    if start == -1:
        return prompt
    end = prompt.find("最终回复", start)
    if end == -1:
        return prompt[:start].strip()
    return (prompt[:start] + prompt[end:]).strip()


def build_assistant_message(assistant_msg: Message, tool_calls: List[ToolCall]) -> Message:
    record: Message = {"role": "assistant"}
    content = assistant_msg.get("content")
    if content is not None:
        record["content"] = content
    if tool_calls:
        record["tool_calls"] = [
            {
                "id": call.get("id", ""),
                "type": "function",
                "function": {
                    "name": call.get("name", ""),
                    "arguments": call.get("arguments", "{}"),
                },
            }
            for call in tool_calls
        ]
    return record


def response_from_events(events: List[ProviderEvent]) -> ProviderResponse:
    response = None
    content = ""
    tool_calls_by_id: Dict[str, ToolCall] = {}
    usage = TokenUsage()
    for event in events:
        if event.event_type == "content_delta":
            content += event.content
        if event.event_type in {"tool_use_start", "tool_use_delta", "tool_use_stop"} and event.tool_call:
            tc = event.tool_call
            tc_id = tc.get("id", "")
            if not tc_id:
                continue
            current = tool_calls_by_id.get(tc_id, {"id": tc_id, "name": "", "arguments": ""})
            if tc.get("name"):
                current["name"] = tc.get("name")
            if tc.get("arguments"):
                current["arguments"] = tc.get("arguments")
            tool_calls_by_id[tc_id] = current
        if event.event_type == "complete" and event.response:
            response = event.response
            usage = event.response.usage
    if response is not None:
        return response
    tool_calls = list(tool_calls_by_id.values())
    return ProviderResponse({"content": content}, tool_calls, {}, usage)


def save_agent_result(
    output_dir: str,
    prompt: str,
    status: str,
    answer: str,
    usage: TokenUsage,
    model: str,
    error_message: str = "",
    activity: Optional[Dict[str, Any]] = None,
    output_speed: Optional[float] = None,
    qa_result: Optional[Dict[str, Any]] = None,
) -> str:
    os.makedirs(output_dir, exist_ok=True)
    run_file_name = f"agent_result_{time.strftime('%Y%m%d-%H%M%S')}.md"
    run_file_path = os.path.join(output_dir, run_file_name)
    latest_file_path = os.path.join(output_dir, "latest.md")
    lines = [
        f"status: {status}",
        f"model: {model}",
        f"prompt: {prompt}",
        "",
        "answer:",
        answer or "",
    ]
    if usage.total_tokens > 0:
        lines.extend(
            [
                "",
                "token_usage:",
                f"input={usage.input_tokens}",
                f"output={usage.output_tokens}",
                f"total={usage.total_tokens}",
            ]
        )
    if output_speed is not None:
        lines.extend(
            [
                "",
                "output_speed:",
                f"tokens_per_second={output_speed:.2f}",
            ]
        )
    if activity:
        tools = activity.get("tools") if isinstance(activity, dict) else None
        agents = activity.get("agents") if isinstance(activity, dict) else None
        todos = activity.get("todos") if isinstance(activity, dict) else None
        lines.extend(["", "activity:"])
        if isinstance(tools, list):
            lines.append("tools:")
            for item in tools:
                if not isinstance(item, dict):
                    continue
                name = item.get("name") if isinstance(item.get("name"), str) else ""
                status = item.get("status") if isinstance(item.get("status"), str) else ""
                target = item.get("target") if isinstance(item.get("target"), str) else ""
                lines.append(f"- [{status}] {name}{f' ({target})' if target else ''}")
        if isinstance(agents, list):
            lines.append("agents:")
            for item in agents:
                if not isinstance(item, dict):
                    continue
                agent_type = item.get("type") if isinstance(item.get("type"), str) else ""
                status = item.get("status") if isinstance(item.get("status"), str) else ""
                model_name = item.get("model") if isinstance(item.get("model"), str) else ""
                desc = item.get("description") if isinstance(item.get("description"), str) else ""
                suffix = f" [{model_name}]" if model_name else ""
                if desc:
                    lines.append(f"- [{status}] {agent_type}{suffix}: {desc}")
                else:
                    lines.append(f"- [{status}] {agent_type}{suffix}")
        if isinstance(todos, list):
            lines.append("todos:")
            valid_todos = _normalize_todo_items(todos)
            completed = sum(1 for t in valid_todos if t.get("status") == "completed")
            total = len(valid_todos)
            focus = _select_focus_todo(valid_todos, prompt)
            if focus:
                lines.append(f"- [{focus.get('status', '')}] {focus.get('content', '')} ({completed}/{total})")
            elif total > 0 and completed == total:
                lines.append(f"- [completed] All todos complete ({completed}/{total})")
            else:
                goal = prompt.strip()
                if goal:
                    lines.append(f"- [in_progress] {goal} (agent goal, {completed}/{total})")
    activity_export = _build_activity_export(activity, output_speed)
    if activity_export is not None:
        lines.extend(
            [
                "",
                "activity_json:",
                "```json",
                json.dumps(activity_export, ensure_ascii=False, indent=2, sort_keys=True),
                "```",
            ]
        )
    if isinstance(qa_result, dict):
        lines.extend(
            [
                "",
                "qa_result:",
                "```json",
                json.dumps(qa_result, ensure_ascii=False, indent=2, sort_keys=True),
                "```",
            ]
        )
    if error_message:
        lines.extend(["", "error:", error_message])
    text = "\n".join(lines)
    with open(run_file_path, "w", encoding="utf-8") as f:
        f.write(text)
    with open(latest_file_path, "w", encoding="utf-8") as f:
        f.write(text)
    return latest_file_path


def _normalize_eval_text(text: str) -> str:
    s = str(text or "").lower()
    s = re.sub(r"[^\w\u4e00-\u9fff]+", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _compute_token_f1(prediction: str, ground_truth: str) -> float:
    pred = _normalize_eval_text(prediction).split()
    gold = _normalize_eval_text(ground_truth).split()
    if not pred or not gold:
        return 0.0
    pred_counts: Dict[str, int] = {}
    for tok in pred:
        pred_counts[tok] = pred_counts.get(tok, 0) + 1
    overlap = 0
    for tok in gold:
        remain = pred_counts.get(tok, 0)
        if remain > 0:
            overlap += 1
            pred_counts[tok] = remain - 1
    if overlap <= 0:
        return 0.0
    precision = overlap / float(len(pred))
    recall = overlap / float(len(gold))
    if precision + recall <= 1e-12:
        return 0.0
    return float(2.0 * precision * recall / (precision + recall))


def _collect_recent_tool_evidence(messages: List[Message], limit: int = 8, max_chars: int = 3500) -> str:
    chunks: List[str] = []
    for msg in reversed(messages):
        if str(msg.get("role", "")).strip().lower() != "tool":
            continue
        name = str(msg.get("name", "")).strip()
        content = str(msg.get("content", "")).strip()
        if not content:
            continue
        chunks.append(f"[{name}] {content}")
        if len(chunks) >= limit:
            break
    chunks.reverse()
    joined = "\n".join(chunks)
    return _truncate_text(joined, max_chars)


def _evaluate_final_qa(
    provider: DeepSeekProvider,
    question: str,
    prediction: str,
    ground_truth: str,
    evidence: str,
    mode: str,
    hybrid_alpha: float,
) -> Dict[str, Any]:
    metric_mode = str(mode or "hybrid").strip().lower()
    pred_text = str(prediction or "")
    gold_text = str(ground_truth or "")
    exact = 1.0 if _normalize_eval_text(pred_text) == _normalize_eval_text(gold_text) and gold_text else 0.0
    f1 = _compute_token_f1(pred_text, gold_text) if gold_text else 0.0
    evidence_score = 0.0
    if evidence.strip():
        evidence_overlap = _compute_token_f1(pred_text, evidence)
        evidence_score = max(0.0, min(1.0, evidence_overlap))
    llm_judge = 0.0
    if metric_mode in {"llm_judge", "hybrid"}:
        judge_prompt = (
            "你是问答质量评估器。请根据问题、答案、参考信息给出 0 到 1 的分数。"
            "只输出 JSON，格式为 {\"score\": 0.xx, \"reason\": \"...\"}。\n"
            f"问题：{question}\n"
            f"答案：{pred_text}\n"
            f"标准答案：{gold_text}\n"
            f"证据：{evidence}\n"
        )
        try:
            resp = provider.request(
                [{"role": "user", "content": judge_prompt}],
                [],
            )
            raw = str(resp.message.get("content", "") if isinstance(resp.message, dict) else "")
            parsed = None
            try:
                parsed = json.loads(raw)
            except Exception:
                m = re.search(r"\{[\s\S]*\}", raw)
                if m:
                    parsed = json.loads(m.group(0))
            if isinstance(parsed, dict):
                score_raw = parsed.get("score", 0.0)
                if isinstance(score_raw, (int, float)):
                    llm_judge = float(max(0.0, min(1.0, float(score_raw))))
        except Exception:
            llm_judge = 0.0
    score = 0.0
    if metric_mode == "exact_match":
        score = exact
    elif metric_mode == "f1":
        score = f1
    elif metric_mode == "llm_judge":
        score = llm_judge
    else:
        alpha = max(0.0, min(1.0, float(hybrid_alpha)))
        score = alpha * llm_judge + (1.0 - alpha) * max(f1, evidence_score)
    score = float(max(0.0, min(1.0, score)))
    return {
        "metric_mode": metric_mode,
        "score": score,
        "raw_score": score,
        "f1": float(f1),
        "exact_match": float(exact),
        "llm_judge": float(llm_judge),
        "evidence_score": float(evidence_score),
    }


def run_agent(
    client: ToolServerClient,
    prompt: str,
    provider_name: str,
    api_key: str,
    base_url: str,
    model: str,
    model_label: str,
    stream_enabled: bool,
    max_steps: int,
    max_steps_hard_limit: int,
    max_steps_extension: int,
    output_dir: str,
    debug_enabled: bool,
    hud_enabled: bool,
    hud_style: str,
    memory_enabled: bool,
    memory_top_k: int,
    memory_max_items: int,
    memory_mode: str,
    memory_backend: str,
    memory_file: str,
    mem_store_override: Optional["LightweightMemoryBank"] = None,
    qa_ground_truth: str = "",
    result_sink: Optional[Dict[str, Any]] = None,
) -> int:
    tools = client.list_tools()
    tool_schema = build_tool_schema(tools)
    provider = DeepSeekProvider(api_key, base_url, model)
    mem_store = mem_store_override
    if mem_store is None:
        mem_store = create_mem_store(
            memory_enabled=memory_enabled,
            memory_top_k=memory_top_k,
            memory_max_items=memory_max_items,
            memory_mode=memory_mode,
            memory_backend=memory_backend,
            memory_file=memory_file,
        )
    qa_mode = str(os.environ.get("AGENT_QA_MODE", "hybrid")).strip().lower()
    qa_hybrid_alpha = _memory_env_float("AGENT_QA_HYBRID_ALPHA", 0.6, min_value=0.0, max_value=1.0)
    qa_ground_truth_env = str(os.environ.get("AGENT_QA_GROUND_TRUTH", "")).strip()
    qa_ground_truth_value = str(qa_ground_truth or qa_ground_truth_env).strip()
    tool_names = [tool.get("name", "") for tool in tools if tool.get("name")]
    tool_list_text = "，".join(tool_names)
    tool_catalog_text = build_tool_prompt_catalog(tools)
    has_skill_search = any(str(tool.get("name", "")).strip() == "skill_search" for tool in tools)
    has_skill_load = any(str(tool.get("name", "")).strip() == "skill_load" for tool in tools)
    if has_skill_search and has_skill_load:
        skill_tool_text = "skill_search、skill_load（已可用）"
        skill_usage_text = "Skill 使用顺序：先用 skill_search 检索，再用 skill_load 按需加载具体技能正文。"
    elif has_skill_search or has_skill_load:
        available_skill_tools = [name for name in ["skill_search", "skill_load"] if name in tool_names]
        skill_tool_text = "、".join(available_skill_tools) if available_skill_tools else "无"
        skill_usage_text = "注意：Skill 工具部分可用，优先使用可用项；若缺失另一项，改用常规工具完成任务；如果常规工具无法完成任务，请优先使用 skill 工具来完成任务。"
    else:
        skill_tool_text = "无"
        skill_usage_text = "当前无 Skill 工具，直接使用常规工具链完成任务。"
    system_prompt = (
        "你是一个会使用工具的助手。输入可能是任意问题。需要时调用工具。完成后直接给出答案。回复内容尽量使用中文。\n"
        f"【工具名称】{tool_list_text}\n"
        "【工具能力说明】\n"
        f"{tool_catalog_text}\n"
        f"【Skill 工具】{skill_tool_text}\n"
        f"【Skill 使用规则】{skill_usage_text}\n"
        f"【写文件约束】如果任务需要写文件且存在 write 工具，必须使用 write，且只能写入 {output_dir} 目录。"
    )
    messages: List[Message] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]
    _debug_log_block(
        debug_enabled,
        "AGENT 启动配置",
        {
            "prompt": prompt,
            "provider": provider_name,
            "model": model,
            "model_label": model_label,
            "base_url": base_url,
            "stream_enabled": stream_enabled,
            "max_steps": max_steps,
            "max_steps_hard_limit": max_steps_hard_limit,
            "max_steps_extension": max_steps_extension,
            "tool_count": len(tool_schema),
            "tools": [t.get("function", {}).get("name", "") for t in tool_schema],
                "memory_enabled": bool(mem_store is not None),
                "memory_top_k": memory_top_k,
                "memory_max_items": memory_max_items,
                "memory_mode": memory_mode,
                "memory_backend": memory_backend,
                "memory_file": memory_file,
        },
        max_len=12000,
    )

    total_usage = TokenUsage()
    tracker = SessionActivityTracker()
    speed_cache_path = os.path.join(output_dir, ".speed-cache.json")
    latest_output_speed: Optional[float] = None

    def _flush_pending_policy_update() -> None:
        if mem_store is None:
            return
        flush_fn = getattr(mem_store, "flush_pending_policy_update", None)
        if callable(flush_fn):
            flush_fn()

    def _sync_result_sink(status: str, qa_value: Dict[str, Any]) -> None:
        if not isinstance(result_sink, dict):
            return
        result_sink.clear()
        payload: Dict[str, Any] = {"status": status, "qa_result": dict(qa_value)}
        if mem_store is not None:
            stats = mem_store.stats()
            if isinstance(stats, dict):
                ppo_summary = stats.get("ppo_summary")
                if isinstance(ppo_summary, dict):
                    payload["ppo_summary"] = ppo_summary
        result_sink.update(payload)

    hard_limit = max(max_steps, max_steps_hard_limit)
    extension = max(1, max_steps_extension)
    active_step_limit = max_steps
    step_index = 1
    while step_index <= active_step_limit and step_index <= hard_limit:
        _debug_log_block(
            debug_enabled,
            f"STEP {step_index} 开始",
            {
                "message_count": len(messages),
                "last_role": messages[-1].get("role") if messages else "",
                "last_content_preview": _truncate_text(str(messages[-1].get("content", "")) if messages else "", 500),
                "usage_before": {
                    "input_tokens": total_usage.input_tokens,
                    "output_tokens": total_usage.output_tokens,
                    "total_tokens": total_usage.total_tokens,
                },
            },
        )
        llm_messages = messages
        if mem_store is not None:
            query_text = prompt
            if messages:
                query_text += "\n" + str(messages[-1].get("content", ""))
            recalled = mem_store.retrieve(query_text, top_k=memory_top_k)
            if recalled:
                memory_lines = ["可参考的历史记忆（按相关度）："]
                for i, memory_item in enumerate(recalled, start=1):
                    memory_lines.append(f"{i}. {memory_item}")
                memory_lines.append("仅在相关时使用这些记忆，不要臆造。")
                llm_messages = messages + [{"role": "system", "content": "\n".join(memory_lines)}]
                _debug_log_block(
                    debug_enabled,
                    f"STEP {step_index} 记忆检索",
                    {"recalled_count": len(recalled), "recalled": recalled, "stats": mem_store.stats()},
                    max_len=5000,
                )
        _debug_log_block(debug_enabled, f"LLM 输入 (step {step_index})", llm_messages, max_len=12000)
        try:
            if stream_enabled:
                events = provider.stream(llm_messages, tool_schema)
                _debug_log_block(
                    debug_enabled,
                    f"LLM 流事件 (step {step_index})",
                    [
                        {
                            "event_type": e.event_type,
                            "content": _truncate_text(e.content or "", 200),
                            "tool_call": e.tool_call,
                            "error": str(e.error) if e.error else "",
                        }
                        for e in events
                    ],
                    max_len=12000,
                )
                response = response_from_events(events)
            else:
                response = provider.request(llm_messages, tool_schema)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            qa_fail = {"metric_mode": qa_mode, "score": 0.0, "raw_score": 0.0}
            if mem_store is not None:
                mem_store.finalize_episode("error", qa_reward=-0.2, qa_meta={"metric_mode": qa_mode, "score": 0.0})
            _flush_pending_policy_update()
            _sync_result_sink("error", qa_fail)
            result_file = save_agent_result(
                output_dir=output_dir,
                prompt=prompt,
                status="error",
                answer="",
                usage=total_usage,
                model=model_label,
                error_message=f"LLM request failed: {e.code} {e.reason}\n{body}",
                activity=tracker.snapshot(),
                output_speed=latest_output_speed,
            )
            print(f"RESULT_FILE: {result_file}", file=sys.stderr)
            return 1
        except Exception as e:
            qa_fail = {"metric_mode": qa_mode, "score": 0.0, "raw_score": 0.0}
            if mem_store is not None:
                mem_store.finalize_episode("error", qa_reward=-0.2, qa_meta={"metric_mode": qa_mode, "score": 0.0})
            _flush_pending_policy_update()
            _sync_result_sink("error", qa_fail)
            result_file = save_agent_result(
                output_dir=output_dir,
                prompt=prompt,
                status="error",
                answer="",
                usage=total_usage,
                model=model_label,
                error_message=f"LLM request failed: {e}",
                activity=tracker.snapshot(),
                output_speed=latest_output_speed,
            )
            print(f"RESULT_FILE: {result_file}", file=sys.stderr)
            return 1

        _debug_log_block(
            debug_enabled,
            f"LLM 回复 (step {step_index})",
            {"message": response.message, "tool_calls": response.tool_calls, "raw": response.raw},
            max_len=12000,
        )
        messages.append(build_assistant_message(response.message, response.tool_calls))

        total_usage.add(response.usage)
        speed = get_output_speed(total_usage.output_tokens, speed_cache_path)
        if speed is not None:
            latest_output_speed = speed
        activity = tracker.snapshot()
        _debug_log_block(
            debug_enabled,
            f"STEP {step_index} 统计",
            {
                "step_usage": {
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "total_tokens": response.usage.total_tokens,
                },
                "usage_total": {
                    "input_tokens": total_usage.input_tokens,
                    "output_tokens": total_usage.output_tokens,
                    "total_tokens": total_usage.total_tokens,
                },
                "output_speed_tokens_per_second": latest_output_speed,
                "activity": activity,
            },
            max_len=12000,
        )
        if hud_enabled:
            emit_activity_hud(activity, latest_output_speed, step_index, hud_style, prompt)

        if not response.tool_calls:
            content = response.message.get("content") or ""
            evidence = _collect_recent_tool_evidence(messages)
            qa_result = _evaluate_final_qa(
                provider=provider,
                question=prompt,
                prediction=content,
                ground_truth=qa_ground_truth_value,
                evidence=evidence,
                mode=qa_mode,
                hybrid_alpha=qa_hybrid_alpha,
            )
            qa_reward = float(qa_result.get("score", 0.0))
            if mem_store is not None and content:
                mem_store.upsert(f"[assistant_final] {content}", source="assistant:final")
            if mem_store is not None:
                mem_store.finalize_episode("success", qa_reward=qa_reward, qa_meta=qa_result)
            _flush_pending_policy_update()
            _sync_result_sink("success", qa_result)
            _debug_log_block(
                debug_enabled,
                f"STEP {step_index} 终止",
                {
                    "reason": "no_tool_calls",
                    "final_answer_preview": _truncate_text(content, 1200),
                    "qa_result": qa_result,
                },
                max_len=2000,
            )
            result_file = save_agent_result(
                output_dir=output_dir,
                prompt=prompt,
                status="success",
                answer=content,
                usage=total_usage,
                model=model_label,
                activity=activity,
                output_speed=latest_output_speed,
                qa_result=qa_result,
            )
            print(f"RESULT_FILE: {result_file}", file=sys.stderr)
            return 0
        if step_index >= active_step_limit and active_step_limit < hard_limit:
            active_step_limit = min(hard_limit, active_step_limit + extension)

        for call in response.tool_calls:
            call_id = call.get("id", "")
            name = call.get("name", "")
            arguments = call.get("arguments", "{}")
            args_obj = normalize_arguments(arguments)
            _debug_log_block(
                debug_enabled,
                f"TOOL 调用开始 (step {step_index})",
                {"tool_call_id": call_id, "name": name, "arguments_raw": arguments, "arguments_obj": args_obj},
                max_len=10000,
            )
            tracker.start_tool(call_id, name, args_obj, time.time())

            try:
                result = client.call_tool(name, args_obj)
                if isinstance(result, dict):
                    tool_content = result.get("content", "")
                    is_error = bool(result.get("is_error"))
                    tracker.finish_tool(call_id, is_error, time.time())
                else:
                    tool_content = str(result)
                    is_error = False
                    tracker.finish_tool(call_id, False, time.time())
                _debug_log_block(
                    debug_enabled,
                    f"TOOL 调用完成 (step {step_index})",
                    {
                        "tool_call_id": call_id,
                        "name": name,
                        "is_error": is_error,
                        "result_preview": _truncate_text(tool_content, 2500),
                        "result_raw": result,
                    },
                    max_len=12000,
                )
                if mem_store is not None:
                    mem_store.observe_tool_result(
                        name,
                        args_obj,
                        tool_content,
                        is_error,
                        provider=provider,
                        debug_enabled=debug_enabled,
                        log_title_suffix=f" (step {step_index}, tool {name})",
                    )
            except Exception as e:
                tool_content = f"tool call failed: {e}"
                tracker.finish_tool(call_id, True, time.time())
                _debug_log_block(
                    debug_enabled,
                    f"TOOL 调用异常 (step {step_index})",
                    {"tool_call_id": call_id, "name": name, "error": str(e)},
                    max_len=3000,
                )

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": tool_content,
                }
            )
            _debug_log_block(
                debug_enabled,
                f"TOOL 结果入历史 (step {step_index})",
                {
                    "tool_call_id": call_id,
                    "name": name,
                    "tool_message_preview": _truncate_text(tool_content, 1200),
                    "message_count_after_append": len(messages),
                },
                max_len=3000,
            )
        step_index += 1

    if mem_store is not None:
        mem_store.finalize_episode("max_steps_reached", qa_reward=-0.2, qa_meta={"metric_mode": qa_mode, "score": 0.0})
    _flush_pending_policy_update()
    _sync_result_sink("max_steps_reached", {"metric_mode": qa_mode, "score": 0.0, "raw_score": 0.0})
    result_file = save_agent_result(
        output_dir=output_dir,
        prompt=prompt,
        status="max_steps_reached",
        answer="",
        usage=total_usage,
        model=model_label,
        error_message="Max steps reached without a final answer.",
        activity=tracker.snapshot(),
        output_speed=latest_output_speed,
    )
    print(f"RESULT_FILE: {result_file}", file=sys.stderr)
    return 1


def _load_training_items(prompt: str, prompts_file: str) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    path = str(prompts_file or "").strip()
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            parsed = json.loads(content)
            if isinstance(parsed, list):
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    p = str(item.get("prompt", "")).strip()
                    gt = str(item.get("ground_truth", "")).strip()
                    if p:
                        items.append({"prompt": p, "ground_truth": gt})
        except Exception:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for raw in f:
                        line = raw.strip()
                        if not line:
                            continue
                        items.append({"prompt": line, "ground_truth": ""})
            except Exception:
                items = []
    if not items:
        items = [{"prompt": prompt, "ground_truth": ""}]
    return items


def run_agent_training(
    client: ToolServerClient,
    prompt: str,
    provider_name: str,
    api_key: str,
    base_url: str,
    model: str,
    model_label: str,
    stream_enabled: bool,
    max_steps: int,
    max_steps_hard_limit: int,
    max_steps_extension: int,
    output_dir: str,
    debug_enabled: bool,
    hud_enabled: bool,
    hud_style: str,
    memory_enabled: bool,
    memory_top_k: int,
    memory_max_items: int,
    memory_mode: str,
    memory_backend: str,
    memory_file: str,
    train_output_dir: str,
) -> int:
    outer_epochs = _memory_env_int("AGENT_TRAIN_OUTER_EPOCHS", 1, min_value=1, max_value=1000)
    inner_epochs = _memory_env_int("AGENT_TRAIN_INNER_EPOCHS", 1, min_value=1, max_value=1000)
    batch_size = _memory_env_int("AGENT_TRAIN_BATCH_SIZE", 1, min_value=1, max_value=1000)
    prompts_file_raw = os.environ.get("AGENT_TRAIN_PROMPTS_FILE", "")
    prompts_file = _resolve_path(prompts_file_raw, os.getcwd()) if str(prompts_file_raw).strip() else ""
    items = _load_training_items(prompt, prompts_file)
    mem_store = create_mem_store(
        memory_enabled=memory_enabled,
        memory_top_k=memory_top_k,
        memory_max_items=memory_max_items,
        memory_mode=memory_mode,
        memory_backend=memory_backend,
        memory_file=memory_file,
    )
    total_runs = outer_epochs * inner_epochs * batch_size
    run_index = 0
    success_count = 0
    epoch_logs: List[Dict[str, Any]] = []
    wandb_logs: List[Dict[str, Any]] = []
    for outer_idx in range(outer_epochs):
        for inner_idx in range(inner_epochs):
            inner_scores: List[float] = []
            inner_raw_scores: List[float] = []
            inner_status: Dict[str, int] = {"success": 0, "error": 0, "max_steps_reached": 0}
            inner_last_ppo: Dict[str, Any] = {}
            ppo_policy_losses: List[float] = []
            ppo_value_losses: List[float] = []
            ppo_entropies: List[float] = []
            ppo_kls: List[float] = []
            ppo_clip_fracs: List[float] = []
            ppo_explained_vars: List[float] = []
            ppo_adv_means: List[float] = []
            ppo_value_means: List[float] = []
            ppo_return_means: List[float] = []
            ppo_process_reward_means: List[float] = []
            for batch_idx in range(batch_size):
                run_index += 1
                item = items[(run_index - 1) % len(items)]
                episode_prompt = str(item.get("prompt", prompt))
                episode_gt = str(item.get("ground_truth", ""))
                episode_result: Dict[str, Any] = {}
                print(
                    f"[TRAIN] outer={outer_idx + 1}/{outer_epochs} inner={inner_idx + 1}/{inner_epochs} batch={batch_idx + 1}/{batch_size} run={run_index}/{total_runs}",
                    file=sys.stderr,
                )
                code = run_agent(
                    client=client,
                    prompt=episode_prompt,
                    provider_name=provider_name,
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    model_label=model_label,
                    stream_enabled=stream_enabled,
                    max_steps=max_steps,
                    max_steps_hard_limit=max_steps_hard_limit,
                    max_steps_extension=max_steps_extension,
                    output_dir=output_dir,
                    debug_enabled=debug_enabled,
                    hud_enabled=hud_enabled,
                    hud_style=hud_style,
                    memory_enabled=memory_enabled,
                    memory_top_k=memory_top_k,
                    memory_max_items=memory_max_items,
                    memory_mode=memory_mode,
                    memory_backend=memory_backend,
                    memory_file=memory_file,
                    mem_store_override=mem_store,
                    qa_ground_truth=episode_gt,
                    result_sink=episode_result,
                )
                status_name = str(episode_result.get("status", "error")).strip().lower()
                if status_name not in inner_status:
                    status_name = "error"
                inner_status[status_name] = inner_status.get(status_name, 0) + 1
                qa_result = episode_result.get("qa_result")
                if isinstance(qa_result, dict):
                    score_raw = qa_result.get("score", 0.0)
                    raw_score_raw = qa_result.get("raw_score", score_raw)
                    if isinstance(score_raw, (int, float)):
                        inner_scores.append(float(score_raw))
                    if isinstance(raw_score_raw, (int, float)):
                        inner_raw_scores.append(float(raw_score_raw))
                ppo_summary = episode_result.get("ppo_summary")
                if isinstance(ppo_summary, dict):
                    inner_last_ppo = ppo_summary
                    pr_mean = ppo_summary.get("process_reward_mean")
                    if isinstance(pr_mean, (int, float)):
                        ppo_process_reward_means.append(float(pr_mean))
                    last = ppo_summary.get("last_update")
                    if isinstance(last, dict):
                        if isinstance(last.get("policy_loss"), (int, float)):
                            ppo_policy_losses.append(float(last.get("policy_loss")))
                        if isinstance(last.get("value_loss"), (int, float)):
                            ppo_value_losses.append(float(last.get("value_loss")))
                        if isinstance(last.get("entropy"), (int, float)):
                            ppo_entropies.append(float(last.get("entropy")))
                        if isinstance(last.get("approx_kl"), (int, float)):
                            ppo_kls.append(float(last.get("approx_kl")))
                        if isinstance(last.get("clip_frac"), (int, float)):
                            ppo_clip_fracs.append(float(last.get("clip_frac")))
                        if isinstance(last.get("explained_variance"), (int, float)):
                            ppo_explained_vars.append(float(last.get("explained_variance")))
                        if isinstance(last.get("advantage_mean"), (int, float)):
                            ppo_adv_means.append(float(last.get("advantage_mean")))
                        if isinstance(last.get("value_mean"), (int, float)):
                            ppo_value_means.append(float(last.get("value_mean")))
                        if isinstance(last.get("return_mean"), (int, float)):
                            ppo_return_means.append(float(last.get("return_mean")))
                if code == 0:
                    success_count += 1
            avg_reward = sum(inner_scores) / float(len(inner_scores)) if inner_scores else 0.0
            avg_raw = sum(inner_raw_scores) / float(len(inner_raw_scores)) if inner_raw_scores else avg_reward
            avg_ppo_policy_loss = sum(ppo_policy_losses) / float(len(ppo_policy_losses)) if ppo_policy_losses else 0.0
            avg_ppo_value_loss = sum(ppo_value_losses) / float(len(ppo_value_losses)) if ppo_value_losses else 0.0
            avg_ppo_entropy = sum(ppo_entropies) / float(len(ppo_entropies)) if ppo_entropies else 0.0
            avg_ppo_kl = sum(ppo_kls) / float(len(ppo_kls)) if ppo_kls else 0.0
            avg_ppo_clip_frac = sum(ppo_clip_fracs) / float(len(ppo_clip_fracs)) if ppo_clip_fracs else 0.0
            avg_ppo_explained_var = sum(ppo_explained_vars) / float(len(ppo_explained_vars)) if ppo_explained_vars else 0.0
            avg_ppo_adv_mean = sum(ppo_adv_means) / float(len(ppo_adv_means)) if ppo_adv_means else 0.0
            avg_ppo_value_mean = sum(ppo_value_means) / float(len(ppo_value_means)) if ppo_value_means else 0.0
            avg_ppo_return_mean = sum(ppo_return_means) / float(len(ppo_return_means)) if ppo_return_means else 0.0
            avg_ppo_process_reward_mean = (
                sum(ppo_process_reward_means) / float(len(ppo_process_reward_means)) if ppo_process_reward_means else 0.0
            )
            last_update = inner_last_ppo.get("last_update") if isinstance(inner_last_ppo, dict) else {}
            if not isinstance(last_update, dict):
                last_update = {}
            epoch_log = {
                "outer_epoch": int(outer_idx + 1),
                "inner_epoch": int(inner_idx + 1),
                "reward": float(avg_reward),
                "raw_performance": float(avg_raw),
                "qa_performance": float(avg_reward),
                "status": dict(inner_status),
                "ppo": {
                    "process_reward_mean": float(avg_ppo_process_reward_mean),
                    "policy_loss": float(avg_ppo_policy_loss),
                    "value_loss": float(avg_ppo_value_loss),
                    "entropy": float(avg_ppo_entropy),
                    "approx_kl": float(avg_ppo_kl),
                    "clip_frac": float(avg_ppo_clip_frac),
                    "explained_variance": float(avg_ppo_explained_var),
                    "advantage_mean": float(avg_ppo_adv_mean),
                    "value_mean": float(avg_ppo_value_mean),
                    "return_mean": float(avg_ppo_return_mean),
                    "n_updates": int(float(last_update.get("n_updates", 0.0) or 0.0)),
                },
            }
            wandb_log = {
                "outer_epoch": int(outer_idx + 1),
                "inner_epoch": int(inner_idx + 1),
                "qa_performance": float(avg_reward),
                "raw_performance": float(avg_raw),
                "process_reward_mean": float(avg_ppo_process_reward_mean),
                "ppo/policy_loss": float(avg_ppo_policy_loss),
                "ppo/value_loss": float(avg_ppo_value_loss),
                "ppo/entropy": float(avg_ppo_entropy),
                "ppo/approx_kl": float(avg_ppo_kl),
                "ppo/clip_frac": float(avg_ppo_clip_frac),
                "ppo/explained_variance": float(avg_ppo_explained_var),
                "ppo/advantage_mean": float(avg_ppo_adv_mean),
                "ppo/value_mean": float(avg_ppo_value_mean),
                "ppo/return_mean": float(avg_ppo_return_mean),
                "ppo/n_updates": int(float(last_update.get("n_updates", 0.0) or 0.0)),
            }
            epoch_logs.append(epoch_log)
            wandb_logs.append(wandb_log)
            print(
                (
                    f"[TRAIN][EPOCH] outer={outer_idx + 1}/{outer_epochs} inner={inner_idx + 1}/{inner_epochs} "
                    f"qa_performance={avg_reward:.4f} raw_performance={avg_raw:.4f} "
                    f"success={inner_status.get('success', 0)}/{batch_size} "
                    f"process_reward_mean={avg_ppo_process_reward_mean:.6f} "
                    f"ppo_policy_loss={avg_ppo_policy_loss:.6f} "
                    f"ppo_value_loss={avg_ppo_value_loss:.6f} "
                    f"ppo_entropy={avg_ppo_entropy:.6f} "
                    f"ppo_kl={avg_ppo_kl:.6f} "
                    f"ppo_clip_frac={avg_ppo_clip_frac:.6f} "
                    f"ppo_explained_var={avg_ppo_explained_var:.6f} "
                    f"ppo_updates={int(float(last_update.get('n_updates', 0.0) or 0.0))}"
                ),
                file=sys.stderr,
            )
    aggregate_keys = [
        "qa_performance",
        "raw_performance",
        "process_reward_mean",
        "ppo/policy_loss",
        "ppo/value_loss",
        "ppo/entropy",
        "ppo/approx_kl",
        "ppo/clip_frac",
        "ppo/explained_variance",
        "ppo/advantage_mean",
        "ppo/value_mean",
        "ppo/return_mean",
        "ppo/n_updates",
    ]
    aggregate: Dict[str, Dict[str, float]] = {}
    for key in aggregate_keys:
        values: List[float] = []
        for row in wandb_logs:
            raw = row.get(key)
            if isinstance(raw, (int, float)):
                values.append(float(raw))
        if not values:
            continue
        mean_value = sum(values) / float(len(values))
        var_value = sum((v - mean_value) * (v - mean_value) for v in values) / float(len(values))
        aggregate[key] = {
            "count": float(len(values)),
            "mean": float(mean_value),
            "std": float(math.sqrt(max(0.0, var_value))),
            "min": float(min(values)),
            "max": float(max(values)),
        }
    train_summary = {
        "total_runs": int(total_runs),
        "success_runs": int(success_count),
        "success_rate": float(success_count / float(max(total_runs, 1))),
        "epochs": epoch_logs,
        "wandb_logs": wandb_logs,
        "last_wandb_log": wandb_logs[-1] if wandb_logs else {},
        "aggregate": aggregate,
    }
    os.makedirs(train_output_dir, exist_ok=True)
    train_file_name = f"agent_train_{time.strftime('%Y%m%d-%H%M%S')}.json"
    train_file_path = os.path.join(train_output_dir, train_file_name)
    train_latest_path = os.path.join(train_output_dir, "agent_train_latest.json")
    train_text = json.dumps(train_summary, ensure_ascii=False, indent=2, sort_keys=True)
    with open(train_file_path, "w", encoding="utf-8") as f:
        f.write(train_text)
    with open(train_latest_path, "w", encoding="utf-8") as f:
        f.write(train_text)
    print(f"TRAIN_RESULT_FILE: {train_latest_path}", file=sys.stderr)
    print(f"[TRAIN] finished total_runs={total_runs} success={success_count}", file=sys.stderr)
    return 0 if success_count > 0 else 1


def main() -> int:
    script_default_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    runtime_root = _resolve_path(os.environ.get("AGENT_RUNTIME_ROOT", ""), script_default_root)
    def _same_path(a: str, b: str) -> bool:
        return os.path.normcase(os.path.normpath(os.path.abspath(a))) == os.path.normcase(os.path.normpath(os.path.abspath(b)))

    workspace_raw = str(os.environ.get("AGENT_WORKSPACE_ROOT", "")).strip()
    workspace_root = _resolve_path(workspace_raw, os.path.join(runtime_root, "workdir"))
    if workspace_raw and _same_path(workspace_root, runtime_root):
        workspace_root = os.path.abspath(os.path.join(runtime_root, "workdir"))
    output_raw = str(os.environ.get("AGENT_OUTPUT_DIR", "")).strip()
    output_dir = _resolve_path(output_raw, workspace_root)
    legacy_output_dir = os.path.abspath(os.path.join(runtime_root, "output"))
    if output_raw and _same_path(output_dir, legacy_output_dir):
        output_dir = workspace_root
    train_output_raw = str(os.environ.get("AGENT_TRAIN_OUTPUT_DIR", "")).strip()
    train_output_dir = _resolve_path(train_output_raw, os.path.join(runtime_root, "train"))
    if _same_path(train_output_dir, workspace_root):
        train_output_dir = os.path.abspath(os.path.join(runtime_root, "train"))
    if not os.path.isdir(runtime_root):
        print(f"runtime root not found: {runtime_root}", file=sys.stderr)
        return 2
    os.makedirs(workspace_root, exist_ok=True)
    load_env_file(os.path.join(runtime_root, ".env"))

    if len(sys.argv) > 1:
        prompt = " ".join(sys.argv[1:])
    else:
        prompt = os.environ.get("AGENT_QUESTION", "").strip()
        if not prompt and not sys.stdin.isatty():
            prompt = sys.stdin.read().strip()
        if not prompt:
            prompt = "请回答用户提出的问题。"
    prompt = strip_tool_order_instructions(prompt)

    stream_enabled = os.environ.get("AGENT_STREAM", "").strip().lower() in {"1", "true", "yes"}
    debug_enabled = os.environ.get("AGENT_DEBUG", "").strip().lower() in {"1", "true", "yes"}
    max_steps_str = os.environ.get("AGENT_MAX_STEPS", "8").strip()
    try:
        max_steps = int(max_steps_str)
    except Exception:
        max_steps = 8
    max_steps_hard_limit_str = os.environ.get("AGENT_MAX_STEPS_HARD_LIMIT", str(max_steps)).strip()
    max_steps_extension_str = os.environ.get("AGENT_MAX_STEPS_EXTENSION", "8").strip()
    try:
        max_steps_hard_limit = int(max_steps_hard_limit_str)
    except Exception:
        max_steps_hard_limit = max_steps
    try:
        max_steps_extension = int(max_steps_extension_str)
    except Exception:
        max_steps_extension = 8
    if max_steps_hard_limit < max_steps:
        max_steps_hard_limit = max_steps
    if max_steps_extension <= 0:
        max_steps_extension = 8
    memory_enabled = os.environ.get("AGENT_MEMORY", "1").strip().lower() not in {"0", "false", "no"}
    memory_top_k_str = os.environ.get("AGENT_MEMORY_TOP_K", "3").strip()
    memory_max_items_str = os.environ.get("AGENT_MEMORY_MAX_ITEMS", "80").strip()
    try:
        memory_top_k = int(memory_top_k_str)
    except Exception:
        memory_top_k = 3
    try:
        memory_max_items = int(memory_max_items_str)
    except Exception:
        memory_max_items = 80
    memory_mode = str(os.environ.get("AGENT_MEMORY_MODE", "controller")).strip().lower()
    if memory_mode not in {"controller"}:
        memory_mode = "controller"
    memory_backend = os.environ.get("AGENT_MEMORY_BACKEND", "memskill").strip().lower()
    if memory_backend not in {"lightweight", "memskill"}:
        memory_backend = "memskill"
    memory_file_raw = os.environ.get("AGENT_MEMORY_FILE", "")
    if str(memory_file_raw or "").strip():
        memory_file_raw_text = str(memory_file_raw).strip()
        if os.path.isabs(memory_file_raw_text):
            memory_file = os.path.abspath(memory_file_raw_text)
        else:
            workspace_name = os.path.basename(os.path.normpath(workspace_root)).strip().lower()
            normalized_raw = memory_file_raw_text.replace("\\", "/").lstrip("./")
            if workspace_name and normalized_raw.lower().startswith(workspace_name + "/"):
                memory_file = _resolve_path(memory_file_raw_text, runtime_root)
            else:
                memory_file = _resolve_path(memory_file_raw_text, workspace_root)
    else:
        memory_file = os.path.abspath(os.path.join(workspace_root, ".agent-memory.json"))

    if os.name == "nt":
        toolserver_candidates = [os.path.join(runtime_root, "toolserver.exe"), os.path.join(runtime_root, "toolserver")]
    else:
        toolserver_candidates = [os.path.join(runtime_root, "toolserver")]

    toolserver = next((p for p in toolserver_candidates if os.path.exists(p)), None)
    if not toolserver:
        print("toolserver binary not found. Build it first:", file=sys.stderr)
        if os.name == "nt":
            print(f"  cd {runtime_root} && go build -o toolserver.exe ./cmd/toolserver", file=sys.stderr)
        else:
            print(f"  cd {runtime_root} && go build -o toolserver ./cmd/toolserver", file=sys.stderr)
        return 2

    env = dict(os.environ)
    env["TOOLSERVER_ROOT"] = workspace_root

    proc = subprocess.Popen(
        [toolserver],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
        env=env,
        cwd=workspace_root,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None

    client = ToolServerClient(proc, debug_enabled=debug_enabled)
    try:
        provider_name = normalize_provider_name(os.environ.get("LLM_PROVIDER", "deepseek"))
        train_mode = os.environ.get("AGENT_TRAIN_MODE", "").strip().lower() in {"1", "true", "yes"}
        try:
            provider_config = resolve_provider_config(provider_name)
            hud_enabled = os.environ.get("AGENT_HUD", "").strip().lower() not in {"0", "false", "no"}
            hud_style = os.environ.get("AGENT_HUD_STYLE", "full")
            if train_mode:
                return run_agent_training(
                    client,
                    prompt,
                    provider_config["provider"],
                    provider_config["api_key"],
                    provider_config["base_url"],
                    provider_config["model"],
                    provider_config["model_label"],
                    stream_enabled,
                    max_steps,
                    max_steps_hard_limit,
                    max_steps_extension,
                    output_dir,
                    debug_enabled,
                    hud_enabled,
                    hud_style,
                    memory_enabled,
                    memory_top_k,
                    memory_max_items,
                    memory_mode,
                    memory_backend,
                    memory_file,
                    train_output_dir,
                )
            return run_agent(
                client,
                prompt,
                provider_config["provider"],
                provider_config["api_key"],
                provider_config["base_url"],
                provider_config["model"],
                provider_config["model_label"],
                stream_enabled,
                max_steps,
                max_steps_hard_limit,
                max_steps_extension,
                output_dir,
                debug_enabled,
                hud_enabled,
                hud_style,
                memory_enabled,
                memory_top_k,
                memory_max_items,
                memory_mode,
                memory_backend,
                memory_file,
            )
        except ValueError as e:
            result_file = save_agent_result(
                output_dir=output_dir,
                prompt=prompt,
                status="error",
                answer="",
                usage=TokenUsage(),
                model="",
                error_message=str(e),
            )
            print(f"RESULT_FILE: {result_file}", file=sys.stderr)
            return 2
    finally:
        try:
            proc.kill()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

