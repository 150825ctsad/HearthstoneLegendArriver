# -*- coding: utf-8 -*-
"""OpenAI 兼容的 LLM API 客户端（GLM/OpenAI/本地 Ollama 均可用）。

密钥只从环境变量 HS_LLM_API_KEY 读取。transport 可注入（测试不发网络）。
"""

import os

import requests


class LlmUnavailableError(RuntimeError):
    """API 不可用（缺密钥/网络/超时/非 200）；调用方应回退盒子流程。"""


class LlmClient:
    name = "llm"

    def __init__(self, settings, api_key=None, post=None):
        self.api_base = str(settings.get("api_base") or "").rstrip("/")
        self.model = str(settings.get("model") or "")
        self.timeout = float(settings.get("timeout_seconds") or 8.0)
        self.max_retries = int(settings.get("max_retries") or 0)
        self.max_tokens = int(settings.get("max_tokens") or 200)
        self.api_key = (api_key if api_key is not None
                        else os.environ.get("HS_LLM_API_KEY") or "")
        # transport 注入：post(url, payload, timeout) -> (status, body_text)
        self._post = post

    def decide(self, system_prompt, user_prompt):
        """一轮对话补全，返回模型文本；任何失败抛 LlmUnavailableError。"""
        if not self.api_key:
            raise LlmUnavailableError("no_api_key（请设置环境变量 "
                                      "HS_LLM_API_KEY）")
        if not self.api_base or not self.model:
            raise LlmUnavailableError("api_base/model 未配置")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "max_tokens": self.max_tokens,
        }
        url = f"{self.api_base}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        attempts = 1 + max(0, self.max_retries)
        last_error = None
        for _ in range(attempts):
            try:
                status, body = self._post(url, payload, timeout=self.timeout,
                                          headers=headers)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                continue
            if status != 200:
                last_error = f"http_{status}: {body[:120]}"
                continue
            try:
                import json
                data = json.loads(body)
                return str(
                    data["choices"][0]["message"]["content"] or "")
            except Exception as exc:
                last_error = f"bad_response: {exc}"
        raise LlmUnavailableError(last_error or "unknown")


def _default_post(url, payload, timeout, headers):
    """requests 实现的真实传输（独立函数便于 monkeypatch）。"""
    response = requests.post(url, json=payload, timeout=timeout,
                             headers=headers)
    return response.status_code, response.text


def make_client(settings):
    """按配置构造客户端。

    密钥取值优先级：环境变量 HS_LLM_API_KEY > ui_config 的 decision.api_key
    （网页「🧠 决策模型」卡片填写）。transport 默认 requests；测试注入 fake。
    """
    api_key = (os.environ.get("HS_LLM_API_KEY")
               or str(settings.get("api_key") or ""))
    return LlmClient(settings, api_key=api_key, post=_default_post)
