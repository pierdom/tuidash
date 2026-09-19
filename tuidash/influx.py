from __future__ import annotations

import requests

from . import config


def query(q: str, timeout: float = 8.0) -> list[dict]:
    """Run an InfluxQL query, return one flat dict per result row (tags + fields + time merged).

    Raises on HTTP/connection errors or on a non-empty "error" field in the response body.
    Returns an empty list for a query that matched no series (not an error).
    """
    url = config.require("TUIDASH_INFLUXDB_URL").rstrip("/") + "/query"
    token = config.require("TUIDASH_INFLUXDB_TOKEN")
    db = config.get("TUIDASH_INFLUXDB_BUCKET", "homelab")

    r = requests.get(
        url,
        params={"db": db, "q": q},
        headers={"Authorization": f"Token {token}"},
        timeout=timeout,
    )
    r.raise_for_status()
    body = r.json()

    rows: list[dict] = []
    for result in body.get("results", []):
        if "error" in result:
            raise RuntimeError(result["error"])
        for series in result.get("series", []):
            cols = series["columns"]
            tags = series.get("tags", {})
            for values in series["values"]:
                row = dict(zip(cols, values))
                row.update(tags)
                rows.append(row)
    return rows


def last(q: str) -> dict | None:
    """Convenience wrapper for a query expected to return at most one row."""
    rows = query(q)
    return rows[0] if rows else None
