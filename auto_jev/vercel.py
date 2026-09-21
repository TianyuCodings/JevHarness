"""Vercel AI Gateway 原生 Jev evaluation 传输层。

本模块只做一件事：通过 Vercel AI Gateway 的 **evaluation-model 协议** 调用
TypeSafe Jev，并把响应规范化成与 ``providers.JevClient.judge`` 同形的核心 dict。

协议（已由主审查者对照官方源码核实）::

    POST https://ai-gateway.vercel.sh/v4/ai/evaluation-model
    Authorization: Bearer <AI Gateway key>
    ai-model-id: typesafe-ai/jev
    ai-evaluation-model-specification-version: 4
    ai-gateway-protocol-version: 0.0.1
    ai-gateway-auth-method: api-key
    body: {"state": ..., "questions": {...}, "providerOptions"?: {...}}

明确不做的事：
* 不使用 chat/completions 之类的语言模型端点；
* 不把 Vercel key 发往 api.typesafe.ai 直连端点；
* endpoint 固定为常量，不跟随任何 3xx 重定向（凭据只发给可信 host）；
* 不在错误信息 / 返回值中携带 api_key 或响应 body 原文。

类型映射
--------
请求 noul 转为 boolean；响应 probability 转回 noul。Choice 与 Score 保留原生
choice / score / probabilities；不从缺失字段推导答案。confidence 来自
providerMetadata.typesafe.confidence，legend 来自提交的 criteria。保留舍入元数据。

模型别名
--------
当前已验证的 Gateway 模型 ID 是 ``typesafe-ai/jev``；传入直连风格的 ``jev-1.13.0`` 会映射为该
别名，但 **无法通过别名固定 Jev 版本**。返回值的 ``model`` 优先取服务在
``response.modelId`` / ``model`` 中报告的实际模型；若服务没有报告，则回退为
请求别名并在 ``gateway_metadata.model_source`` 标明 ``"requested_alias"``——
绝不把请求的 ``jev-1.13.0`` 当作实际版本。
"""

from __future__ import annotations

import json
import math
import re
import time
from typing import Any, Callable

import httpx

__all__ = [
    "VERCEL_EVALUATION_URL",
    "GATEWAY_JEV_MODEL_ID",
    "EVALUATION_SPEC_VERSION",
    "GATEWAY_PROTOCOL_VERSION",
    "GATEWAY_AUTH_METHOD",
    "VercelError",
    "vercel_judge",
    "resolve_gateway_model",
    "translate_questions",
    "normalize_answers",
    "build_headers",
]

# ---------------------------------------------------------------------------
# 常量：固定可信端点与协议头
# ---------------------------------------------------------------------------

VERCEL_EVALUATION_URL = "https://ai-gateway.vercel.sh/v4/ai/evaluation-model"
GATEWAY_JEV_MODEL_ID = "typesafe-ai/jev"
EVALUATION_SPEC_VERSION = "4"
GATEWAY_PROTOCOL_VERSION = "0.0.1"
GATEWAY_AUTH_METHOD = "api-key"
USER_AGENT = "auto-jev-vercel-transport/1"

# 重试退避参数（秒）。attempt 从 1 开始：0.5, 1, 2, 4, 8, 8, ...
RETRY_BACKOFF_BASE = 0.5
RETRY_BACKOFF_MAX = 8.0
RETRY_AFTER_MAX = 30.0

# 允许写入 gateway_metadata.response_headers 的响应头白名单（只挑字段，不整份复制）。
SAFE_RESPONSE_HEADERS = ("x-vercel-id", "x-request-id", "x-vercel-cache")

# 允许写入 gateway_metadata.response 的 body.response 字段白名单。
SAFE_RESPONSE_FIELDS = ("id", "modelId", "timestamp")

_SUPPORTED_QUESTION_TYPES = ("noul", "boolean", "choice", "score")
_TYPE_TO_GATEWAY = {
    "noul": "boolean",
    "boolean": "boolean",
    "choice": "choice",
    "score": "score",
}
_COMPATIBLE_ANSWER_TYPES = {
    "noul": frozenset({"boolean", "noul"}),
    "boolean": frozenset({"boolean", "noul"}),
    "choice": frozenset({"choice"}),
    "score": frozenset({"score"}),
}
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_ERROR_DETAIL_MAX = 200

MODEL_VERSION_NOTE = (
    "Vercel AI 当前已验证的 Gateway 模型 ID 是 typesafe-ai/jev，无法通过别名固定 Jev 版本；"
    "实际版本以服务响应报告为准，未报告则视为未知。"
)

# 可被测试 monkeypatch 的 sleep 钩子，避免真实等待。
_sleep: Callable[[float], None] = time.sleep


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class VercelError(RuntimeError):
    """Vercel AI Gateway 传输层错误。

    ``kind`` 取值：``auth`` / ``rate_limit`` / ``server`` / ``request`` /
    ``redirect`` / ``timeout`` / ``network`` / ``schema``。
    消息永不包含 api_key 或响应 body 原文。
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "error",
        status_code: int | None = None,
        attempts: int = 0,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.attempts = attempts
        self.retryable = retryable

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return (
            f"VercelError(kind={self.kind!r}, status_code={self.status_code!r}, "
            f"attempts={self.attempts!r}, message={str(self)!r})"
        )


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _redact(text: str, secret: str | None) -> str:
    if secret and secret in text:
        text = text.replace(secret, "[redacted]")
    return text


def _short(text: str, limit: int = _ERROR_DETAIL_MAX) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _validate_api_key(api_key: Any) -> None:
    if not isinstance(api_key, str) or not api_key or any(ch.isspace() for ch in api_key):
        raise VercelError(
            "api_key must be a non-empty string without whitespace (value not shown)",
            kind="request",
        )


def _new_client(timeout: float) -> httpx.Client:
    """创建仅用于固定端点的客户端：不跟随重定向。"""
    return httpx.Client(timeout=timeout, follow_redirects=False)


def _backoff_delay(failed_attempts: int, retry_after: str | None) -> float:
    """第 ``failed_attempts`` 次失败后的等待秒数；优先采用数值型 Retry-After。"""
    if retry_after is not None:
        try:
            value = float(retry_after.strip())
        except (TypeError, ValueError):
            value = None
        if value is not None and math.isfinite(value) and value >= 0:
            return min(value, RETRY_AFTER_MAX)
    exponent = max(failed_attempts - 1, 0)
    return min(RETRY_BACKOFF_BASE * (2**exponent), RETRY_BACKOFF_MAX)


def _safe_error_detail(response: httpx.Response, api_key: str) -> str | None:
    """从错误响应中挑出 error.message 之类的短文本；非 JSON 或无消息则返回 None。"""
    try:
        data = response.json()
    except ValueError:
        return None
    message: Any = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            message = err.get("message") or err.get("type")
        elif isinstance(err, str):
            message = err
        if message is None:
            message = data.get("message")
    if not isinstance(message, str) or not message.strip():
        return None
    return _short(_redact(message, api_key))


# ---------------------------------------------------------------------------
# 模型别名与请求构造
# ---------------------------------------------------------------------------


def resolve_gateway_model(model: Any) -> tuple[str, bool]:
    """把调用方给的 model 解析为 Gateway 的 ``ai-model-id``。

    返回 ``(gateway_model_id, alias_mapped)``。直连风格的 ``jev`` / ``jev-1.13.0``
    会映射到 ``typesafe-ai/jev``（``alias_mapped=True``）；带 ``/`` 的 id 原样透传。
    """
    if not isinstance(model, str) or not model.strip():
        raise VercelError("model must be a non-empty string", kind="request")
    candidate = model.strip()
    if candidate == GATEWAY_JEV_MODEL_ID:
        return candidate, False
    if "/" in candidate:
        if not _MODEL_ID_RE.match(candidate):
            raise VercelError(f"model id {candidate!r} contains invalid characters", kind="request")
        return candidate, False
    if candidate == "jev" or candidate.startswith("jev-"):
        return GATEWAY_JEV_MODEL_ID, True
    raise VercelError(
        f"cannot map model {candidate!r} to a Vercel AI Gateway evaluation model id "
        f"(expected {GATEWAY_JEV_MODEL_ID!r} or a direct 'jev-*' name)",
        kind="request",
    )


def _question_type(qid: Any, question: Any) -> str:
    if not isinstance(qid, str) or not qid:
        raise VercelError("question ids must be non-empty strings", kind="request")
    if not isinstance(question, dict):
        raise VercelError(f"question {qid!r} must be a dict", kind="request")
    qtype = question.get("type")
    if qtype not in _SUPPORTED_QUESTION_TYPES:
        raise VercelError(
            f"question {qid!r} has unsupported type {qtype!r}; "
            f"supported: {', '.join(_SUPPORTED_QUESTION_TYPES)}",
            kind="request",
        )
    return qtype


def translate_questions(questions: Any) -> dict[str, dict[str, Any]]:
    """把 TypeSafe 风格的 questions 转成 Gateway 形式（``noul`` → ``boolean``）。"""
    if not isinstance(questions, dict) or not questions:
        raise VercelError("questions must be a non-empty dict keyed by question id", kind="request")
    translated: dict[str, dict[str, Any]] = {}
    for qid, question in questions.items():
        qtype = _question_type(qid, question)
        if not question.get("instructions"):
            raise VercelError("question instructions are required", kind="request")
        criteria = question.get("criteria")
        if qtype == "choice" and (not isinstance(criteria, dict) or not 1 <= len(criteria) <= 255
                                 or not all(isinstance(k,str) and isinstance(v,str) for k,v in criteria.items())):
            raise VercelError("choice criteria must map option names to descriptions", kind="request")
        if qtype == "score" and (not isinstance(criteria, list) or not 2 <= len(criteria) <= 10
                                or not all(isinstance(v,str) for v in criteria)):
            raise VercelError("score criteria must contain 2 to 10 levels", kind="request")
        copied = dict(question)
        copied["type"] = _TYPE_TO_GATEWAY[qtype]
        translated[qid] = copied
    return translated


def build_headers(api_key: str, gateway_model_id: str) -> dict[str, str]:
    """Gateway evaluation 协议头。模型只在 header，不在 body。"""
    _validate_api_key(api_key)
    if not _MODEL_ID_RE.match(gateway_model_id):
        raise VercelError(f"model id {gateway_model_id!r} contains invalid characters", kind="request")
    return {
        "authorization": f"Bearer {api_key}",
        "ai-model-id": gateway_model_id,
        "ai-evaluation-model-specification-version": EVALUATION_SPEC_VERSION,
        "ai-gateway-protocol-version": GATEWAY_PROTOCOL_VERSION,
        "ai-gateway-auth-method": GATEWAY_AUTH_METHOD,
        "content-type": "application/json",
        "accept": "application/json",
        "user-agent": USER_AGENT,
    }


# ---------------------------------------------------------------------------
# 响应规范化
# ---------------------------------------------------------------------------


def _parse_unit_probability(value: Any, ctx: str) -> float:
    if not _is_finite_number(value):
        raise VercelError(f"{ctx}: probability must be a finite number, got {type(value).__name__}", kind="schema")
    prob = float(value)
    if not 0.0 <= prob <= 1.0:
        raise VercelError(f"{ctx}: probability {prob!r} is outside [0, 1]", kind="schema")
    return prob


def _parse_probabilities(value: Any, ctx: str) -> dict[str, float]:
    if not isinstance(value, dict) or not value:
        raise VercelError(f"{ctx}: probabilities must be a non-empty dict", kind="schema")
    parsed: dict[str, float] = {}
    for key, prob in value.items():
        if not isinstance(key, str):
            raise VercelError(f"{ctx}: probabilities keys must be strings", kind="schema")
        parsed[key] = _parse_unit_probability(prob, f"{ctx} probabilities[{key!r}]")
    return parsed


def normalize_answers(questions: Any, raw_answers: Any, rounding=None) -> dict:
    """Validate Gateway answers and preserve the native TypeSafe field names."""
    translate_questions(questions)
    if not isinstance(raw_answers, dict):
        raise VercelError("gateway answers must be an object", kind="schema")
    decimals = (rounding or {}).get("probabilityDecimals", 2)
    if not isinstance(decimals, int) or not 0 <= decimals <= 12:
        raise VercelError("invalid probability rounding", kind="schema")
    unit = 10 ** (-decimals)
    result = {}
    for qid, question in questions.items():
        raw = raw_answers.get(qid)
        kind = question["type"]
        expected_type = _TYPE_TO_GATEWAY[kind]
        if not isinstance(raw, dict) or raw.get("type") != expected_type:
            raise VercelError(f"missing or incompatible answer: {qid}", kind="schema")
        if kind in ("noul", "boolean"):
            value = _parse_unit_probability(raw.get("probability"), qid)
            result[qid] = {"type": "noul", "noul": value}
            continue
        probs = _parse_probabilities(raw.get("probabilities"), qid)
        criteria = question["criteria"]
        keys = set(criteria) if kind == "choice" else {str(i) for i in range(len(criteria))}
        if set(probs) != keys or abs(sum(probs.values()) - 1) > len(probs)*unit/2 + 1e-8:
            raise VercelError(f"invalid probability distribution: {qid}", kind="schema")
        if kind == "choice":
            value = raw.get("choice")
            if not isinstance(value, str) or value not in criteria:
                raise VercelError(f"invalid choice: {qid}", kind="schema")
            if probs[value] + unit < max(probs.values()):
                raise VercelError(f"choice disagrees with probabilities: {qid}", kind="schema")
            answer = {"type": kind, "choice": value, "probabilities": probs}
        else:
            value = raw.get("score")
            if not _is_finite_number(value) or not 0 <= value <= len(criteria)-1:
                raise VercelError(f"invalid score: {qid}", kind="schema")
            expected = sum(int(k)*v for k,v in probs.items())
            tolerance = unit * (1+sum(range(len(criteria)))) / 2 + 1e-8
            if abs(value-expected) > tolerance:
                raise VercelError(f"score disagrees with probabilities: {qid}", kind="schema")
            answer = {"type": kind, "score": value, "probabilities": probs,
                      "legend": {str(i):v for i,v in enumerate(criteria)}}
        result[qid] = answer
    return result


def _reported_model(data: dict[str, Any]) -> tuple[str | None, str | None]:
    """从响应中挑出服务报告的实际模型 id；找不到返回 (None, None)。"""
    response_block = data.get("response")
    if isinstance(response_block, dict):
        model_id = response_block.get("modelId")
        if isinstance(model_id, str) and model_id.strip():
            return model_id.strip(), "response.modelId"
    model = data.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip(), "model"
    return None, None


def _build_result(
    response: httpx.Response,
    questions: dict[str, Any],
    *,
    requested_model: str,
    gateway_model_id: str,
    alias_mapped: bool,
    attempts: int,
) -> dict[str, Any]:
    # JSONDecodeError.doc 会持有整份 body，因此不在 except 块内抛出，避免被链到异常上。
    parse_failed = False
    data: Any = None
    try:
        data = response.json()
    except ValueError:
        parse_failed = True
    if parse_failed:
        raise VercelError(
            f"Vercel AI Gateway returned a non-JSON body with HTTP {response.status_code}",
            kind="schema",
            status_code=response.status_code,
            attempts=attempts,
        )
    if not isinstance(data, dict):
        raise VercelError(
            f"Vercel AI Gateway returned a JSON {type(data).__name__}, expected an object",
            kind="schema",
            status_code=response.status_code,
            attempts=attempts,
        )
    if "answers" not in data:
        keys = ", ".join(sorted(str(k) for k in data.keys())) or "<none>"
        raise VercelError(
            f"Vercel AI Gateway response has no top-level 'answers'; top-level keys: {keys}",
            kind="schema",
            status_code=response.status_code,
            attempts=attempts,
        )
    raw_answers = data["answers"]
    answers = normalize_answers(questions, raw_answers, data.get("rounding"))
    confidence = data.get("providerMetadata", {}).get("typesafe", {}).get("confidence", {})
    for qid, answer in answers.items():
        if answer["type"] != "noul" and qid in confidence:
            answer["confidence"] = _parse_unit_probability(confidence[qid], qid)
    unexpected_ids = sorted(str(k) for k in raw_answers.keys() if k not in questions)

    usage_raw = data.get("usage")
    usage_reported = isinstance(usage_raw, dict)
    usage: dict[str, Any] = dict(usage_raw) if usage_reported else {}

    reported_model, model_source = _reported_model(data)
    if reported_model is None:
        model_value = gateway_model_id
        model_source = "requested_alias"
    else:
        model_value = reported_model

    response_block = data.get("response")
    response_meta: dict[str, Any] = {}
    if isinstance(response_block, dict):
        for field in SAFE_RESPONSE_FIELDS:
            value = response_block.get(field)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                response_meta[field] = value

    response_headers = {
        name: response.headers[name] for name in SAFE_RESPONSE_HEADERS if name in response.headers
    }

    provider_metadata = data.get("providerMetadata")
    warnings = data.get("warnings")

    gateway_metadata: dict[str, Any] = {
        "transport": "vercel-ai-gateway",
        "endpoint": VERCEL_EVALUATION_URL,
        "requested_model": requested_model,
        "gateway_model_id": gateway_model_id,
        "model_alias_mapped": alias_mapped,
        "model_source": model_source,
        "model_version_pinned": False,
        "model_version_note": MODEL_VERSION_NOTE,
        "specification_version": EVALUATION_SPEC_VERSION,
        "protocol_version": GATEWAY_PROTOCOL_VERSION,
        "http_status": response.status_code,
        "attempts": attempts,
        "usage_reported": usage_reported,
        "response": response_meta,
        "response_headers": response_headers,
        "provider_metadata": provider_metadata if isinstance(provider_metadata, dict) else None,
        "warnings": warnings if isinstance(warnings, list) else [],
    }
    if unexpected_ids:
        gateway_metadata["unexpected_answer_ids"] = unexpected_ids

    return {
        "answers": answers,
        "rounding": data.get("rounding", {}),
        "model": model_value,
        "usage": usage,
        "gateway_metadata": gateway_metadata,
    }


# ---------------------------------------------------------------------------
# HTTP 发送与重试
# ---------------------------------------------------------------------------


def _post_with_retries(
    client: httpx.Client,
    content: bytes,
    headers: dict[str, str],
    *,
    timeout: float,
    max_retries: int,
    api_key: str,
) -> tuple[httpx.Response, int]:
    attempts = 0
    while True:
        attempts += 1
        # 传输层异常先转成 VercelError 再在 except 块之外抛出：这样既没有 __cause__
        # 也没有 __context__ 指向 httpx 异常（其 .request.headers 含 Authorization）。
        failure: VercelError | None = None
        response: httpx.Response | None = None
        try:
            response = client.post(
                VERCEL_EVALUATION_URL,
                content=content,
                headers=headers,
                timeout=timeout,
                follow_redirects=False,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # 请求尚未到达服务端，重试是安全的。
            if attempts <= max_retries:
                _sleep(_backoff_delay(attempts, None))
                continue
            failure = VercelError(
                f"could not connect to Vercel AI Gateway after {attempts} attempt(s): {type(exc).__name__}",
                kind="network",
                attempts=attempts,
                retryable=True,
            )
        except httpx.TimeoutException as exc:
            # 读/写超时：服务端可能已处理该请求，不自动重复计费，直接报错。
            failure = VercelError(
                f"Vercel AI Gateway request timed out after {timeout}s ({type(exc).__name__})",
                kind="timeout",
                attempts=attempts,
            )
        except httpx.HTTPError as exc:
            failure = VercelError(
                f"network error talking to Vercel AI Gateway: {type(exc).__name__}: "
                f"{_short(_redact(str(exc), api_key))}",
                kind="network",
                attempts=attempts,
            )
        if failure is not None:
            raise failure
        assert response is not None

        status = response.status_code
        if 300 <= status < 400:
            raise VercelError(
                f"unexpected HTTP {status} redirect from Vercel AI Gateway; credentials are only "
                "sent to the fixed endpoint and redirects are never followed",
                kind="redirect",
                status_code=status,
                attempts=attempts,
            )
        if status in (401, 403):
            raise VercelError(
                f"Vercel AI Gateway rejected the credentials (HTTP {status}); "
                "check the AI Gateway API key (e.g. AI_GATEWAY_API_KEY)",
                kind="auth",
                status_code=status,
                attempts=attempts,
            )
        if status == 429 or 500 <= status < 600:
            if attempts <= max_retries:
                _sleep(_backoff_delay(attempts, response.headers.get("retry-after")))
                continue
            detail = _safe_error_detail(response, api_key)
            raise VercelError(
                f"Vercel AI Gateway returned HTTP {status} after {attempts} attempt(s); retries exhausted"
                + (f": {detail}" if detail else ""),
                kind="rate_limit" if status == 429 else "server",
                status_code=status,
                attempts=attempts,
                retryable=True,
            )
        if status >= 400:
            detail = _safe_error_detail(response, api_key)
            raise VercelError(
                f"Vercel AI Gateway rejected the request (HTTP {status})" + (f": {detail}" if detail else ""),
                kind="request",
                status_code=status,
                attempts=attempts,
            )
        return response, attempts


# ---------------------------------------------------------------------------
# 公共接口
# ---------------------------------------------------------------------------


def vercel_judge(
    api_key: str,
    state: object,
    questions: dict,
    *,
    model: str = GATEWAY_JEV_MODEL_ID,
    timeout: float = 60.0,
    max_retries: int = 3,
    client: httpx.Client | None = None,
    provider_options: dict | None = None,
) -> dict:
    """通过 Vercel AI Gateway evaluation 协议调用 Jev。

    参数
    ----
    api_key
        Vercel AI Gateway key，只放进 ``Authorization`` 头，不写入返回值或错误。
    state
        任意可 JSON 序列化对象（NaN/Infinity 会被拒绝）。
    questions
        TypeSafe 风格 ``{qid: {"type": "noul|boolean|choice|score", ...}}``。
    model
        ``typesafe-ai/jev``（默认）或直连风格 ``jev-1.13.0``（映射为别名，版本不保证）。
    timeout
        每次 HTTP 请求的超时秒数（注入 client 时同样按请求生效）。
    max_retries
        429/5xx/连接失败后的最大重试次数（总尝试次数 = ``max_retries + 1``）。
    client
        可注入 ``httpx.Client``（如 ``MockTransport``）；注入的不会被关闭，自建的一定关闭。
    provider_options
        可选，原样放进 body 的 ``providerOptions``。

    返回 ``{"answers", "model", "usage", "gateway_metadata"}``，见模块文档。
    """
    _validate_api_key(api_key)
    if not _is_finite_number(timeout) or timeout <= 0:
        raise VercelError("timeout must be a positive finite number of seconds", kind="request")
    if not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries < 0:
        raise VercelError("max_retries must be a non-negative integer", kind="request")

    gateway_model_id, alias_mapped = resolve_gateway_model(model)
    translated = translate_questions(questions)

    body: dict[str, Any] = {"state": state, "questions": translated}
    if provider_options is not None:
        if not isinstance(provider_options, dict):
            raise VercelError("provider_options must be a dict when provided", kind="request")
        body["providerOptions"] = provider_options
    serialize_error: str | None = None
    content = b""
    try:
        content = json.dumps(body, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        serialize_error = type(exc).__name__
    if serialize_error is not None:
        # 不链到原异常上：其消息可能包含 state 片段。
        raise VercelError(
            f"request body is not JSON-serializable ({serialize_error}); state/questions must be plain JSON",
            kind="request",
        )

    headers = build_headers(api_key, gateway_model_id)

    own_client = client is None
    active_client = _new_client(timeout) if own_client else client
    assert active_client is not None
    try:
        response, attempts = _post_with_retries(
            active_client,
            content,
            headers,
            timeout=timeout,
            max_retries=max_retries,
            api_key=api_key,
        )
    finally:
        if own_client:
            active_client.close()

    return _build_result(
        response,
        questions,
        requested_model=model,
        gateway_model_id=gateway_model_id,
        alias_mapped=alias_mapped,
        attempts=attempts,
    )
