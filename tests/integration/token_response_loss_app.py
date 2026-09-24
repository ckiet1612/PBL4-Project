"""Crash after a CLI token has committed and before its secret is returned."""

from nexa.api.main import app as production_app
from tests.integration.response_loss_app import CrashAfterCommitBeforeResponse

app = CrashAfterCommitBeforeResponse(production_app, path="/v1/tokens", success_status=201)
