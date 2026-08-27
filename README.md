# kc-sdk-python — Kvindo Cloud Python SDK

[![PyPI version](https://img.shields.io/pypi/v/kc-sdk-python)](https://pypi.org/project/kc-sdk-python/)
[![Python versions](https://img.shields.io/pypi/pyversions/kc-sdk-python)](https://pypi.org/project/kc-sdk-python/)
[![License: MIT](https://img.shields.io/pypi/l/kc-sdk-python)](LICENSE)

Official **Python SDK / client for the [Kvindo Cloud](https://cloud.kvindo.com) API** — manage
cloud infrastructure as code from Python: VMs, volumes, S3 object storage, Kubernetes, load
balancers, VPCs, VPNs, and managed PostgreSQL.

A thin, typed client over the REST API: one resource client per resource type, all sharing the
same create / read / update / delete / list contract.

> **Using Claude Code?** This repo ships a [Claude Code skill](.claude/skills/kvindo-python-sdk/SKILL.md)
> that teaches Claude how to script Kvindo Cloud with this SDK — clone this repo (or otherwise have
> it on disk) and open Claude Code there to pick it up automatically.

## Install

```sh
pip install kc-sdk-python
```

Dependencies: `requests`, `marshmallow-dataclass`, `py-ulid`.

## Usage

```python
from kc_api import KcClient

client = KcClient("YOUR_API_TOKEN")  # api_url defaults to https://cloud-api.kvindo.ru
# TLS certificate verification is on by default (verify_ssl=True). Pass
# verify_ssl=False only to point this SDK at a genuinely self-hosted instance
# with a self-signed cert.

# List (label-filtered, paginated)
resp = client.vms.get_by_labels({"env": "prod"}, max_page_size=50)
for vm in resp.resources:
    print(vm["metadata"]["name"])

# Read one
vm = client.vms.read("01H...")
print(vm.resource)

# Create / update (async) then wait for it to reconcile
created = client.vms.create_or_update({
    "metadata": {"name": "my-vm", "folderId": "01H..."},
    "spec": {"offerId": "g3-1c2-100", "state": "running", ...},
})
status = client.vms.wait_request_satisfied(created.requestId, timeout_seconds=300)
assert status.succeeded

# Delete (optionally block until reconciled)
client.vms.delete("01H...", wait=True)
```

Create / update / delete are **asynchronous**: they return a `requestId`; poll
`read_request(requestId)` or use `wait_request_satisfied(...)`. Every response
object carries `errorMessage` / `errorCode` (a typed `KcApi*ErrorCode`) which are
`None` on success.

### Available resources

`KcClient` exposes one `KcResourceClient` per type, e.g. `client.vms`,
`client.volumes`, `client.s3_buckets`, `client.kubernetes`,
`client.load_balancers`, `client.vpcs`, `client.postgresqls`,
`client.folders`, `client.transactions`, … (the surface mirrors the official
Kvindo Cloud API).

## Related projects

Part of the Kvindo Cloud developer toolchain:

- **[kc CLI](https://github.com/Kvindo/kc-cli)** — kubectl-style command-line client for Kvindo Cloud.
- **[terraform-provider-kvindo](https://github.com/Kvindo/terraform-provider-kvindo)** — Terraform provider ([Registry](https://registry.terraform.io/providers/kvindo/kvindo/latest)).
- **[kc-mcp-server](https://github.com/Kvindo/kc-mcp-server)** — MCP server for Kvindo Cloud ([npm](https://www.npmjs.com/package/kc-mcp-server)), for managing resources from Claude Desktop/Code and other MCP clients.
- **[Kvindo Cloud console](https://cloud.kvindo.com)** — web UI and API.
- **[Claude Code skill](.claude/skills/kvindo-python-sdk/SKILL.md)** — lets Claude script Kvindo Cloud with this SDK conversationally.

## License

MIT
