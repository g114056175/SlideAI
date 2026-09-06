import time
import os
import asyncio
import base64
import mimetypes
import requests
import threading
from backend.app.services.utility.text import remove_markdown

def _configured_llm_concurrency() -> int:
	try:
		return max(1, int(os.getenv("LLM_MAX_CONCURRENCY", "3")))
	except (TypeError, ValueError):
		return 3


# This is process-global rather than request-local: five users do not each get
# three independent API slots. The production launcher intentionally runs one
# backend process, so the limit is shared by every active request.
_LLM_REQUEST_SLOTS = threading.BoundedSemaphore(_configured_llm_concurrency())


def _with_llm_request_slot(callable_):
	with _LLM_REQUEST_SLOTS:
		return callable_()

def get_google_generative_model_name() -> str:
	return os.getenv("GOOGLE_GENERATIVE_MODEL", os.getenv("GEMINI_MODEL", "gemini-2.5-flash")).strip() or "gemini-2.5-flash"


def get_google_generative_endpoint(model_name: str) -> str:
	endpoint = os.getenv("GOOGLE_GENERATIVE_ENDPOINT", "").strip()
	if endpoint:
		if "{model}" in endpoint:
			return endpoint.format(model=model_name)
		if endpoint.rstrip("/").endswith(":generateContent"):
			return endpoint
		return f"{endpoint.rstrip('/')}/models/{model_name}:generateContent"
	return f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"


def get_openai_model_name() -> str:
	return os.getenv("OPENAI_MODEL", os.getenv("EXTERNAL_LLM_MODEL", "gpt-4.1-mini")).strip() or "gpt-4.1-mini"


def get_anthropic_model_name() -> str:
	return os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest").strip() or "claude-3-5-sonnet-latest"


def get_openrouter_model_name() -> str:
	return os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1-mini").strip() or "openai/gpt-4.1-mini"


def get_xai_model_name() -> str:
	return os.getenv("XAI_MODEL", "grok-3-mini").strip() or "grok-3-mini"


def get_groq_model_name() -> str:
	return os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip() or "llama-3.3-70b-versatile"


def get_custom_llm_model_name() -> str:
	return os.getenv("CUSTOM_LLM_MODEL", os.getenv("EXTERNAL_LLM_MODEL", "")).strip()


def get_llm_model_name(provider: str) -> str:
	if provider == "google":
		return get_google_generative_model_name()
	if provider == "anthropic":
		return get_anthropic_model_name()
	if provider == "openrouter":
		return get_openrouter_model_name()
	if provider == "xai":
		return get_xai_model_name()
	if provider == "groq":
		return get_groq_model_name()
	if provider == "custom":
		return get_custom_llm_model_name()
	return get_openai_model_name()


def get_chat_completion_endpoint(provider: str) -> str:
	defaults = {
		"openai": "https://api.openai.com/v1/chat/completions",
		"openrouter": "https://openrouter.ai/api/v1/chat/completions",
		"xai": "https://api.x.ai/v1/chat/completions",
		"groq": "https://api.groq.com/openai/v1/chat/completions",
	}
	override_names = {
		"openai": ("OPENAI_ENDPOINT", "EXTERNAL_LLM_ENDPOINT"),
		"openrouter": ("OPENROUTER_ENDPOINT",),
		"xai": ("XAI_ENDPOINT",),
		"groq": ("GROQ_ENDPOINT",),
		"custom": ("CUSTOM_LLM_ENDPOINT", "EXTERNAL_LLM_ENDPOINT"),
	}
	for env_name in override_names.get(provider, ()):
		value = os.getenv(env_name, "").strip()
		if value:
			return value
	if provider == "custom":
		return ""
	return defaults.get(provider, defaults["openai"])


def get_anthropic_endpoint() -> str:
	return os.getenv("ANTHROPIC_ENDPOINT", "https://api.anthropic.com/v1/messages").strip()


def get_llm_api_key(provider: str | None = None) -> str:
	"""Return the credential belonging to the selected provider.

	The legacy ``api_key`` variable remains a fallback, but it must not override
	a provider-specific credential when users switch vendors by editing .env.
	"""
	selected = (provider or os.getenv("LLM_PROVIDER", "")).strip().lower()
	selected = {
		"gemini": "google",
		"claude": "anthropic",
		"openai-compatible": "custom",
		"openai_compatible": "custom",
	}.get(selected, selected)
	provider_vars = {
		"google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
		"openai": ("OPENAI_API_KEY",),
		"anthropic": ("ANTHROPIC_API_KEY",),
		"openrouter": ("OPENROUTER_API_KEY",),
		"xai": ("XAI_API_KEY",),
		"groq": ("GROQ_API_KEY",),
		"custom": ("CUSTOM_LLM_API_KEY", "EXTERNAL_LLM_API_KEY"),
	}
	if selected in provider_vars:
		for env_name in (*provider_vars[selected], "api_key"):
			value = os.getenv(env_name, "").strip()
			if value:
				return value
		return ""
	for env_name in (
		"api_key", "GOOGLE_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY",
		"ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "XAI_API_KEY",
		"GROQ_API_KEY", "CUSTOM_LLM_API_KEY", "EXTERNAL_LLM_API_KEY",
	):
		value = os.getenv(env_name, "").strip()
		if value:
			return value
	return ""


def get_configured_llm_provider(api_key: str | None = None) -> str:
	explicit = os.getenv("LLM_PROVIDER", "").strip().lower()
	aliases = {
		"gemini": "google",
		"claude": "anthropic",
		"openai-compatible": "custom",
		"openai_compatible": "custom",
	}
	explicit = aliases.get(explicit, explicit)
	if explicit in {"google", "openai", "anthropic", "openrouter", "xai", "groq", "custom"}:
		return explicit
	return infer_llm_provider_from_key(api_key)


def llm_is_configured() -> bool:
	provider = get_configured_llm_provider()
	if provider == "custom":
		return bool(get_chat_completion_endpoint("custom") and get_custom_llm_model_name())
	return provider != "missing" and bool(get_llm_api_key())


def infer_llm_provider_from_key(api_key: str | None = None) -> str:
	key = (api_key or get_llm_api_key()).strip()
	if not key:
		return "missing"
	lower_key = key.lower()
	if lower_key.startswith("sk-ant"):
		return "anthropic"
	if lower_key.startswith("sk-or-v1"):
		return "openrouter"
	if lower_key.startswith("xai-"):
		return "xai"
	if lower_key.startswith("gsk_"):
		return "groq"
	if lower_key.startswith("sk"):
		return "openai"
	if key.startswith("AI") or key.startswith("AQ."):
		return "google"
	return "unknown"


def get_llm_config_summary() -> dict:
	api_key = get_llm_api_key()
	provider = get_configured_llm_provider(api_key)
	return {
		"provider": provider,
		"model": get_llm_model_name(provider),
		"endpoint": (
			"https://generativelanguage.googleapis.com"
			if provider == "google"
			else get_anthropic_endpoint()
			if provider == "anthropic"
			else get_chat_completion_endpoint(provider)
		),
		"google_model": get_google_generative_model_name(),
		"google_endpoint": "https://generativelanguage.googleapis.com",
		"openai_endpoint": get_chat_completion_endpoint("openai"),
		"openai_model": get_openai_model_name(),
		"anthropic_endpoint": get_anthropic_endpoint(),
		"anthropic_model": get_anthropic_model_name(),
		"openrouter_endpoint": get_chat_completion_endpoint("openrouter"),
		"openrouter_model": get_openrouter_model_name(),
		"custom_endpoint": get_chat_completion_endpoint("custom"),
		"custom_model": get_custom_llm_model_name(),
		"has_key": bool(api_key),
		"configured": llm_is_configured(),
	}


async def generate_presentation_scripts_from_images(
    image_paths=None, api_key=None, language=None, model_name_override=None,
):
    """One image per request, using the configured vision-capable provider.

    Errors propagate instead of silently replacing scripts with empty text.
    The existing process-wide request slots also apply to image requests.
    """
    if image_paths is None:
        raise ValueError("image_paths can't be None")
    api_key = (api_key or get_llm_api_key()).strip()
    provider = get_configured_llm_provider(api_key)
    model = model_name_override or get_llm_model_name(provider)
    if provider == "missing" or (not api_key and provider != "custom"):
        raise ValueError("LLM api_key is empty")
    is_en = str(language or "zh").lower().startswith("en")
    prompt = (
        "Read this slide image and write one complete spoken English paragraph. "
        "Keep title/outline slides brief; explain visible diagrams, methods and results "
        "in proportion to their information density. Preserve technical names. "
        "Do not invent values or unreadable details. No greetings, bullets or preamble."
        if is_en else
        "請根據這一頁投影片圖片撰寫單一完整段落的繁體中文口語講稿。"
        "標題、目錄頁簡短帶過；方法、圖表與實驗結果依資訊密度說明。"
        "保留專有名詞原文，不杜撰數值或看不清楚的內容，不要開場問候、條列或前言。"
    )
    timeout = int(os.getenv("EXTERNAL_LLM_TIMEOUT_SEC", "90"))

    def call_one(path):
        mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
        with open(path, "rb") as handle:
            encoded = base64.b64encode(handle.read()).decode("ascii")
        headers = {"Content-Type": "application/json"}
        if provider == "google":
            endpoint = get_google_generative_endpoint(model)
            headers["x-goog-api-key"] = api_key
            payload = {"contents": [{"role": "user", "parts": [
                {"text": prompt}, {"inline_data": {"mime_type": mime, "data": encoded}},
            ]}], "generationConfig": {"temperature": 0.75}}
        elif provider == "anthropic":
            endpoint = get_anthropic_endpoint()
            headers.update({"x-api-key": api_key, "anthropic-version": os.getenv("ANTHROPIC_VERSION", "2023-06-01")})
            payload = {"model": model, "max_tokens": 2048, "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": encoded}},
                {"type": "text", "text": prompt},
            ]}]}
        else:
            endpoint = get_chat_completion_endpoint(provider)
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            payload = {"model": model, "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
            ]}]}
        for attempt in range(3):
            try:
                response = _with_llm_request_slot(lambda: requests.post(
                    endpoint, headers=headers, json=payload, timeout=timeout,
                ))
                response.raise_for_status()
                break
            except requests.RequestException as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                transient = isinstance(exc, (requests.Timeout, requests.ConnectionError)) or status == 429 or (status is not None and status >= 500)
                if transient and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(
                    f"{provider} 圖片講稿請求失敗" + (f"（HTTP {status}）" if status else "")
                    + "，請確認模型支援圖片輸入及服務可用。"
                ) from exc
        data = response.json()
        if provider == "google":
            candidate = (data.get("candidates") or [{}])[0]
            if candidate.get("finishReason") == "MAX_TOKENS":
                raise RuntimeError("講稿輸出被截斷，請調整模型輸出限制後重試。")
            text = "".join(part.get("text", "") for part in (candidate.get("content") or {}).get("parts", []) if not part.get("thought"))
        elif provider == "anthropic":
            if data.get("stop_reason") == "max_tokens":
                raise RuntimeError("講稿輸出被截斷，請調整模型輸出限制後重試。")
            text = "".join(part.get("text", "") for part in data.get("content", []) if part.get("type") == "text")
        else:
            choice = (data.get("choices") or [{}])[0]
            if choice.get("finish_reason") == "length":
                raise RuntimeError("講稿輸出被截斷，請調整模型輸出限制後重試。")
            text = (choice.get("message") or {}).get("content") or ""
        text = remove_markdown(str(text)).strip()
        if not text:
            raise RuntimeError("模型未回傳有效講稿；原講稿已保留。")
        return text

    semaphore = asyncio.Semaphore(_configured_llm_concurrency())

    async def guarded(path):
        async with semaphore:
            return await asyncio.to_thread(call_one, path)

    # Wait for all workers before the caller removes temporary page images.
    results = await asyncio.gather(*(guarded(path) for path in image_paths), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results
