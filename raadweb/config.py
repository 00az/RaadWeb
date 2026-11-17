"""Deploy inputs for the landing site, read from the environment and checked before synth.

Hostname, certificate ARN and hosted zone ID are deploy inputs (GitHub environment variables in
CI); none of them is written into the code.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

ENVIRONMENTS = ("dev", "stg", "prd")
PROTECTED_ENVIRONMENTS = frozenset({"stg", "prd"})

_HOSTNAME = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_CERTIFICATE_ARN = re.compile(
    r"^arn:aws:acm:(?P<region>[a-z0-9-]+):\d{12}:certificate/[0-9a-f][0-9a-f-]{7,63}$"
)
_HOSTED_ZONE_ID = re.compile(r"^Z[A-Z0-9]{1,31}$")


class ConfigError(ValueError):
    """A deploy input is missing or not acceptable."""


@dataclass(frozen=True)
class SiteConfig:
    environment: str
    domain_name: str
    certificate_arn: str
    hosted_zone_id: str | None = None

    @property
    def is_protected(self) -> bool:
        """`stg` and `prd` keep their bucket and refuse stack deletion."""
        return self.environment in PROTECTED_ENVIRONMENTS


def _check_domain(domain_name: str) -> None:
    if not domain_name:
        raise ConfigError("RAADWEB_DOMAIN_NAME is required (the page's own hostname)")
    if domain_name == "cloudfront.net" or domain_name.endswith(".cloudfront.net"):
        raise ConfigError("RAADWEB_DOMAIN_NAME must be the site's own hostname, not cloudfront.net")
    if not _HOSTNAME.match(domain_name):
        raise ConfigError(f"RAADWEB_DOMAIN_NAME is not a valid lowercase hostname: {domain_name!r}")


def _check_certificate(certificate_arn: str) -> None:
    if not certificate_arn:
        raise ConfigError("RAADWEB_CERTIFICATE_ARN is required (an ACM certificate in us-east-1)")
    match = _CERTIFICATE_ARN.match(certificate_arn)
    if not match:
        raise ConfigError("RAADWEB_CERTIFICATE_ARN is not an ACM certificate ARN")
    if match["region"] != "us-east-1":
        raise ConfigError("RAADWEB_CERTIFICATE_ARN must be in us-east-1 for CloudFront")


def load_config(env: Mapping[str, str]) -> SiteConfig:
    """Read and check the deploy inputs; raise `ConfigError` on the first problem."""
    environment = env.get("RAAD_ENVIRONMENT", "dev").strip()
    if environment not in ENVIRONMENTS:
        raise ConfigError(f"RAAD_ENVIRONMENT must be one of {', '.join(ENVIRONMENTS)}")
    domain_name = env.get("RAADWEB_DOMAIN_NAME", "").strip()
    certificate_arn = env.get("RAADWEB_CERTIFICATE_ARN", "").strip()
    hosted_zone_id = env.get("RAAD_HOSTED_ZONE_ID", "").strip() or None
    _check_domain(domain_name)
    _check_certificate(certificate_arn)
    if hosted_zone_id is not None and not _HOSTED_ZONE_ID.match(hosted_zone_id):
        raise ConfigError("RAAD_HOSTED_ZONE_ID is not a Route 53 hosted zone ID")
    return SiteConfig(
        environment=environment,
        domain_name=domain_name,
        certificate_arn=certificate_arn,
        hosted_zone_id=hosted_zone_id,
    )
