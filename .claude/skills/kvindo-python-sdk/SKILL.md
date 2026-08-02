---
name: kvindo-python-sdk
description: >
  Use this skill when the user wants Claude to write Python code that manages Kvindo Cloud
  resources (cloud.kvindo.com) via `kc-sdk-python` — e.g. "script this with the Kvindo Python
  SDK", "use KcClient to create a VM/S3 bucket/etc.", "automate Kvindo Cloud from Python", or any
  request mentioning `kc_api`, `KcClient`, `KcResourceClient`, or `client.vms`/`client.s3_buckets`-
  style attribute access. Requires the package already installed (`pip install kc-sdk-python`) —
  this skill is about using the client, not developing or releasing the SDK (that's the monorepo's
  own `kvindo-python-sdk-release` skill).
version: 1.0.0
---

# Using the Kvindo Cloud Python SDK (`kc-sdk-python`)

Written against `v3.1.0` (current). This is a small, single-file client (`kc_api.py`) — every claim
below was checked directly against that file, not inferred from the README alone.

## TLS certificate verification is on by default (as of v3.1.0)

Every HTTP request this SDK makes — all 6 call sites that talk to the network (`KcResourceClient`'s
`delete`, `read`, `get_by_labels`, `read_request`, `create_or_update`, plus `KcClient`'s
`get_transaction_collection_keys`) — goes through `create_http_client_with_retries()`, whose
`verify_ssl` parameter now defaults to `True`, threaded down from a `verify_ssl` parameter on both
`KcResourceClient`/`KcClient`. **Prior versions (`v3.0.0` and earlier) defaulted `verify_ssl=False`
at every call site with no public override — every request, including against the real public API,
skipped TLS certificate validation.** If you (or code you're helping someone with) is still on an
older version, upgrade rather than working around it — `pip install -U kc-sdk-python`.

Pass `verify_ssl=False` explicitly only to point this SDK at a genuinely self-hosted instance with a
self-signed certificate:

```python
client = kc_api.KcClient(token, verify_ssl=False)  # only for a genuinely self-signed target
```

## The contract

One `KcResourceClient` per resource type, off a single `KcClient(token)` (e.g. `client.vms`,
`client.s3_buckets`, `client.kubernetes`, …). Every resource client shares 5 methods:

- `read(id)` — one resource, raw `dict`.
- `get_by_labels(labels, enumerator_id, max_page_size)` — paginated list.
- `create_or_update(data)` — create or update, idempotent on id.
- `delete(id, wait=)` — delete, optionally block until reconciled.
- `read_request(request_id)` / `wait_request_satisfied(request_id, timeout_seconds)` — poll an async
  change request.

`create`/`update` exist only as legacy aliases for `create_or_update` — use `create_or_update`
directly.

`KcClient` itself has one more method, `get_transaction_collection_keys()` (the valid child-
collection keys for a bulk `transaction` create). **As of v3.1.0 it checks the response status before
caching** — before that, a failed call (auth error, 500, anything that still returned a JSON body)
got cached as if it were the real list, for the rest of that `KcClient` instance's life. On an older
version, treat unexpected/empty-looking output from this method with suspicion and construct a fresh
`KcClient` rather than trusting the cache.

## Everything is async — always check `.succeeded`, don't assume completion

`create_or_update`/`delete` return immediately with a `requestId`; provisioning happens server-side.
Poll `read_request`/`wait_request_satisfied` rather than treating the initial call returning as proof
the resource is ready.

**`wait_request_satisfied` returns on timeout too, silently** — it returns the last-seen (still
pending) status without raising when `timeout_seconds` runs out. The only way to tell "succeeded"
from "timed out still pending" apart is checking `.succeeded` on the result:

```python
created = client.vms.create_or_update({...})
status = client.vms.wait_request_satisfied(created.requestId, timeout_seconds=300)
assert status.succeeded  # NOT proof of anything unless you check this
```

There's no built-in default for `timeout_seconds` — the SDK's own `delete(wait=True)` uses `300`
internally, a reasonable starting point; scale up for something known to be slow (a VM, a Kubernetes
cluster).

**On a timeout, don't blindly fire a new `create_or_update`** — the original request may genuinely
still be in flight, and a second one risks `ResourceIsScheduling` rather than helping. Re-poll
`read_request` with the *same* `requestId`, or `read()` the resource directly, before deciding a
fresh call is actually needed. If re-polling itself keeps timing out on consecutive attempts, stop
and surface that rather than looping indefinitely — that's worth a human look, not an ever-longer
wait.

## Errors mostly aren't exceptions

HTTP 200/400/401/403/422 are all "handled" and come back as a typed response object with
`errorMessage`/`errorCode` (`None` on success) — not a raised exception. Only a genuinely unexpected
status code raises. **Branch on `errorCode`**, not `errorMessage` (human text, not for program
logic) — and each method's `errorCode` is a *different* enum (verified against every response
dataclass in `kc_api.py`):

| Method(s) | Error enum |
|---|---|
| `delete`, `create_or_update` | `KcApiModificationErrorCode` (`NotFound`, `ResourceIsDeleteProtected`, `MissingIdField`, `MissingNameField`, `ResourceIsScheduling`, `Unauthorized`, `BadData`, `Unknown`) |
| `read` | `KcApiReadErrorCode` |
| `read_request` / `wait_request_satisfied` | `KcApiReadRequestErrorCode` |
| `get_by_labels` | `KcApiGetByLabelsErrorCode` (`PageSizeTooBig`, `EnumeratorNotFound`, …) |

Don't assume a code from one enum can appear on a different call — `ResourceIsDeleteProtected`, for
instance, only makes sense on `delete`/`create_or_update`.

## No local schema — discovery is always against something live

`read()` returns `resource` as an untyped raw `dict`; `create_or_update` accepts a raw `dict`. There
is no per-resource-type schema anywhere in this SDK to check offline. To learn a resource's actual
field shape: the swagger UI/JSON at `cloud-api.kvindo.ru/swagger` (or `.com`, see below — same API),
a `read()` on an existing resource of the same type, or `kc get <type> <name> -o yaml`/`-o json` if
the `kc` CLI and its `kvindo-cloud` skill are available (all three toolchain skills compose this way).

**`create_or_update` accepts two body shapes**, detected by a heuristic, not real validation: the
kubectl-style envelope (`{"metadata": {...}, "spec": {...}}`) or a flat dict — the SDK picks based on
whether `"metadata"` is a key in what you passed. There's no guard against a wrong guess beyond the
API itself rejecting it. If the server complains about an unexpected field shape, that's the signal
to check a live `read()` rather than guessing the other shape blind.

**`create_or_update` mutates its input `dict` in place** to inject a generated id when one's missing
— it doesn't return a new dict. This happens as plain Python code *before* the HTTP request is sent
(the id-injection runs, then the URL is built, then the request goes out), so it's unconditional, not
dependent on the request succeeding — retrying with the *same* dict object after any failure reuses
the id already injected, which is exactly what makes retries idempotent. The mutation itself is still
worth knowing about (a function that mutates its argument *and* returns a separate response object is
easy to be surprised by), just not something to work around.

**You usually don't need to generate an id yourself** — omit it and let the SDK generate one.
Generate one explicitly only to (a) pre-assign an id before the resource exists (cross-referencing a
sub-resource inside a transaction, same pattern the CLI/Terraform skills use) or (b) guarantee the
same resource gets the same id across independent script runs. When you do: **`ULID().generate()`,
not `str(ULID())`** — `py-ulid`'s `ULID` has no `__str__`/`__repr__` override, so `str(ULID())`
yields a Python object repr, not the Crockford-base32 string the API needs. (A real, fixed bug in
this SDK's own history, per its `v1.0.0` changelog.)

## Other things worth knowing

- **`api_url` defaults to `https://cloud-api.kvindo.ru`**, while `kc`/Terraform default to
  `https://cloud-api.kvindo.com` — verified live, at time of writing, to be the identical API on
  both hosts (matching `swagger.json`'s `info` block exactly), so this isn't a bug. If a script's
  behavior via this SDK genuinely diverges from `kc`/Terraform against the same account, checking
  whether the two hosts still agree is a reasonable thing to re-check, not an assumption to trust
  forever. Pass `api_url="https://cloud-api.kvindo.com"` explicitly if you want one canonical host
  across your whole toolchain.
- **Pagination**: `max_page_size` on `get_by_labels` must not exceed 100 (`PageSizeTooBig`). Loop
  using the returned `pagination.enumeratorId` as the next call's `enumerator_id`. The cursor can
  expire mid-pagination (`EnumeratorNotFound`) — restart from the top once; if restarting keeps
  hitting the same expiration, narrow the `labels` filter instead of looping forever.
- **The resource-attribute list on `KcClient` (`client.vms`, …) is complete for this SDK version,
  but not necessarily for the live API.** It's hardcoded in `kc_api.py`, so it can't drift from what
  *this installed version* exposes — but a resource type the API added after this release won't have
  an attribute yet. If a needed type is missing from `dir(client)`, check the swagger or
  `kc api-resources` before concluding it doesn't exist — every type follows the same REST contract
  (`PUT`/`GET <id>`/`DELETE <id>`/`GET .../get-by-labels` under `/api/v1/<kebab-case-type>`), so you
  can reach a not-yet-wrapped type directly via `requests`, or by constructing
  `KcResourceClient("the-kebab-case-type", token)` yourself — it's a public class, not private to
  `KcClient`.
- **Connection pooling doesn't actually happen**, despite the docstring saying "reuse it for
  connection pooling" — every request builds a fresh `requests.Session`. Fine for occasional use;
  for a bulk script (hundreds+ calls) it's a real cumulative cost with no built-in way to opt out of
  yet — a caller who needs real session reuse would have to monkeypatch
  `kc_api.create_http_client_with_retries` themselves.
- Requires Python >= 3.8; depends on `requests`, `marshmallow-dataclass`, `py-ulid`.

## Further reading

- Full `README.md` in this repo.
- `docs.kvindo.ru/sdk/api-getting-started` — REST API concepts the SDK wraps.
- `cloud-api.kvindo.ru/swagger` (or `.com`) — the full, current API contract.
