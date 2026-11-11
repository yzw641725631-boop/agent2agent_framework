"""Run the synthetic native benchmark in an isolated server test environment."""
import argparse
import os
from pathlib import Path
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    urls = [os.getenv("HUB_TEST_OWNER_URL", ""), os.getenv("HUB_TEST_RUNTIME_URL", "")]
    if os.name == "nt" or any(urlsplit(value).path != "/hub_test" for value in urls):
        parser.error("Use the isolated hub_test database and real embedding service on the server")
    target = args.output.resolve()
    if target.exists() or not target.parent.is_dir():
        parser.error("Choose a new report file in an existing directory")
    os.environ["HUB_MEMORY_BENCHMARK_REPORT"] = str(target)
    import pytest

    return pytest.main(["-q", "-p", "no:cacheprovider", "--tb=short", "tests/test_memory_quality_benchmark.py"])


if __name__ == "__main__":
    raise SystemExit(main())
