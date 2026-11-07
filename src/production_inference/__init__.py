from production_inference.logging_config import configure_logging, get_logger


def main() -> None:
    configure_logging()
    get_logger(__name__).info("Hello from production-inference!")
