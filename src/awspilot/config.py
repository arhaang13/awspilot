"""Runtime settings, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

LOCALSTACK_URL = "http://localhost:4566"

DEFAULT_SERVICES = frozenset(
    {
        "apigateway",
        "cloudwatch",
        "dynamodb",
        "ec2",
        "events",
        "iam",
        "kms",
        "lambda",
        "logs",
        "s3",
        "secretsmanager",
        "sns",
        "sqs",
        "ssm",
        "stepfunctions",
    }
)

DEFAULT_MODELS = {"anthropic": "claude-opus-5-5", "openai": "gpt-4o"}


@dataclass(frozen=True)
class Settings:
    provider: str = "anthropic"
    model: str = DEFAULT_MODELS["anthropic"]
    # "localstack", "aws", or "moto" (in-process fake, used by tests and offline runs)
    target: str = "localstack"
    endpoint_url: str | None = LOCALSTACK_URL
    region: str = "us-east-1"
    allowed_regions: frozenset[str] = frozenset({"us-east-1"})
    allowed_services: frozenset[str] = DEFAULT_SERVICES
    max_repairs: int = 2
    state_dir: Path = field(default_factory=lambda: Path(".awspilot"))

    @property
    def is_real_aws(self) -> bool:
        return self.target == "aws"

    @classmethod
    def from_env(cls) -> Settings:
        provider = os.environ.get("AWSPILOT_PROVIDER", "anthropic").lower()
        target = os.environ.get("AWSPILOT_TARGET", "localstack").lower()
        if target not in {"localstack", "aws", "moto"}:
            raise ValueError(f"AWSPILOT_TARGET must be localstack, aws or moto, got {target!r}")
        region = os.environ.get("AWSPILOT_REGION", "us-east-1")
        regions = os.environ.get("AWSPILOT_ALLOWED_REGIONS", region)
        services = os.environ.get("AWSPILOT_ALLOWED_SERVICES")
        return cls(
            provider=provider,
            model=os.environ.get("AWSPILOT_MODEL", DEFAULT_MODELS.get(provider, "")),
            target=target,
            endpoint_url=(
                os.environ.get("AWSPILOT_ENDPOINT_URL", LOCALSTACK_URL)
                if target == "localstack"
                else None
            ),
            region=region,
            allowed_regions=frozenset(r.strip() for r in regions.split(",") if r.strip()),
            allowed_services=(
                frozenset(s.strip() for s in services.split(",") if s.strip())
                if services
                else DEFAULT_SERVICES
            ),
            max_repairs=int(os.environ.get("AWSPILOT_MAX_REPAIRS", "2")),
            state_dir=Path(os.environ.get("AWSPILOT_STATE_DIR", ".awspilot")),
        )
