from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import GenerationTask, User


def get_current_user(
    request: Request, db: Session = Depends(get_db)
) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    user = db.get(User, user_id)
    if not user or not user.is_active:
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效")
    return user


def get_page_user(request: Request, db: Session = Depends(get_db)) -> User:
    try:
        return get_current_user(request, db)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": "/login"},
            ) from exc
        raise


def get_admin_user(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return current_user


def get_page_admin(current_user: User = Depends(get_page_user)) -> User:
    if not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return current_user


def check_rate_limit(request: Request, namespace: str, limit: int) -> None:
    """A lightweight local limiter. Use a shared limiter when scaling to many web nodes."""
    now = time.monotonic()
    client = request.client.host if request.client else "unknown"
    storage: dict[tuple[str, str], deque[float]] = getattr(
        request.app.state, "rate_limits", defaultdict(deque)
    )
    request.app.state.rate_limits = storage
    key = (namespace, client)
    attempts = storage[key]
    while attempts and attempts[0] <= now - 60:
        attempts.popleft()
    if len(attempts) >= limit:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    attempts.append(now)


def ensure_task_access(task: GenerationTask, user: User) -> None:
    if task.user_id != user.id and not user.is_admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
