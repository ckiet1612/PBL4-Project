import hashlib
from dataclasses import asdict

import httpx
import pytest

from nexa.worker.client import WorkerApiClient
from nexa.worker.models import Authority

ID = "018f05c4-a922-7d0d-9f55-f9084a72d999"
AUTH = Authority(ID, ID, ID, ID, ID, 1)


def test_claim_and_result_callbacks_keep_exact_identity():
    paths = []

    def respond(request):
        paths.append(request.url.path)
        assert request.headers["x-callback-id"] == ID
        return httpx.Response(200, json={"accepted": True})

    client = WorkerApiClient("http://api", "secret", transport=httpx.MockTransport(respond))
    for method in ("claim", "start", "reserve_result", "complete"):
        assert hasattr(client, method), f"missing {method} callback"
        getattr(client, method)(ID, ID, {"authority": asdict(AUTH)})
    assert paths == [
        f"/v1/attempts/{ID}/{suffix}"
        for suffix in ("claim", "start", "result-reservations", "complete")
    ]


@pytest.mark.parametrize("bad", ["checksum", "media", "size", None])
def test_execution_download_validates_closed_bytes(tmp_path, bad):
    body = b"input"
    checksum = "sha256:" + hashlib.sha256(body).hexdigest()

    def respond(request):
        assert request.headers["x-job-fence"] == "1"
        return httpx.Response(
            200,
            content=body,
            headers={
                "etag": '"' + ("sha256:" + "a" * 64 if bad == "checksum" else checksum) + '"',
                "x-artifact-media-type": "text/plain" if bad == "media" else "application/json",
            },
        )

    client = WorkerApiClient("http://api", transport=httpx.MockTransport(respond))
    assert hasattr(client, "download_execution"), "missing bounded download"
    descriptor = {
        "artifact_id": ID,
        "checksum": checksum,
        "size_bytes": 9 if bad == "size" else 5,
        "media_type": "application/json",
    }
    target = tmp_path / "input"
    if bad:
        with pytest.raises(ValueError):
            client.download_execution(AUTH, descriptor, target)
        assert not target.exists()
    else:
        client.download_execution(AUTH, descriptor, target)
        assert target.read_bytes() == body
