"""One-off migration: dub.co links + files already in R2 -> links table.

Exposed as POST /api/admin/import so it runs where the (sensitive) env vars live.
Never overwrites existing links, so it's safe to run more than once.
"""

import os
import re
from urllib.parse import quote, unquote

import httpx

import db
import r2


RAILWAY_FILE_RE = re.compile(r"^https://[^/]*railway\.app/f/(.+)$")
OLD_UPLOAD_RE = re.compile(r"^[0-9a-f]{8}-")  # how the old app named uploads in R2


def r2_url(object_key: str) -> str:
    return f"{r2.R2_PUBLIC_URL}/{quote(object_key)}"


def fix_url(url: str) -> str:
    """Old links to the Railway file proxy -> the file's public R2 URL."""
    m = RAILWAY_FILE_RE.match(url)
    return r2_url(unquote(m.group(1))) if m and r2.R2_PUBLIC_URL else url


def import_dub(reserved: set[str], dry_run: bool) -> dict:
    api_key, domain = os.environ["DUB_API_KEY"], os.getenv("DUB_DOMAIN")
    links, page = [], 1
    with httpx.Client(headers={"Authorization": f"Bearer {api_key}"}, timeout=30) as client:
        while True:
            params = {"page": page, "pageSize": 100}
            if domain:
                params["domain"] = domain
            r = client.get("https://api.dub.co/links", params=params)
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            links.extend(batch)
            page += 1

    imported, existing, skipped, rewritten = [], [], [], []
    for link in links:
        key, short = link.get("key"), link.get("shortLink")
        url = fix_url(link.get("url") or "")
        if url != link.get("url"):
            rewritten.append({"short": short, "from": link.get("url"), "to": url})
        if domain and link.get("domain") != domain:
            skipped.append({"short": short, "reason": "other domain"})
        elif not key or key == "_root" or not url:
            skipped.append({"short": short, "reason": "root/empty link"})
        elif key.split("/")[0].lower() in reserved:
            skipped.append({"short": short, "reason": "reserved key, recreate under a new key"})
        elif dry_run:
            imported.append(key)
        elif db.create_link(
            key, url, (link.get("user") or {}).get("email"),
            source="dub", created_at=link.get("createdAt"), skip_existing=True,
        ):
            imported.append(key)
        else:
            existing.append(key)

    return {
        "fetched": len(links),
        "imported": len(imported),
        "already_present": len(existing),
        "skipped": skipped,
        "rewritten_from_railway": rewritten,
        # minimal backup of everything dub.co had
        "export": [
            {k: l.get(k) for k in ("id", "domain", "key", "url", "shortLink", "createdAt", "clicks")}
            for l in links
        ],
    }


def _slug(object_key: str) -> str:
    name = re.sub(r"^[0-9a-f]{8}-", "", object_key)  # old uploads were "<8 hex>-<filename>"
    name = os.path.splitext(name)[0]
    name = re.sub(r"[^a-zA-Z0-9-]", "-", name)
    return re.sub(r"-+", "-", name).strip("-").lower()[:50] or "file"


def import_r2(reserved: set[str], dry_run: bool) -> dict:
    """Link files uploaded by the old app that no link points at yet. Run after import_dub.

    Only "<8 hex>-<name>" objects: the bucket also holds unrelated site assets.
    """
    links = db.list_links()
    linked_urls = {unquote(l["url"]) for l in links}
    taken = {l["key"].lower() for l in links}
    created, already = [], 0
    objects = sorted(
        (o for o in r2.list_objects() if OLD_UPLOAD_RE.match(o["Key"])), key=lambda o: o["LastModified"]
    )
    for obj in objects:
        url = r2_url(obj["Key"])
        if unquote(url) in linked_urls:
            already += 1
            continue
        base = _slug(obj["Key"])
        if base in reserved:
            base = f"file-{base}"
        key, n = base, 2
        while key.lower() in taken:
            key, n = f"{base}-{n}", n + 1
        taken.add(key.lower())
        if not dry_run:
            db.create_link(key, url, None, source="r2", created_at=obj["LastModified"].isoformat(timespec="seconds"))
        created.append({"key": key, "url": url})
    return {"files": len(objects), "created": created, "already_linked": already}
