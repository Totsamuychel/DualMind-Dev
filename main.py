"""DualMind Dev — Entry point."""

import asyncio
import logging
from pathlib import Path

import yaml

from core.orchestrator import Orchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dualmind")


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


async def main():
    logger.info("🧠 DualMind Dev starting...")
    config = load_config()
    orchestrator = Orchestrator(config)
    await orchestrator.run()


if __name__ == "__main__":
    asyncio.run(main())
