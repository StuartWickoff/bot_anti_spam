import re
import unicodedata
from typing import List, Tuple


class SpamDetector:
    def __init__(self) -> None:
        self.instant_ban_phrases = [
            r"ami en ligne.*recommande", r"pensais.*etait.*arnaque", r"tenter.*coup",
            r"realise.*benefice", r"partager.*experience", r"rejoignez.*chaine",
            r"en seulement.*heures", r"tout fonctionne parfaitement", r"retrouvez.*chaine",
            r"decide.*tenter.*chance", r"petit capital.*depart", r"j ai recolte",
            r"c est vraiment passionnant", r"signaux gratuits", r"robot trading|ia trading|copy trading",
            r"lien en prive|contacte.*en prive", r"ci dessous.*chaine", r"pour plus d.*infos",
            r"resultat garanti", r"methode infaillible",
        ]
        self.spam_keywords = {
            "money_talk": [r"investi", r"benefice", r"profit", r"gagne", r"recolte", r"capital"],
            "scam_words": [r"recommande", r"arnaque", r"tenter", r"experience", r"ami en ligne", r"plateforme"],
            "call_to_action": [r"rejoignez", r"cliquez", r"chaine", r"canal", r"retrouvez", r"plus dinfos"],
        }
        self.money_pattern = re.compile(
            r"(?:investi|mis|depense|capital|investissement|mise|depart).*?"
            r"(\d{1,4}(?:[.,\s]?\d{3})*(?:[.,]\d{1,2})?)\s*(?:€|\$|euros?|dollars?|eur|usd)?"
            r".*?(?:→|a|à|-|vers|et|devient|gagne|profit|benefice|recolte|obtenu).*?"
            r"(\d{1,4}(?:[.,\s]?\d{3})*(?:[.,]\d{1,2})?)\s*(?:€|\$|euros?|dollars?|eur|usd)?",
            re.IGNORECASE | re.DOTALL,
        )
        self.gain_claim_pattern = re.compile(
            r"(?:"
            r"(?:gagne|profit|benefice|recolte|obtenu).*?"
            r"\d{1,4}(?:[.,\s]?\d{3})*(?:[.,]\d{1,2})?\s*(?:€|\$|euros?|dollars?|eur|usd)"
            r"(?:.*?(?:benefice|profit|gain))?"
            r"|"
            r"\d{1,4}(?:[.,\s]?\d{3})*(?:[.,]\d{1,2})?\s*(?:€|\$|euros?|dollars?|eur|usd)"
            r".*?(?:benefice|profit|gain|gagne|recolte|obtenu)"
            r")",
            re.IGNORECASE | re.DOTALL,
        )
        self.emoji_spam = re.compile(r"(👇|⬇️|👈|👉|⤵){3,}")
        self.suspicious_links = re.compile(r"t\.me/[^\s]+")
        self.trader_mention = re.compile(r"@[A-Z_]+(?:FX|TRADER|TRADE|CRYPTO|SIGNAL|INVEST)", re.IGNORECASE)
        self.duplicate_rejoin = re.compile(r"(rejoignez.{0,30}chaine.{0,30}){2,}", re.IGNORECASE)
        self.whatsapp_pattern = re.compile(r"\+?\d{10,15}|whatsapp|telegram.*contact", re.IGNORECASE)

    @staticmethod
    def normalize_text(text: str) -> str:
        text = unicodedata.normalize("NFD", text.lower())
        text = "".join(char for char in text if unicodedata.category(char) != "Mn")
        return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text)).strip()

    @staticmethod
    def _normalize_for_money(text: str) -> str:
        text = unicodedata.normalize("NFD", text.lower())
        return "".join(char for char in text if unicodedata.category(char) != "Mn")

    @staticmethod
    def _parse_money(value: str) -> float:
        try:
            return float(re.sub(r"[^\d.,]", "", value).replace(" ", "").replace(",", "."))
        except ValueError:
            return 0.0

    def is_spam(self, text: str) -> Tuple[bool, List[str], int]:
        normalized = self.normalize_text(text)
        for pattern in self.instant_ban_phrases:
            match = re.search(pattern, normalized)
            if match:
                return True, [f"Phrase signature: {match.group()[:40]}"], 95

        reasons: List[str] = []
        category_count = 0
        for category, patterns in self.spam_keywords.items():
            if any(re.search(pattern, normalized) for pattern in patterns):
                category_count += 1
                reasons.append(f"Catégorie {category}")

        gain_match = self.money_pattern.search(self._normalize_for_money(text))
        unrealistic = False
        if gain_match:
            invested = self._parse_money(gain_match.group(1))
            profit = self._parse_money(gain_match.group(2))
            unrealistic = invested > 0 and profit > invested * 4
            if unrealistic:
                reasons.append(f"Gains irréalistes x{profit / invested:.1f}")
        elif self.gain_claim_pattern.search(self._normalize_for_money(text)):
            unrealistic = True
            reasons.append("Gain financier suspect")

        structural_score = 0
        if self.emoji_spam.search(text):
            reasons.append("Emojis spam")
            structural_score = max(structural_score, 60)
        link_count = len(self.suspicious_links.findall(text))
        if link_count:
            reasons.append("Lien t.me suspect")
            structural_score = max(structural_score, 80 if link_count > 1 else 50)
        if self.trader_mention.search(text):
            reasons.append("Mention trader suspect")
            structural_score = max(structural_score, 60)
        if self.duplicate_rejoin.search(normalized):
            reasons.append("Appel à rejoindre répété")
            structural_score = max(structural_score, 80)
        if self.whatsapp_pattern.search(text):
            reasons.append("Contact privé suspect")
            structural_score = max(structural_score, 70)

        if category_count >= 3:
            score = 80
        elif unrealistic:
            score = 75
        elif category_count >= 2 and structural_score >= 40:
            score = 60
        elif category_count == 1:
            score = 30
        else:
            score = structural_score
        return score > 0, reasons, min(score, 100)