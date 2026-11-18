"""The landing site's hosting: a private S3 bucket behind CloudFront, one stack per environment.

The bucket is never public; CloudFront reads it through origin access control. Pages are served
over HTTPS only (TLS 1.2 minimum) with the custom hostname and its ACM certificate, and every
response carries the security headers in `raadweb.web_policy`. No visitor is logged: CloudFront
access logs and S3 server access logs stay off, and there is no WAF on this static page.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from aws_cdk import (
    Acknowledgment,
    CfnOutput,
    DefaultStackSynthesizer,
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
    Token,
    Validations,
)
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as targets
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct, IConstruct

from raadweb.config import SiteConfig
from raadweb.web_policy import (
    ASSET_CACHE_CONTROL,
    CONTENT_SECURITY_POLICY,
    CONTENT_TYPES,
    ERROR_CACHE_SECONDS,
    HASHED_SUFFIXES,
    HSTS_MAX_AGE_SECONDS,
    HTML_CACHE_CONTROL,
    UNHASHED_CACHE_CONTROL,
)

BUILD_MARKER = ".raadweb-build"
NONCURRENT_VERSION_DAYS = 30
NONCURRENT_VERSIONS_KEPT = 3
INCOMPLETE_UPLOAD_DAYS = 7
NOT_FOUND_PAGE = "/404.html"
HTML_PATHS = ("/index.html", "/en/index.html", NOT_FOUND_PAGE)

# S3's REST endpoint has no index documents, so `/` and `/en/` are mapped to their
# `index.html` at the edge, and a known page folder without its slash (`/en`) is redirected.
# Any other path goes to S3 as it is, and a miss comes back as the not-found page.
# This runs in CloudFront, not in the page: the pages have no script.
_INDEX_REWRITE_TEMPLATE = """\
var FOLDERS = %s;
function handler(event) {
  var request = event.request;
  var uri = request.uri;
  if (uri.endsWith('/')) {
    request.uri = uri + 'index.html';
    return request;
  }
  if (FOLDERS.indexOf(uri) !== -1) {
    return {
      statusCode: 301,
      statusDescription: 'Moved Permanently',
      headers: { location: { value: uri + '/' } }
    };
  }
  return request;
}
"""


def index_rewrite_code(folders: Sequence[str]) -> str:
    """The edge function's code for the given page folders (for example `["/en"]`)."""
    return _INDEX_REWRITE_TEMPLATE % json.dumps(sorted(folders))


def page_folders(site_dir: Path) -> list[str]:
    """Folders below the root that hold an `index.html`, as URL paths without the slash."""
    return sorted(
        "/" + page.parent.relative_to(site_dir).as_posix()
        for page in site_dir.rglob("index.html")
        if page.parent != site_dir
    )


def cache_control_for(suffix: str) -> str:
    """Pages revalidate, hashed assets are immutable, fixed-name files last a day."""
    if suffix == ".html":
        return HTML_CACHE_CONTROL
    return ASSET_CACHE_CONTROL if suffix in HASHED_SUFFIXES else UNHASHED_CACHE_CONTROL


def acknowledge(construct: IConstruct, *findings: tuple[str, str]) -> None:
    """Accept cdk-nag findings on a construct; each finding carries its reason."""
    Validations.of(construct).acknowledge(
        *(Acknowledgment(id=rule, reason=reason) for rule, reason in findings)
    )


def _deployable_suffixes(site_dir: Path) -> list[str]:
    """Every file type in the built site, each of which needs a known content type."""
    if not (site_dir / BUILD_MARKER).is_file():
        raise ValueError(f"{site_dir} is not a build: run scripts/build_site.py first")
    for page in HTML_PATHS:
        if not (site_dir / page.lstrip("/")).is_file():
            raise ValueError(f"the build in {site_dir} has no {page}")
    suffixes = {
        path.suffix
        for path in site_dir.rglob("*")
        if path.is_file() and not path.name.startswith(".")
    }
    unknown = sorted(suffixes - set(CONTENT_TYPES))
    if unknown:
        raise ValueError(f"no content type is defined for {', '.join(unknown)}")
    return sorted(suffixes)


class LandingSiteStack(Stack):
    """S3, CloudFront and (optionally) Route 53 records for one environment's landing page."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: SiteConfig,
        site_dir: Path,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            scope,
            construct_id,
            description=f"RAAD landing page ({config.environment})",
            termination_protection=config.is_protected,
            **kwargs,
        )
        suffixes = _deployable_suffixes(site_dir)
        Tags.of(self).add("raad:environment", config.environment)
        Tags.of(self).add("raad:repo", "RaadWeb")

        self.bucket = self._bucket(config)
        self.distribution = self._distribution(config, page_folders(site_dir))
        self._publish(site_dir, suffixes)
        if config.hosted_zone_id:
            self._alias_records(config.hosted_zone_id, config.domain_name)

        CfnOutput(self, "SiteUrl", value=f"https://{config.domain_name}/")
        CfnOutput(self, "DistributionId", value=self.distribution.distribution_id)
        CfnOutput(self, "DistributionDomainName", value=self.distribution.distribution_domain_name)

    def _bucket(self, config: SiteConfig) -> s3.Bucket:
        bucket = s3.Bucket(
            self,
            "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            minimum_tls_version=1.2,
            versioned=True,  # keeps the last known-good objects for rollback
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            # Bound the growth of old object versions: a replaced page or asset is kept for 30 days
            # (the 3 most recent replaced versions are always kept), and an interrupted upload is
            # cleaned up after 7 days. Current objects, including older hashed assets that cached
            # pages may still name, are never expired.
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireOldVersions",
                    noncurrent_version_expiration=Duration.days(NONCURRENT_VERSION_DAYS),
                    noncurrent_versions_to_retain=NONCURRENT_VERSIONS_KEPT,
                    abort_incomplete_multipart_upload_after=Duration.days(INCOMPLETE_UPLOAD_DAYS),
                )
            ],
            # stg and prd keep the bucket if the stack is deleted (and the stack refuses
            # deletion). dev's bucket is emptied and deleted with its stack.
            removal_policy=RemovalPolicy.RETAIN if config.is_protected else RemovalPolicy.DESTROY,
            auto_delete_objects=not config.is_protected,
        )
        acknowledge(
            bucket,
            (
                "AwsSolutions-S1",
                "No visitor logging by design (privacy design, NFR10.10): S3 server access "
                "logs stay off. The bucket holds only the public static site.",
            ),
        )
        return bucket

    def _security_headers(self) -> cloudfront.ResponseHeadersPolicy:
        return cloudfront.ResponseHeadersPolicy(
            self,
            "SecurityHeaders",
            comment="RAAD landing page security headers",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=CONTENT_SECURITY_POLICY, override=True
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.seconds(HSTS_MAX_AGE_SECONDS),
                    include_subdomains=False,
                    override=True,
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.NO_REFERRER, override=True
                ),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True
                ),
            ),
        )

    def _cache_policy(
        self, construct_id: str, comment: str, default_ttl: Duration, max_ttl: Duration
    ) -> cloudfront.CachePolicy:
        # No cookies, headers or query strings in the cache key: the site has none to vary on,
        # and arbitrary query strings can't split the cache or reach the origin.
        return cloudfront.CachePolicy(
            self,
            construct_id,
            comment=comment,
            min_ttl=Duration.seconds(0),
            default_ttl=default_ttl,
            max_ttl=max_ttl,
            cookie_behavior=cloudfront.CacheCookieBehavior.none(),
            header_behavior=cloudfront.CacheHeaderBehavior.none(),
            query_string_behavior=cloudfront.CacheQueryStringBehavior.none(),
            enable_accept_encoding_gzip=True,
            enable_accept_encoding_brotli=True,
        )

    def _distribution(self, config: SiteConfig, folders: list[str]) -> cloudfront.Distribution:
        origin = origins.S3BucketOrigin.with_origin_access_control(self.bucket)
        headers = self._security_headers()
        html_cache = self._cache_policy(
            "HtmlCache", "Pages: short, revalidated", Duration.minutes(1), Duration.minutes(5)
        )
        asset_cache = self._cache_policy(
            "AssetCache", "Hashed assets: one year", Duration.days(365), Duration.days(365)
        )
        index_rewrite = cloudfront.Function(
            self,
            "IndexRewrite",
            comment="Serve /en/ from /en/index.html; redirect /en to /en/",
            code=cloudfront.FunctionCode.from_inline(index_rewrite_code(folders)),
            runtime=cloudfront.FunctionRuntime.JS_2_0,
        )
        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            comment=f"RAAD landing page ({config.environment})",
            domain_names=[config.domain_name],
            certificate=acm.Certificate.from_certificate_arn(
                self, "Certificate", config.certificate_arn
            ),
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            default_root_object="index.html",
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            # Includes the Middle East edge locations nearest RAAD's users.
            price_class=cloudfront.PriceClass.PRICE_CLASS_200,
            enable_logging=False,
            # The private origin answers 403 for a missing object; visitors get the bilingual
            # not-found page with a real 404, cached only briefly.
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=status,
                    response_http_status=404,
                    response_page_path=NOT_FOUND_PAGE,
                    ttl=Duration.seconds(ERROR_CACHE_SECONDS),
                )
                for status in (403, 404)
            ],
            default_behavior=cloudfront.BehaviorOptions(
                origin=origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
                cache_policy=html_cache,
                response_headers_policy=headers,
                compress=True,
                function_associations=[
                    cloudfront.FunctionAssociation(
                        function=index_rewrite,
                        event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                    )
                ],
            ),
            additional_behaviors={
                "/assets/*": cloudfront.BehaviorOptions(
                    origin=origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
                    cache_policy=asset_cache,
                    response_headers_policy=headers,
                    compress=True,
                )
            },
        )
        acknowledge(
            distribution,
            (
                "AwsSolutions-CFR1",
                "The public demo page is meant to be readable from any country; there is no "
                "content to restrict by location.",
            ),
            (
                "AwsSolutions-CFR2",
                "No WAF: the page is static (no forms, scripts or dynamic content) and the WAF "
                "cost is not authorized for it.",
            ),
            (
                "AwsSolutions-CFR3",
                "No visitor logging by design (privacy design, NFR10.10): CloudFront access "
                "logs stay off.",
            ),
        )
        return distribution

    def _publish(self, site_dir: Path, suffixes: list[str]) -> None:
        """Upload the build: assets first, then the pages, then invalidate the pages.

        Older objects are never deleted (`prune=False`), so a page cached before a deploy can
        still load the assets it names, and the bucket keeps the last known-good release.
        """
        source = s3deploy.Source.asset(str(site_dir), exclude=[BUILD_MARKER])
        asset_deployments = [
            s3deploy.BucketDeployment(
                self,
                f"Publish{suffix.lstrip('.').capitalize()}",
                sources=[source],
                destination_bucket=self.bucket,
                exclude=["*"],
                include=[f"*{suffix}"],
                prune=False,
                content_type=CONTENT_TYPES[suffix],
                cache_control=[s3deploy.CacheControl.from_string(cache_control_for(suffix))],
                memory_limit=256,
            )
            for suffix in suffixes
            if suffix != ".html"
        ]
        pages = s3deploy.BucketDeployment(
            self,
            "PublishPages",
            sources=[source],
            destination_bucket=self.bucket,
            exclude=["*"],
            include=["*.html"],
            prune=False,
            content_type=CONTENT_TYPES[".html"],
            cache_control=[s3deploy.CacheControl.from_string(cache_control_for(".html"))],
            memory_limit=256,
            distribution=self.distribution,
            distribution_paths=list(HTML_PATHS),
        )
        for deployment in asset_deployments:
            pages.node.add_dependency(deployment)
        self._acknowledge_deployment_handler()

    def _acknowledge_deployment_handler(self) -> None:
        """Accept the findings on the upload function that aws-cdk-lib's BucketDeployment creates.

        The construct owns that function's runtime and permissions; RaadWeb cannot narrow them.
        """
        handlers = [
            child
            for child in self.node.children
            if child.node.id.startswith("Custom::CDKBucketDeployment")
        ]
        if not handlers:
            raise RuntimeError("BucketDeployment's upload function was not found")
        account = "<AWS::AccountId>" if Token.is_unresolved(self.account) else self.account
        qualifier = DefaultStackSynthesizer.DEFAULT_QUALIFIER
        assets_bucket = f"arn:<AWS::Partition>:s3:::cdk-{qualifier}-assets-{account}-{self.region}"
        bucket_cfn = self.bucket.node.default_child
        if not isinstance(bucket_cfn, s3.CfnBucket):
            raise RuntimeError("the site bucket has no CloudFormation resource")
        site_bucket = f"<{self.get_logical_id(bucket_cfn)}.Arn>"
        read_reason = (
            "BucketDeployment's own read grant: it reads the build zip from the CDK assets "
            "bucket and lists this stack's site bucket. No other bucket is named."
        )
        for handler in handlers:
            acknowledge(
                handler,
                (
                    "AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/"
                    "service-role/AWSLambdaBasicExecutionRole]",
                    "BucketDeployment's upload function writes its own logs through the AWS "
                    "managed basic execution role; the construct offers no other role shape.",
                ),
                ("AwsSolutions-IAM5[Action::s3:GetObject*]", read_reason),
                ("AwsSolutions-IAM5[Action::s3:GetBucket*]", read_reason),
                ("AwsSolutions-IAM5[Action::s3:List*]", read_reason),
                (
                    "AwsSolutions-IAM5[Action::s3:DeleteObject*]",
                    "BucketDeployment's write grant, limited to this stack's site bucket. "
                    "prune is off, so published objects are never deleted by a deploy.",
                ),
                (
                    "AwsSolutions-IAM5[Action::s3:Abort*]",
                    "BucketDeployment's write grant (abort a failed multipart upload), limited "
                    "to this stack's site bucket.",
                ),
                (
                    f"AwsSolutions-IAM5[Resource::{site_bucket}/*]",
                    "Objects in this stack's own site bucket only: the upload target.",
                ),
                (
                    f"AwsSolutions-IAM5[Resource::{assets_bucket}/*]",
                    "Objects in the account's CDK bootstrap assets bucket, which holds the build "
                    "zip the function copies from.",
                ),
                (
                    "AwsSolutions-IAM5[Resource::*]",
                    "cloudfront:CreateInvalidation and GetInvalidation, which CloudFront does not "
                    "scope below the account in BucketDeployment's grant; the function "
                    "invalidates only this distribution's three pages.",
                ),
                (
                    "AwsSolutions-L1",
                    "The upload function's runtime is set by aws-cdk-lib's BucketDeployment and "
                    "moves forward with each CDK release.",
                ),
            )

    def _alias_records(self, hosted_zone_id: str, domain_name: str) -> None:
        zone = route53.HostedZone.from_hosted_zone_id(self, "Zone", hosted_zone_id)
        target = route53.RecordTarget.from_alias(targets.CloudFrontTarget(self.distribution))
        # A trailing dot makes the name fully qualified, so the zone's name is not needed.
        route53.ARecord(self, "AliasIpv4", zone=zone, record_name=f"{domain_name}.", target=target)
        route53.AaaaRecord(
            self, "AliasIpv6", zone=zone, record_name=f"{domain_name}.", target=target
        )
