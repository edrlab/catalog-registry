"""Every outbound HTTP request to a URL someone typed goes through this package (ADR-066).

- `domain/url_policy.py` (pure, shared with services): is this URL allowed at all.
- `public_backend`: connect only to globally routable addresses, the one that was checked.
- `retry`: when a failure is worth another attempt, and how long to wait.
- `safe_client`: the fetcher: redirects re-checked by hand, size caps, a deadline, retries.
"""
