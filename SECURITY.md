# Security and privacy

Report vulnerabilities privately using GitHub's repository security reporting if
available; otherwise ask the maintainer for a private channel without posting account
data. Publish only a redacted reproduction. Do not attach usage caches, ledgers,
backups, credentials, prompts or account identifiers to issues.

The Python transport sends only fixed initialize, initialized and read-only quota
requests to the installed stock CLI. It does not open credential files. The stock
CLI uses the user's existing session. Backend error text and stderr are suppressed.
No runtime third-party dependency, HTTP listener, telemetry or background process is
installed. Process cleanup is bounded; a quota failure does not grant permission.

Snapshots contain a SHA-256 account fingerprint and planning metadata. This is
pseudonymous, not anonymous. Keep the data directory private. Explicitly selected
cache metadata is preserved locally and never uploaded by this utility.

Do not edit global config/instructions concurrently with the optional integration
installer or rollback. Filesystem replacement has no cross-editor CAS guarantee.
