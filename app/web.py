from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.services.audio import format_timecode


templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
templates.env.filters["timecode"] = format_timecode
