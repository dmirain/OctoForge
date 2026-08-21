"""Argument parser for the quickstart entry point."""

import argparse
from pathlib import Path


def parser(default_env: Path) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Prepare .env for a first run.")
    result.add_argument("--env", default=str(default_env))
    result.add_argument("--llm-key")
    result.add_argument("--llm-base-url")
    result.add_argument("--llm-model")
    result.add_argument("--embedding-model", default="text-embedding-3-small")
    return result
