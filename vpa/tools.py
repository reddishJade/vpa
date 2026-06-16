import contextlib
import re
import subprocess
from pathlib import Path

from openai.types.chat import ChatCompletionFunctionToolParam

from . import ledger as L

# ── Command validation ─────────────────────────────────────────────────────

ALLOWED_GIT_SUBCOMMANDS = {
    "diff", "diff-tree", "log", "show", "blame", "status", "branch",
    "rev-parse", "rev-list", "merge-base", "range-diff", "tag",
    "describe", "ls-tree", "cat-file", "for-each-ref", "stash",
}

BLACKLIST_PATTERNS = [
    r"\bgit\s+(reset|clean|checkout|restore\b(?!\s+--staged)|rebase)\b",
    r"\brm\s+(-rf?\s+)?",
    r"--force\b",
    r"\b-f\b",
    r">\s*/dev/",
    r"\bmv\b",
    r"\bdd\b",
]


def _validate_command(cmd):
    for pattern in BLACKLIST_PATTERNS:
        if re.search(pattern, cmd):
            return False, f"command blocked by pattern: {pattern}"
    if re.match(r"^git\s+", cmd):
        parts = cmd.split()
        if len(parts) >= 2:
            subcmd = parts[1]
            if subcmd not in ALLOWED_GIT_SUBCOMMANDS:
                return False, f"git subcommand '{subcmd}' not in allowlist"
    return True, "ok"


# ── OpenAI tool definitions ────────────────────────────────────────────────

TOOL_DEFINITIONS: list[ChatCompletionFunctionToolParam] = [
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": (
                "Run a read-only shell command in the local repo. "
                "Allowed: git (diff/log/show/blame/status only), grep, find, build/test commands. "
                "Blocked: git reset/clean/checkout/restore, rm, mv, --force. "
                "Returns {stdout, stderr, exit_code}."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {
                        "type": "string",
                        "description": "Shell command to execute in the local repo directory",
                    }
                },
                "required": ["cmd"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a file from the local repository. "
                "Use offset/limit to read specific line ranges. "
                "Returns {content, path, total_lines, lines_read}."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to local repo root",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "First line to read (1-based, default 1)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max lines to read (default all)",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_symbol",
            "description": (
                "Search for a symbol definition or reference across repos. "
                "Returns structured results with file, line, kind, and single-line context."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Symbol name to search for, e.g. 'gen_intermediate_code'",
                    },
                    "repo": {
                        "type": "string",
                        "enum": ["upstream", "local", "both"],
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["definition", "reference", "all"],
                    },
                    "file_filter": {
                        "type": "string",
                        "description": "Optional glob to limit scope, e.g. 'src/*.c'",
                    },
                },
                "required": ["symbol", "repo", "kind"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Edit a file by exact string replacement. "
                "ALWAYS call with dry_run=true FIRST. "
                "After editing, verify with run_bash('git diff -- <path>'). "
                "Returns {matched: bool, count: int}."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to local repo root",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Exact text to replace",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Replacement text",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "true = check match only, false = execute replacement",
                    },
                },
                "required": ["path", "old_string", "new_string", "dry_run"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create a NEW file in the local repo. Fails if already exists."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to local repo root",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full file content",
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_intent",
            "description": (
                "Record the upstream commit's intent BEFORE making any edits. "
                "Call this FIRST after reading the upstream diff. "
                "Describe what problem the upstream commit solves, in one sentence."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "commit_sha": {
                        "type": "string",
                        "description": "Full commit SHA",
                    },
                    "intent_summary": {
                        "type": "string",
                        "description": (
                            "One-sentence summary of what the upstream commit "
                            "intends to accomplish"
                        ),
                    },
                },
                "required": ["commit_sha", "intent_summary"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_work_item",
            "description": (
                "Begin working on a work item. Must be called before append_decision "
                "or complete_work_item for that item."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "commit_sha": {
                        "type": "string",
                        "description": "Full commit SHA",
                    },
                    "work_item_id": {
                        "type": "string",
                        "description": "Work item ID, e.g. 'abc123:src/foo.c:0'",
                    },
                },
                "required": ["commit_sha", "work_item_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_decision",
            "description": (
                "Record your porting decision for a work item. This is append-only — "
                "previous decisions are preserved. Provide confidence level, reasoning, "
                "and verifiable evidence (file + line number + snippet)."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "commit_sha": {"type": "string", "description": "Full commit SHA"},
                    "work_item_id": {
                        "type": "string",
                        "description": "Work item ID",
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": "Confidence in this judgment",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why this porting decision was made",
                    },
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file": {"type": "string"},
                                "line": {"type": "integer"},
                                "snippet": {"type": "string"},
                            },
                            "required": ["file", "line", "snippet"],
                            "additionalProperties": False,
                        },
                        "description": "Verifiable evidence: file + line number + code snippet",
                    },
                },
                "required": ["commit_sha", "work_item_id", "confidence", "reason"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_work_item",
            "description": (
                "Mark a work item as complete with a final status. "
                "Valid statuses: ported, skipped, needs_human, blocked. "
                "For ported items, specify method (direct_patch or semantic_port)."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "commit_sha": {"type": "string", "description": "Full commit SHA"},
                    "work_item_id": {
                        "type": "string",
                        "description": "Work item ID",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["ported", "skipped", "needs_human", "blocked"],
                        "description": "Final status for this work item",
                    },
                    "method": {
                        "type": "string",
                        "enum": ["direct_patch", "semantic_port"],
                        "description": "Required when status=ported",
                    },
                },
                "required": ["commit_sha", "work_item_id", "status"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_work_item",
            "description": (
                "Create a synthetic work item for local-only adaptations that have "
                "no corresponding upstream hunk (e.g. compatibility shims)."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "commit_sha": {"type": "string", "description": "Full commit SHA"},
                    "kind": {
                        "type": "string",
                        "const": "synthetic",
                        "description": "Must be 'synthetic'",
                    },
                    "upstream_file": {
                        "type": "string",
                        "description": "Related upstream file (or empty string)",
                    },
                    "description": {
                        "type": "string",
                        "description": "What this synthetic item does and why it's needed",
                    },
                    "local_file": {
                        "type": "string",
                        "description": "Local file that will be created/modified",
                    },
                },
                "required": ["commit_sha", "kind", "description"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_human",
            "description": (
                "Request human intervention. Provide the specific work item and reason."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "commit_sha": {"type": "string", "description": "Full commit SHA"},
                    "work_item_id": {
                        "type": "string",
                        "description": "Work item ID needing human help",
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "Specific conflict location and why human judgment is needed"
                        ),
                    },
                },
                "required": ["commit_sha", "work_item_id", "reason"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "signal_done",
            "description": (
                "Signal that the current processing unit is complete. "
                "All work items for the given commit must be completed "
                "before calling this."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "commit_sha": {
                        "type": "string",
                        "description": "Full commit SHA to validate completion for",
                    },
                },
                "required": ["commit_sha"],
                "additionalProperties": False,
            },
        },
    },
]


# ── Tool handler ───────────────────────────────────────────────────────────

class ToolHandler:
    def __init__(self, local_repo, upstream_repo, ledger, ledger_path):
        self.local_repo = Path(local_repo).resolve()
        self.upstream_repo = Path(upstream_repo).resolve() if upstream_repo else None
        self.ledger = ledger
        self.ledger_path = ledger_path
        self._edit_counts = {}
        self._intent_recorded = set()
        self._dry_run_verified: set[tuple[str, str]] = set()

    def _resolve_safe_path(self, path):
        """Resolve path and verify it stays inside local_repo."""
        full = (self.local_repo / path).resolve()
        if not str(full).startswith(str(self.local_repo) + "/") and full != self.local_repo:
            raise ValueError(f"path escapes repo: {path}")
        return full

    def dispatch(self, name, args):
        method = getattr(self, f"_tool_{name}", None)
        if method is None:
            return {"error": f"unknown tool: {name}"}
        try:
            return method(**args)
        except Exception as e:
            return {"error": str(e)}

    # ── Read/inspect tools ─────────────────────────────────────────────

    def _tool_run_bash(self, cmd):
        ok, reason = _validate_command(cmd)
        if not ok:
            return {"blocked": True, "reason": reason}
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            cwd=self.local_repo,
            timeout=60,
        )
        return {
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-2000:],
            "exit_code": result.returncode,
            "truncated_stdout": len(result.stdout) > 4000,
            "truncated_stderr": len(result.stderr) > 2000,
        }

    def _tool_read_file(self, path, offset=None, limit=None):
        try:
            full_path = self._resolve_safe_path(path)
        except ValueError as e:
            return {"error": str(e)}
        if not full_path.exists():
            return {"error": f"file not found: {path}"}
        with open(full_path) as f:
            lines = f.readlines()
        total = len(lines)
        start = max(0, min((offset - 1) if offset else 0, total - 1))
        end = min(start + limit, total) if limit else total
        content = "".join(lines[start:end])
        return {
            "path": path,
            "total_lines": total,
            "lines_read": f"{start + 1}-{end}",
            "content": content,
        }

    def _tool_search_symbol(self, symbol, repo, kind, file_filter=None):
        results = []
        max_results = 50
        repos = []
        if repo in ("local", "both"):
            repos.append(("local", self.local_repo))
        if repo in ("upstream", "both") and self.upstream_repo:
            repos.append(("upstream", self.upstream_repo))

        for repo_name, repo_path in repos:
            scope = file_filter if file_filter else "."
            try:
                grep_result = subprocess.run(
                    ["grep", "-rn", "--include", scope, symbol, "."],
                    capture_output=True,
                    text=True,
                    cwd=repo_path,
                    timeout=30,
                )
            except subprocess.TimeoutExpired:
                continue

            for line in grep_result.stdout.strip().split("\n"):
                if not line or len(results) >= max_results:
                    break
                parts = line.split(":", 2)
                if len(parts) < 3:
                    continue
                fp, ln, ctx = parts[0], parts[1], parts[2].strip()
                is_def = bool(
                    re.search(
                        r"\b(def|fn|func|function|class|struct|enum|typedef|#define|macro)\b",
                        ctx,
                    )
                )
                k = "definition" if is_def else "reference"
                if kind != "all" and k != kind:
                    continue
                results.append(
                    {
                        "repo": repo_name,
                        "file": fp,
                        "line": int(ln),
                        "kind": k,
                        "context": ctx[:200],
                    }
                )

        return {
            "symbol": symbol,
            "repo": repo,
            "results": results[:max_results],
            "truncated": len(results) >= max_results,
        }

    # ── Edit tools ──────────────────────────────────────────────────────

    def _tool_edit_file(self, path, old_string, new_string, dry_run):
        try:
            full_path = self._resolve_safe_path(path)
        except ValueError as e:
            return {"error": str(e)}
        if not full_path.exists():
            return {"error": f"file not found: {path} | hint: use write_file for new files"}

        with open(full_path) as f:
            content = f.read()

        count = content.count(old_string)
        if count == 0:
            return {"matched": False, "count": 0}

        if dry_run:
            self._dry_run_verified.add((path, old_string))
            return {"matched": True, "count": count, "dry_run": True}

        # Require prior dry_run verification for this (path, old_string)
        if (path, old_string) not in self._dry_run_verified:
            return {
                "error": (
                    "edit_file with dry_run=false requires a prior successful "
                    "dry_run=true call for the same (path, old_string)."
                )
            }

        ecount = self._edit_counts.get(path, 0)
        if ecount >= 5:
            return {
                "error": f"edit limit (5) exceeded for {path}. Call request_human instead."
            }

        new_content = content.replace(old_string, new_string, 1)
        with open(full_path, "w") as f:
            f.write(new_content)

        self._edit_counts[path] = ecount + 1
        return {"matched": True, "count": count, "dry_run": False, "replaced": 1}

    def _tool_write_file(self, path, content):
        try:
            full_path = self._resolve_safe_path(path)
        except ValueError as e:
            return {"error": str(e)}
        if full_path.exists():
            return {"error": f"file already exists: {path}. Use edit_file to modify."}
        full_path.parent.mkdir(parents=True, exist_ok=True)
        with open(full_path, "w") as f:
            f.write(content)
        return {"path": path, "created": True}

    # ── Ledger tools ────────────────────────────────────────────────────

    def _tool_record_intent(self, commit_sha, intent_summary):
        L.record_intent_summary(self.ledger, commit_sha, intent_summary)
        L.write_ledger(self.ledger_path, self.ledger)
        self._intent_recorded.add(commit_sha)
        return {"recorded": True, "commit_sha": commit_sha}

    def _tool_start_work_item(self, commit_sha, work_item_id):
        L.start_work_item(self.ledger, commit_sha, work_item_id)
        L.write_ledger(self.ledger_path, self.ledger)
        return {"started": True, "work_item_id": work_item_id}

    def _tool_append_decision(self, commit_sha, work_item_id, confidence, reason, evidence=None):
        L.append_decision(
            self.ledger, commit_sha, work_item_id, confidence, reason, evidence
        )
        L.write_ledger(self.ledger_path, self.ledger)
        return {"appended": True, "work_item_id": work_item_id}

    def _tool_complete_work_item(self, commit_sha, work_item_id, status, method=None):
        L.complete_work_item(
            self.ledger, commit_sha, work_item_id, status, method=method
        )
        L.write_ledger(self.ledger_path, self.ledger)
        return {"completed": True, "work_item_id": work_item_id, "status": status}

    def _tool_create_work_item(
        self, commit_sha, kind, description, upstream_file=None, local_file=None
    ):
        if kind != "synthetic":
            return {"error": "only synthetic work items can be created by agent"}
        wi = L.create_work_item(
            self.ledger, commit_sha, kind, upstream_file or "", description, local_file
        )
        L.write_ledger(self.ledger_path, self.ledger)
        return {"created": True, "work_item_id": wi["id"]}

    def _tool_request_human(self, commit_sha, work_item_id, reason):
        with contextlib.suppress(ValueError):
            L.append_decision(
                self.ledger, commit_sha, work_item_id, "low", reason,
            )
        with contextlib.suppress(ValueError):
            L.complete_work_item(self.ledger, commit_sha, work_item_id, "needs_human")
        L.write_ledger(self.ledger_path, self.ledger)
        return {"manual_required": True, "commit_sha": commit_sha, "work_item_id": work_item_id}

    def _tool_signal_done(self, commit_sha):
        entry = self.ledger.get("commits", {}).get(commit_sha)
        if entry:
            terminal = {
                "ported", "skipped", "blocked", "needs_human",
                "validation_failed", "final_manual",
            }
            for wi in entry.get("work_items", []):
                if wi["status"] not in terminal:
                    return {
                        "done": False,
                        "error": (
                            f"Work item {wi['id']} is not in terminal state "
                            f"({wi['status']})"
                        ),
                    }
        return {"done": True}
