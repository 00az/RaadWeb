"""CDK entry point: one landing-site stack for the environment named in `RAAD_ENVIRONMENT`.

Every synth rebuilds `dist/` from `site/` first, so a deploy always ships the current source,
and runs the cdk-nag AwsSolutions rules; any unacknowledged finding fails the synth.
"""

import os
import sys
from pathlib import Path

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from raadweb.config import ConfigError, load_config
from raadweb.stack import LandingSiteStack
from scripts.build_site import BuildError, build_site

ROOT = Path(__file__).resolve().parent
REGION = "us-east-1"


def main() -> None:
    try:
        config = load_config(os.environ)
        build = build_site(ROOT / "site", ROOT / "dist")
    except (ConfigError, BuildError) as error:
        sys.exit(f"cannot synth the landing site: {error}")
    region = os.environ.get("CDK_DEFAULT_REGION", REGION)
    if region != REGION:
        sys.exit(f"the landing site deploys to {REGION} (team practice); got {region}")

    app = cdk.App()
    LandingSiteStack(
        app,
        f"RaadWeb-{config.environment}",
        config=config,
        site_dir=build.out_dir,
        env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=region),
    )
    cdk.Validations.of(app).add_plugins(
        AwsSolutionsChecks(app, verbose=True, write_suppressions_to_cloud_formation=True)
    )
    app.synth()


if __name__ == "__main__":
    main()
