from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jose import jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app import models
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
    return templates.TemplateResponse("auth/login.html", {**ctx, "error": None})


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    ctx = {"request": request, "current_user": None}
    user = db.query(models.User).filter(models.User.email == email.lower()).first()
    if not user or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(
            "auth/login.html",
            {**ctx, "error": "Invalid email or password."},
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

    user = models.User(
        email=email.lower(),
        full_name=full_name,
        phone=phone or None,
        hashed_password=hash_password(password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(user.id)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(key="access_token", value=token, httponly=True,
                        max_age=settings.access_token_expire_minutes * 60, samesite="lax")
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("access_token")
    return response
