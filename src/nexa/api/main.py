import os

from nexa.api.app import create_app
from nexa.config import load_settings
from nexa.observability.logging import configure_logging

settings = load_settings(os.environ)
configure_logging("api", level=settings.log_level, fmt=settings.log_format)
app = create_app(settings)
