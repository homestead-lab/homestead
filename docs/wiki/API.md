# API

Homestead has an HTTP API for Home Assistant, scripts and AI agents: read the
cluster's status, and start, stop and restart containers and virtual machines.
It is reached with an **API key**, and every key expires, can do only what its
scopes allow, and works on this API alone.

The API describes itself: `GET /api/v1/openapi.json` on your Homestead returns
an [OpenAPI 3.1](https://spec.openapis.org/oas/v3.1.0) document of every
endpoint, its scope, its parameters and the shape of its answer. The same
document is in the repository as
[`docs/api/openapi.json`](https://github.com/wjcloudy/homestead/blob/main/docs/api/openapi.json),
for tools that read a spec from GitHub - load it into Swagger UI, Postman, or
an agent's tool list.

## Making a key

**Settings › Users and access › API keys › ＋ New key** (administrators only):

- **Name** - what will use it, so you know which to revoke.
- **What it may do** - its scopes:

  | Scope | Allows |
  |---|---|
  | `read` | Read status: the cluster, nodes, containers, VMs, alerts and jobs |
  | `containers:control` | Start, stop and restart containers |
  | `vms:control` | Start, stop and restart virtual machines |

- **Expires after** - a day to a year. There are no keys that never expire.
- **Only from** (optional) - addresses or networks it may be used from, such as
  your Home Assistant's address. A key copied anywhere else is refused.

The key is shown **once**, as `hsk_<id>_<secret>`. Homestead keeps only a hash
of it; if you lose it, revoke it and make another. Revoking stops a key at once.

The card lists each key's scopes, when it expires, and when and from where it
was last used. Making, revoking and refusing keys are recorded in the sign-in
history (**Events › Sign-ins**).

## Using a key

Send it in the `Authorization` header:

```sh
curl -H "Authorization: Bearer hsk_…" https://homestead.example.com/api/v1/status
```

```json
{"version": "2.8.273",
 "nodes": {"total": 3, "ready": 3},
 "containers": {"total": 24, "running": 22, "stopped": 2, "failing": 0},
 "vms": {"total": 4, "running": 3},
 "alerts": {"active": 1, "critical": 0}}
```

Starting a container:

```sh
curl -X POST -H "Authorization: Bearer hsk_…" \
  https://homestead.example.com/api/v1/containers/lab/jellyfin/start
```

```json
{"ok": true, "action": "start", "warnings": [], "job": null}
```

## Endpoints

| Method | Path | Scope | What |
|---|---|---|---|
| GET | `/api/v1/whoami` | any key | The key making the request, its scopes and expiry |
| GET | `/api/v1/status` | `read` | Counts of nodes, containers, VMs and alerts |
| GET | `/api/v1/nodes` | `read` | Each node: ready, CPU and memory use, pods |
| GET | `/api/v1/alerts` | `read` | What is wrong right now |
| GET | `/api/v1/containers` | `read` | Every container (`?namespace=` to narrow) |
| GET | `/api/v1/containers/{namespace}/{name}` | `read` | One container |
| POST | `/api/v1/containers/{namespace}/{name}/start` | `containers:control` | Start with one replica |
| POST | `/api/v1/containers/{namespace}/{name}/stop` | `containers:control` | Scale to zero |
| POST | `/api/v1/containers/{namespace}/{name}/restart` | `containers:control` | Restart its pods |
| GET | `/api/v1/vms` | `read` | Every virtual machine (`?namespace=` to narrow) |
| GET | `/api/v1/vms/{namespace}/{name}` | `read` | One virtual machine |
| POST | `/api/v1/vms/{namespace}/{name}/start` | `vms:control` | Start it |
| POST | `/api/v1/vms/{namespace}/{name}/stop` | `vms:control` | Shut it down (its power button) |
| POST | `/api/v1/vms/{namespace}/{name}/restart` | `vms:control` | Restart it |
| GET | `/api/v1/jobs/{id}` | `read` | A background job an action started |
| GET | `/api/v1/openapi.json` | any key | This API's own description |

A start goes through the same capacity check as the app. One that cannot fit
is refused with `409` and the reasons; one that fits with warnings goes ahead
and returns them - the key's control scope stands in for the confirmation the
app asks a person for. A VM start that needs a person (setting up missing TPM
or EFI state) is refused; do it in the app. Homestead's own containers cannot
be stopped through the API.

### Errors

Every error is JSON, `{"error": "what happened"}`:

| Status | Meaning |
|---|---|
| 400 | The request is malformed (a name that cannot be one) |
| 401 | No key, or one that is not valid, has expired, or is used from a network it is not allowed |
| 403 | The key does not have the scope this needs |
| 404 | No such endpoint, container, VM or job |
| 405 | That method is not used here |
| 409 | Refused: it cannot be done now - not enough room, Homestead itself, or it needs a person |
| 429 | Too many wrong keys from this address; wait a few minutes |

## Home Assistant

A sensor for active alerts, and buttons for a container - in
`configuration.yaml`:

```yaml
sensor:
  - platform: rest
    name: Homestead alerts
    resource: https://homestead.example.com/api/v1/status
    headers:
      Authorization: !secret homestead_api_key
    value_template: "{{ value_json.alerts.active }}"
    json_attributes: [nodes, containers, vms]
    scan_interval: 60

rest_command:
  jellyfin_start:
    url: https://homestead.example.com/api/v1/containers/lab/jellyfin/start
    method: post
    headers:
      Authorization: !secret homestead_api_key
  jellyfin_stop:
    url: https://homestead.example.com/api/v1/containers/lab/jellyfin/stop
    method: post
    headers:
      Authorization: !secret homestead_api_key
```

and in `secrets.yaml`:

```yaml
homestead_api_key: "Bearer hsk_…"
```

Give the key `read` and `containers:control`, and limit it to Home Assistant's
address.

## AI agents

Hand the agent a key and the description:

- the OpenAPI document - `GET /api/v1/openapi.json` with the key, or
  `docs/api/openapi.json` from the repository - lists every operation with an
  `operationId`, a summary and its answer's schema, which agent frameworks turn
  into tools directly;
- give it `read` alone until you want it to act, and the control scopes for
  only what it should touch. `GET /api/v1/whoami` tells it what it may do.

An action returns a `job` id when it starts something that takes a while (a
VM starting); `GET /api/v1/jobs/{id}` follows it to `succeeded` or `failed`.

## What a key can never do

A key works on `/api/v1` and nothing else - not on the routes the web app
uses - so whatever its scopes, it cannot:

- manage users or other keys, or change any setting;
- open a shell on a host, read a Secret, or see Kubernetes objects;
- act on a linked cluster: a key is for the Homestead that made it.

A key also acts for the administrator who made it, never beyond them: if they
are removed the key stops working, and if they are demoted it keeps only the
scopes their role still allows. Wrong keys are counted per address, as wrong
passwords are, and refused for a while after twenty.

## Stability

`/api/v1` is versioned. Within it an answer may gain fields but never loses or
renames one, and endpoints are not removed. A change that would break a client
becomes `/api/v2`, beside v1.

If Homestead is behind Cloudflare Access, requests through Cloudflare need
Access's own credentials too; reach the API on your network, or give the client
a Cloudflare service token.
