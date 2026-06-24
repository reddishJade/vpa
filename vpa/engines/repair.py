"""LLM repair engine boundary.

Phase 1 deliberately has no LLM dependency here. Later phases will inject a
client and require patch-oriented output after llm_gate approval.
"""

from __future__ import annotations


class RepairEngine:
    def __init__(self, llm_client=None):
        self.llm_client = llm_client

    def semantic_port(self, *args, **kwargs):
        raise NotImplementedError("semantic port repair is planned for Phase 3")

