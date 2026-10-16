
import logging

from app.core.logger import get_logger


def test_get_logger_returns_logger():
    logger = get_logger("anaya.test")
    assert isinstance(logger, logging.Logger)


def test_logger_level_is_info():
    logger = get_logger("anaya.test.level")
    assert logger.level == logging.INFO
