import os
from pathlib import Path
from typing import List

from dotenv import load_dotenv


load_dotenv(Path(__file__).parent / ".env")


class Config:
    def __init__(self) -> None:
        self.BOT_TOKEN = os.getenv("BOT_TOKEN", "")
        self.ADMIN_IDS = self._parse_int_list(os.getenv("ADMIN_IDS", ""))

        self.GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
        self.AI_ENABLED = os.getenv("AI_ENABLED", "true").lower() == "true"
        self.AI_MODEL = os.getenv("AI_MODEL", "gemini-2.5-flash")
        self.AI_CONFIDENCE_THRESHOLD = float(os.getenv("AI_CONFIDENCE_THRESHOLD", "0.85"))
        self.MIN_REQUEST_INTERVAL = int(os.getenv("MIN_REQUEST_INTERVAL", "4"))

        self.DB_PATH = os.getenv("DB_PATH", "data/gorillebot.db")
        self.LOG_DIR = os.getenv("LOG_DIR", "logs")
        self.LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
        self.LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "90"))

        self.REVIEW_ENABLED = os.getenv("REVIEW_ENABLED", "true").lower() == "true"
        self.VOTES_TO_BAN = int(os.getenv("VOTES_TO_BAN", "2"))
        self.VOTES_TO_SAFE = int(os.getenv("VOTES_TO_SAFE", "2"))
        self.REVIEW_TIMEOUT_HOURS = int(os.getenv("REVIEW_TIMEOUT_HOURS", "24"))

        self._validate()

    @staticmethod
    def _parse_int_list(value: str) -> List[int]:
        result = []
        for item in value.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                result.append(int(item))
            except ValueError as exc:
                raise ValueError(f"ADMIN_IDS invalide : '{item}' n'est pas un entier") from exc
        return result

    def _validate(self) -> None:
        errors = []
        if not self.BOT_TOKEN or self.BOT_TOKEN == "your_bot_token_here":
            errors.append("BOT_TOKEN manquant dans .env")
        if self.AI_ENABLED and (not self.GEMINI_API_KEY or self.GEMINI_API_KEY == "your_gemini_api_key_here"):
            errors.append("GEMINI_API_KEY manquant alors que AI_ENABLED=true")
        if self.VOTES_TO_BAN <= 0:
            errors.append("VOTES_TO_BAN doit être supérieur à 0")
        if self.VOTES_TO_SAFE <= 0:
            errors.append("VOTES_TO_SAFE doit être supérieur à 0")
        if self.REVIEW_TIMEOUT_HOURS <= 0:
            errors.append("REVIEW_TIMEOUT_HOURS doit être supérieur à 0")
        if self.MIN_REQUEST_INTERVAL < 0:
            errors.append("MIN_REQUEST_INTERVAL ne peut pas être négatif")
        if errors:
            raise ValueError("Configuration invalide : " + "; ".join(errors))


config = Config()