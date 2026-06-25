"""LLM-backed repair and semantic-port boundary.

The orchestrator decides when this engine may run. This module only builds the
minimal context and asks an injected client for a target-side patch.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, cast

from vpa.orchestrator.models import (
    AgentLoopResult,
    ChangeAnalysis,
    ChangeKind,
    CommitContext,
    FailureCode,
    FileMapping,
    GateDecision,
    GateDecisionKind,
    MappingStatus,
    MergeConflictResolution,
    SemanticPortContext,
    SemanticPortResult,
    TargetFileContext,
)


class SemanticPortClient(Protocol):
    def semantic_port(self, context: SemanticPortContext) -> str | None:
        """Return a unified diff patch for target files, or None."""


@dataclass(frozen=True)
class OpenAICompatibleConfig:
    model: str
    api_key: str | None = None
    base_url: str | None = None
    max_completion_tokens: int = 1000000


class OpenAICompatibleSemanticPortClient:
    def __init__(self, config: OpenAICompatibleConfig):
        self.config = config

    def __call__(self, prompt: str) -> str | None:
        return self._chat(prompt, system=None)

    def semantic_port(self, context: SemanticPortContext) -> str | None:
        return self._chat(
            _semantic_port_user_prompt(
                context,
                max_chars=self.config.max_completion_tokens,
            ),
            system=_semantic_port_system_prompt(),
        )

    def _chat(self, user_prompt: str, system: str | None) -> str | None:
        from openai import OpenAI

        client = OpenAI(api_key=self.config.api_key, base_url=self.config.base_url)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_prompt})
        response = client.chat.completions.create(
            model=self.config.model,
            max_completion_tokens=self.config.max_completion_tokens,
            messages=messages,
        )
        if not response.choices:
            return None
        return response.choices[0].message.content


class RepairEngine:
    def __init__(
        self,
        llm_client: (
            SemanticPortClient | Callable[[SemanticPortContext], str | None] | None
        ) = None,
        model_name: str = "gpt-4o",
    ):
        self.llm_client = llm_client
        self.model_name = model_name

    def semantic_port(
        self,
        context: CommitContext,
        analysis: ChangeAnalysis,
        gate_decision: GateDecision,
        local_repo: Path,
    ) -> SemanticPortResult:
        semantic_context = build_semantic_port_context(
            context,
            analysis,
            gate_decision,
            local_repo,
        )
        if self.llm_client is None:
            return SemanticPortResult(
                patch_text=None,
                context=semantic_context,
                llm_used=False,
            )

        patch_text = self._call_client(semantic_context)
        if not patch_text or not patch_text.strip():
            return SemanticPortResult(
                patch_text=None,
                context=semantic_context,
                llm_used=True,
            )
        return SemanticPortResult(
            patch_text=patch_text,
            context=semantic_context,
            llm_used=True,
        )

    def _call_client(self, context: SemanticPortContext) -> str | None:
        if hasattr(self.llm_client, "semantic_port"):
            return cast(SemanticPortClient, self.llm_client).semantic_port(context)
        return cast(Callable[[SemanticPortContext], str | None], self.llm_client)(context)

    def resolve_merge_conflicts(
        self,
        conflict_files: list[Path],
    ) -> MergeConflictResolution:
        resolved: list[Path] = []
        failed: list[Path] = []

        for file_path in conflict_files:
            try:
                content = file_path.read_text(encoding="utf-8")
            except Exception:
                failed.append(file_path)
                continue

            sections = _parse_conflict_markers(content)
            if not sections:
                resolved.append(file_path)
                continue

            if self.llm_client is None:
                failed.append(file_path)
                continue

            prompt = _build_conflict_prompt(file_path, content, sections)
            resolved_text = self._call_llm_text(prompt)

            if not resolved_text or not resolved_text.strip():
                failed.append(file_path)
                continue

            file_path.write_text(resolved_text.strip(), encoding="utf-8")
            resolved.append(file_path)

        return MergeConflictResolution(
            resolved_files=resolved,
            failed_files=failed,
        )

    def _call_llm_text(self, prompt: str) -> str | None:
        if self.llm_client is None:
            return None
        return cast(Callable, self.llm_client)(prompt)

    def isa_translate(
        self,
        context: CommitContext,
        analysis: ChangeAnalysis,
        gate_decision: GateDecision,
        local_repo: Path,
    ) -> AgentLoopResult:
        if self.llm_client is None:
            return AgentLoopResult(
                success=False,
                failure_code=FailureCode.NO_LLM_CONFIGURED,
                status_reason="No LLM client configured",
            )
        semantic_context = build_semantic_port_context(
            context, analysis, gate_decision, local_repo,
        )
        messages = [
            {"role": "system", "content": _system_prompt("translate")},
            {
                "role": "user",
                "content": _semantic_port_user_prompt(semantic_context, 1000000),
            },
        ]
        return self._run_tool_loop("translate", messages=messages, workspace=local_repo)

    def agent_loop(
        self,
        op: str,
        file_path: Path | None = None,
        context: CommitContext | None = None,
    ) -> AgentLoopResult:
        if self.llm_client is None:
            return AgentLoopResult(
                success=False,
                failure_code=FailureCode.NO_LLM_CONFIGURED,
                status_reason="No LLM client configured",
            )
        return self._run_tool_loop(op, file_path, context)

    def _run_tool_loop(
        self,
        op: str,
        file_path: Path | None = None,
        context: CommitContext | None = None,
        max_retries: int = 3,
        messages: list[dict[str, Any]] | None = None,
        workspace: Path = Path("."),
    ) -> AgentLoopResult:
        tools = _tools_for_op(op)
        if messages is None:
            messages = [
                {"role": "system", "content": _system_prompt(op)},
                {"role": "user", "content": _user_content(op, file_path, context)},
            ]
        debug_log = Path("logs/agent_loop_debug.jsonl")
        retry_count = 0
        iteration = 0
        while retry_count < max_retries:
            response = _llm_call(self._llm_chat, self.model_name, messages, tools)
            _log_llm_debug(debug_log, op, iteration, messages, response)
            iteration += 1
            if response is None:
                return AgentLoopResult(
                    success=False,
                    failure_code=FailureCode.LLM_ERROR,
                    status_reason="LLM returned no response",
                )
            msg = response["choices"][0]["message"]
            if msg.get("tool_calls"):
                msg_copy = dict(msg)
                if msg_copy.get("content") is None:
                    msg_copy["content"] = ""
                messages.append(msg_copy)
                for call in msg["tool_calls"]:
                    args = json.loads(call["function"]["arguments"])
                    result = _execute_tool(call["function"]["name"], args, op)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(result),
                    })
                continue
            integrity_error = _check_integrity(op, msg, workspace)
            if integrity_error is None:
                patched = _collect_patched(op, msg, workspace)
                return AgentLoopResult(success=True, patched_files=patched)
            retry_count += 1
            messages.append({
                "role": "user",
                "content": integrity_error,
            })
        return AgentLoopResult(
            success=False,
            failure_code=FailureCode.MAX_RETRIES,
            status_reason=f"Agent loop exceeded max retries: {retry_count} integrity failures",
        )

    @property
    def _llm_chat(self) -> Callable:
        return cast(Callable, self.llm_client)


def _log_llm_debug(
    log_path: Path, op: str, attempt: int,
    request_messages: list[dict[str, Any]], response: dict[str, Any] | None,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry: dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "op": op,
        "attempt": attempt,
    }
    if response is not None:
        msg = response.get("choices", [{}])[0].get("message", {})
        entry["response"] = {
            "role": msg.get("role"),
            "content": msg.get("content"),
            "tool_calls": msg.get("tool_calls"),
        }
    else:
        entry["response"] = None
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _llm_call(
    chat_fn: Callable,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> dict[str, Any] | None:
    from openai import OpenAI

    client = OpenAI()
    response = client.chat.completions.create(
        model=model,
        messages=messages,  # type: ignore[arg-type]
        tools=tools if tools else None,  # type: ignore[arg-type]
    )
    if not response.choices:
        return None
    choice = response.choices[0]
    tcs = []
    for tc in (choice.message.tool_calls or []):
        tcs.append({
            "id": tc.id,
            "type": "function",
            "function": {"name": tc.function.name, "arguments": tc.function.arguments},  # type: ignore[attr-defined]
        })
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": choice.message.content,
                "tool_calls": tcs,
            }
        }]
    }


def _tools_for_op(op: str) -> list[dict[str, Any]]:
    shared = [_tool_read(), _tool_grep(), _tool_bash()]
    if op == "resolve":
        return shared + [_tool_write()]
    if op == "translate":
        return shared + [_tool_apply_patch()]
    return shared


def _tool_read() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read file content, optionally with line range",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "line_range": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                },
                "required": ["path"],
            },
        },
    }


def _tool_grep() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search for a pattern in a file",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["pattern", "path"],
            },
        },
    }


def _tool_bash() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a read-only shell command",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string"},
                },
                "required": ["cmd"],
            },
        },
    }


def _tool_write() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Write content to a file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    }


def _tool_apply_patch() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "Apply a structured diff to a file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "patch_text": {"type": "string"},
                },
                "required": ["path", "patch_text"],
            },
        },
    }


_READONLY_BASH_COMMANDS = {"git show", "git log", "git diff"}


def _apply_changeset(text: str, repo_root: Path = Path(".")) -> list[Path]:
    data = json.loads(text)
    changes = data.get("changes", [])
    patched: list[Path] = []
    for change in changes:
        op = change.get("op", "modify")
        path = (repo_root / change["path"]).resolve()
        if op == "create":
            content = change.get("content", "")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            patched.append(path)
        elif op in {"modify", "replace"}:
            edits = change.get("edits", [])
            if edits:
                original = path.read_text(encoding="utf-8")
                for edit in edits:
                    old = edit["old"]
                    new = edit["new"]
                    idx = original.find(old)
                    if idx == -1:
                        raise ValueError(f"Anchor not found in {path}")
                    if original.find(old, idx + len(old)) != -1:
                        raise ValueError(f"Multiple anchor matches in {path}")
                    original = original[:idx] + new + original[idx + len(old):]
                path.write_text(original, encoding="utf-8")
                patched.append(path)
    return patched


_CONFLICT_MARKER_RE = re.compile(r"<<<<<<< |=======|>>>>>>>")


def _validate_changeset(text: str, repo_root: Path) -> str | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return f"ChangeSet is not valid JSON: {e}"
    changes = data.get("changes", [])
    if not changes:
        return "ChangeSet has no changes."
    for change in changes:
        path = (repo_root / change["path"]).resolve()
        if not path.exists():
            return f"Target file not found: {path}"
        edits = change.get("edits", [])
        if not edits:
            continue
        original = path.read_text(encoding="utf-8")
        for edit in edits:
            old = edit.get("old", "")
            if original.find(old) == -1:
                return f"Anchor not found in {path}"
            if original.find(old, original.find(old) + len(old)) != -1:
                return f"Multiple anchor matches in {path}"
    return None


def _check_integrity(
    op: str, msg: dict[str, Any], workspace: Path,
) -> str | None:
    if op == "translate":
        content = msg.get("content") or ""
        if not content.strip():
            return "LLM returned empty response. Provide a ChangeSet with the translation."
        return _validate_changeset(content, workspace)
    if op == "resolve":
        content = msg.get("content") or ""
        if content.strip() and _CONFLICT_MARKER_RE.search(content):
            return "Resolved content still contains conflict markers."
        return None
    return None


def _collect_patched(
    op: str, msg: dict[str, Any], workspace: Path,
) -> list[Path]:
    if op == "translate":
        content = msg.get("content") or ""
        return _apply_changeset(content, workspace)
    return []


def _execute_tool(name: str, args: dict[str, Any], op: str) -> dict[str, Any]:
    if name == "read":
        return _tool_handler_read(args)
    if name == "grep":
        return _tool_handler_grep(args)
    if name == "bash":
        cmd = args.get("cmd", "")
        if not any(cmd.startswith(prefix) for prefix in _READONLY_BASH_COMMANDS):
            return {"error": f"Command not in whitelist: {cmd}"}
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return {"stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode}
    if name == "write" and op == "resolve":
        _tool_handler_write(args)
        return {"success": True}
    if name == "apply_patch" and op == "translate":
        _tool_handler_apply_patch(args)
        return {"success": True}
    return {"error": f"Unknown tool or operation mismatch: {name}"}


def _tool_handler_read(args: dict[str, Any]) -> dict[str, Any]:
    path = Path(args["path"])
    if not path.exists():
        return {"error": f"File not found: {path}"}
    if path.is_dir():
        return {"error": f"Path is a directory, not a file: {path}"}
    content = path.read_text(encoding="utf-8")
    line_range = args.get("line_range")
    if line_range:
        start, end = line_range
        lines = content.splitlines()
        content = "\n".join(lines[start - 1:end])
    return {"content": content, "size": len(content)}


def _tool_handler_grep(args: dict[str, Any]) -> dict[str, Any]:
    path = Path(args["path"])
    if not path.exists():
        return {"error": f"File not found: {path}"}
    if path.is_dir():
        return {"error": f"Path is a directory, not a file: {path}"}
    matches: list[dict[str, Any]] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if re.search(args["pattern"], line):
            matches.append({"line": i, "text": line})
    return {"matches": matches, "count": len(matches)}


def _tool_handler_write(args: dict[str, Any]) -> None:
    Path(args["path"]).write_text(args["content"], encoding="utf-8")


def _tool_handler_apply_patch(args: dict[str, Any]) -> None:
    # Parse unified diff, verify anchors, apply in memory, write
    path = Path(args["path"])
    patch_text = args["patch_text"]
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    hunks = _parse_unified_diff(patch_text)
    for hunk in hunks:
        old_section = "".join(hunk["old_lines"])
        start = hunk["old_start"] - 1
        end = start + hunk["old_count"]
        actual = "".join(lines[start:end])
        if actual != old_section:
            raise ValueError(f"Anchor mismatch at {path}:{hunk['old_start']}")
        lines[start:end] = hunk["new_lines"]
    path.write_text("".join(lines), encoding="utf-8")


def _parse_unified_diff(patch_text: str) -> list[dict[str, Any]]:
    hunks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in patch_text.splitlines():
        m = re.match(r"^@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@", line)
        if m:
            current = {
                "old_start": int(m.group(1)),
                "old_count": int(m.group(2)) if m.group(2) else 1,
                "new_start": int(m.group(3)),
                "new_count": int(m.group(4)) if m.group(4) else 1,
                "old_lines": [],
                "new_lines": [],
            }
            hunks.append(current)
        elif current:
            if line.startswith("-"):
                current["old_lines"].append(line[1:] + "\n")
            elif line.startswith("+"):
                current["new_lines"].append(line[1:] + "\n")
            else:
                current["old_lines"].append(line + "\n")
                current["new_lines"].append(line + "\n")
    return hunks


def _system_prompt(op: str) -> str:
    if op == "resolve":
        return (
            "You are VPA's merge conflict resolver. Given a file with git merge "
            "conflict markers, decide which side to keep or how to combine them. "
            "Use the read tool to inspect the file, then write the resolved content."
        )
    if op == "translate":
        return (
            "You are VPA's ISA translation engine. Port the reference ISA change "
            "to the target ISA file(s).\n\n"
            "Rules:\n"
            "1. FIRST use the read tool to inspect target files. Do NOT guess their content.\n"
            "2. Use grep to search for relevant symbols or patterns.\n"
            "3. Use git show/log/diff (via bash tool) to inspect commit history if needed.\n"
            "4. When you have enough context, respond with a JSON ChangeSet.\n"
            "5. Each edit's \"old\" string MUST be copied exactly from the file you read.\n"
            "6. Do NOT include surrounding context that isn't in the file.\n"
            "7. Do NOT use bash to search the filesystem. Use read/grep instead.\n\n"
            "ChangeSet format:\n"
            '{"changes": [{"op": "modify", "path": "src/dynarec/sw64_core3/foo.c", '
            '"edits": [{"old": "exact text from file", "new": "replacement text"}]}]}'
        )
    return "You are VPA's agent."


def _user_content(op: str, file_path: Path | None, context: CommitContext | None) -> str:
    if op == "resolve" and file_path:
        content = file_path.read_text(encoding="utf-8") if file_path.exists() else "<missing>"
        return f"Resolve conflicts in {file_path}:\n\n{content}"
    if op == "translate" and context:
        ctx = build_semantic_port_context(
            context,
            ChangeAnalysis(
                kind=ChangeKind.UNKNOWN, confidence=0.0, signals=[],
                changed_symbols=[], mapped_target_candidates=[],
                suggested_gate=GateDecisionKind.NEEDS_SEMANTIC_PORT,
            ),
            GateDecision(kind=GateDecisionKind.NEEDS_SEMANTIC_PORT, reasons=[]),
            Path("."),
        )
        return _semantic_port_user_prompt(ctx, 1000000)
    return ""


def build_semantic_port_context(
    context: CommitContext,
    analysis: ChangeAnalysis,
    gate_decision: GateDecision,
    local_repo: Path,
) -> SemanticPortContext:
    reference_patches = {
        file_diff.path: file_diff.raw_patch
        for file_diff in context.diff_context.files
        if file_diff.path is not None
        and _mapped_file(context.isa_mapping.mapping_for(file_diff.path))
    }
    target_files = _target_file_contexts(context, local_repo)
    return SemanticPortContext(
        commit=context.commit,
        reference_patches=reference_patches,
        target_files=target_files,
        analysis=analysis,
        gate_reasons=gate_decision.reasons,
    )


def _target_file_contexts(context: CommitContext, local_repo: Path) -> list[TargetFileContext]:
    seen: set[Path] = set()
    target_files: list[TargetFileContext] = []
    for mapping in context.isa_mapping.file_mappings:
        if mapping.status != MappingStatus.MAPPED:
            continue
        for target in mapping.target_candidates:
            if target in seen:
                continue
            seen.add(target)
            full_path = local_repo / target
            content = full_path.read_text(encoding="utf-8") if full_path.exists() else None
            target_files.append(TargetFileContext(path=target, content=content))
    return target_files


def _mapped_file(mapping: FileMapping | None) -> bool:
    return mapping is not None and mapping.status == MappingStatus.MAPPED


def _parse_conflict_markers(content: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    lines = content.splitlines()
    ours: list[str] = []
    theirs: list[str] = []
    in_conflict = False
    in_theirs = False

    for line in lines:
        if line.startswith("<<<<<<<"):
            in_conflict = True
            in_theirs = False
            ours = []
            theirs = []
        elif line.startswith("======="):
            in_theirs = True
        elif line.startswith(">>>>>>>"):
            if in_conflict:
                sections.append(("\n".join(ours), "\n".join(theirs)))
            in_conflict = False
            in_theirs = False
        elif in_conflict:
            if in_theirs:
                theirs.append(line)
            else:
                ours.append(line)

    return sections


def _build_conflict_prompt(
    file_path: Path, content: str, sections: list[tuple[str, str]]
) -> str:
    section_texts: list[str] = []
    for i, (ours, theirs) in enumerate(sections, 1):
        block = (
            f"Conflict block {i}:\n<<<<<<< ours\n{ours}\n"
            f"=======\n{theirs}\n>>>>>>> theirs"
        )
        section_texts.append(block)
    return (
        f"Resolve the git merge conflicts in {file_path.as_posix()}.\n"
        "Keep both sides' changes when they are compatible. "
        "When only one side is correct, choose the right one.\n"
        "Return ONLY the resolved file content, no explanations, "
        "no markdown fences.\n\n"
        + "\n\n".join(section_texts)
    )


def _semantic_port_system_prompt() -> str:
    return (
        "You are VPA's semantic porting engine. Port the reference ISA change "
        "to the target ISA file(s). Return only a unified diff patch that "
        "applies to the target repository. Do not include markdown fences, "
        "explanations, or changes to reference files. If no safe patch can be "
        "produced, return an empty response."
    )


def _semantic_port_user_prompt(context: SemanticPortContext, max_chars: int) -> str:
    parts = [
        f"Commit: {context.commit.sha}",
        f"Subject: {context.commit.subject}",
        f"Analysis kind: {context.analysis.kind}",
        f"Analysis confidence: {context.analysis.confidence:.2f}",
        "Gate reasons:",
        *[f"- {reason}" for reason in context.gate_reasons],
        "",
        "Reference patches:",
    ]
    for path, patch in context.reference_patches.items():
        parts.extend([f"--- reference: {path.as_posix()} ---", patch])
    parts.append("")
    parts.append("Target file contents:")
    for target in context.target_files:
        parts.append(f"--- target: {target.path.as_posix()} ---")
        parts.append(target.content if target.content is not None else "<missing target file>")
    prompt = "\n".join(parts)
    if len(prompt) <= max_chars:
        return prompt
    return prompt[:max_chars] + "\n\n[truncated]"
