import logging

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def configure_logging(level: int = logging.INFO) -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    if not root_logger.handlers:
        root_logger.addHandler(logging.StreamHandler())

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    for handler in root_logger.handlers:
        handler.setFormatter(formatter)
