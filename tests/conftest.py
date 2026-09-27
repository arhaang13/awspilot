from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from awspilot.aws.caller import AwsCaller
from awspilot.bench.runner import clean_backend
from awspilot.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(target="moto", endpoint_url=None, state_dir=tmp_path / "state")


@pytest.fixture
def aws(settings: Settings) -> Iterator[AwsCaller]:
    with clean_backend(settings):
        yield AwsCaller(settings)
