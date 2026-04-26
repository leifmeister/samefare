import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jose import jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app import models, email as mailer
from app.config import get_settings
from app.database import get_db
from app.dependencies import get_current_user_optional, get_template_context

settings = get_settings()
templates = Jinja2Templates(directory="templates")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
router = APIRouter(tags=["auth"])


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(user_id: int) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.access_token_expire_minutes)
    return jwt.encode({"sub": str(user_id), "exp": expire},
                      settings.secret_key, algorithm=settings.algorithm)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, ctx: dict = Depends(get_template_context)):
    if ctx["current_user"]:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("auth/login.html", {**ctx, "error": None, "email": ""})


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    ctx = {"request": request, "current_user": None}
    user = db.query(models.User).filter(models.User.email == email.lower()).first()
    if not user:
        return templates.TemplateResponse(
            "auth/login.html",
            {**ctx, "error": "no_account", "email": email},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    if not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(
            "auth/login.html",
            {**ctx, "error": "wrong_password", "email": email},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    token = create_access_token(user.id)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(key="access_token", value=token, httponly=True,
                        max_age=settings.access_token_expire_minutes * 60, samesite="lax")
    return response


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, ctx: dict = Depends(get_template_context)):
    if ctx["current_user"]:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("auth/register.html", {**ctx, "error": None})


@router.post("/register", response_class=HTMLResponse)
def register(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(""),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    ctx = {"request": request, "current_user": None}

    if password != confirm_password:
        return templates.TemplateResponse(
            "auth/register.html",
            {**ctx, "error": "Passwords do not match."},
            status_code=400,
        )
    if db.query(models.User).filter(models.User.email == email.lower()).first():
        return templates.TemplateResponse(
            "auth/register.html",
            {**ctx, "error": "That email is already registered."},
            status_code=400,
        )

    # In beta mode skip email verification — mark as verified immediately
    if settings.beta_mode:
        user = models.User(
            email=email.lower(),
            full_name=full_name,
            phone=phone or None,
            hashed_password=hash_password(password),
            email_verified=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        token = create_access_token(user.id)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(key="access_token", value=token, httponly=True,
                            max_age=settings.access_token_expire_minutes * 60, samesite="lax")
        return response

    verify_token = secrets.token_urlsafe(32)
    user = models.User(
        email=email.lower(),
        full_name=full_name,
        phone=phone or None,
        hashed_password=hash_password(password),
        email_verified=False,
        email_verify_token=verify_token,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    mailer.email_verification(user, verify_token)

    token = create_access_token(user.id)
    response = RedirectResponse("/check-your-email", status_code=303)
    response.set_cookie(key="access_token", value=token, httponly=True,
                        max_age=settings.access_token_expire_minutes * 60, samesite="lax")
    return response


@router.get("/check-your-email", response_class=HTMLResponse)
def check_your_email(request: Request, ctx: dict = Depends(get_template_context)):
    return templates.TemplateResponse("auth/check_your_email.html", {**ctx})


@router.get("/verify-email", response_class=HTMLResponse)
def verify_email(
    request: Request,
    token:   str     = "",
    ctx:     dict    = Depends(get_template_context),
    db:      Session = Depends(get_db),
):
    user = db.query(models.User).filter(models.User.email_verify_token == token).first() if token else None
    if not user:
        return templates.TemplateResponse("auth/verify_email_invalid.html", {**ctx})
    user.email_verified     = True
    user.email_verify_token = None
    db.commit()
    return RedirectResponse("/?verified=1", status_code=303)


@router.post("/resend-verification", response_class=HTMLResponse)
def resend_verification(
    request:      Request,
    ctx:          dict         = Depends(get_template_context),
    current_user: models.User  = Depends(get_current_user_optional),
    db:           Session      = Depends(get_db),
):
    if not current_user or current_user.email_verified:
        return RedirectResponse("/", status_code=303)
    token = secrets.token_urlsafe(32)
    current_user.email_verify_token = token
    db.commit()
    mailer.email_verification(current_user, token)
    return templates.TemplateResponse("auth/check_your_email.html", {**ctx, "resent": True})


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request, ctx: dict = Depends(get_template_context)):
    return templates.TemplateResponse("auth/forgot_password.html",
                                      {**ctx, "error": None, "sent": False})


@router.post("/forgot-password", response_class=HTMLResponse)
def forgot_password(
    request: Request,
    ctx:   dict    = Depends(get_template_context),
    email: str     = Form(...),
    db:    Session = Depends(get_db),
):
    user = db.query(models.User).filter(models.User.email == email.strip().lower()).first()

    # Always show the same success message to avoid revealing whether an email exists
    if user:
        token   = secrets.token_urlsafe(32)
        expires = datetime.utcnow() + timedelta(hours=1)
        user.reset_token         = token
        user.reset_token_expires = expires
        db.commit()
        mailer.password_reset(user, token)

    return templates.TemplateResponse("auth/forgot_password.html",
        {**ctx, "error": None, "sent": True})


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(
    request: Request,
    token:   str  = "",
    ctx:     dict = Depends(get_template_context),
    db:      Session = Depends(get_db),
):
    valid = _valid_token(token, db)
    return templates.TemplateResponse("auth/reset_password.html",
        {**ctx, "token": token, "valid": valid, "error": None, "success": False})


@router.post("/reset-password", response_class=HTMLResponse)
def reset_password(
    request:          Request,
    ctx:              dict    = Depends(get_template_context),
    token:            str     = Form(...),
    new_password:     str     = Form(...),
    confirm_password: str     = Form(...),
    db:               Session = Depends(get_db),
):
    user = _valid_token(token, db)
    if not user:
        return templates.TemplateResponse("auth/reset_password.html",
            {**ctx, "token": token, "valid": False, "error": None, "success": False})

    if new_password != confirm_password:
        return templates.TemplateResponse("auth/reset_password.html",
            {**ctx, "token": token, "valid": True,
             "error": "Passwords do not match.", "success": False}, status_code=400)

    if len(new_password) < 8:
        return templates.TemplateResponse("auth/reset_password.html",
            {**ctx, "token": token, "valid": True,
             "error": "Password must be at least 8 characters.", "success": False}, status_code=400)

    user.hashed_password     = hash_password(new_password)
    user.reset_token         = None
    user.reset_token_expires = None
    db.commit()

    return templates.TemplateResponse("auth/reset_password.html",
        {**ctx, "token": "", "valid": True, "error": None, "success": True})


def _valid_token(token: str, db: Session) -> models.User | None:
    """Return the User if the token is valid and unexpired, else None."""
    if not token:
        return None
    user = db.query(models.User).filter(models.User.reset_token == token).first()
    if not user:
        return None
    if not user.reset_token_expires or datetime.utcnow() > user.reset_token_expires:
        return None
    return user


@router.get("/logout")
def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("access_token")
    return response
