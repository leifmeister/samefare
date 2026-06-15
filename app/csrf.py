"""
CSRF protection — double-submit cookie validated as a global dependency.

A non-HttpOnly `csrftoken` cookie is issued by the security middleware. The
client (base.html) echoes it back on every unsafe request: as a hidden
`csrf_token` form field, and as an `X-CSRF-Token` header for HTMX/fetch. This
dependency validates the echo matches the cookie.

Why a dependency (not raw ASGI middleware): FastAPI memoises `request.form()`,
so reading the form here does NOT consume the body — the route's own `Form(...)`
params still parse normally. Raw Starlette middleware reading the body would
break every form POST.
"""
import secrets

from fastapi import HTTPException, Request

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
# Server-to-server callbacks authenticate by provider HMAC signature, not a
# browser cookie — they legitimately can't carry a CSRF token.
_EXEMPT_PREFIXES = ("/webhooks",)


async def csrf_protect(request: Request) -> None:
    if request.method in _SAFE_METHODS:
        return
    path = request.url.path
    if any(path.startswith(p) for p in _EXEMPT_PREFIXES):
        return

    cookie_token = request.cookies.get("csrftoken")
    sent = request.headers.get("x-csrf-token")
    if not sent:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith("application/x-www-form-urlencoded") or ctype.startswith("multipart/form-data"):
            try:
                form = await request.form()          # memoised by FastAPI
                sent = form.get("csrf_token")
            except Exception:
                sent = None

    if not cookie_token or not sent or not secrets.compare_digest(str(cookie_token), str(sent)):
        raise HTTPException(status_code=403, detail="CSRF validation failed. Please reload and try again.")
