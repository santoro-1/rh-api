from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.services.audio import format_timecode
from app.services.csrf import get_csrf_token


BEIJING_TIMEZONE = timezone(timedelta(hours=8))


def format_beijing_datetime(
    value: datetime,
    output_format: str = "%Y-%m-%d %H:%M",
) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BEIJING_TIMEZONE).strftime(output_format)


templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent / "templates"),
    context_processors=[lambda request: {"csrf_token": get_csrf_token(request)}],
)
templates.env.filters["timecode"] = format_timecode
templates.env.filters["beijing_datetime"] = format_beijing_datetime
