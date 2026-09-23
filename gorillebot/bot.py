import logging
import logging.handlers
from collections import deque
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from gorillebot.ai_analyzer import AIAnalyzer
from gorillebot.database import MessageDatabase
from gorillebot.handlers.admin_handler import AdminHandler
from gorillebot.handlers.review_handler import ReviewHandler
from gorillebot.spam_detector import SpamDetector


class TelegramAntiSpamBot:
    def __init__(self, config) -> None:
        self.config = config
        self.logger = self._configure_logging()
        self.database = MessageDatabase(config.DB_PATH, self.logger)
        self.detector = SpamDetector()
        self.ai_analyzer = AIAnalyzer(config, self.logger)
        self.review_handler = ReviewHandler(config, self.logger)
        self.processed_ids = deque(maxlen=1000)
        self.started_at = datetime.now()

    def _configure_logging(self):
        Path(self.config.LOG_DIR).mkdir(parents=True, exist_ok=True)
        logging.basicConfig(level=getattr(logging, self.config.LOG_LEVEL.upper(), logging.INFO), format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        logger = logging.getLogger("GorilleBot")
        handler = logging.handlers.TimedRotatingFileHandler(Path(self.config.LOG_DIR) / "gorillebot.log", when="midnight", backupCount=self.config.LOG_RETENTION_DAYS, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
        logger.addHandler(handler)
        return logger

    async def post_init(self, application: Application) -> None:
        await self.database.connect()
        await self.database.init_db()

    async def post_shutdown(self, application: Application) -> None:
        await self.database.close()

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        if not message or not user or not chat or message.message_id in self.processed_ids:
            return
        self.processed_ids.append(message.message_id)
        if user.id in self.config.ADMIN_IDS:
            return
        text = message.text or message.caption or ""
        is_spam, reasons, score = self.detector.is_spam(text)
        action = "none"
        ai_result = None
        if score >= 80:
            await self._process_spam(message, user, chat, reasons, context)
            action = "ban"
        elif self.config.AI_ENABLED and (text or message.photo):
            image = None
            if message.photo:
                image_file = await context.bot.get_file(message.photo[-1].file_id)
                image = await image_file.download_as_bytearray()
            ai_result = await self.ai_analyzer.analyze(text, image)
            if ai_result["is_spam"] and ai_result["confidence"] >= self.config.AI_CONFIDENCE_THRESHOLD:
                await self._process_spam(message, user, chat, [ai_result["reason"]], context)
                action = "ban"
            elif ai_result["is_spam"] and ai_result["confidence"] >= 0.6 and self.config.REVIEW_ENABLED:
                await self._send_to_review(message, user, chat, ai_result, context)
                action = "review"
        await self.database.log_message(user.id, user.username or "", user.first_name or "", chat.id, text, bool(is_spam or (ai_result and ai_result.get("is_spam"))), reasons, action)

    async def _process_spam(self, message, user, chat, reasons, context) -> None:
        try:
            await message.delete()
            await context.bot.ban_chat_member(chat.id, user.id)
            await self.database.log_ban(user.id, user.username or "", "; ".join(reasons))
        except Exception:
            self.logger.exception("Erreur lors du traitement du spam de %s", user.id)

    async def _send_to_review(self, message, user, chat, ai_result, context) -> None:
        try:
            await message.delete()
            await self.review_handler.send_review_message(context, chat.id, user.id, user.username or user.first_name, message.text or message.caption or "", ai_result["reason"], ai_result["confidence"])
        except Exception:
            self.logger.exception("Erreur lors de la création de la review")

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.exception("Erreur Telegram", exc_info=context.error)
        errors = context.application.bot_data.setdefault("errors", [])
        errors.append(str(context.error))
        del errors[:-10]

    def run(self) -> None:
        application = (ApplicationBuilder().token(self.config.BOT_TOKEN).post_init(self.post_init).post_shutdown(self.post_shutdown).build())
        admin = AdminHandler(self.config, self.database, self.started_at, self.logger)
        application.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, self.handle_message))
        application.add_handler(CallbackQueryHandler(self.review_handler.handle_callback, pattern=r"^review_(ban|safe)_\d+$"))
        application.add_handler(CommandHandler("stats", admin.stats))
        application.add_handler(CommandHandler("health", admin.health))
        application.add_handler(CommandHandler("errors", admin.errors))
        application.add_error_handler(self.error_handler)
        if self.config.REVIEW_ENABLED and application.job_queue:
            application.job_queue.run_repeating(self.review_handler.check_expired_reviews, interval=3600, first=3600)
        application.run_polling(allowed_updates=Update.ALL_TYPES)