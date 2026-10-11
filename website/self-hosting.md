# Deploy Token Weather yourself

The source is MIT licensed. You choose and control the machine and network. The publisher has no shared budget endpoint and does not receive requests from your deployment through this software. Local use needs no hosting. Your chosen server/domain/network may have costs; no free-tier guarantee is made.

## Native Python behind your own HTTPS proxy

Use an existing Python 3.9+ runtime. On the server, run this from the package root:

```sh
python3 -B mcp/server.py --transport http --bind 127.0.0.1 --port 8000 --public-origin https://YOUR-ACTUAL-MCP-HOST
```

The uppercase host is documentation only; replace it with your actual HTTPS origin, without `/mcp`, credentials, query or fragment. Proxy `/mcp` to `http://127.0.0.1:8000/mcp`, preserve the public Host header, keep the backend private, and terminate TLS at your proxy. The client endpoint is `https://your-actual-host/mcp`. No example hostname is included in active remote configuration.

Loopback is the default. `--bind 0.0.0.0` is available only with an explicit `--public-origin` (or TOKEN_WEATHER_PUBLIC_ORIGIN). Use it inside a private container network or behind your own firewall/proxy. The Python server itself does not terminate TLS. Never expose its plaintext backend as the client endpoint.

## Optional Docker and Caddy

Use this only on a server you own, with Docker/Compose already available or installed through your own chosen workflow. DNS must point your real hostname to that server, and ports 80/443 must support your certificate setup. Container images must be downloaded by the deployer. Docker/Caddy has not been executed in this release test. The native Python backend has been tested behind a real temporary loopback TLS proxy; no public deployment occurred.

Set your real hostname in the environment before invoking Compose:

```sh
MCP_HOST=your-actual-host docker compose -f deploy/compose.yaml up --build -d
```

The example creates a non-root read-only MCP container and a Caddy TLS proxy. Port 8000 is not published to the host. Only the two pure calculator/server files enter the MCP image; local Codex records, runtime and checkpoints are not copied. Proxy volumes store certificate/configuration state. Review image versions, server capacity, firewall and provider charges before using it.

To stop your own deployment:

```sh
docker compose -f deploy/compose.yaml down
```

Named certificate/configuration volumes remain unless you deliberately remove them. Do not delete unrelated volumes. The example has no access-log directive, but host/container/proxy/provider diagnostic logs may still exist. Set retention and controls for your own deployment; do not promise no logs merely because calculator request persistence is absent.

## Verify and connect

Use an MCP client to initialise, discover `plan_context_budget` and run the synthetic cases. HTTP requests use the negotiated `MCP-Protocol-Version: 2025-11-25`, JSON content and acceptance of JSON plus SSE. The server returns stateless JSON, no SSE stream or session history. Actual TLS/DNS/host integration still needs verification on your deployment.

After verifying your endpoint, generate a separate remote declaration:

```sh
python3 -B scripts/configure-remote.py --url https://your-actual-host/mcp --output mcp.remote.json
```

This writes a new local file only and refuses overwriting existing files. It does not register a client, deploy, upload or verify network access. Use your host's supported custom remote MCP connection flow. Authentication is not implemented by the calculator; if you add private accounts/actions, redesign authorisation before exposing them. Use your own network/proxy access controls for private deployments.

## Public directory boundary

A user-controlled endpoint serves that user's connection; it is not automatically the universal public endpoint required for a directory submission. Official template URL support is limited to trusted developers. A public With MCP package needs a genuine accessible endpoint, relevant published policies, host recording and portal checks. This source ZIP is not that completed public-upload package.

For zero publisher hosting expenditure, distribute the source and let users operate their own instances. Do not offer a shared service or create resources on behalf of users without a separate request.

## Reproduce the native deployment test

With an existing Python runtime and OpenSSL executable, run:

```sh
python3 -B scripts/test-native-deployment.py --report native-deployment-test.json
```

This starts the actual Python backend and a temporary TLS reverse proxy on loopback. The client validates the test certificate and hostname using a private test trust context. It runs initialization, discovery, all eight numeric scenarios, origin/protocol rejection, then stops all test processes and deletes temporary keys. It changes no system trust store and calls no model/cloud API. This verifies the native local deployment path, not Docker/Caddy, public DNS or a live ChatGPT connection.
