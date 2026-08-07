from app.bot import Bot
from app.config import Config
from app.logging_config import configure_logging

if __name__ == "__main__":
    config = Config.from_env()
    configure_logging(config.data_dir, config.log_level)
    Bot(config).run()
