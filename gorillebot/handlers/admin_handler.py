from typing import cast

from telegram import Message, Update
from telegram.ext import ContextTypes


class AdminHandler:
    def __init__(self, config, database, started_at, logger, review_handler=None) -> None:
        self.config = config
        self.database = database
        self.started_at = started_at
        self.logger = logger
        self.review_handler = review_handler

    def _is_admin(self, update: Update) -> bool:
        return bool(update.effective_user and update.effective_user.id in self.config.ADMIN_IDS)

    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        if update.message is None:
            return
        message = cast(Message, update.message)
        stats = await self.database.get_stats(1)
        await message.reply_text(f"Messages analysés : {stats['messages_analyzed']}\nSpam détecté : {stats['spam_detected']}\nBans : {stats['users_banned']}")

    async def health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        if update.message is None:
            return
        message = cast(Message, update.message)
        uptime = __import__("datetime").datetime.now() - self.started_at
        ai_state = "activée" if self.config.AI_ENABLED else "désactivée"
        review_count = len(self.review_handler.reviews) if self.review_handler else "N/A"
        await message.reply_text(
            f"DB : {'OK' if self.database.connection else 'KO'}\n"
            f"IA : {ai_state}\nReviews actives : {review_count}\nUptime : {uptime}"
        )

    async def errors(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        if update.message is None:
            return
        message = cast(Message, update.message)
        errors = context.application.bot_data.get("errors", [])
        await message.reply_text("\n".join(errors[-10:]) if errors else "Aucune erreur récente.")