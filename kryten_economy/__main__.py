"""CLI entry point for kryten-economy."""
import argparse
import asyncio
import logging
import signal
import sys

from .main import EconomyApp


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kryten Economy — Channel Currency Service")
    parser.add_argument("--config", type=str, help="Path to config.yaml")
    parser.add_argument("--log-level", type=str, default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--validate-config", action="store_true", help="Validate config and exit without starting")
    return parser.parse_args()


async def main_async() -> None:
    args = parse_args()
    setup_logging(args.log_level)
    logger = logging.getLogger("economy")

    # Config path resolution
    config_path = args.config
    if not config_path:
        from pathlib import Path

        for candidate in [
            "/etc/kryten/kryten-economy/config.yaml",
            "./config.yaml",
        ]:
            if Path(candidate).exists():
                config_path = candidate
                break
    if not config_path:
        logger.error("No config file found. Use --config or place config.yaml in CWD.")
        sys.exit(1)

    if args.validate_config:
        from .config import load_config

        try:
            load_config(config_path)
            logger.info("Config is valid.")
        except Exception as e:
            logger.error("Config validation failed: %s", e)
            sys.exit(1)
        return

    app = EconomyApp(config_path)

    # Signal handling (Unix only; Windows uses KeyboardInterrupt)
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(app.stop()))

    try:
        await app.start()
    except KeyboardInterrupt:
        pass
    finally:
        await app.stop()


def main() -> None:
    """Sync entry point for pyproject.toml [project.scripts]."""
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
