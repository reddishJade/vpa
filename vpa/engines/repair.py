"""LLM-backed repair and semantic-port boundary.

The orchestrator decides when this engine may run. This module only builds the
minimal context and asks an injected client for a target-side patch.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from vpa.orchestrator.models import (
    ChangeAnalysis,
    CommitContext,
    FileMapping,
    GateDecision,
    MappingStatus,
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
    temperature: float = 0.0
    max_context_chars: int = 60_000


class OpenAICompatibleSemanticPortClient:
    def __init__(self, config: OpenAICompatibleConfig):
        self.config = config

    def semantic_port(self, context: SemanticPortContext) -> str | None:
        from openai import OpenAI

        client = OpenAI(api_key=self.config.api_key, base_url=self.config.base_url)
        response = client.chat.completions.create(
            model=self.config.model,
            temperature=self.config.temperature,
            messages=[
                {
                    "role": "system",
                    "content": _semantic_port_system_prompt(),
                },
                {
                    "role": "user",
                    "content": _semantic_port_user_prompt(
                        context,
                        max_chars=self.config.max_context_chars,
                    ),
                },
            ],
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
    ):
        self.llm_client = llm_client

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
                manual_item="Semantic porting requires an injected LLM client.",
            )

        patch_text = self._call_client(semantic_context)
        if not patch_text or not patch_text.strip():
            return SemanticPortResult(
                patch_text=None,
                context=semantic_context,
                llm_used=True,
                manual_item="Semantic porter returned no patch.",
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
