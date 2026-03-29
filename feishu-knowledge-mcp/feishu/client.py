"""
飞书 API 客户端封装

负责飞书的 App 认证和 Token 管理。
基于官方 lark-oapi SDK 封装，同时提供原始 HTTP 请求能力。
"""

import json
import logging
import time
from typing import Any, Dict, Optional
from urllib import error, parse, request

import lark_oapi as lark

logger = logging.getLogger(__name__)


class FeishuAPIError(RuntimeError):
    """飞书 API 调用失败。"""


class FeishuClient:
    """
    飞书 API 客户端

    封装飞书 App 认证（tenant_access_token）和自动刷新机制。
    其他模块（document.py、wiki.py）基于此客户端进行 API 调用。

    用法:
        client = FeishuClient(config["feishu"])
        lark_client = client.get_client()  # 获取 lark SDK 客户端
    """

    def __init__(self, config: dict):
        """
        初始化飞书客户端

        Args:
            config: feishu 配置字典，包含:
                - app_id: 飞书自建应用 App ID
                - app_secret: 飞书自建应用 App Secret
        """
        self.app_id = config["app_id"]
        self.app_secret = config["app_secret"]
        self.open_base_url = config.get("open_base_url", "https://open.feishu.cn/open-apis")
        retry_config = config.get("retry", {}) or {}
        self.retry_max_attempts = int(retry_config.get("max_attempts", 3) or 3)
        self.retry_initial_delay_seconds = float(retry_config.get("initial_delay_seconds", 1.0) or 1.0)
        self.retry_backoff_multiplier = float(retry_config.get("backoff_multiplier", 2.0) or 2.0)
        self._tenant_access_token: Optional[str] = None
        self._tenant_access_token_expire_at = 0.0

        # 创建 lark SDK 客户端（SDK 内置 Token 缓存和自动刷新）
        self._client = (
            lark.Client.builder()
            .app_id(self.app_id)
            .app_secret(self.app_secret)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )

        logger.info(f"飞书客户端初始化完成 | App ID: {self.app_id[:8]}...")

    def get_client(self) -> lark.Client:
        """获取 lark SDK 客户端实例"""
        return self._client

    def get_tenant_access_token(self) -> str:
        """获取 tenant_access_token，并做本地缓存。"""
        if self._tenant_access_token and time.time() < self._tenant_access_token_expire_at:
            return self._tenant_access_token

        payload = json.dumps(
            {
                "app_id": self.app_id,
                "app_secret": self.app_secret,
            },
            ensure_ascii=False,
        ).encode("utf-8")

        token_request = request.Request(
            url=f"{self.open_base_url}/auth/v3/tenant_access_token/internal",
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )

        def _fetch_token() -> str:
            try:
                with request.urlopen(token_request, timeout=30) as response:
                    response_body = response.read().decode("utf-8")
            except error.HTTPError as exc:
                error_body = exc.read().decode("utf-8", errors="ignore")
                raise FeishuAPIError(
                    f"获取 tenant_access_token 失败: HTTP {exc.code} | {error_body[:500]}"
                ) from exc
            except Exception as exc:
                raise FeishuAPIError(f"获取 tenant_access_token 失败: {exc}") from exc

            response_data = json.loads(response_body or "{}")
            if response_data.get("code") != 0:
                raise FeishuAPIError(
                    f"获取 tenant_access_token 失败: code={response_data.get('code')} | "
                    f"msg={response_data.get('msg', '')}"
                )

            token = response_data.get("tenant_access_token")
            if not token:
                raise FeishuAPIError("获取 tenant_access_token 失败: 返回结果缺少 tenant_access_token")

            expire = int(response_data.get("expire", 7200))
            self._tenant_access_token = token
            self._tenant_access_token_expire_at = time.time() + max(expire - 60, 60)
            return token

        return self._with_retry(_fetch_token, action="获取 tenant_access_token")

    def request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """调用飞书开放平台原始 HTTP API。"""
        url = f"{self.open_base_url.rstrip('/')}/{path.lstrip('/')}"
        if params:
            encoded_params = {
                key: value for key, value in params.items() if value is not None and value != ""
            }
            if encoded_params:
                url = f"{url}?{parse.urlencode(encoded_params)}"

        request_data = None
        if body is not None:
            request_data = json.dumps(body, ensure_ascii=False).encode("utf-8")

        def _do_request() -> Dict[str, Any]:
            api_request = request.Request(
                url=url,
                data=request_data,
                headers={
                    "Authorization": f"Bearer {self.get_tenant_access_token()}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                method=method.upper(),
            )

            try:
                with request.urlopen(api_request, timeout=30) as response:
                    response_body = response.read().decode("utf-8")
            except error.HTTPError as exc:
                error_body = exc.read().decode("utf-8", errors="ignore")
                raise FeishuAPIError(
                    f"飞书 API 请求失败: {method.upper()} {path} | HTTP {exc.code} | {error_body[:500]}"
                ) from exc
            except Exception as exc:
                raise FeishuAPIError(
                    f"飞书 API 请求失败: {method.upper()} {path} | {exc}"
                ) from exc

            response_data = json.loads(response_body or "{}")
            if response_data.get("code") not in (None, 0):
                raise FeishuAPIError(
                    f"飞书 API 请求失败: {method.upper()} {path} | code={response_data.get('code')} | "
                    f"msg={response_data.get('msg', '')}"
                )

            return response_data.get("data", response_data)

        return self._with_retry(_do_request, action=f"飞书 API 请求 {method.upper()} {path}")

    def _with_retry(self, func, action: str):
        attempts = max(1, self.retry_max_attempts)
        delay = max(0.0, self.retry_initial_delay_seconds)
        multiplier = max(1.0, self.retry_backoff_multiplier)
        last_error: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            try:
                return func()
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                logger.warning(
                    "%s 失败，准备重试 (%s/%s): %s",
                    action,
                    attempt,
                    attempts,
                    exc,
                )
                if delay > 0:
                    time.sleep(delay)
                delay = delay * multiplier if delay > 0 else 0.0

        assert last_error is not None
        raise last_error