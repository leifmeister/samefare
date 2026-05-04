from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from app.utils import safe_redirect

router = APIRouter(prefix="/lang", tags=["language"])


@router.get("/set/{lang}")
def set_language(lang: str, redirect_to: str = "/"):
    supported = {"en", "is"}
    if lang not in supported:
        lang = "en"
    response = RedirectResponse(safe_redirect(redirect_to), status_code=303)
    response.set_cookie("lang", lang, max_age=60 * 60 * 24 * 365, samesite="lax")
    return response
