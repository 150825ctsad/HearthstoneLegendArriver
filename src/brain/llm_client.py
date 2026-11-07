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
        """一轮对话补全，返回模型文本；任何失败抛 LlmUnavailableError。

        推理型模型（deepseek-flash/reasoner 等）的思考链计入 max_tokens，
        若响应以 finish_reason=length 截断（答案没写完），自动把
        max_tokens 加倍重试一次——否则截断的 JSON 会让解析层全部失败
        （实测 deepseek-flash 一次决策思考即消耗 100-500 tokens）。
        """
        import json

        if not self.api_key:
            raise LlmUnavailableError("no_api_key（请设置环境变量 "
                                      "HS_LLM_API_KEY）")
        if not self.api_base or not self.model:
            raise LlmUnavailableError("api_base/model 未配置")
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        url = f"{self.api_base}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        attempts = 1 + max(0, self.max_retries) + 1
        max_tokens = self.max_tokens
        last_error = None
        for _ in range(attempts):
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": 0,
                "max_tokens": max_tokens,
            }
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
                data = json.loads(body)
                choice = data["choices"][0]
                text = str(choice["message"]["content"] or "")
            except Exception as exc:
                last_error = f"bad_response: {exc}"
                continue
            if str(choice.get("finish_reason") or "") == "length":
                # 答案被长度截断（推理型模型思考链计入 max_tokens）：
                # 加倍上限重试一次，计费按实际用量、上限抬高不多花钱
                max_tokens *= 2
                last_error = "finish_reason_length"
                continue
            return text
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
