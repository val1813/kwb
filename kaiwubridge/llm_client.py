"""LLM客户端：异步调用OpenAI兼容接口

职责：
- 封装对任意OpenAI兼容LLM端点的HTTP调用
- 支持普通聊天、JSON结构化输出、流式响应三种模式
- 默认回退到本地Ollama（http://localhost:11434/v1）

设计决策：
- 使用httpx异步客户端，避免LLM调用（通常1~10秒）阻塞事件循环
- 超时设置120秒：本地大模型推理可能较慢，需要足够的等待时间
"""

from __future__ import annotations

import json
from typing import AsyncGenerator

import httpx

from .models import ChatMessage

# LLM请求超时（秒）：本地模型推理可能较慢，120秒足够覆盖大多数场景
_REQUEST_TIMEOUT_SECONDS = 120.0

# 默认LLM端点：本地Ollama的OpenAI兼容接口
_DEFAULT_BASE_URL = "http://localhost:11434/v1"


class LLMClient:
    """OpenAI兼容的异步LLM客户端

    支持任意实现了OpenAI /v1/chat/completions 接口的后端：
    - Ollama（本地）
    - vLLM
    - DeepSeek API
    - 通义千问 API
    - 任何OpenAI兼容的代理
    """

    def __init__(self, base_url: str, api_key: str = "", model: str = "qwen2.5:7b"):
        # 确保base_url有协议前缀，防止httpx报错
        base_url = base_url.rstrip("/")
        if not base_url:
            base_url = _DEFAULT_BASE_URL
        if not base_url.startswith(("http://", "https://")):
            base_url = "http://" + base_url

        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        """延迟创建httpx异步客户端"""
        if self._client is None or self._client.is_closed:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        return self._client

    async def chat(
        self,
        messages: list[ChatMessage],
        temperature: float = 0.1,
        max_tokens: int = 2048,
    ) -> str:
        """发送聊天请求，返回完整响应文本

        Args:
            messages: 消息列表
            temperature: 温度参数，SQL生成建议用0.1保证稳定性
            max_tokens: 最大生成token数
        """
        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        resp = await self.client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    async def generate_json(
        self,
        messages: list[ChatMessage],
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> dict:
        """发送请求并解析JSON响应

        自动处理LLM可能返回的代码块包装（```json ... ```）
        """
        text = await self.chat(messages, temperature, max_tokens)
        text = text.strip()
        # 去掉代码块标记（LLM经常用```json包装输出）
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [line for line in lines if not line.startswith("```")]
            text = "\n".join(lines)
        return json.loads(text)

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        temperature: float = 0.1,
        max_tokens: int = 2048,
    ) -> AsyncGenerator[str, None]:
        """流式聊天，逐块返回文本

        用于最终回答的流式输出，提升用户体验。
        遵循OpenAI SSE协议：每行 "data: {json}\n\n"
        """
        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        async with self.client.stream("POST", "/chat/completions", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]  # 跳过"data: "前缀（6个字符）
                if data_str.strip() == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                    delta = data["choices"][0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        yield content
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

    async def close(self):
        """关闭HTTP客户端，释放连接资源"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
