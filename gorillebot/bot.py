import fcntl
import logging
import logging.handlers
import os
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
        self.review_handler = ReviewHandler(config, self.logger, self.database)
        self.processed_ids = deque(maxlen=1000)
        self.started_at = datetime.now()
        self._lock_file = None

    def _acquire_instance_lock(self) -> None:
        lock_path = Path(self.config.DB_PATH).parent / "gorillebot.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = lock_path.open("w")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            self._lock_file = None
            raise RuntimeError("Une autre instance de GorilleBot tourne déjà.") from exc
        self._lock_file.write(str(os.getpid()))
        self._lock_file.flush()

    def _release_instance_lock(self) -> None:
        if self._lock_file:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None

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
        await self.review_handler.load_active_reviews()

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
            banned = await self._process_spam(message, user, chat, reasons, context)
            action = "ban" if banned else "ban_failed"
        elif message.video or message.document or message.animation or message.sticker:
            ai_result = {"is_spam": True, "confidence": 0.6, "reason": "Média non analysable automatiquement"}
            if self.config.REVIEW_ENABLED:
                reviewed = await self._send_to_review(message, user, chat, ai_result, context)
                action = "review" if reviewed else "review_failed"
            else:
                try:
                    await message.delete()
                    action = "unsupported_media_deleted"
                except Exception:
                    self.logger.exception("Échec suppression du média non analysable")
                    action = "unsupported_media_failed"
        elif self.config.AI_ENABLED and self._should_use_ai(text, message):
            image = None
            if message.photo:
                try:
                    image_file = await context.bot.get_file(message.photo[-1].file_id)
                    image = await image_file.download_as_bytearray()
                except Exception:
                    self.logger.exception("Échec téléchargement de l'image")
                    action = "image_download_failed"
            ai_result = await self.ai_analyzer.analyze(text, image)
            if ai_result["status"] != "ok":
                reasons.append(ai_result["reason"])
                if self.config.REVIEW_ENABLED and (message.photo or score > 0 or self._should_use_ai(text, message)):
                    fallback = {
                        "is_spam": True,
                        "confidence": 0.6,
                        "reason": f"Vérification humaine requise : IA indisponible ({ai_result['reason']})",
                    }
                    reviewed = await self._send_to_review(message, user, chat, fallback, context)
                    action = "review" if reviewed else "review_failed"
                else:
                    action = "ai_unavailable"
            elif ai_result["is_spam"] and ai_result["confidence"] >= self.config.AI_CONFIDENCE_THRESHOLD:
                banned = await self._process_spam(message, user, chat, [ai_result["reason"]], context)
                action = "ban" if banned else "ban_failed"
            elif ai_result["is_spam"] and ai_result["confidence"] >= 0.6 and self.config.REVIEW_ENABLED:
                reviewed = await self._send_to_review(message, user, chat, ai_result, context)
                action = "review" if reviewed else "review_failed"
        all_reasons = reasons + ([ai_result["reason"]] if ai_result else [])
        final_spam = action in {"ban", "review"}
        await self.database.log_message(
            user.id, user.username or "", user.first_name or "", chat.id, text,
            final_spam, all_reasons, action, score,
            ai_result.get("confidence") if ai_result else None,
            ai_result.get("reason") if ai_result else None,
        )

    def _should_use_ai(self, text: str, message) -> bool:
        if message.photo:
            return True
        lowered = text.lower()
        triggers = (
            "investi", "profit", "gagn", "gain", "trader", "mentor", "signal",
            "rejoign", "contact", "plateforme", "crypto", "mt5", "mt4",
            "benefice", "rentabil", "http", "t.me", "@",
        )
        return (len(text) > 40 and any(word in lowered for word in triggers)) or len(text) > 120

    async def _process_spam(self, message, user, chat, reasons, context) -> bool:
        try:
            await message.delete()
        except Exception:
            self.logger.exception("Échec suppression message de %s", user.id)
        try:
            await context.bot.ban_chat_member(chat.id, user.id)
            await self.database.log_ban(user.id, user.username or "", "; ".join(reasons))
            return True
        except Exception:
            self.logger.exception("Échec ban de %s", user.id)
            return False

    async def _send_to_review(self, message, user, chat, ai_result, context) -> bool:
        try:
            display_user = f"@{user.username}" if user.username else user.first_name or "Inconnu"
            content = message.text or message.caption or "[Média non textuel]"
            review_message_id = await self.review_handler.send_review_message(
                context, chat.id, user.id, display_user, content,
                ai_result["reason"], ai_result["confidence"],
            )
            if review_message_id is None:
                return False
            try:
                await message.delete()
            except Exception:
                self.logger.exception("Échec suppression message original après création review %s", user.id)
            return True
        except Exception:
            self.logger.exception("Erreur lors de la création de la review")
            return False

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.exception("Erreur Telegram", exc_info=context.error)
        errors = context.application.bot_data.setdefault("errors", [])
        errors.append(str(context.error))
        del errors[:-10]

    def run(self) -> None:
        self._acquire_instance_lock()
        try:
            application = (ApplicationBuilder().token(self.config.BOT_TOKEN).post_init(self.post_init).post_shutdown(self.post_shutdown).build())
            admin = AdminHandler(self.config, self.database, self.started_at, self.logger, self.review_handler)
            application.add_handler(CommandHandler("stats", admin.stats))
            application.add_handler(CommandHandler("health", admin.health))
            application.add_handler(CommandHandler("errors", admin.errors))
            application.add_handler(CallbackQueryHandler(self.review_handler.handle_callback, pattern=r"^review_(ban|safe)_\d+$"))
            media_filters = filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.ANIMATION | filters.Sticker.ALL
            application.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND) | media_filters, self.handle_message))
            application.add_error_handler(self.error_handler)
            if self.config.REVIEW_ENABLED and application.job_queue:
                application.job_queue.run_repeating(self.review_handler.check_expired_reviews, interval=3600, first=3600)
            application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
        finally:
            self._release_instance_lock()