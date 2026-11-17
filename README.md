# RaadWeb

The RAAD landing page: a static site on AWS (S3 and CloudFront). Everything else lives in the mobile app.

Planning and design records live in [00az/Raad](https://github.com/00az/Raad).

## What is here

| Path | What it holds |
|---|---|
| `site/` | The pages: `index.html` (Arabic, `/`), `en/index.html` (English, `/en/`) and `404.html` (the bilingual not-found page CloudFront shows for any unknown path), the stylesheet, the logo, the favicon (`favicon.ico` and `assets/icon-180.png`, cut from the logo's bolt) and the IBM Plex Sans Arabic font files (SIL Open Font License, `site/assets/fonts/OFL.txt`). Plain HTML and CSS, no JavaScript. |
| `scripts/build_site.py` | Copies `site/` to `dist/` and gives the stylesheet, fonts and logo content-hashed names. |
| `scripts/smoke_site.py` | Checks a deployed site over HTTPS in both languages. |
| `raadweb/` | The AWS CDK stack (Python): a private S3 bucket behind CloudFront, one per environment. |
| `app.py` | The CDK entry point. Every synth rebuilds `dist/` and runs the cdk-nag AwsSolutions checks. |
| `tests/` | pytest tests; `tests/browser/` holds the headless-Chromium accessibility scan. |

## Prerequisites

- [uv](https://docs.astral.sh/uv/). It installs and uses Python 3.12, which this repo pins.
- Node.js 22 and the AWS CDK CLI at the version CI uses: `npm install --global aws-cdk@2.1148.1`.
  Its major must match `aws-cdk-lib` in `uv.lock`; `tests/test_workflows.py` checks the workflows.
- Chromium for the accessibility scan, installed by Playwright (the setup command below).

## Setup

```sh
uv sync --locked --all-groups && uv run playwright install chromium
```

## Test

```sh
uv run pytest tests --cov=raadweb --cov=scripts --cov-fail-under=80
```

This runs every test, including the browser scan, and holds the Python in `raadweb/` and
`scripts/` to 80% line coverage. If Chromium cannot start, the browser tests are reported as
skipped (not run). Set `RAADWEB_REQUIRE_BROWSER=1` to make that a failure, as CI does.

The other checks CI runs:

```sh
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pip-audit
```

## Build

```sh
uv run python scripts/build_site.py --out dist
```

## Synth (no AWS credentials needed)

`cdk synth` builds the site, synthesizes the stack and runs cdk-nag; any finding nobody has
acknowledged in the code fails it. With fixture inputs:

```sh
RAAD_ENVIRONMENT=dev \
RAADWEB_DOMAIN_NAME=dev.raad.example.test \
RAADWEB_CERTIFICATE_ARN=arn:aws:acm:us-east-1:111111111111:certificate/00000000-0000-0000-0000-000000000000 \
CDK_DEFAULT_ACCOUNT=111111111111 CDK_DEFAULT_REGION=us-east-1 \
uv run cdk synth -q
```

## Deploy inputs

The deploy reads these from the environment (in GitHub Actions, the `dev` environment's
variables). None of them is written into the code.

| Variable | Meaning |
|---|---|
| `RAAD_ENVIRONMENT` | `dev`, `stg` or `prd`. The stack is named `RaadWeb-<environment>`. |
| `RAADWEB_DOMAIN_NAME` | The page's hostname, for example `dev.raadwallet.com`. A `cloudfront.net` name is refused. |
| `RAADWEB_CERTIFICATE_ARN` | An issued ACM certificate in `us-east-1` that covers that hostname. |
| `RAAD_HOSTED_ZONE_ID` | Optional. The Route 53 hosted zone; when set, A and AAAA alias records are created. |
| `AWS_ROLE_ARN_DEV` | The GitHub OIDC role the dev deploy assumes. Until it is set, the deploy workflow skips. |

`stg` and `prd` keep their bucket if the stack is deleted, and their stacks have termination
protection. Deleting the `dev` stack empties and deletes its bucket. `stg` and `prd` promotion is
added to the pipeline later.

## Smoke test

After a deploy, check both languages on the real host:

```sh
uv run python scripts/smoke_site.py https://dev.raadwallet.com
```

It fails on a missing or empty page, the wrong contact addresses, a missing stylesheet or
security header, a reference to any other host, an unknown path that does not answer 404 with
the not-found page, or a missing favicon.

## Rolling back

Redeploy the last known-good commit: re-run its deploy, or revert on `main` through a pull
request. Older assets are never deleted on deploy, and the bucket is versioned: a replaced
object's earlier versions are kept for 30 days (the 3 most recent always), and stalled uploads
are cleaned up after 7 days.
