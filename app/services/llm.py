"""LLM 客户端。

只用 requests 直接打 OpenAI 兼容的 `/chat/completions`：OpenAI、DeepSeek、
以及 Ollama（自带 OpenAI 兼容端点）共享同一套代码，省掉 openai SDK 这个依赖。
"""

from __future__ import annotations

import json
import re

import requests


def extract_json(text: str) -> dict | None:
    """从模型回复里抠出第一个 JSON 对象（模型常会包一层 ```json 或加客套话）。"""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


class LLMClient:
    def __init__(self, cfg: dict) -> None:
        llm = cfg.get("llm") or {}
        self.enabled = bool(llm.get("enabled"))
        self.base_url = (llm.get("base_url") or "").rstrip("/")
        self.api_key = llm.get("api_key") or ""
        self.model = llm.get("model") or ""
        self.timeout = int(llm.get("timeout") or 60)

    def available(self) -> bool:
        """未启用或没填全时返回 False，匹配引擎会自动降级到离线模式。"""
        return bool(self.enabled and self.base_url and self.model)

    def chat(self, system: str, user: str) -> str | None:
        if not self.available():
            return None
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
        }
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except (requests.RequestException, KeyError, IndexError, ValueError):
            return None

    def check(self) -> tuple[bool, str]:
        """设置窗口的「测试连接」，返回 (是否可用, 人类可读说明)。"""
        if not self.enabled:
            return False, "LLM 模式未启用，将使用离线 RapidFuzz 匹配。"
        if not self.base_url or not self.model:
            return False, "base_url 或 model 未填写完整。"
        reply = self.chat("你是一个测试助手。", "只回复两个字：正常")
        if reply is None:
            return False, f"请求失败：无法访问 {self.base_url}，请确认服务已启动。"
        return True, f"连接成功，模型返回：{reply.strip()[:30]}"
