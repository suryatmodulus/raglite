"""Retrieval-augmented generation."""

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import numpy as np
from litellm import (  # type: ignore[attr-defined]
    ChatCompletionMessageToolCall,
    acompletion,
    completion,
    stream_chunk_builder,
    supports_function_calling,
)

from raglite._config import RAGLiteConfig
from raglite._database import ChunkSpan
from raglite._litellm import get_context_size
from raglite._search import hybrid_search, rerank_chunks, retrieve_chunk_spans
from raglite._typing import SearchMethod

# The default RAG instruction template follows Anthropic's best practices [1].
# [1] https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/long-context-tips
RAG_INSTRUCTION_TEMPLATE = """
You are a friendly and knowledgeable assistant that provides complete and insightful answers.
Whenever possible, use only the provided context to respond to the question at the end.
When responding, you MUST NOT reference the existence of the context, directly or indirectly.
Instead, you MUST treat the context as if its contents are entirely part of your working memory.

{context}

{user_prompt}
""".strip()


def retrieve_rag_context(
    query: str,
    *,
    num_chunks: int = 5,
    chunk_neighbors: tuple[int, ...] | None = (-1, 1),
    search: SearchMethod = hybrid_search,
    config: RAGLiteConfig | None = None,
) -> list[ChunkSpan]:
    """Retrieve context for RAG."""
    # If the user has configured a reranker, we retrieve extra contexts to rerank.
    config = config or RAGLiteConfig()
    extra_chunks = 3 * num_chunks if config.reranker else 0
    # Search for relevant chunks.
    chunk_ids, _ = search(query, num_results=num_chunks + extra_chunks, config=config)
    # Rerank the chunks from most to least relevant.
    chunks = rerank_chunks(query, chunk_ids=chunk_ids, config=config)
    # Extend the top contexts with their neighbors and group chunks into contiguous segments.
    context = retrieve_chunk_spans(chunks[:num_chunks], neighbors=chunk_neighbors, config=config)
    return context


def create_rag_instruction(
    user_prompt: str,
    context: list[ChunkSpan],
    *,
    rag_instruction_template: str = RAG_INSTRUCTION_TEMPLATE,
) -> dict[str, str]:
    """Convert a user prompt to a RAG instruction.

    The RAG instruction's format follows Anthropic's best practices [1].

    [1] https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/long-context-tips
    """
    message = {
        "role": "user",
        "content": rag_instruction_template.format(
            user_prompt=user_prompt.strip(),
            context="\n".join(
                chunk_span.to_xml(index=i + 1) for i, chunk_span in enumerate(context)
            ),
        ),
    }
    return message


def _clip(messages: list[dict[str, str]], max_tokens: int) -> list[dict[str, str]]:
    """Left clip a messages array to avoid hitting the context limit."""
    cum_tokens = np.cumsum([len(message.get("content") or "") // 3 for message in messages][::-1])
    first_message = -np.searchsorted(cum_tokens, max_tokens)
    return messages[first_message:]


def _get_tools(
    messages: list[dict[str, str]], config: RAGLiteConfig
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | str | None]:
    """Get tools to search the knowledge base if no RAG context is provided in the messages."""
    # Check if messages already contain RAG context or if the LLM supports tool use.
    final_message = messages[-1].get("content", "")
    messages_contain_rag_context = any(s in final_message for s in ("</document>", "from_chunk_id"))
    llm_provider = "llama-cpp-python" if config.llm.startswith("llama-cpp") else None
    llm_supports_function_calling = supports_function_calling(config.llm, llm_provider)
    if not messages_contain_rag_context and not llm_supports_function_calling:
        error_message = "You must either explicitly provide RAG context in the last message, or use an LLM that supports function calling."
        raise ValueError(error_message)
    # Add a tool to search the knowledge base if no RAG context is provided in the messages. Because
    # llama-cpp-python cannot stream tool_use='auto' yet, we use a workaround that forces the LLM
    # to use a tool, but allows it to skip the search.
    auto_tool_use_workaround = (
        {
            "skip": {
                "type": "boolean",
                "description": "True if a satisfactory answer can be provided without the knowledge base, false otherwise.",
            }
        }
        if llm_provider == "llama-cpp-python"
        else {}
    )
    tools: list[dict[str, Any]] | None = (
        [
            {
                "type": "function",
                "function": {
                    "name": "search_knowledge_base",
                    "description": "Search the knowledge base. Note: only use this tool if not enough information is available to provide an answer.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            **auto_tool_use_workaround,
                            "query": {
                                "type": ["string", "null"],
                                "description": "\n".join(  # noqa: FLY002
                                    [
                                        "The query string to search the knowledge base with.",
                                        "The query string MUST satisfy ALL of the following criteria:"
                                        "- The query string MUST be a precise question in the user's language.",
                                        "- The query string MUST resolve all pronouns to explicit nouns from the conversation history.",
                                        "- The query string MUST be `null` if `skip` is `true`.",
                                    ]
                                ),
                            },
                        },
                        "required": [*list(auto_tool_use_workaround), "query"],
                        "additionalProperties": False,
                    },
                },
            }
        ]
        if not messages_contain_rag_context
        else None
    )
    tool_choice: dict[str, Any] | str | None = (
        (
            {"type": "function", "function": {"name": "search_knowledge_base"}}
            if auto_tool_use_workaround
            else "auto"
        )
        if tools
        else None
    )
    return tools, tool_choice


def _run_tools(
    tool_calls: list[ChatCompletionMessageToolCall], config: RAGLiteConfig
) -> list[dict[str, Any]]:
    """Run tools to search the knowledge base for RAG context."""
    tool_messages: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        if tool_call.function.name == "search_knowledge_base":
            kwargs = json.loads(tool_call.function.arguments)
            kwargs["config"] = config
            skip = kwargs.pop("skip", False)
            tool_messages.append(
                {
                    "role": "tool",
                    "content": '{{"documents": [{elements}]}}'.format(
                        elements=", ".join(
                            chunk_span.to_json(index=i + 1)
                            for i, chunk_span in enumerate(retrieve_rag_context(**kwargs))
                        )
                    )
                    if not skip and kwargs["query"]
                    else "{}",
                    "tool_call_id": tool_call.id,
                }
            )
        else:
            error_message = f"Unknown function `{tool_call.function.name}`."
            raise ValueError(error_message)
    return tool_messages


def rag(messages: list[dict[str, str]], *, config: RAGLiteConfig) -> Iterator[str]:
    # If the final message does not contain RAG context, get a tool to search the knowledge base.
    max_tokens = get_context_size(config)
    tools, tool_choice = _get_tools(messages, config)
    # Stream the LLM response, which is either a tool call request or an assistant response.
    chunks = []
    clipped_messages = _clip(messages, max_tokens)
    if tools and config.llm.startswith("llama-cpp-python"):
        # Help llama.cpp LLMs plan their response by providing a JSON schema for the tool call.
        clipped_messages[-1]["content"] += (
            f"\n\nDecide whether to use or skip these tools in your response:\n{json.dumps(tools)}"
        )
    stream = completion(
        model=config.llm,
        messages=clipped_messages,
        tools=tools,
        tool_choice=tool_choice,
        stream=True,
    )
    for chunk in stream:
        chunks.append(chunk)
        if isinstance(token := chunk.choices[0].delta.content, str):
            yield token
    # Check if there are tools to be called.
    response = stream_chunk_builder(chunks, messages)
    tool_calls = response.choices[0].message.tool_calls  # type: ignore[union-attr]
    if tool_calls:
        # Add the tool call request to the message array.
        messages.append(response.choices[0].message.to_dict())  # type: ignore[arg-type,union-attr]
        # Run the tool calls to retrieve the RAG context and append the output to the message array.
        messages.extend(_run_tools(tool_calls, config))
        # Stream the assistant response.
        chunks = []
        stream = completion(model=config.llm, messages=_clip(messages, max_tokens), stream=True)
        for chunk in stream:
            chunks.append(chunk)
            if isinstance(token := chunk.choices[0].delta.content, str):
                yield token
    # Append the assistant response to the message array.
    response = stream_chunk_builder(chunks, messages)
    messages.append(response.choices[0].message.to_dict())  # type: ignore[arg-type,union-attr]


async def async_rag(messages: list[dict[str, str]], *, config: RAGLiteConfig) -> AsyncIterator[str]:
    # If the final message does not contain RAG context, get a tool to search the knowledge base.
    max_tokens = get_context_size(config)
    tools, tool_choice = _get_tools(messages, config)
    # Asynchronously stream the LLM response, which is either a tool call or an assistant response.
    chunks = []
    clipped_messages = _clip(messages, max_tokens)
    if tools and config.llm.startswith("llama-cpp-python"):
        # Help llama.cpp LLMs plan their response by providing a JSON schema for the tool call.
        clipped_messages[-1]["content"] += (
            f"\n\nDecide whether to use or skip these tools in your response:\n{json.dumps(tools)}"
        )
    async_stream = await acompletion(
        model=config.llm,
        messages=clipped_messages,
        tools=tools,
        tool_choice=tool_choice,
        stream=True,
    )
    async for chunk in async_stream:
        chunks.append(chunk)
        if isinstance(token := chunk.choices[0].delta.content, str):
            yield token
    # Check if there are tools to be called.
    response = stream_chunk_builder(chunks, messages)
    tool_calls = response.choices[0].message.tool_calls  # type: ignore[union-attr]
    if tool_calls:
        # Add the tool call requests to the message array.
        messages.append(response.choices[0].message.to_dict())  # type: ignore[arg-type,union-attr]
        # Run the tool calls to retrieve the RAG context and append the output to the message array.
        # TODO: Make this async.
        messages.extend(_run_tools(tool_calls, config))
        # Asynchronously stream the assistant response.
        chunks = []
        async_stream = await acompletion(
            model=config.llm, messages=_clip(messages, max_tokens), stream=True
        )
        async for chunk in async_stream:
            chunks.append(chunk)
            if isinstance(token := chunk.choices[0].delta.content, str):
                yield token
    # Append the assistant response to the message array.
    response = stream_chunk_builder(chunks, messages)
    messages.append(response.choices[0].message.to_dict())  # type: ignore[arg-type,union-attr]
