import hashlib
import os
import re
import secrets
import string
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

from authlib.integrations.starlette_client import OAuth
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

import db
import importers
import r2

INDEX_HTML = (Path(__file__).resolve().parent / "static" / "index.html").read_text()

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-me")
ALLOWED_EMAIL_DOMAIN = os.getenv("ALLOWED_EMAIL_DOMAIN", "assemblyai.com")
# Where the app itself lives (used for the OAuth callback). Falls back to the request host.
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
# What short links look like, e.g. https://go.example.com. Falls back to the request host.
SHORT_BASE_URL = os.getenv("SHORT_BASE_URL", "").rstrip("/")
# Where the bare short domain (e.g. https://assembly.ai/) sends people.
ROOT_REDIRECT_URL = os.getenv("ROOT_REDIRECT_URL", "https://www.assemblyai.com")
IS_PROD = os.getenv("VERCEL_ENV") == "production"

# First path segments that belong to the app and can't be used as short-link keys.
RESERVED = {"", "api", "auth", "health", "static", "favicon.ico", "robots.txt"}
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~/-]{0,199}$")

app = FastAPI(title="publiclinks", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, https_only=IS_PROD, same_site="lax")

oauth = OAuth()
oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


# --- auth -------------------------------------------------------------------

def current_email(request: Request) -> str | None:
    return request.session.get("email")


def require_session(request: Request) -> str:
    """Browser login only — used for managing API keys."""
    email = current_email(request)
    if not email:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return email


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def require_auth(request: Request) -> str:
    """Browser session, or `Authorization: Bearer pl_...` API key."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        email = db.email_for_api_key(hash_key(auth[7:].strip()))
        if not email:
            raise HTTPException(status_code=401, detail="Invalid API key")
        return email
    return require_session(request)


def base_url(request: Request) -> str:
    return BASE_URL or str(request.base_url).rstrip("/")


@app.get("/auth/login")
async def login(request: Request):
    return await oauth.google.authorize_redirect(request, f"{base_url(request)}/auth/callback")


@app.get("/auth/callback")
async def auth_callback(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as e:
        print(f"Auth error: {e}")
        raise HTTPException(status_code=400, detail="Authentication failed")
    info = token.get("userinfo") or {}
    email = info.get("email", "")
    if not info.get("email_verified") or not email.endswith(f"@{ALLOWED_EMAIL_DOMAIN}"):
        return HTMLResponse(
            f"<p style='font-family:monospace;padding:40px'>Only @{ALLOWED_EMAIL_DOMAIN} accounts "
            f"are allowed. <a href='/'>back</a></p>",
            status_code=403,
        )
    request.session["email"] = email
    return RedirectResponse("/", status_code=302)


@app.get("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=302)


@app.get("/api/me")
async def me(request: Request):
    email = current_email(request)
    if not email:
        return JSONResponse({"email": None}, status_code=401)
    return {
        "email": email,
        "short_base_url": SHORT_BASE_URL or base_url(request),
        "uploads": r2.enabled(),
    }


# --- links API --------------------------------------------------------------

def clean_key(key: str) -> str:
    key = key.strip().strip("/")
    if not KEY_RE.match(key) or "//" in key:
        raise HTTPException(400, "Key may only contain letters, numbers and . _ ~ / -")
    if key.split("/")[0].lower() in RESERVED:
        raise HTTPException(400, f"'{key.split('/')[0]}' is reserved")
    return key


def clean_url(url: str) -> str:
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(400, "Destination must be an http(s) URL")
    return url


def random_key(n: int = 7) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


@app.get("/api/links")
async def list_links(_: str = Depends(require_auth)):
    return {"links": db.list_links()}


@app.post("/api/links")
async def create_link(request: Request, email: str = Depends(require_auth)):
    body = await request.json()
    url = clean_url(body.get("url", ""))
    custom = (body.get("key") or "").strip()
    if custom:
        key = clean_key(custom)
        try:
            db.create_link(key, url, email)
        except db.KeyTaken:
            raise HTTPException(409, f"'{key}' is already taken")
    else:
        for _ in range(5):
            key = random_key()
            try:
                db.create_link(key, url, email)
                break
            except db.KeyTaken:
                continue
        else:
            raise HTTPException(500, "Could not generate a free key")
    return db.get_link(key)


@app.put("/api/links/{key:path}")
async def update_link(key: str, request: Request, _: str = Depends(require_auth)):
    if not db.get_link(key):
        raise HTTPException(404, "Link not found")
    body = await request.json()
    new_key = clean_key(body.get("key") or key)
    url = clean_url(body.get("url", ""))
    try:
        db.update_link(key, new_key, url)
    except db.KeyTaken:
        raise HTTPException(409, f"'{new_key}' is already taken")
    return db.get_link(new_key)


@app.delete("/api/links/{key:path}")
async def delete_link(key: str, _: str = Depends(require_auth)):
    if not db.delete_link(key):
        raise HTTPException(404, "Link not found")
    return {"ok": True}


@app.get("/api/keys")
async def list_keys(email: str = Depends(require_session)):
    return {"keys": db.list_api_keys(email)}


@app.post("/api/keys")
async def create_key(request: Request, email: str = Depends(require_session)):
    body = await request.json()
    name = (body.get("name") or "").strip()[:100] or "unnamed"
    key = "pl_" + secrets.token_urlsafe(32)
    key_id = secrets.token_hex(6)
    db.create_api_key(key_id, hash_key(key), name, email)
    return {"id": key_id, "name": name, "key": key}


@app.delete("/api/keys/{key_id}")
async def delete_key(key_id: str, email: str = Depends(require_session)):
    if not db.delete_api_key(key_id, email):
        raise HTTPException(404, "Key not found")
    return {"ok": True}


@app.post("/api/uploads")
async def create_upload(request: Request, _: str = Depends(require_auth)):
    """Returns a presigned URL; the browser PUTs the file directly to R2."""
    if not r2.enabled():
        raise HTTPException(400, "Uploads are not configured")
    body = await request.json()
    filename = re.sub(r"[^A-Za-z0-9._-]", "-", body.get("filename", "file")).strip("-") or "file"
    return r2.presign_upload(filename, body.get("content_type") or "application/octet-stream")


@app.post("/api/admin/import")
async def run_import(dry_run: bool = True, _: str = Depends(require_auth)):
    """Migrate dub.co links, then link any R2 files without one. Dry run unless ?dry_run=false."""
    dub = importers.import_dub(RESERVED, dry_run)
    files = importers.import_r2(RESERVED, dry_run) if r2.enabled() else None
    return {"dry_run": dry_run, "dub": dub, "r2": files}


# --- pages & redirects ------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


def on_short_domain(request: Request) -> bool:
    return bool(SHORT_BASE_URL) and urlparse(SHORT_BASE_URL).hostname == request.url.hostname


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # The short domain's root is public traffic, not the admin UI.
    if on_short_domain(request):
        return RedirectResponse(ROOT_REDIRECT_URL, status_code=302)
    return HTMLResponse(INDEX_HTML)


NOT_FOUND_HTML = "<p style='font-family:monospace;padding:40px'>Link not found.</p>"


@app.api_route("/{key:path}", methods=["GET", "HEAD"])
async def follow(key: str, request: Request):
    link = db.get_link(key.strip("/"))
    if not link:
        # Like dub.co did: unknown short links land on the website rather than a 404.
        if on_short_domain(request):
            return RedirectResponse(ROOT_REDIRECT_URL, status_code=302)
        return HTMLResponse(NOT_FOUND_HTML, status_code=404)
    # 302 (not 301) so edits take effect immediately instead of being cached by browsers.
    return RedirectResponse(link["url"], status_code=302, headers={"Cache-Control": "no-store"})
