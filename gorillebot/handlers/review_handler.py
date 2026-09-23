from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Set

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes


@dataclass
class Review:
    chat_id: int
    target_user_id: int
    target_username: str
    deadline: datetime
    ai_reason: str
    ai_confidence: float
    content: str
    ban_votes: Set[int] = field(default_factory=set)
    safe_votes: Set[int] = field(default_factory=set)


class ReviewHandler:
    def __init__(self, config, logger, database) -> None:
        self.config = config
        self.logger = logger
        self.database = database
        self.reviews: Dict[int, Review] = {}

    async def load_active_reviews(self) -> None:
        for item in await self.database.get_active_reviews():
            review = Review(
                chat_id=item["chat_id"], target_user_id=item["target_user_id"],
                target_username=item["target_username"] or "sans nom",
                deadline=datetime.fromisoformat(item["deadline"]),
                ai_reason=item["ai_reason"] or "Aucune raison fournie",
                ai_confidence=float(item["ai_confidence"] or 0.0),
                content=item["content"] or "",
            )
            for vote in item["votes"]:
                (review.ban_votes if vote["vote"] == "ban" else review.safe_votes).add(vote["voter_id"])
            self.reviews[item["review_message_id"]] = review

    async def send_review_message(self, context, chat_id: int, target_user_id: int,
                                  target_username: str, message_content: str,
                                  ai_reason: str, ai_confidence: float) -> int:
        review = Review(chat_id, target_user_id, target_username or "sans nom",
                        datetime.now(timezone.utc) + timedelta(hours=self.config.REVIEW_TIMEOUT_HOURS),
                        ai_reason, ai_confidence, message_content[:300])
        sent = await context.bot.send_message(chat_id, self._display(review), reply_markup=self._keyboard(review))
        self.reviews[sent.message_id] = review
        await self.database.create_review(sent.message_id, chat_id, target_user_id, review.target_username,
                          review.content, ai_reason, ai_confidence, review.deadline)
        return sent.message_id

    def _keyboard(self, review: Review) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton(
            f"🔨 Bannir ({len(review.ban_votes)}/{self.config.VOTES_TO_BAN})", callback_data=f"review_ban_{review.target_user_id}"),
            InlineKeyboardButton(f"✅ Innocenter ({len(review.safe_votes)}/{self.config.VOTES_TO_SAFE})", callback_data=f"review_safe_{review.target_user_id}")]])

    def _display(self, review: Review) -> str:
        return ("⚠️ Message suspect détecté\n\n"
                f"👤 Utilisateur : {review.target_username} (ID: {review.target_user_id})\n"
                f"🤖 Analyse IA ({review.ai_confidence:.0%}) : {review.ai_reason}\n\n"
                f"📝 Contenu :\n> {review.content.replace(chr(10), ' ')[:300]}\n\n"
                f"⏳ Timeout : {self.config.REVIEW_TIMEOUT_HOURS}h | Votes requis : {self.config.VOTES_TO_BAN}\n"
                "Le concerné ne peut pas voter.")

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        review = self.reviews.get(query.message.message_id)
        voter = query.from_user.id
        if not review or voter == review.target_user_id:
            return
        is_admin = voter in self.config.ADMIN_IDS
        vote = query.data.split("_")[1]
        if voter in review.ban_votes or voter in review.safe_votes:
            return
        if not await self.database.add_review_vote(query.message.message_id, voter, vote):
            return
        if vote == "ban":
            review.ban_votes.add(voter)
        else:
            review.safe_votes.add(voter)
        decided = is_admin or len(review.ban_votes) >= self.config.VOTES_TO_BAN or len(review.safe_votes) >= self.config.VOTES_TO_SAFE
        if not decided:
            await query.edit_message_text(self._display(review), reply_markup=self._keyboard(review))
            return
        self.reviews.pop(query.message.message_id, None)
        if vote == "ban" and (is_admin or len(review.ban_votes) >= self.config.VOTES_TO_BAN):
            banned = await self._execute_ban(context, review.chat_id, review.target_user_id)
            resolution = "ban" if banned else "ban_failed"
            text = "🔨 Décision : utilisateur banni." if banned else "❌ Échec du bannissement. Un admin doit intervenir manuellement."
            await self.database.resolve_review(query.message.message_id, resolution)
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([]))
        else:
            await self.database.resolve_review(query.message.message_id, "safe")
            await query.edit_message_text("✅ Décision : message innocenté.", reply_markup=InlineKeyboardMarkup([]))

    async def check_expired_reviews(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        now = datetime.now(timezone.utc)
        expired = [message_id for message_id, review in self.reviews.items() if review.deadline <= now]
        for message_id in expired:
            review = self.reviews.pop(message_id)
            try:
                await context.bot.edit_message_text("⏰ Vote expiré", review.chat_id, message_id, reply_markup=InlineKeyboardMarkup([]))
                await self.database.resolve_review(message_id, "expired")
            except Exception:
                self.logger.exception("Impossible de fermer la review %s", message_id)

    async def _execute_ban(self, context, chat_id: int, user_id: int) -> bool:
        try:
            await context.bot.ban_chat_member(chat_id, user_id)
            return True
        except Exception:
            self.logger.exception("Échec du bannissement de %s", user_id)
            return False