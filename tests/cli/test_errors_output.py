from nexa.cli.errors import ApiError, OutputMode, render_error
from nexa.cli.output import emit_success


def test_api_error_contains_request_id_without_secret(capsys):
    error = ApiError.from_response(
        403, {"code": "permission_denied", "message": "bad"}, {"X-Request-Id": "req-1"}
    )
    render_error(error, OutputMode.HUMAN)
    captured = capsys.readouterr()
    assert "req-1" in captured.err
    assert "secret" not in captured.err
    assert error.exit_code == 4


def test_api_error_does_not_render_server_message(capsys):
    error = ApiError.from_response(
        500,
        {"code": "internal_error", "message": "password=super-secret /srv/nexa/input.bin"},
        {},
    )
    render_error(error, OutputMode.HUMAN)
    rendered = capsys.readouterr().err
    assert "super-secret" not in rendered
    assert "/srv/nexa/input.bin" not in rendered


def test_api_error_preserves_location_and_etag():
    error = ApiError.from_response(
        409,
        {"code": "conflict", "message": "bad"},
        {"X-Request-Id": "req-1", "Location": "/v1/jobs/1", "ETag": '"v2"'},
    )
    assert error.location == "/v1/jobs/1"
    assert error.etag == '"v2"'


def test_render_error_includes_safe_response_metadata(capsys):
    error = ApiError.from_response(
        409, {"code": "conflict", "message": "bad"}, {"Location": "/v1/jobs/1", "ETag": '"v2"'}
    )
    render_error(error, OutputMode.JSON)
    output = capsys.readouterr().err
    assert '"location":"/v1/jobs/1"' in output
    assert '"etag":"\\"v2\\""' in output


def test_human_error_includes_safe_etag(capsys):
    error = ApiError.from_response(409, {"message": "bad"}, {"ETag": '"v2"'})
    render_error(error, OutputMode.HUMAN)
    assert 'etag "v2"' in capsys.readouterr().err


def test_human_error_includes_status_and_retry_after(capsys):
    error = ApiError.from_response(
        429,
        {"code": "rate_limited"},
        {"Retry-After": "3"},
    )
    render_error(error, OutputMode.HUMAN)
    rendered = capsys.readouterr().err
    assert "status 429" in rendered
    assert "retry-after 3" in rendered


def test_success_json_is_deterministic(capsys):
    emit_success({"z": 1, "a": 2}, mode=OutputMode.JSON)
    assert capsys.readouterr().out == '{"a":2,"z":1}\n'
