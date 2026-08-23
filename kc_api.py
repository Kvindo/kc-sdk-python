"""Python client (SDK) for the Kvindo Cloud API.

`KcClient` exposes one `KcResourceClient` per resource type (vms, volumes,
load balancers, kubernetes, s3, …). Every resource speaks the same REST
contract under `/api/v1/<resource-type>`:

    PUT    /api/v1/<type>              create or update (idempotent on metadata.id)
    GET    /api/v1/<type>/<id>         read one
    DELETE /api/v1/<type>/<id>         delete one
    GET    /api/v1/<type>/get-by-labels   list (label-filtered, paginated)
    GET    /api/v1/<type>/request/<reqId> poll an async change-request's status

Create/update/delete are **asynchronous**: they return a `requestId`
immediately; the actual provisioning is done by server-side reconcilers. Poll
`read_request(requestId)` (or use `wait_request_satisfied`) until it succeeds.

The resource surface mirrors the maintained C# client
`KvindoCloud.Api/KvindoCloudClient.cs`, which is the source of truth.

Dependencies: requests, marshmallow-dataclass, py-ulid.
"""

import time
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import urlencode
from enum import Enum

# https://stackoverflow.com/questions/15476983/deserialize-a-json-string-to-an-object-in-python
from marshmallow_dataclass import dataclass
from typing import List, Optional
from dataclasses import field
from ulid import ULID

# Why a module logger here instead of importing one: this file is published as a
# standalone SDK, so it must not depend on the internal kc_common module (which
# pulls in python-json-logger and a pre-configured logger). Callers configure
# logging as they see fit; by default this logger is silent.
logger = logging.getLogger(__name__)


# Vendored from kc_common (create_http_client_with_retries / create_url_with_query_params)
# so the SDK carries no local-module dependency — its only third-party deps are
# requests, marshmallow-dataclass and py-ulid.
def create_http_client_with_retries(
    retry_statuses=[500, 502, 503, 504, 520, 521],
    verify_ssl: bool = True,
) -> requests.Session:
    """Build a `requests.Session` that retries idempotent failures with backoff.

    Args:
        retry_statuses: HTTP status codes that should be retried. 5xx are
            server-side; 520 = web server returned an unknown error;
            521 = origin down (Cloudflare-specific).
        verify_ssl: enable/disable TLS certificate verification. Defaults to
            True — every real Kvindo Cloud API host (dev and public prod) has
            a valid, publicly-trusted certificate. Pass False only to point
            this SDK at a genuinely self-hosted instance with a self-signed
            cert.

    Returns:
        A configured `requests.Session` (reuse it for connection pooling).
    """
    session = requests.Session()
    session.verify = verify_ssl
    # total=5 attempts; backoff_factor=1 -> delays 0.5, 1, 2, 4, 8 ... seconds.
    retries = Retry(total=5, backoff_factor=1, status_forcelist=retry_statuses)
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def create_url_with_query_params(base_url: str, query_params: dict) -> str:
    """Append a query string to `base_url`, URL-encoding keys and values.

    A dict-valued param is flattened to repeated `key[subkey]=value` pairs
    (used for the `labels` filter). `None` values are dropped entirely.

    Args:
        base_url: URL without a query string.
        query_params: flat dict; values may be scalars or a one-level dict.

    Returns:
        The full URL including the `?...` query string.
    """
    # Drop params whose value is None (e.g. an unset enumeratorId).
    params = {k: v for k, v in query_params.items() if v is not None}

    url = base_url + "?"

    # TODO: Add recursive traverse
    for p in params:
        if isinstance(params[p], dict):
            # Flatten one nesting level: labels={"env":"dev"} -> labels[env]=dev
            for p2 in params[p]:
                url = f"{url}{'' if url.endswith('?') else '&'}{urlencode({f'{p}[{p2}]': params[p][p2]})}"
        else:
            url = f"{url}{'' if url.endswith('?') else '&'}{urlencode({p: params[p]})}"

    return url


# ── Error codes ───────────────────────────────────────────────────────────────
# Error-code enums mirror the C# enums in KvindoCloud.Api/Models/*ErrorCode.cs.
# The API serializes them as the PascalCase member NAME (each C# enum carries
# [JsonConverter(typeof(JsonStringEnumConverter))]), and marshmallow loads Enum
# fields by name, so the member names below must match the C# names exactly.
# Why each value is the name string (not the HTTP status code it used to be):
# duplicate values make Python collapse members into aliases of the first one
# with that value (e.g. every 422 became an alias of NotFound), so "MissingIdField"
# on the wire silently loaded as NotFound. Unique values keep every code distinct.
#
# Note: the """docstrings""" after each member/field are attribute docstrings —
# editors (Pylance/PyCharm) surface them on hover, unlike `#` comments.


class KcApiGenericErrorCode(Enum):
    """Generic top-level errors not specific to one operation."""

    Unauthorized = "Unauthorized"
    """Token missing/invalid or lacks the required permission."""
    BadData = "BadData"
    """Request was malformed (bad JSON, wrong types, etc.)."""


class KcApiModificationErrorCode(Enum):
    """Errors returned by create / update / delete (PUT and DELETE)."""

    NotFound = "NotFound"
    """No resource with the given id."""
    Unauthorized = "Unauthorized"
    """Token lacks permission for this resource/action."""
    MissingIdField = "MissingIdField"
    """Body had no id (and none could be derived)."""
    ResourceIsScheduling = "ResourceIsScheduling"
    """A previous change request is still in flight; retry later."""
    MissingNameField = "MissingNameField"
    """Body had no name."""
    Unknown = "Unknown"
    """Unhandled server-side error (treat as a 5xx)."""
    ResourceIsDeleteProtected = "ResourceIsDeleteProtected"
    """deleteProtection=true; clear it before deleting."""
    BadData = "BadData"
    """Request was malformed."""
    QuotaExceeded = "QuotaExceeded"
    """The organization's quota for this resource type/parameter would be exceeded, counting
    resources that are submitted but not yet reconciled ("holds"). Not retryable - the caller
    must delete resources or raise the quota. HTTP 409."""
    SubmitLockBusy = "SubmitLockBusy"
    """The per-organization submit lock (QuotaSubmitLock) could not be acquired within its
    budget window (concurrent submit contention for the same org - another create/modify/delete
    was already in flight). Retryable - safe to retry the identical request, the lock exists
    precisely to serialize this race. HTTP 422."""


# All three response classes below mirror KvindoCloud.Api.Models.ApiModificationResponse -
# marshmallow_dataclass's default Schema().load() raises ValidationError on any field the C#
# side sends that isn't declared here (unknown = RAISE is the marshmallow default), so every
# field ApiModificationResponse carries must have a matching one here or parsing crashes for
# every caller of create/update/delete, not just the endpoint that happens to populate it.
# (A retryAfterSeconds body field was added and removed again the same day for the synchronous
# submit-time quota gate - it crashed every create/update/delete call for exactly this reason.
# The retry hint is now carried as a real HTTP Retry-After response header instead, which this
# marshmallow schema never sees, so there is nothing to mirror here for it.)

@dataclass
class KcResourceDeleteResponse(object):
    """Response of `KcResourceClient.delete`."""

    requestId: str = None
    """Id of the async change request to poll; None on error."""
    resourceId: str = None
    """Id of the resource being deleted; None on error."""
    errorMessage: str = None
    """Human-readable error; None on success."""
    errorCode: KcApiModificationErrorCode = None
    """Machine-readable error; None on success."""


@dataclass
class KcResourceCreateResponse(object):
    """Response of `KcResourceClient.create` / `create_or_update`."""

    requestId: str = None
    """Id of the async change request to poll; None on error."""
    resourceId: str = None
    """Id of the created/updated resource; None on error."""
    errorMessage: str = None
    """Human-readable error; None on success."""
    errorCode: KcApiModificationErrorCode = None
    """Machine-readable error; None on success."""


@dataclass
class KcResourceUpdateResponse(object):
    """Response of `KcResourceClient.update`. Shape-identical to the create response."""

    requestId: str = None
    """Id of the async change request to poll; None on error."""
    resourceId: str = None
    """Id of the updated resource; None on error."""
    errorMessage: str = None
    """Human-readable error; None on success."""
    errorCode: KcApiModificationErrorCode = None
    """Machine-readable error; None on success."""


class KcApiReadErrorCode(Enum):
    """Errors returned by a single-resource read (`GET /api/v1/<type>/<id>`)."""

    Unauthorized = "Unauthorized"
    """Token lacks read permission."""
    NotFound = "NotFound"
    """No resource with the given id."""
    ResourceIsScheduling = "ResourceIsScheduling"
    """Resource exists but its first change request hasn't completed."""
    Unknown = "Unknown"
    """Unhandled server-side error."""
    BadData = "BadData"
    """Malformed request (e.g. invalid id)."""


@dataclass
class KcResourceReadResponse(object):
    """Response of `KcResourceClient.read`."""

    resource: dict = None
    """The resource as a raw dict; None if errorMessage is set."""
    errorMessage: str = None
    """Human-readable error; None on success."""
    errorCode: KcApiReadErrorCode = None
    """Machine-readable error; set iff errorMessage is set."""


class KcApiReadRequestErrorCode(Enum):
    """Errors returned when polling an async change-request's status."""

    Unauthorized = "Unauthorized"
    """Token lacks permission."""
    NotFound = "NotFound"
    """No change request with the given requestId."""
    Unknown = "Unknown"
    """Unhandled server-side error."""
    BadData = "BadData"
    """Malformed request."""
    UnableToReconcile = "UnableToReconcile"
    """The reconciler failed to apply the change (terminal failure)."""


@dataclass
class KcResourceReadRequestResponse(object):
    """Status of an async create/update/delete request (`GET .../request/<id>`)."""

    succeeded: bool
    """True once the reconciler has finished applying the change."""
    scheduledResourceId: str
    """Id of the resource the request targets."""
    errorMessage: str = None
    """Set if the request failed; None while pending or on success."""
    errorCode: KcApiReadRequestErrorCode = None
    """Machine-readable failure code; None while pending or on success."""


class KcApiGetByLabelsErrorCode(Enum):
    """Errors returned by label-filtered list (`GET .../get-by-labels`)."""

    Unauthorized = "Unauthorized"
    """Token lacks list permission."""
    PageSizeTooBig = "PageSizeTooBig"
    """maxPageSize exceeded the server limit (max 100)."""
    EnumeratorNotFound = "EnumeratorNotFound"
    """The pagination enumeratorId expired or is unknown."""
    Unknown = "Unknown"
    """Unhandled server-side error."""
    BadData = "BadData"
    """Malformed request."""


@dataclass
class KcResourceGetByLabelsPagination(object):
    """Pagination cursor returned by get-by-labels; pass it back to fetch the next page."""

    enumeratorId: str = None
    """Opaque cursor; feed to the next get_by_labels call as enumerator_id."""


@dataclass
class KcResourceGetByLabelsResponse(object):
    """Response of `KcResourceClient.get_by_labels` (one page of results)."""

    # pagination and resources are null on error responses (errorMessage set), so
    # they must be Optional or marshmallow rejects the payload before the caller
    # can read errorMessage.
    pagination: Optional[KcResourceGetByLabelsPagination] = None
    """Cursor for the next page; None on error."""
    resources: Optional[List[dict]] = field(default_factory=list)
    """This page's resources as raw dicts; None on error."""
    errorMessage: str = None
    """Human-readable error; None on success."""
    errorCode: KcApiGetByLabelsErrorCode = None
    """Machine-readable error; None on success."""


# HTTP statuses the API uses to carry a structured (deserializable) body. Anything
# outside this set is an unexpected transport/server failure and is raised instead.
_HANDLED_STATUS_CODES = [200, 400, 401, 403, 409, 422]


class KcResourceClient:
    """Client for a single resource type of the Kvindo Cloud API.

    One instance is bound to one resource type (e.g. "vm", "s3-bucket") and
    reused for all calls against it. Obtain instances via `KcClient`, e.g.
    `KcClient(token).vms`.

    See https://cloud-api.kvindo.ru/swagger/index.html for the full contract.
    """

    def __init__(
        self,
        resource_type: str,
        token: str,
        api_url: str = "https://cloud-api.kvindo.ru",
        log_extra: dict = None,
        verify_ssl: bool = True,
    ):
        """
        Args:
            resource_type: the kebab-case API path segment (e.g. "vm", "s3-bucket").
            token: the bearer token; a leading "Bearer " prefix is stripped if present.
            api_url: base URL of the Cloud API (no trailing slash).
            log_extra: optional dict merged into every debug log record's `extra`.
            verify_ssl: enable/disable TLS certificate verification for every
                request this client makes. Defaults to True; pass False only
                to point this SDK at a genuinely self-hosted instance with a
                self-signed cert.
        """
        self.__token = token.replace("Bearer ", "")
        self.__resource_type = resource_type
        self.__api_url = api_url
        self.__log_extra = log_extra if log_extra is not None else {}
        self.__verify_ssl = verify_ssl

    def __headers(self) -> dict:
        """Standard auth + content-type headers for every request."""
        return {
            "accept": "*/*",
            "Authorization": f"Bearer {self.__token}",
            "Content-Type": "application/json-patch+json",
        }

    def delete(self, id: str, wait=False) -> KcResourceDeleteResponse:
        """Delete a resource by id (asynchronous).

        Args:
            id: id of the resource to delete.
            wait: if True, block (up to 300s) until the delete reconciles via
                `wait_request_satisfied` before returning.

        Returns:
            KcResourceDeleteResponse with `requestId` to poll, or `errorMessage`/
            `errorCode` set on a handled error.

        Raises:
            Exception: on an unexpected HTTP status (outside 200/400/401/403/422).
        """
        url = f"{self.__api_url}/api/v1/{self.__resource_type}/{id}"

        response = create_http_client_with_retries(verify_ssl=self.__verify_ssl).delete(url, headers=self.__headers())

        logger.debug(
            f"Got {response.status_code} status code while making request DELETE {url}\nResponse body: {response.text}",
            extra=self.__log_extra,
        )

        if response.status_code in _HANDLED_STATUS_CODES:
            result: KcResourceDeleteResponse = KcResourceDeleteResponse.Schema().load(
                response.json()
            )
            if wait:
                self.wait_request_satisfied(result.requestId, 300)
            return result
        else:
            raise Exception(
                f"Got {response.status_code} status code while making request DELETE {url}\nResponse body: {response.text}"
            )

    def read(self, id: str) -> KcResourceReadResponse:
        """Read a single resource by id.

        Args:
            id: id of the resource to read.

        Returns:
            KcResourceReadResponse with `resource` (a raw dict) on success, or
            `errorMessage`/`errorCode` set on a handled error.

        Raises:
            Exception: on an unexpected HTTP status.
        """
        url = f"{self.__api_url}/api/v1/{self.__resource_type}/{id}"

        response = create_http_client_with_retries(verify_ssl=self.__verify_ssl).get(url, headers=self.__headers())

        logger.debug(
            f"Got {response.status_code} status code while making request GET {url}\nResponse body: {response.text}",
            extra=self.__log_extra,
        )

        if response.status_code in _HANDLED_STATUS_CODES:
            return KcResourceReadResponse.Schema().load(response.json())
        else:
            raise Exception(
                f"Got {response.status_code} status code while making request GET {url}\nResponse body: {response.text}"
            )

    def get_by_labels(
        self, labels: dict = None, enumerator_id: str = None, max_page_size: int = 10
    ) -> KcResourceGetByLabelsResponse:
        """List resources of this type, filtered by labels and paginated.

        Args:
            labels: label filter as a dict; values may use `*` wildcards. Empty/None
                matches all. Defaults to None (treated as no filter).
            enumerator_id: pagination cursor from a previous call's
                `pagination.enumeratorId`; None starts at the first page.
            max_page_size: max resources per page. **Must not exceed 100** or the
                API returns `PageSizeTooBig`.

        Returns:
            KcResourceGetByLabelsResponse: one page in `resources`, the next-page
            cursor in `pagination`, or `errorMessage`/`errorCode` on a handled error.

        Raises:
            Exception: on an unexpected HTTP status.
        """
        url = f"{self.__api_url}/api/v1/{self.__resource_type}/get-by-labels"
        params = {
            "labels": labels if labels is not None else {},
            "maxPageSize": max_page_size,
            "enumeratorId": enumerator_id,
        }

        url = create_url_with_query_params(url, params)
        response = create_http_client_with_retries(verify_ssl=self.__verify_ssl).get(url, headers=self.__headers())

        logger.debug(
            f"Got {response.status_code} status code while making request GET {url}\nResponse body: {response.text}",
            extra=self.__log_extra,
        )

        if response.status_code in _HANDLED_STATUS_CODES:
            return KcResourceGetByLabelsResponse.Schema().load(response.json())
        else:
            raise Exception(
                f"Got {response.status_code} status code while making request GET {url}\nResponse body: {response.text}"
            )

    def read_request(self, request_id: str) -> KcResourceReadRequestResponse:
        """Read the current status of an async change request (one poll).

        Args:
            request_id: the `requestId` returned by create/update/delete.

        Returns:
            KcResourceReadRequestResponse: `succeeded` True once applied,
            `errorMessage`/`errorCode` set if it failed, otherwise still pending.

        Raises:
            Exception: on an unexpected HTTP status.
        """
        url = f"{self.__api_url}/api/v1/{self.__resource_type}/request/{request_id}"

        response = create_http_client_with_retries(verify_ssl=self.__verify_ssl).get(url, headers=self.__headers())

        logger.debug(
            f"Got {response.status_code} status code while making request GET {url}\nResponse body: {response.text}",
            extra=self.__log_extra,
        )

        if response.status_code in _HANDLED_STATUS_CODES:
            return KcResourceReadRequestResponse.Schema().load(response.json())
        else:
            raise Exception(
                f"Got {response.status_code} status code while making request GET {url}\nResponse body: {response.text}"
            )

    def wait_request_satisfied(
        self, request_id: str, timeout_seconds: int
    ) -> KcResourceReadRequestResponse:
        """Poll a change request once per second until it finishes or times out.

        Returns as soon as the request succeeds (`succeeded == True`) or fails
        (both `errorMessage` and `errorCode` set). On timeout it returns the last
        (still-pending) status rather than raising — inspect `succeeded` to tell
        the cases apart.

        Args:
            request_id: the `requestId` to poll.
            timeout_seconds: max number of 1-second polls before giving up.

        Returns:
            The final (or last-seen) KcResourceReadRequestResponse.
        """
        result = self.read_request(request_id)

        i = 0
        while result.succeeded == False and result.errorMessage == None:
            i = i + 1
            if i > timeout_seconds:
                return result  # timed out while still pending

            time.sleep(1)
            result = self.read_request(request_id)

            if result.succeeded == True:
                return result  # applied successfully
            if result.errorMessage != None and result.errorCode != None:
                return result  # failed terminally

        return result

    def create_or_update(self, data: dict) -> KcResourceCreateResponse:
        """Create a resource, or update it if one with the same id already exists.

        Idempotent on the resource id: if no id is present in `data` (neither
        `data["metadata"]["id"]` for the envelope shape nor top-level `data["id"]`
        for the flat shape), a fresh ULID is generated and used, so re-sending the
        same `data` object updates the same resource.

        Args:
            data: the resource body, either the kubectl-style envelope
                ({"metadata": {...}, "spec": {...}}) or the flat shape. **Mutated
                in place** to inject the generated id when absent.

        Returns:
            KcResourceCreateResponse with `requestId`/`resourceId`, or
            `errorMessage`/`errorCode` on a handled error.

        Raises:
            Exception: on an unexpected HTTP status.
        """
        logger.debug(f"create_or_update({data})")

        # Inject a ULID id when absent so the call is idempotent and the caller can
        # poll the returned requestId. Two body shapes are supported:
        # py-ulid's ULID has no __str__/__repr__ override, so str(ULID()) yields
        # "<ulid.ulid.ULID object at 0x...>" instead of the Crockford-base32 string the
        # API requires — always use .generate() to get the actual string form.
        if "metadata" in data:
            # Envelope shape: id lives under metadata.
            if "id" not in data["metadata"] or not data["metadata"]["id"]:
                data["metadata"]["id"] = ULID().generate()
        elif "id" not in data or not data["id"]:
            # Flat shape: id at the top level.
            data["id"] = ULID().generate()

        url = f"{self.__api_url}/api/v1/{self.__resource_type}"

        response = create_http_client_with_retries(verify_ssl=self.__verify_ssl).put(url, json=data, headers=self.__headers())

        logger.debug(
            f"Got {response.status_code} status code while making request PUT {url}\nRequest body: {data}\nResponse body: {response.text}",
            extra=self.__log_extra,
        )

        if response.status_code in _HANDLED_STATUS_CODES:
            return KcResourceCreateResponse.Schema().load(response.json())
        else:
            raise Exception(
                f"Got {response.status_code} status code while making request PUT {url}\nRequest body: {data}\nResponse body: {response.text}"
            )

    def create(self, data: dict) -> KcResourceCreateResponse:
        """Left for compatibility! Use create_or_update instead.

        Args:
            data: see `create_or_update`.
        """
        return self.create_or_update(data)

    def update(self, data: dict) -> KcResourceUpdateResponse:
        """Left for compatibility! Use create_or_update instead.

        Args:
            data: see `create_or_update`.
        """
        return self.create_or_update(data)


class KcClient:
    """Top-level Kvindo Cloud API client.

    Construct once with a token, then access a per-type `KcResourceClient` via the
    attributes below (e.g. `KcClient(token).vms.get_by_labels(...)`).

    See https://cloud-api.kvindo.ru/swagger/index.html. The resource surface
    mirrors the maintained C# client KvindoCloud.Api/KvindoCloudClient.cs.
    """

    # Compute
    vms: KcResourceClient
    on_off_schedules: KcResourceClient
    vm_command_schedules: KcResourceClient
    volumes: KcResourceClient
    volume_attachments: KcResourceClient
    images: KcResourceClient
    image_schedules: KcResourceClient
    ssh_keys: KcResourceClient
    ssh_private_keys: KcResourceClient
    certificates: KcResourceClient

    # Networking
    floating_ips: KcResourceClient
    vpcs: KcResourceClient
    vpc_subnets: KcResourceClient
    vpc_peerings: KcResourceClient
    vpc_peering_peers: KcResourceClient
    vpc_peering_external_peers: KcResourceClient
    route_tables: KcResourceClient
    route_table_attachments: KcResourceClient
    route_table_routes: KcResourceClient
    security_groups: KcResourceClient

    # Load balancer
    load_balancers: KcResourceClient
    load_balancer_http_listeners: KcResourceClient
    load_balancer_http_listener_rules: KcResourceClient
    load_balancer_https_listeners: KcResourceClient
    load_balancer_https_listener_rules: KcResourceClient
    load_balancer_tcp_listeners: KcResourceClient
    load_balancer_tcp_listener_rules: KcResourceClient
    load_balancer_udp_listeners: KcResourceClient
    load_balancer_udp_listener_rules: KcResourceClient
    load_balancer_tls_listeners: KcResourceClient
    load_balancer_tls_listener_rules: KcResourceClient
    load_balancer_target_groups: KcResourceClient
    load_balancer_target_group_service_discovery_targets: KcResourceClient
    load_balancer_target_group_static_targets: KcResourceClient

    # S3
    s3_buckets: KcResourceClient
    s3_users: KcResourceClient
    s3_user_access_policies: KcResourceClient

    # Managed services
    kubernetes: KcResourceClient
    kubernetes_node_groups: KcResourceClient
    kubernetes_users: KcResourceClient
    kubernetes_user_roles: KcResourceClient
    postgresql_parameters_sets: KcResourceClient
    open_vpns: KcResourceClient
    open_vpn_users: KcResourceClient
    open_vpn_user_settings: KcResourceClient
    gitlabs: KcResourceClient
    gitlab_runners: KcResourceClient
    ollamas: KcResourceClient
    etcds: KcResourceClient
    valkeys: KcResourceClient
    valkey_parameters_sets: KcResourceClient

    # IaM / org
    folders: KcResourceClient
    hosting_providers: KcResourceClient
    access_policies: KcResourceClient
    users: KcResourceClient
    user_tokens: KcResourceClient
    billing_accounts: KcResourceClient
    quotas: KcResourceClient
    quota_change_requests: KcResourceClient
    support_plans: KcResourceClient
    support_tickets: KcResourceClient
    support_ticket_comments: KcResourceClient
    support_ticket_comment_attachments: KcResourceClient
    transactions: KcResourceClient

    def __init__(
        self,
        token: str,
        api_url: str = "https://cloud-api.kvindo.ru",
        log_extra: dict = None,
        verify_ssl: bool = True,
    ):
        """
        Args:
            token: the bearer token; a leading "Bearer " prefix is stripped if present.
            api_url: base URL of the Cloud API (no trailing slash).
            log_extra: optional dict merged into every debug log record's `extra`;
                propagated to every per-resource client.
            verify_ssl: enable/disable TLS certificate verification for every
                request this client (and every per-resource client it builds)
                makes. Defaults to True; pass False only to point this SDK at
                a genuinely self-hosted instance with a self-signed cert.
        """
        self.__log_extra = log_extra if log_extra is not None else {}
        self.__token = token.replace("Bearer ", "")
        self.__api_url = api_url
        self.__verify_ssl = verify_ssl
        # Cached response of get_transaction_collection_keys (lazy, fetched once).
        self._transaction_collection_keys = None

        def _r(resource_type: str) -> KcResourceClient:
            """Build a per-type client sharing this client's token/url/log_extra/verify_ssl."""
            return KcResourceClient(resource_type, token, api_url, log_extra, verify_ssl)

        # Compute
        self.vms = _r("vm")
        self.on_off_schedules = _r("on-off-schedule")
        self.vm_command_schedules = _r("vm-command-schedule")
        self.volumes = _r("volume")
        self.volume_attachments = _r("volume-attachment")
        self.images = _r("image")
        self.image_schedules = _r("image-schedule")
        self.ssh_keys = _r("ssh-key")
        self.ssh_private_keys = _r("ssh-private-key")
        self.certificates = _r("certificate")

        # Networking
        self.floating_ips = _r("floating-ip")
        self.vpcs = _r("vpc")
        self.vpc_subnets = _r("vpc-subnet")
        self.vpc_peerings = _r("vpc-peering")
        self.vpc_peering_peers = _r("vpc-peering-peer")
        self.vpc_peering_external_peers = _r("vpc-peering-external-peer")
        self.route_tables = _r("route-table")
        self.route_table_attachments = _r("route-table-attachment")
        self.route_table_routes = _r("route-table-route")
        self.security_groups = _r("security-group")

        # Load balancer
        self.load_balancers = _r("loadbalancer")
        self.load_balancer_http_listeners = _r("loadbalancer-http-listener")
        self.load_balancer_http_listener_rules = _r("loadbalancer-http-listener-rule")
        self.load_balancer_https_listeners = _r("loadbalancer-https-listener")
        self.load_balancer_https_listener_rules = _r("loadbalancer-https-listener-rule")
        self.load_balancer_tcp_listeners = _r("loadbalancer-tcp-listener")
        self.load_balancer_tcp_listener_rules = _r("loadbalancer-tcp-listener-rule")
        self.load_balancer_udp_listeners = _r("loadbalancer-udp-listener")
        self.load_balancer_udp_listener_rules = _r("loadbalancer-udp-listener-rule")
        self.load_balancer_tls_listeners = _r("loadbalancer-tls-listener")
        self.load_balancer_tls_listener_rules = _r("loadbalancer-tls-listener-rule")
        self.load_balancer_target_groups = _r("loadbalancer-target-group")
        self.load_balancer_target_group_service_discovery_targets = _r("loadbalancer-target-group-service-discovery-target")
        self.load_balancer_target_group_static_targets = _r("loadbalancer-target-group-static-target")

        # S3
        self.s3_buckets = _r("s3-bucket")
        self.s3_users = _r("s3-user")
        self.s3_user_access_policies = _r("s3-user-access-policy")

        # Managed services
        self.kubernetes = _r("kubernetes")
        self.kubernetes_node_groups = _r("kubernetes-node-group")
        self.kubernetes_users = _r("kubernetes-user")
        self.kubernetes_user_roles = _r("kubernetes-user-role")
        self.postgresql_parameters_sets = _r("postgresql-parameters-set")
        self.open_vpns = _r("open-vpn")
        self.open_vpn_users = _r("open-vpn-user")
        self.open_vpn_user_settings = _r("open-vpn-user-settings")
        self.gitlabs = _r("gitlab")
        self.gitlab_runners = _r("gitlab-runner")
        self.ollamas = _r("ollama")
        self.etcds = _r("etcd")
        self.valkeys = _r("valkey")
        self.valkey_parameters_sets = _r("valkey-parameters-set")

        # IaM / org
        self.folders = _r("folder")
        self.hosting_providers = _r("hosting-provider")
        self.access_policies = _r("access-policy")
        self.users = _r("user")
        self.user_tokens = _r("user-token")
        self.billing_accounts = _r("billing-account")
        self.quotas = _r("quota")
        self.quota_change_requests = _r("quota-change-request")
        self.support_plans = _r("support-plan")
        self.support_tickets = _r("support-ticket")
        self.support_ticket_comments = _r("support-ticket-comment")
        self.support_ticket_comment_attachments = _r("support-ticket-comment-attachment")
        self.transactions = _r("transaction")

    def get_transaction_collection_keys(self) -> list:
        """Return the transaction-spec collection keys (the child-resource
        collection names accepted inside an OrganizationTransaction).

        Fetched once from `/api/v1/internal/transaction-spec` and cached on the
        instance for subsequent calls.

        Returns:
            The raw list returned by the transaction-spec endpoint.

        Raises:
            Exception: on an unexpected HTTP status. Unlike the per-resource
                methods, any non-200 here is treated as unhandled (this
                endpoint has no typed error-code contract) — previously this
                method cached whatever `.json()` returned regardless of status,
                so a transient failure (e.g. an auth blip) got cached as if it
                were the real key list, permanently, for this instance's life.
        """
        if self._transaction_collection_keys is None:
            url = f"{self.__api_url}/api/v1/internal/transaction-spec"
            headers = {"Authorization": f"Bearer {self.__token}"}
            response = create_http_client_with_retries(verify_ssl=self.__verify_ssl).get(url, headers=headers)
            if response.status_code != 200:
                raise Exception(
                    f"Got {response.status_code} status code while making request GET {url}\nResponse body: {response.text}"
                )
            self._transaction_collection_keys = response.json()
        return self._transaction_collection_keys
