#!/usr/bin/env python3
"""Write a new remote MCP declaration for an endpoint the deployer has verified."""
import argparse
import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit


def remote_url(value):
    parts = urlsplit(value)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or parts.path != "/mcp"
            or not parts.netloc.isascii() or any(c.isspace() for c in parts.netloc)):
        raise ValueError("Use your real HTTPS /mcp endpoint, without credentials, query or fragment.")
    _port = parts.port
    hostname = parts.hostname.lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".invalid", ".example")) or hostname in ("example.com", "example.org", "example.net") or hostname.endswith((".example.com", ".example.org", ".example.net")):
        raise ValueError("Local or documentation-only domains are not an endpoint.")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if "." not in hostname:
            raise ValueError("Use a public DNS name.") from None
    else:
        if not address.is_global:
            raise ValueError("Private and loopback endpoints are unsupported.")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        url = remote_url(args.url)
    except ValueError as exc:
        parser.error(str(exc))
    config = {"$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
              "mcpServers": {"context-budget": {"type": "streamable-http", "url": url}}}
    # Exclusive creation; no modification of an installed client or existing file.
    try:
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(config, output, indent=2)
            output.write("\n")
    except FileExistsError:
        parser.error("Output already exists; choose a new file. Nothing was overwritten.")
    print("Created a local configuration file only. Endpoint reachability and host integration are not verified by this helper.")


if __name__ == "__main__":
    main()
