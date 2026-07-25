from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.services.audio import format_timecode
from app.services.csrf import get_csrf_token


templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent / "templates"),
    context_processors=[lambda request: {"csrf_token": get_csrf_token(request)}],
)
templates.env.filters["timecode"] = format_timecode
