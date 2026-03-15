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
from typing import Any, Dict, List, Optional


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
    if error_message:
        lines.extend(["", "error:", error_message])
    text = "\n".join(lines)
    with open(run_file_path, "w", encoding="utf-8") as f:
        f.write(text)
    with open(latest_file_path, "w", encoding="utf-8") as f:
        f.write(text)
    return latest_file_path


def run_agent(
    client: ToolServerClient,
    prompt: str,
    api_key: str,
    base_url: str,
    model: str,
    stream_enabled: bool,
    max_steps: int,
    output_dir: str,
    debug_enabled: bool,
    hud_enabled: bool,
    hud_style: str,
) -> int:
    tools = client.list_tools()
    tool_schema = build_tool_schema(tools)
    provider = DeepSeekProvider(api_key, base_url, model)
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
            "model": model,
            "base_url": base_url,
            "stream_enabled": stream_enabled,
            "max_steps": max_steps,
            "tool_count": len(tool_schema),
            "tools": [t.get("function", {}).get("name", "") for t in tool_schema],
        },
        max_len=12000,
    )

    total_usage = TokenUsage()
    tracker = SessionActivityTracker()
    speed_cache_path = os.path.join(output_dir, ".speed-cache.json")
    latest_output_speed: Optional[float] = None

    for step_index in range(1, max_steps + 1):
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
        _debug_log_block(debug_enabled, f"LLM 输入 (step {step_index})", messages, max_len=12000)
        try:
            if stream_enabled:
                events = provider.stream(messages, tool_schema)
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
                response = provider.request(messages, tool_schema)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            result_file = save_agent_result(
                output_dir=output_dir,
                prompt=prompt,
                status="error",
                answer="",
                usage=total_usage,
                model=model,
                error_message=f"LLM request failed: {e.code} {e.reason}\n{body}",
                activity=tracker.snapshot(),
                output_speed=latest_output_speed,
            )
            print(f"RESULT_FILE: {result_file}", file=sys.stderr)
            return 1
        except Exception as e:
            result_file = save_agent_result(
                output_dir=output_dir,
                prompt=prompt,
                status="error",
                answer="",
                usage=total_usage,
                model=model,
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
            _debug_log_block(
                debug_enabled,
                f"STEP {step_index} 终止",
                {"reason": "no_tool_calls", "final_answer_preview": _truncate_text(content, 1200)},
                max_len=2000,
            )
            result_file = save_agent_result(
                output_dir=output_dir,
                prompt=prompt,
                status="success",
                answer=content,
                usage=total_usage,
                model=model,
                activity=activity,
                output_speed=latest_output_speed,
            )
            print(f"RESULT_FILE: {result_file}", file=sys.stderr)
            return 0

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

    result_file = save_agent_result(
        output_dir=output_dir,
        prompt=prompt,
        status="max_steps_reached",
        answer="",
        usage=total_usage,
        model=model,
        error_message="Max steps reached without a final answer.",
        activity=tracker.snapshot(),
        output_speed=latest_output_speed,
    )
    print(f"RESULT_FILE: {result_file}", file=sys.stderr)
    return 1


def main() -> int:
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    load_env_file(os.path.join(repo_root, ".env"))
    output_dir = os.path.join(repo_root, "output")

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

    if os.name == "nt":
        toolserver_candidates = [os.path.join(repo_root, "toolserver.exe"), os.path.join(repo_root, "toolserver")]
    else:
        toolserver_candidates = [os.path.join(repo_root, "toolserver")]

    toolserver = next((p for p in toolserver_candidates if os.path.exists(p)), None)
    if not toolserver:
        print("toolserver binary not found. Build it first:", file=sys.stderr)
        if os.name == "nt":
            print("  go build -o toolserver.exe ./cmd/toolserver", file=sys.stderr)
        else:
            print("  go build -o toolserver ./cmd/toolserver", file=sys.stderr)
        return 2

    env = dict(os.environ)
    env["TOOLSERVER_ROOT"] = repo_root

    proc = subprocess.Popen(
        [toolserver],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
        env=env,
        cwd=repo_root,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None

    client = ToolServerClient(proc, debug_enabled=debug_enabled)
    try:
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            result_file = save_agent_result(
                output_dir=output_dir,
                prompt=prompt,
                status="error",
                answer="",
                usage=TokenUsage(),
                model="",
                error_message="DEEPSEEK_API_KEY is required.",
            )
            print(f"RESULT_FILE: {result_file}", file=sys.stderr)
            return 2

        model = os.environ.get("DEEPSEEK_MODEL", DEEPSEEK_DEFAULT_MODEL).strip() or DEEPSEEK_DEFAULT_MODEL
        base_url = os.environ.get("DEEPSEEK_BASE_URL", "").strip() or DEEPSEEK_DEFAULT_BASE_URL
        try:
            return run_agent(
                client,
                prompt,
                api_key,
                base_url,
                model,
                stream_enabled,
                max_steps,
                output_dir,
                debug_enabled,
                os.environ.get("AGENT_HUD", "").strip().lower() not in {"0", "false", "no"},
                os.environ.get("AGENT_HUD_STYLE", "full"),
            )
        except ValueError as e:
            result_file = save_agent_result(
                output_dir=output_dir,
                prompt=prompt,
                status="error",
                answer="",
                usage=TokenUsage(),
                model=model,
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

