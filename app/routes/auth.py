from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import User
from app.routes.dependencies import check_rate_limit, get_current_user
from app.services.security import verify_password
from app.web import templates


router = APIRouter()


@router.get("/")
def home(request: Request):
    return RedirectResponse("/generate" if request.session.get("user_id") else "/login")


@router.get("/login")
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/generate", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    check_rate_limit(request, "login", settings.login_rate_limit_per_minute)
    user = db.scalar(select(User).where(User.username == username.strip()))
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "用户名或密码错误"},
            status_code=401,
        )
    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse("/generate", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/api/session")
def session_info(current_user: User = Depends(get_current_user)):
    return {"username": current_user.username, "isAdmin": current_user.is_admin}
