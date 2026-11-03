"""Public Elysium research dashboard (Starlette + Jinja2, server-rendered).

Reads PostgreSQL only through the `public_api` views as the read-only `dashboard_ro` role
(migration 0004). It never calls Agora or any RPC: market numbers are the collector's
stored snapshots, labelled with their capture time.
"""
