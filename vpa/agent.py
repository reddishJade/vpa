from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import openai
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionFunctionToolParam,
    ChatCompletionMessageFunctionToolCall,
    ChatCompletionMessageParam,
    ChatCompletionMessageToolCallParam,
    ChatCompletionToolMessageParam,
)


def estimate_context_usage(messages, model_limit_chars=100000):
    """Char-count proxy for context usage. Returns fraction 0.0-1.0."""
    total = sum(len(m.get("content", "") or "") for m in messages)
    return total / model_limit_chars


def run_agent(
    *,
    system_prompt: str,
    user_message: str,
    tools: list[ChatCompletionFunctionToolParam],
    on_tool_call: Callable[[str, dict[str, Any]], Any],
    model: str = "gpt-4o",
    api_key: str,
    base_url: str | None = None,
    max_turns: int = 50,
) -> tuple[str | None, list[ChatCompletionMessageParam]]:
    """OpenAI-compatible tool-calling loop.

    Returns (final_text, messages) on success.
    Raises RuntimeError if max_turns exceeded or API call fails.
    """
    client = openai.OpenAI(api_key=api_key, base_url=base_url)

    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    for _turn in range(max_turns):
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
        )
        msg = response.choices[0].message

        if msg.tool_calls:
            # Build assistant tool_call params, narrowing from the discriminated union
            tc_list: list[ChatCompletionMessageToolCallParam] = []
            for tc in msg.tool_calls:
                if isinstance(tc, ChatCompletionMessageFunctionToolCall):
                    tc_list.append(
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                    )

            assistant_msg: ChatCompletionAssistantMessageParam = {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": tc_list,
            }
            messages.append(assistant_msg)

            # Execute tool calls
            for tc in msg.tool_calls:
                if not isinstance(tc, ChatCompletionMessageFunctionToolCall):
                    continue
                raw_args = tc.function.arguments
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    args = {}
                result = on_tool_call(tc.function.name, args)
                tool_msg: ChatCompletionToolMessageParam = {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
                messages.append(tool_msg)
        else:
            return msg.content, messages

    raise RuntimeError(f"Agent exceeded max turns ({max_turns})")
