import asyncio
import json
import logging
import time
from typing import Optional


class AIAnalyzer:
    SYSTEM_PROMPT = (
        "Tu es un modérateur expert pour un groupe Telegram de trading francophone. "
        "Analyse le contenu et détecte les arnaques : gains irréalistes, faux témoignages, "
        "captures MT4/MT5 truquées, rabatage privé et signaux gratuits, dans toute langue. "
        "Réponds UNIQUEMENT en JSON strict : {\"is_spam\": true/false, "
        "\"confidence\": 0.0, \"reason\": \"explication courte\"}."
    )

    def __init__(self, config, logger: Optional[logging.Logger] = None) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self.model = None
        self._last_request = 0.0
        self._cache = {}
        self._lock = asyncio.Lock()
        if config.AI_ENABLED:
            import google.generativeai as genai
            genai.configure(api_key=config.GEMINI_API_KEY)
            self.model = genai.GenerativeModel(config.AI_MODEL, system_instruction=self.SYSTEM_PROMPT)

    async def _request(self, content, cache_key: str) -> dict:
        cached = self._cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 3600:
            return cached[1]
        if not self.model:
            return {"is_spam": False, "confidence": 0.0, "reason": "IA désactivée"}
        async with self._lock:
            delay = self.config.MIN_REQUEST_INTERVAL - (time.monotonic() - self._last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            for attempt in range(3):
                try:
                    response = await asyncio.to_thread(self.model.generate_content, content)
                    result = json.loads(response.text.strip())
                    result = {"is_spam": bool(result["is_spam"]), "confidence": float(result["confidence"]), "reason": str(result["reason"])}
                    self._last_request = time.monotonic()
                    self._cache[cache_key] = (self._last_request, result)
                    return result
                except json.JSONDecodeError:
                    return {"is_spam": False, "confidence": 0.0, "reason": "Erreur de parsing IA"}
                except Exception as exc:
                    self._last_request = time.monotonic()
                    if attempt == 2:
                        self.logger.exception("Erreur Gemini: %s", exc)
                        return {"is_spam": False, "confidence": 0.0, "reason": "Erreur API IA"}
                    await asyncio.sleep(2 ** attempt)

    async def analyze_text(self, text: str) -> dict:
        return await self._request(text, "text:" + text)

    async def analyze_image(self, image_bytes: bytes, caption: str = "") -> dict:
        content = [caption or "Analyse cette image.", {"mime_type": "image/jpeg", "data": image_bytes}]
        return await self._request(content, "image:" + str(hash(image_bytes)))

    async def analyze(self, text: str, photo=None) -> dict:
        return await self.analyze_image(photo, text) if photo else await self.analyze_text(text)