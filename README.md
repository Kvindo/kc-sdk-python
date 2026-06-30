# kc-sdk-python

Python SDK / client for the **Kvindo Cloud API**.

A thin, typed client over the REST API: one resource client per resource type
(VMs, volumes, load balancers, kubernetes, S3, VPCs, …), all sharing the same
create / read / update / delete / list contract.

## Install

```sh
pip install kc-sdk-python
```

Dependencies: `requests`, `marshmallow-dataclass`, `py-ulid`.

## Usage

```python
from kc_api import KcClient

client = KcClient("YOUR_API_TOKEN")  # api_url defaults to https://cloud-api.kvindo.ru

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
`client.load_balancers`, `client.vpcs`, `client.postgresql_standalones`,
`client.folders`, `client.transactions`, … (the surface mirrors the official
Kvindo Cloud API).

## License

MIT
