"""
OpenAI Embedding 模型封装

统一使用 OpenAI text-embedding-3-small（1536 维）。
支持通过 api_base 配置代理/中转地址。
"""

import logging
import time
from typing import List

import openai

logger = logging.getLogger(__name__)


class Embedder:
    """
    OpenAI Embedding 封装类

    用法:
        embedder = Embedder(config["embedding"])
        vec = embedder.encode("这是一段文本")
        vecs = embedder.encode_batch(["文本1", "文本2"])
    """

    def __init__(self, config: dict):
        """
        初始化 Embedder

        Args:
            config: embedding 配置字典，包含:
                - api_key: OpenAI API Key
                - api_base: API 地址（可替换为代理地址）
                - model: 模型名称（默认 text-embedding-3-small）
                - dimensions: 向量维度（默认 1536）
        """
        self.client = openai.OpenAI(
            api_key=config["api_key"],
            base_url=config.get("api_base", "https://api.openai.com/v1"),
        )
        self.model = config.get("model", "text-embedding-3-small")
        self.dimensions = config.get("dimensions", 1536)
        retry_config = config.get("retry", {}) or {}
        self.retry_max_attempts = int(retry_config.get("max_attempts", 3) or 3)
        self.retry_initial_delay_seconds = float(retry_config.get("initial_delay_seconds", 1.0) or 1.0)
        self.retry_backoff_multiplier = float(retry_config.get("backoff_multiplier", 2.0) or 2.0)

        logger.info(
            f"Embedder 初始化完成 | 模型: {self.model} | 维度: {self.dimensions} | "
            f"API地址: {config.get('api_base', 'https://api.openai.com/v1')}"
        )

    def encode(self, text: str) -> List[float]:
        """
        将单条文本转为向量

        Args:
            text: 输入文本

        Returns:
            1536 维浮点数向量

        Raises:
            openai.AuthenticationError: API Key 无效
            openai.RateLimitError: 请求频率超限
            openai.APIError: 其他 API 错误
        """
        if not text or not text.strip():
            raise ValueError("输入文本不能为空")

        def _do_encode() -> List[float]:
            response = self.client.embeddings.create(
                input=text,
                model=self.model,
                dimensions=self.dimensions,
            )
            return response.data[0].embedding

        try:
            return self._with_retry(_do_encode, action="Embedding 编码")
        except openai.AuthenticationError:
            logger.error("OpenAI API Key 无效，请检查 embedding.api_key 配置")
            raise
        except openai.RateLimitError:
            logger.warning("OpenAI API 请求频率超限，重试后仍失败")
            raise
        except Exception as e:
            logger.error(f"Embedding 编码失败: {e}")
            raise

    def encode_batch(self, texts: List[str]) -> List[List[float]]:
        """
        批量将文本转为向量（OpenAI API 原生支持批量请求）

        Args:
            texts: 文本列表

        Returns:
            向量列表，与输入文本一一对应

        Raises:
            ValueError: 输入列表为空
        """
        if not texts:
            raise ValueError("输入文本列表不能为空")

        cleaned_texts = [t for t in texts if t and t.strip()]
        if not cleaned_texts:
            raise ValueError("输入文本列表中没有有效文本")

        def _do_encode_batch() -> List[List[float]]:
            response = self.client.embeddings.create(
                input=cleaned_texts,
                model=self.model,
                dimensions=self.dimensions,
            )
            return [item.embedding for item in response.data]

        try:
            return self._with_retry(_do_encode_batch, action="批量 Embedding 编码")
        except openai.AuthenticationError:
            logger.error("OpenAI API Key 无效，请检查 embedding.api_key 配置")
            raise
        except openai.RateLimitError:
            logger.warning("OpenAI API 请求频率超限，重试后仍失败")
            raise
        except Exception as e:
            logger.error(f"批量 Embedding 编码失败: {e}")
            raise

    def _with_retry(self, func, action: str):
        attempts = max(1, self.retry_max_attempts)
        delay = max(0.0, self.retry_initial_delay_seconds)
        multiplier = max(1.0, self.retry_backoff_multiplier)
        last_error = None

        for attempt in range(1, attempts + 1):
            try:
                return func()
            except (openai.AuthenticationError, ValueError):
                raise
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