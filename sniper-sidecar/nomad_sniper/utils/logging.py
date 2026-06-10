"""Loguru-based logger, single config."""

from __future__ import annotations

import sys

from loguru import logger

from nomad_sniper.utils.settings import settings

_configured = False


def get_logger():
    """Lazily configure and return the singleton logger."""
    global _configured
    if not _configured:
        logger.remove()
        logger.add(
            sys.stderr,
            level=settings.log_level,
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
                "<level>{message}</level>"
            ),
            colorize=True,
        )
        _configured = True
    return logger
