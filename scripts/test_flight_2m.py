"""Compatibility test entry point for the organized flight tools."""
from pathlib import Path
import unittest

def load_tests(loader, tests, pattern):
    base = Path(__file__).resolve().parent
    return loader.discover(str(base / "flight_2m" / "tests"), top_level_dir=str(base))


if __name__ == "__main__":
    suite = load_tests(unittest.defaultTestLoader, None, None)
    raise SystemExit(not unittest.TextTestRunner().run(suite).wasSuccessful())
