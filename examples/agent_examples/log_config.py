"""
Shared logging configuration for example scripts.

Usage (in any examples/run_*.py):

    from log_config import setup_logging
    setup_logging(__file__)
"""

import logging
from datetime import datetime
from pathlib import Path

_LOGS_DIR = Path(__file__).resolve().parent / "logs"


def setup_logging(script_file: str) -> str:
    """Configure root logger to write to ``examples/logs/{script}_{timestamp}.log``.

    Returns the absolute path to the log file.
    """
    _LOGS_DIR.mkdir(exist_ok=True)

    script_name = Path(script_file).stem
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = _LOGS_DIR / f"{script_name}_{timestamp}.log"

    # File handler: captures everything (DEBUG+)
    file_handler = logging.FileHandler(log_path, mode="w")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(name)-30s | %(levelname)-7s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )

    # Console handler: only user-facing messages (INFO+)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(message)s"))

    # Root logger → file (catch-all for all modules)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in root.handlers[:]:
        h.close()
        root.removeHandler(h)
    root.addHandler(file_handler)

    # "user" logger → console + file (high-level progress)
    user_logger = logging.getLogger("user")
    if not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in user_logger.handlers
    ):
        user_logger.addHandler(console_handler)

    # "status" logger → console + file (phase updates)
    status_logger = logging.getLogger("status")
    if not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in status_logger.handlers
    ):
        status_logger.addHandler(console_handler)

    # Quiet noisy HTTP modules
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    log_abs = str(log_path)
    logging.getLogger("user").info("Logging to %s", log_abs)
    return log_abs
