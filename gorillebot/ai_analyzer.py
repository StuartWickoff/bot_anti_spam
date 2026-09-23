import asyncio
import hashlib
import io
import json
import logging
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Optional

from PIL import Image


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
        self._cache = OrderedDict()
        self._cache_max_size = 500
        self._lock = asyncio.Lock()
        self._request_count_date = None
        self._request_count = 0
        self._daily_limit = 1400
        if config.AI_ENABLED:
            import google.generativeai as genai
            genai.configure(api_key=config.GEMINI_API_KEY)
            self.model = genai.GenerativeModel(config.AI_MODEL, system_instruction=self.SYSTEM_PROMPT)

    @staticmethod
    def _extract_json(raw: str) -> dict:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.removeprefix("```").removeprefix("json").strip()
            raw = raw.removesuffix("```").strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end <= start:
            raise json.JSONDecodeError("Aucun objet JSON", raw, 0)
        parsed = json.loads(raw[start:end + 1])
        if not isinstance(parsed, dict):
            raise json.JSONDecodeError("La réponse JSON n'est pas un objet", raw, 0)
        return parsed

    @staticmethod
    def _prepare_image(image_bytes: bytes) -> tuple[bytes, str]:
        image = Image.open(io.BytesIO(image_bytes))
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGB")
        image.thumbnail((1024, 1024))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=85)
        return buffer.getvalue(), "image/jpeg"

    async def _request(self, content, cache_key: str) -> dict:
        cached = self._cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 3600:
            self._cache.move_to_end(cache_key)
            return cached[1]
        if not self.model:
            return {"is_spam": False, "confidence": 0.0, "reason": "IA désactivée", "status": "disabled"}
        async with self._lock:
            today = datetime.now(timezone.utc).date()
            if self._request_count_date != today:
                self._request_count_date = today
                self._request_count = 0
            if self._request_count >= self._daily_limit:
                return {"is_spam": False, "confidence": 0.0, "reason": "Quota journalier Gemini atteint", "status": "quota"}
            delay = self.config.MIN_REQUEST_INTERVAL - (time.monotonic() - self._last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            for attempt in range(3):
                response = None
                try:
                    self._request_count += 1
                    self._last_request = time.monotonic()
                    response = await asyncio.wait_for(
                        asyncio.to_thread(self.model.generate_content, content), timeout=30
                    )
                    raw = getattr(response, "text", "")
                    result = self._extract_json(raw)
                    result = {
                        "is_spam": bool(result.get("is_spam", False)),
                        "confidence": max(0.0, min(1.0, float(result.get("confidence", 0.0)))),
                        "reason": str(result.get("reason", "Aucune raison fournie")),
                        "status": "ok",
                    }
                    self._cache[cache_key] = (self._last_request, result)
                    self._cache.move_to_end(cache_key)
                    while len(self._cache) > self._cache_max_size:
                        self._cache.popitem(last=False)
                    return result
                except (json.JSONDecodeError, TypeError, ValueError):
                    self.logger.warning("Réponse Gemini non JSON : %s", getattr(response, "text", ""))
                    return {"is_spam": False, "confidence": 0.0, "reason": "Erreur de parsing IA", "status": "parse_error"}
                except asyncio.TimeoutError:
                    self.logger.error("Timeout Gemini à la tentative %s", attempt + 1)
                    if attempt == 2:
                        return {"is_spam": False, "confidence": 0.0, "reason": "Timeout API IA", "status": "timeout"}
                    await asyncio.sleep(2 ** attempt)
                except Exception as exc:
                    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
                    self.logger.error("Erreur Gemini attempt=%s status=%s type=%s error=%s", attempt + 1, status, type(exc).__name__, exc)
                    if "429" in str(exc) or "resource exhausted" in str(exc).lower():
                        self._request_count = self._daily_limit
                        return {"is_spam": False, "confidence": 0.0, "reason": "Quota Gemini atteint", "status": "quota"}
                    if attempt == 2:
                        return {"is_spam": False, "confidence": 0.0, "reason": "Erreur API IA", "status": "api_error"}
                    await asyncio.sleep(2 ** attempt)

    async def analyze_text(self, text: str) -> dict:
        return await self._request(text, "text:" + text)

    async def analyze_image(self, image_bytes: bytes, caption: str = "") -> dict:
        try:
            prepared_image, mime_type = await asyncio.to_thread(self._prepare_image, bytes(image_bytes))
        except Exception:
            self.logger.exception("Impossible de préparer l'image pour Gemini")
            return {"is_spam": False, "confidence": 0.0, "reason": "Image illisible", "status": "api_error"}
        content = [caption or "Analyse cette image.", {"mime_type": mime_type, "data": prepared_image}]
        cache_key = "image:" + hashlib.sha256(prepared_image).hexdigest()
        return await self._request(content, cache_key)

    async def analyze(self, text: str, photo=None) -> dict:
        return await self.analyze_image(photo, text) if photo else await self.analyze_text(text)