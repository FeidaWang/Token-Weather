# Contributing

Token Weather source is MIT licensed. General questions and proposed changes use the public GitHub Issues destination in SUPPORT.md. This preparation does not publish the new runtime to that repository or create a PR automatically.

Before proposing calculator changes, run `python3 -B -m unittest discover -s mcp/tests -v`. Use synthetic numeric fixtures. Preserve null/unknown semantics, explicit provenance, separate output reserves and stateless requests. Test externally visible results and boundaries rather than wording.

Keep the pure budget path independent of local Codex transcripts, credentials and account access. Do not deploy the existing local workflow/runtime modules with the arithmetic server. Keep browser bundles/profiles read-only and retain normal `/hooks` review.

Changes to hosted data collection, authentication, logging or retention require corresponding deployment and privacy documentation. Do not commit `.env`, tokens, private chat files, caches, identity materials or personal configuration. Actual host/TLS validation is separate from local proxy simulation.

Version 1.0.0 here means the open-source MCP edition. Earlier skills-only 1.0.0 ZIPs are historical and separate; do not overwrite them. Directory identity/version reuse is not guaranteed by the source version.
