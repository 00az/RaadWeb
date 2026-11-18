"""CDK assertion tests for the landing-site hosting stack and its deploy inputs.

Every stack here is synthesized with the cdk-nag AwsSolutions checks attached, exactly as
`app.py` does. Inputs are fixtures: account 111111111111 and `*.raad.example.test` hostnames.
No AWS API is called.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from functools import cache
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk import aws_s3 as s3
from aws_cdk.assertions import Match, Template
from cdk_nag import AwsSolutionsChecks

from raadweb.config import ConfigError, SiteConfig, load_config
from raadweb.stack import LandingSiteStack
from raadweb.web_policy import (
    ASSET_CACHE_CONTROL,
    CONTENT_SECURITY_POLICY,
    HTML_CACHE_CONTROL,
    UNHASHED_CACHE_CONTROL,
)
from scripts.build_site import build_site

ACCOUNT = "111111111111"
CERTIFICATE = f"arn:aws:acm:us-east-1:{ACCOUNT}:certificate/00000000-0000-0000-0000-000000000000"
DOMAINS = {
    "dev": "dev.raad.example.test",
    "stg": "stg.raad.example.test",
    "prd": "raad.example.test",
}
ZONE_ID = "Z0123456789ABCDEFGHIJ"
EXPECTED_ACKNOWLEDGED = {
    "AwsSolutions-S1",
    "AwsSolutions-CFR1",
    "AwsSolutions-CFR2",
    "AwsSolutions-CFR3",
    "AwsSolutions-IAM4",
    "AwsSolutions-IAM5",
    "AwsSolutions-L1",
}


@pytest.fixture(scope="module")
def dist(tmp_path_factory: pytest.TempPathFactory, site_dir: Path) -> Path:
    return build_site(site_dir, tmp_path_factory.mktemp("build") / "dist").out_dir


def make_config(environment: str = "dev", zone: str | None = None) -> SiteConfig:
    return SiteConfig(
        environment=environment,
        domain_name=DOMAINS[environment],
        certificate_arn=CERTIFICATE,
        hosted_zone_id=zone,
    )


def make_stack(config: SiteConfig, site: Path) -> tuple[cdk.App, LandingSiteStack]:
    app = cdk.App()
    stack = LandingSiteStack(
        app,
        f"RaadWeb-{config.environment}",
        config=config,
        site_dir=site,
        env=cdk.Environment(account=ACCOUNT, region="us-east-1"),
    )
    cdk.Validations.of(app).add_plugins(
        AwsSolutionsChecks(app, write_suppressions_to_cloud_formation=True)
    )
    return app, stack


@cache
def _synth(environment: str, zone: str | None, site: str) -> dict[str, Any]:
    """Synthesize the whole app as `cdk synth` does; cdk-nag raises here on any finding."""
    app, stack = make_stack(make_config(environment, zone), Path(site))
    assembly = app.synth()
    template_file = Path(assembly.directory) / stack.template_file
    return json.loads(template_file.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def template(dist: Path, environment: str = "dev", zone: str | None = None) -> Template:
    return Template.from_json(_synth(environment, zone, str(dist)))


def resources(built: Template, kind: str) -> list[dict[str, Any]]:
    return [dict(resource) for resource in built.find_resources(kind).values()]


def distribution_config(built: Template) -> dict[str, Any]:
    [distribution] = resources(built, "AWS::CloudFront::Distribution")
    return distribution["Properties"]["DistributionConfig"]  # type: ignore[no-any-return]


def test_bucket_is_private_encrypted_versioned_and_ssl_only(dist: Path) -> None:
    built = template(dist)
    built.resource_count_is("AWS::S3::Bucket", 1)
    built.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
            "BucketEncryption": {
                "ServerSideEncryptionConfiguration": [
                    {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                ]
            },
            "VersioningConfiguration": {"Status": "Enabled"},
            "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]},
        },
    )
    [bucket] = resources(built, "AWS::S3::Bucket")
    assert "LoggingConfiguration" not in bucket["Properties"]
    assert "WebsiteConfiguration" not in bucket["Properties"]


def bucket_policy_statements(built: Template) -> list[dict[str, Any]]:
    [policy] = resources(built, "AWS::S3::BucketPolicy")
    return policy["Properties"]["PolicyDocument"]["Statement"]  # type: ignore[no-any-return]


@pytest.mark.parametrize("environment", ["stg", "prd"])
def test_bucket_policy_lets_only_this_distribution_read_through_oac(
    dist: Path, environment: str
) -> None:
    built = template(dist, environment)
    statements = bucket_policy_statements(built)
    allows = [s for s in statements if s["Effect"] == "Allow"]
    assert len(allows) == 1
    [allow] = allows
    assert allow["Principal"] == {"Service": "cloudfront.amazonaws.com"}
    assert allow["Action"] == "s3:GetObject"
    source_arn = json.dumps(allow["Condition"]["StringEquals"]["AWS:SourceArn"])
    assert '"Ref": "Distribution830FAC52"' in source_arn
    denies = [s for s in statements if s["Effect"] == "Deny"]
    assert {"aws:SecureTransport": "false"} in [s["Condition"].get("Bool") for s in denies]
    assert {"s3:TlsVersion": 1.2} in [s["Condition"].get("NumericLessThan") for s in denies]
    [origin] = distribution_config(built)["Origins"]
    assert "OriginAccessControlId" in origin
    assert origin["S3OriginConfig"].get("OriginAccessIdentity", "") == ""
    built.has_resource_properties(
        "AWS::CloudFront::OriginAccessControl",
        {
            "OriginAccessControlConfig": Match.object_like(
                {"OriginAccessControlOriginType": "s3", "SigningBehavior": "always"}
            )
        },
    )


def test_dev_bucket_policy_adds_only_the_auto_delete_role(dist: Path) -> None:
    allows = [s for s in bucket_policy_statements(template(dist)) if s["Effect"] == "Allow"]
    assert len(allows) == 2
    oac, auto_delete = sorted(allows, key=lambda s: "Service" not in s["Principal"])
    assert oac["Principal"] == {"Service": "cloudfront.amazonaws.com"}
    role = json.dumps(auto_delete["Principal"])
    assert "CustomS3AutoDeleteObjectsCustomResourceProviderRole" in role
    assert set(auto_delete["Action"]) == {
        "s3:DeleteObject*",
        "s3:GetBucket*",
        "s3:List*",
        "s3:PutBucketPolicy",
    }


@pytest.mark.parametrize("environment", ["dev", "stg", "prd"])
def test_old_versions_and_stalled_uploads_are_cleaned_up(dist: Path, environment: str) -> None:
    [bucket] = resources(template(dist, environment), "AWS::S3::Bucket")
    assert bucket["Properties"]["LifecycleConfiguration"] == {
        "Rules": [
            {
                "Id": "ExpireOldVersions",
                "Status": "Enabled",
                "NoncurrentVersionExpiration": {"NoncurrentDays": 30, "NewerNoncurrentVersions": 3},
                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
            }
        ]
    }
    # Current objects never expire: no Expiration rule on them.
    assert "ExpirationInDays" not in json.dumps(bucket["Properties"]["LifecycleConfiguration"])


def test_distribution_is_https_only_tls12_on_the_custom_host_without_logs(dist: Path) -> None:
    config = distribution_config(template(dist))
    assert config["Aliases"] == ["dev.raad.example.test"]
    assert config["ViewerCertificate"] == {
        "AcmCertificateArn": CERTIFICATE,
        "MinimumProtocolVersion": "TLSv1.2_2021",
        "SslSupportMethod": "sni-only",
    }
    behaviors = [config["DefaultCacheBehavior"], *config["CacheBehaviors"]]
    for behavior in behaviors:
        assert behavior["ViewerProtocolPolicy"] == "redirect-to-https"
        assert behavior["AllowedMethods"] == ["GET", "HEAD"]
    assert config["DefaultRootObject"] == "index.html"
    assert "Logging" not in config
    assert "WebACLId" not in config


def test_every_response_carries_the_security_headers(dist: Path) -> None:
    built = template(dist)
    [policy] = resources(built, "AWS::CloudFront::ResponseHeadersPolicy")
    headers = policy["Properties"]["ResponseHeadersPolicyConfig"]["SecurityHeadersConfig"]
    assert headers["ContentSecurityPolicy"] == {
        "ContentSecurityPolicy": CONTENT_SECURITY_POLICY,
        "Override": True,
    }
    assert headers["StrictTransportSecurity"]["AccessControlMaxAgeSec"] >= 31_536_000
    assert headers["ContentTypeOptions"] == {"Override": True}
    assert headers["ReferrerPolicy"] == {"ReferrerPolicy": "no-referrer", "Override": True}
    assert headers["FrameOptions"] == {"FrameOption": "DENY", "Override": True}
    config = distribution_config(built)
    for behavior in [config["DefaultCacheBehavior"], *config["CacheBehaviors"]]:
        assert "ResponseHeadersPolicyId" in behavior


def test_pages_cache_briefly_and_hashed_assets_for_a_year(dist: Path) -> None:
    built = template(dist)
    policies = {
        p["Properties"]["CachePolicyConfig"]["Comment"]: p["Properties"]["CachePolicyConfig"]
        for p in resources(built, "AWS::CloudFront::CachePolicy")
    }
    html = policies["Pages: short, revalidated"]
    assets = policies["Hashed assets: one year"]
    assert (html["MinTTL"], html["DefaultTTL"], html["MaxTTL"]) == (0, 60, 300)
    assert assets["DefaultTTL"] == assets["MaxTTL"] == 31_536_000
    for policy in (html, assets):
        key = policy["ParametersInCacheKeyAndForwardedToOrigin"]
        assert key["QueryStringsConfig"] == {"QueryStringBehavior": "none"}
        assert key["CookiesConfig"] == {"CookieBehavior": "none"}
    config = distribution_config(built)
    [asset_behavior] = config["CacheBehaviors"]
    assert asset_behavior["PathPattern"] == "/assets/*"

    uploads = resources(built, "Custom::CDKBucketDeployment")
    by_type = {u["Properties"]["SystemMetadata"]["content-type"]: u["Properties"] for u in uploads}
    expected_cache = {
        "text/html; charset=utf-8": HTML_CACHE_CONTROL,
        "text/css; charset=utf-8": ASSET_CACHE_CONTROL,
        "font/woff2": ASSET_CACHE_CONTROL,
        "image/webp": ASSET_CACHE_CONTROL,
        "image/png": ASSET_CACHE_CONTROL,
        # Fixed names (favicon.ico, the font licence) are cached for a day, not a year.
        "image/x-icon": UNHASHED_CACHE_CONTROL,
        "text/plain; charset=utf-8": UNHASHED_CACHE_CONTROL,
    }
    assert set(by_type) == set(expected_cache)
    for content_type, upload in by_type.items():
        assert upload["Prune"] is False
        assert upload["SystemMetadata"]["cache-control"] == expected_cache[content_type]
    pages = by_type["text/html; charset=utf-8"]
    assert pages["DistributionPaths"] == ["/index.html", "/en/index.html", "/404.html"]
    assert all(
        "DistributionPaths" not in u
        for t, u in by_type.items()
        if t != pages["SystemMetadata"]["content-type"]
    )


def test_pages_are_published_after_the_assets_they_name(dist: Path) -> None:
    built = template(dist).to_json()["Resources"]
    uploads = {k: v for k, v in built.items() if v["Type"] == "Custom::CDKBucketDeployment"}
    [(pages_id, pages)] = [
        (k, v) for k, v in uploads.items() if "DistributionPaths" in v["Properties"]
    ]
    assert set(pages["DependsOn"]) >= {k for k in uploads if k != pages_id}


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed to run the function")
@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("/", {"uri": "/index.html"}),
        ("/en/", {"uri": "/en/index.html"}),
        ("/en", {"statusCode": 301, "location": "/en/"}),
        ("/assets/styles.0123abcd.css", {"uri": "/assets/styles.0123abcd.css"}),
        # Not a page folder: passed through, so the miss becomes the 404 page, not a redirect.
        ("/no-such-page", {"uri": "/no-such-page"}),
    ],
)
def test_index_rewrite_function_maps_folders_to_their_page(
    dist: Path, uri: str, expected: dict[str, Any]
) -> None:
    built = template(dist)
    config = distribution_config(built)
    [association] = config["DefaultCacheBehavior"]["FunctionAssociations"]
    assert association["EventType"] == "viewer-request"
    [function] = resources(built, "AWS::CloudFront::Function")
    assert function["Properties"]["FunctionConfig"]["Runtime"] == "cloudfront-js-2.0"
    script = (
        function["Properties"]["FunctionCode"]  # the code exactly as synthesized
        + "const r = handler({request: {uri: process.argv[1]}});"
        + "console.log(JSON.stringify(r.statusCode ? "
        + "{statusCode: r.statusCode, location: r.headers.location.value} : {uri: r.uri}));"
    )
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run(  # noqa: S603 - fixed local Node.js, inline test script
        [node, "-e", script, uri], capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout) == expected


def test_missing_objects_show_the_not_found_page_with_a_real_404(dist: Path) -> None:
    config = distribution_config(template(dist))
    responses = sorted(config["CustomErrorResponses"], key=lambda r: r["ErrorCode"])
    assert responses == [
        {
            "ErrorCode": code,
            "ResponseCode": 404,
            "ResponsePagePath": "/404.html",
            "ErrorCachingMinTTL": 30,
        }
        for code in (403, 404)
    ]
    assert (dist / "404.html").is_file()


def test_alias_records_exist_only_when_a_hosted_zone_is_given(dist: Path) -> None:
    template(dist).resource_count_is("AWS::Route53::RecordSet", 0)
    with_zone = template(dist, zone=ZONE_ID)
    records = resources(with_zone, "AWS::Route53::RecordSet")
    assert sorted(r["Properties"]["Type"] for r in records) == ["A", "AAAA"]
    for record in records:
        assert record["Properties"]["Name"] == "dev.raad.example.test."
        assert record["Properties"]["HostedZoneId"] == ZONE_ID
        assert "AliasTarget" in record["Properties"]


@pytest.mark.parametrize(
    ("environment", "deletion", "protected"),
    [("dev", "Delete", False), ("stg", "Retain", True), ("prd", "Retain", True)],
)
def test_stg_and_prd_keep_the_bucket_and_refuse_deletion(
    dist: Path, environment: str, deletion: str, protected: bool
) -> None:
    built = template(dist, environment)
    [bucket] = resources(built, "AWS::S3::Bucket")
    assert bucket["DeletionPolicy"] == deletion
    assert bucket["UpdateReplacePolicy"] == deletion
    # Only dev empties its bucket on stack deletion; stg and prd never delete objects.
    auto_delete = resources(built, "Custom::S3AutoDeleteObjects")
    assert len(auto_delete) == (0 if protected else 1)
    tags = {tag["Key"]: tag["Value"] for tag in bucket["Properties"].get("Tags", [])}
    assert ("aws-cdk:auto-delete-objects" in tags) is not protected
    assert distribution_config(built)["Aliases"] == [DOMAINS[environment]]
    _, stack = make_stack(make_config(environment), dist)
    assert stack.termination_protection is protected


def test_cdk_nag_passes_and_every_acknowledgement_has_a_reason(dist: Path) -> None:
    for environment in ("dev", "prd"):
        built = template(dist, environment, ZONE_ID).to_json()
        acknowledged: dict[str, str] = {}
        for resource in built["Resources"].values():
            for entry in (
                resource.get("Metadata", {}).get("cdk_nag", {}).get("rules_to_suppress", [])
            ):
                acknowledged[entry["id"]] = entry["reason"]
        assert {rule.split("[")[0] for rule in acknowledged} == EXPECTED_ACKNOWLEDGED
        for rule, reason in acknowledged.items():
            assert len(reason) >= 40, rule


def test_cdk_nag_blocks_a_finding_nobody_acknowledged(dist: Path) -> None:
    app, stack = make_stack(make_config(), dist)
    s3.Bucket(stack, "UnreviewedBucket")  # no logging, no SSL enforcement: AwsSolutions-S1/S10
    with pytest.raises(RuntimeError, match="ValidationFailed") as failure:
        app.synth()
    assert "AwsSolutions-S1" in str(failure.value)
    assert "AwsSolutions-S10" in str(failure.value)
    assert "RaadWeb-dev/UnreviewedBucket" in str(failure.value)


def test_stack_refuses_a_site_that_was_not_built(site_dir: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a build"):
        make_stack(make_config(), site_dir)
    odd = tmp_path / "dist"
    build_site(site_dir, odd)
    (odd / "assets" / "notes.md").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match=r"no content type is defined for \.md"):
        make_stack(make_config(), odd)


def test_load_config_reads_the_deploy_inputs() -> None:
    config = load_config(
        {
            "RAAD_ENVIRONMENT": "prd",
            "RAADWEB_DOMAIN_NAME": "raad.example.test",
            "RAADWEB_CERTIFICATE_ARN": CERTIFICATE,
            "RAAD_HOSTED_ZONE_ID": ZONE_ID,
        }
    )
    assert config == SiteConfig("prd", "raad.example.test", CERTIFICATE, ZONE_ID)
    assert config.is_protected
    dev = load_config(
        {"RAADWEB_DOMAIN_NAME": DOMAINS["dev"], "RAADWEB_CERTIFICATE_ARN": CERTIFICATE}
    )
    assert (dev.environment, dev.hosted_zone_id, dev.is_protected) == ("dev", None, False)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"RAAD_ENVIRONMENT": "qa"}, "must be one of dev, stg, prd"),
        ({"RAADWEB_DOMAIN_NAME": ""}, "RAADWEB_DOMAIN_NAME is required"),
        ({"RAADWEB_DOMAIN_NAME": "d111111abcdef8.cloudfront.net"}, "not cloudfront.net"),
        ({"RAADWEB_DOMAIN_NAME": "Raad Wallet"}, "not a valid lowercase hostname"),
        ({"RAADWEB_CERTIFICATE_ARN": ""}, "RAADWEB_CERTIFICATE_ARN is required"),
        (
            {"RAADWEB_CERTIFICATE_ARN": CERTIFICATE.replace("us-east-1", "me-central-1")},
            "must be in us-east-1",
        ),
        ({"RAADWEB_CERTIFICATE_ARN": "arn:aws:iam::111111111111:role/x"}, "not an ACM"),
        ({"RAAD_HOSTED_ZONE_ID": "zone-1"}, "not a Route 53 hosted zone ID"),
    ],
)
def test_load_config_refuses_bad_inputs(change: dict[str, str], message: str) -> None:
    env = {
        "RAAD_ENVIRONMENT": "dev",
        "RAADWEB_DOMAIN_NAME": DOMAINS["dev"],
        "RAADWEB_CERTIFICATE_ARN": CERTIFICATE,
        **change,
    }
    with pytest.raises(ConfigError, match=message):
        load_config(env)
