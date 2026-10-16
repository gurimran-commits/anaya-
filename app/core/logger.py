
import logging


def get_logger(name: str) -> logging.Logger:
    """Return a named logger for an Anaya module."""
    logger = logging.getLogger(name)

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
            )
        )
        logger.addHandler(handler)

    logger.setLevel(logging.INFO)
    return logger
