import os

from nexa.api.app import create_app
from nexa.config import load_settings

app = create_app(load_settings(os.environ))
