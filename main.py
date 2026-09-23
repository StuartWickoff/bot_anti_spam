from config import config
from gorillebot.bot import TelegramAntiSpamBot


def main() -> None:
    TelegramAntiSpamBot(config).run()


if __name__ == "__main__":
    main()