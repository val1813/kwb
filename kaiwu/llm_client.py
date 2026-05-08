"""LLM客户端：异步调用OpenAI兼容接口"""

from __future__ import annotations

import json
from typing import AsyncGenerator

import httpx

from .models import ChatMessage


class LLMClient:
    """OpenAI兼容的LLM客户端"""

    def __init__(self, base_url: str, api_key: str = "", model: str = "qwen2.5:7b"):
        # 确保base_url有协议前缀，默认http
        base_url = base_url.rstrip("/")
        if not base_url:
            base_url = "http://localhost:11434/v1"
        if not base_url.startswith(("http://", "https://")):
            base_url = "http://" + base_url
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=120.0,
            )
        return self._client

    async def chat(
        self,
        messages: list[ChatMessage],
        temperature: float = 0.1,
        max_tokens: int = 2048,
    ) -> str:
        """发送聊天请求，返回完整响应文本"""
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
        """发送请求并解析JSON响应"""
        text = await self.chat(messages, temperature, max_tokens)
        # 尝试从响应中提取JSON
        text = text.strip()
        if text.startswith("```"):
            # 去掉代码块标记
            lines = text.split("\n")
            lines = [l for l in lines if not l.startswith("```")]
            text = "\n".join(lines)
        return json.loads(text)

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        temperature: float = 0.1,
        max_tokens: int = 2048,
    ) -> AsyncGenerator[str, None]:
        """流式聊天，逐块返回文本"""
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
                data_str = line[6:]
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
        if self._client and not self._client.is_closed:
            await self._client.aclose()
