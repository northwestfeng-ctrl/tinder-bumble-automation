#!/usr/bin/env python3
"""
LLM 批处理调用模块
提供批量生成回复的能力，减少 API 往返次数
"""
from __future__ import annotations

import time
import json
import urllib.request
import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed

from unified_reply_engine import SAFE_FALLBACK_REPLY, _extract_llm_reply, sanitize_reply_for_send

log = logging.getLogger("LLMBatch")


@dataclass
class BatchRequest:
    """批处理请求项"""
    request_id: str
    prompt: str
    context: Dict[str, Any] = field(default_factory=dict)
    static_system_prompt: str = ""
    dynamic_user_prompt: str = ""
    
    def __hash__(self):
        return hash(self.request_id)


@dataclass
class BatchResponse:
    """批处理响应项"""
    request_id: str
    reply: str
    success: bool = True
    error: Optional[str] = None
    latency_ms: float = 0.0


class LLMBatchProcessor:
    """
    LLM 批处理器
    
    功能：
    - 批量生成回复（减少 API 往返）
    - 并发限流（避免过载）
    - 失败重试（单个失败不影响整体）
    - 性能统计
    """
    
    def __init__(self, config: Any):
        """
        初始化批处理器
        
        Args:
            config: 统一配置对象
        """
        self.config = config
        
        # 并发配置
        llm_config = getattr(config, "llm", config)
        self.max_workers = int(getattr(llm_config, "max_workers", 3) or 3)
        self.batch_size = int(getattr(llm_config, "batch_size", 5) or 5)
        
        # 统计
        self.stats = {
            "total_requests": 0,
            "successful": 0,
            "failed": 0,
            "total_latency_ms": 0.0,
        }
        
        log.info(
            f"LLMBatchProcessor 初始化 "
            f"(max_workers={self.max_workers}, batch_size={self.batch_size})"
        )
    
    def process_batch(
        self,
        requests: List[BatchRequest],
    ) -> List[BatchResponse]:
        """
        批量处理请求
        
        Args:
            requests: 请求列表
        
        Returns:
            响应列表（顺序与请求对应）
        """
        if not requests:
            return []
        
        log.info(f"开始批处理 {len(requests)} 个请求")
        start_time = time.time()
        
        # 分批处理（避免单批过大）
        all_responses = []
        for i in range(0, len(requests), self.batch_size):
            batch = requests[i:i + self.batch_size]
            responses = self._process_batch_chunk(batch)
            all_responses.extend(responses)
        
        # 统计
        elapsed_ms = (time.time() - start_time) * 1000
        success_count = sum(1 for r in all_responses if r.success)
        
        log.info(
            f"批处理完成: {success_count}/{len(requests)} 成功, "
            f"耗时 {elapsed_ms:.0f}ms"
        )
        
        return all_responses
    
    def _process_batch_chunk(
        self,
        batch: List[BatchRequest],
    ) -> List[BatchResponse]:
        """处理单个批次"""
        responses = {}
        
        # 并发调用
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_req = {
                executor.submit(self._call_llm, req): req
                for req in batch
            }
            
            for future in as_completed(future_to_req):
                req = future_to_req[future]
                try:
                    response = future.result()
                    responses[req.request_id] = response
                except Exception as e:
                    log.error(f"请求 {req.request_id} 失败: {e}")
                    responses[req.request_id] = BatchResponse(
                        request_id=req.request_id,
                        reply="",
                        success=False,
                        error=str(e),
                    )
        
        # 按原始顺序返回
        return [responses[req.request_id] for req in batch]
    
    def _call_llm(self, request: BatchRequest) -> BatchResponse:
        """调用 LLM API"""
        start_time = time.time()
        context = request.context if isinstance(request.context, dict) else {}
        message_context = context.get("messages") if isinstance(context.get("messages"), list) else None
        try:
            max_len = int(context.get("max_len", 50) or 50)
        except Exception:
            max_len = 50

        messages = None
        if request.static_system_prompt or request.dynamic_user_prompt:
            messages = []
            if request.static_system_prompt:
                messages.append({"role": "system", "content": request.static_system_prompt})
            if request.dynamic_user_prompt:
                messages.append({"role": "user", "content": request.dynamic_user_prompt})
        else:
            # 兼容旧批处理调用；若未来恢复使用，优先通过显式 system/user 双段输入接入共享协议。
            messages = [{"role": "user", "content": request.prompt}]

        def _payload_for(model: str) -> bytes:
            return json.dumps({
                "model": model,
                "messages": messages,
                "max_tokens": self.config.llm.max_tokens,
                "temperature": self.config.llm.temperature,
            }).encode()

        def _chat_endpoint(url: str) -> str:
            normalized = str(url or "").rstrip("/")
            if normalized.endswith("/v1"):
                return f"{normalized}/chat/completions"
            return normalized

        def _request_once(url: str, api_key: str, payload: bytes, timeout: int) -> str:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
                msg = data["choices"][0]["message"]
                rc = msg.get("reasoning_content") or ""
                content = msg.get("content") or ""
                reply = _extract_llm_reply(rc, content, max_len=max_len)
                return sanitize_reply_for_send(reply, max_len=max_len, messages=message_context)

        payload = _payload_for(self.config.llm.model)
        
        last_err = None
        for attempt in range(self.config.llm.max_retries):
            try:
                reply = _request_once(
                    self.config.llm.base_url,
                    self.config.llm.api_key,
                    payload,
                    self.config.llm.timeout,
                )
                if not reply or reply == SAFE_FALLBACK_REPLY:
                    latency_ms = (time.time() - start_time) * 1000
                    self.stats["total_requests"] += 1
                    self.stats["failed"] += 1
                    return BatchResponse(
                        request_id=request.request_id,
                        reply="",
                        success=False,
                        error="no_safe_reply",
                        latency_ms=latency_ms,
                    )

                # 统计
                latency_ms = (time.time() - start_time) * 1000
                self.stats["total_requests"] += 1
                self.stats["successful"] += 1
                self.stats["total_latency_ms"] += latency_ms

                return BatchResponse(
                    request_id=request.request_id,
                    reply=reply,
                    success=True,
                    latency_ms=latency_ms,
                )

            except Exception as e:
                last_err = e
                should_retry = (
                    attempt < self.config.llm.max_retries - 1 and (
                        "529" in str(e) or
                        "timeout" in str(e).lower() or
                        "connection" in str(e).lower()
                    )
                )
                
                if should_retry:
                    delay = 5 * (2 ** attempt)
                    log.warning(
                        f"请求 {request.request_id} 失败 "
                        f"(attempt {attempt+1}/{self.config.llm.max_retries}): {e}, "
                        f"等待 {delay}s"
                    )
                    time.sleep(delay)
                else:
                    break

        fallback_key = str(getattr(self.config.llm, "fallback_api_key", "") or "")
        fallback_model = str(getattr(self.config.llm, "fallback_model", "") or "")
        if fallback_key and fallback_model and last_err is not None:
            fb_url = _chat_endpoint(getattr(self.config.llm, "fallback_base_url", "https://api.deepseek.com/v1"))
            try:
                log.warning(f"请求 {request.request_id} 主模型失败，尝试 fallback: {fallback_model}")
                reply = _request_once(
                    fb_url,
                    fallback_key,
                    _payload_for(fallback_model),
                    max(int(getattr(self.config.llm, "timeout", 30) or 30), 60),
                )
                if reply and reply != SAFE_FALLBACK_REPLY:
                    latency_ms = (time.time() - start_time) * 1000
                    self.stats["total_requests"] += 1
                    self.stats["successful"] += 1
                    self.stats["total_latency_ms"] += latency_ms
                    return BatchResponse(
                        request_id=request.request_id,
                        reply=reply,
                        success=True,
                        latency_ms=latency_ms,
                    )
                last_err = RuntimeError("fallback_no_safe_reply")
            except Exception as fb_err:
                last_err = fb_err
                log.error(f"请求 {request.request_id} fallback 失败: {fb_err}")
        
        # 失败
        self.stats["total_requests"] += 1
        self.stats["failed"] += 1
        
        return BatchResponse(
            request_id=request.request_id,
            reply="",
            success=False,
            error=str(last_err),
        )
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        total = self.stats["total_requests"]
        avg_latency = (
            self.stats["total_latency_ms"] / self.stats["successful"]
            if self.stats["successful"] > 0
            else 0
        )
        
        return {
            **self.stats,
            "success_rate": f"{self.stats['successful'] / total * 100:.1f}%" if total > 0 else "N/A",
            "avg_latency_ms": f"{avg_latency:.0f}",
        }


# 全局批处理器实例（单例）
_processor: Optional[LLMBatchProcessor] = None


def get_llm_batch_processor(config: Any) -> LLMBatchProcessor:
    """
    获取全局 LLM 批处理器（单例）
    
    Args:
        config: 统一配置对象
    
    Returns:
        LLMBatchProcessor 实例
    """
    global _processor
    if _processor is None:
        _processor = LLMBatchProcessor(config)
    return _processor


def batch_generate_replies(
    config: Any,
    prompts: List[str],
    contexts: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    """
    批量生成回复（便捷函数）
    
    Args:
        config: 统一配置对象
        prompts: Prompt 列表
        contexts: 上下文列表（可选）
    
    Returns:
        回复列表（顺序与 prompts 对应）
    """
    processor = get_llm_batch_processor(config)
    
    # 构建请求
    requests = [
        BatchRequest(
            request_id=f"req_{i}",
            prompt=prompt,
            context=contexts[i] if contexts else {},
            static_system_prompt=str((contexts[i] if contexts else {}).get("static_system_prompt", "") or ""),
            dynamic_user_prompt=str((contexts[i] if contexts else {}).get("dynamic_user_prompt", "") or ""),
        )
        for i, prompt in enumerate(prompts)
    ]
    
    # 批处理
    responses = processor.process_batch(requests)
    
    # 提取回复
    return [r.reply if r.success else "" for r in responses]
