"""Connection recovery contracts against isolated Native HTTP servers."""

import asyncio
import copy
import io

import pytest
from aiohttp import web
from conftest import module
from PIL import Image


class MemoryKV:
    def __init__(self):
        self.data = {}
        self.fail = False

    async def get_kv_data(self, key, default=None):
        return copy.deepcopy(self.data.get(key, default))

    async def put_kv_data(self, key, value):
        if self.fail:
            raise OSError("SENSITIVE KV FAILURE")
        self.data[key] = copy.deepcopy(value)

    async def delete_kv_data(self, key):
        self.data.pop(key, None)


def robot(url="http://native.example", **updates):
    return {
        "id": "native-1",
        "type": "wechat_native",
        "enable": True,
        "server_url": url,
        "account_id": "a",
        "token": "synthetic-read-token",
        **updates,
    }


def operation(state="awaiting_scan", generation=1, **updates):
    return {
        "id": "op-1",
        "accountId": "a",
        "kind": "login",
        "state": state,
        "result": {"qrAvailable": state == "awaiting_scan", "qrGeneration": generation},
        "createdAt": "2026-11-12T10:00:00Z",
        "updatedAt": "2026-11-12T10:00:00Z",
        **updates,
    }


def png():
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(buffer, "PNG")
    return buffer.getvalue()


@pytest.mark.parametrize(
    "authenticated,connection,health,auth_state",
    [
        (False, "disconnected", "offline", "unauthenticated"),
        (True, "reconnecting", "offline", "authenticated"),
        (True, "online", "online", "authenticated"),
        ("false", "online", "unavailable", "unknown"),
    ],
)
async def test_shared_probe_requires_strict_account_online(
    serve,
    authenticated,
    connection,
    health,
    auth_state,
):
    async def handler(req):
        assert req.method == "GET"
        assert req.headers["Authorization"] == "Bearer synthetic-read-token"
        return web.json_response(
            {"state": "ready"}
            if req.path.endswith("status")
            else {
                "id": "a",
                "authenticated": authenticated,
                "connection": connection,
            }
        )

    result = await module("connection_probe").probe_connection(robot(await serve(handler)))
    assert result.health == health
    assert result.auth_state == auth_state


async def test_native_login_uses_tag_specific_routes_body_and_admin_token(serve):
    calls = []
    image = png()

    async def handler(req):
        calls.append((req.method, req.raw_path))
        assert req.headers["Authorization"] == "Bearer synthetic-admin-token"
        if req.method == "POST":
            assert req.headers["Idempotency-Key"] == "durable-key"
            assert await req.json() == {
                "strategy": "auto",
                "allowQr": True,
                "forceQr": False,
                "timeoutSeconds": 900,
                "qrRefresh": {"mode": "auto", "maxRefreshes": 3},
            }
            return web.json_response(operation(), status=202)
        if req.path.endswith("/qr"):
            return web.Response(body=image, content_type="image/png")
        return web.json_response(operation())

    native = module("native_client")
    async with native.NativeClient(await serve(handler) + "/v1", "a", "synthetic-admin-token") as c:
        assert (await c.start_login("durable-key"))["id"] == "op-1"
        assert (await c.login_status("op-1"))["kind"] == "login"
        assert await c.login_qr("op-1", 1) == image
    assert calls == [
        ("POST", "/v1/accounts/a/login-attempts"),
        ("GET", "/v1/accounts/a/login-attempts/op-1"),
        ("GET", "/v1/accounts/a/login-attempts/op-1/qr?generation=1"),
    ]


@pytest.mark.parametrize(
    "body,mime,status",
    [
        (b"<html>secret</html>", "text/html", 200),
        (b"\x89PNG\r\n\x1a\ninvalid", "image/png", 200),
        (b"x" * (1024 * 1024 + 1), "image/png", 200),
        (b"", "image/png", 409),
        (b"", "image/png", 302),
    ],
)
async def test_native_login_rejects_unsafe_qr(serve, body, mime, status):
    async def handler(req):
        return web.Response(
            body=body,
            content_type=mime,
            status=status,
            headers={"Location": "http://untrusted.invalid"},
        )

    native = module("native_client")
    async with native.NativeClient(await serve(handler) + "/v1", "a", "admin") as c:
        with pytest.raises(native.NativeAPIError):
            await c.login_qr("op-1", 1)


@pytest.mark.parametrize(
    "updates",
    [
        {"id": "foreign"},
        {"accountId": "foreign"},
        {"kind": "logout"},
        {"result": {"qrAvailable": "true", "qrGeneration": 1}},
        {"result": {"qrAvailable": True, "qrGeneration": True}},
        {"result": {"qrAvailable": True, "qrGeneration": "1"}},
    ],
)
async def test_native_login_validates_operation_identity_and_qr_types(serve, updates):
    async def handler(req):
        return web.json_response(operation(**updates))

    native = module("native_client")
    async with native.NativeClient(await serve(handler) + "/v1", "a", "admin") as c:
        with pytest.raises(native.NativeAPIError):
            await c.login_status("op-1")


@pytest.mark.parametrize("state", ["starting", "resuming", "authenticating"])
async def test_native_v230_pre_qr_states_can_start_and_resume(serve, tmp_path, state):
    bot, data = await login_server(serve, tmp_path)
    data["operation"] = operation(state, result={})
    login = module("login_session")
    manager = login.LoginSessionManager(data["kv"])
    assert await manager.start(bot) == {"state": state, "qr_available": False}
    manager = login.LoginSessionManager(data["kv"])
    assert await manager.status(bot) == {"state": state, "qr_available": False}
    assert len(data["posts"]) == 1


async def test_native_malformed_state_raises_safe_transport_error(serve):
    async def handler(req):
        return web.json_response(operation(state={"private": "untrusted"}))

    native = module("native_client")
    async with native.NativeClient(await serve(handler) + "/v1", "a", "admin") as client:
        with pytest.raises(native.NativeAPIError):
            await client.login_status("op-1")


async def login_server(serve, tmp_path, *, authenticated=False, lost=False):
    state = {"posts": [], "gets": 0, "operation": operation(), "authenticated": authenticated}
    token_file = tmp_path / "admin"
    token_file.write_text("synthetic-admin-token")

    async def handler(req):
        if req.path.endswith("/status"):
            return web.json_response({"state": "ready"})
        if req.path == "/v1/accounts/a":
            return web.json_response(
                {"id": "a", "authenticated": state["authenticated"], "connection": "reconnecting"}
            )
        assert req.headers["Authorization"] == "Bearer synthetic-admin-token"
        stage = "start" if req.method == "POST" else "qr" if req.path.endswith("/qr") else "status"
        failure = state.get("http_errors", {}).get(stage)
        if failure:
            status, code = failure
            return web.json_response(
                {"error": {"code": code, "message": "SENSITIVE SERVER BODY"}}, status=status
            )
        if req.method == "POST":
            key = req.headers["Idempotency-Key"]
            state["posts"].append(key)
            assert any(v.get("idempotency_key") == key for v in state["kv"].data.values())
            if lost and len(state["posts"]) == 1:
                return web.Response(text="lost response", status=502)
            return web.json_response(state["operation"], status=202)
        state["gets"] += 1
        if req.path.endswith("/qr"):
            return web.Response(body=png(), content_type="image/png")
        return web.json_response(state["operation"])

    bot = robot(await serve(handler), admin_token_file=str(token_file))
    state["kv"] = MemoryKV()
    return bot, state


async def test_login_parallel_starts_restart_and_explicit_terminal_retry(serve, tmp_path):
    bot, state = await login_server(serve, tmp_path)
    login = module("login_session")
    manager = login.LoginSessionManager(state["kv"])
    results = await asyncio.gather(*(manager.start(bot) for _ in range(5)))
    assert all(r["state"] == "awaiting_scan" for r in results)
    assert len(state["posts"]) == 1
    manager = login.LoginSessionManager(state["kv"])
    assert (await manager.start(bot))["qr_generation"] == 1
    assert len(state["posts"]) == 1
    state["operation"] = operation("expired")
    assert (await manager.start(bot))["state"] == "expired"
    assert (await manager.start(bot))["state"] == "expired"
    assert len(state["posts"]) == 1
    state["operation"] = operation(id="op-2")
    assert (await manager.start(bot, retry=True))["state"] == "awaiting_scan"
    assert len(set(state["posts"])) == 2
    assert "synthetic-admin-token" not in str(state["kv"].data)


async def test_login_lost_post_response_reuses_persisted_key(serve, tmp_path):
    bot, state = await login_server(serve, tmp_path, lost=True)
    login = module("login_session")
    with pytest.raises(login.LoginError):
        await login.LoginSessionManager(state["kv"]).start(bot)
    result = await login.LoginSessionManager(state["kv"]).start(bot)
    assert result["state"] == "awaiting_scan"
    assert len(state["posts"]) == 2 and len(set(state["posts"])) == 1


async def test_login_kv_failure_prevents_post_and_authenticated_account_never_logs_in(
    serve, tmp_path
):
    bot, state = await login_server(serve, tmp_path)
    login = module("login_session")
    state["kv"].fail = True
    with pytest.raises(login.LoginError):
        await login.LoginSessionManager(state["kv"]).start(bot)
    assert state["posts"] == []
    state["kv"].fail = False
    state["authenticated"] = True
    with pytest.raises(login.LoginError):
        await login.LoginSessionManager(state["kv"]).start(bot)
    assert state["posts"] == []


async def test_login_qr_checks_current_generation_and_terminal_state(serve, tmp_path):
    bot, state = await login_server(serve, tmp_path)
    login = module("login_session")
    manager = login.LoginSessionManager(state["kv"])
    await manager.start(bot)
    assert (await manager.qr(bot, 1))["image"].startswith("data:image/png;base64,")
    state["operation"] = operation(generation=2)
    with pytest.raises(login.LoginError):
        await manager.qr(bot, 1)
    assert (await manager.qr(bot, 2))["generation"] == 2
    state["operation"] = operation("succeeded")
    with pytest.raises(login.LoginError):
        await manager.qr(bot, 2)


async def web_login(context, endpoint, *, username="admin", data=None, query="", method="GET"):
    import json

    from astrbot.api.web import PluginRequest, bind_request_context
    from starlette.requests import Request

    path = "/astrbot_plugin_wechat_native/connection/login/" + endpoint
    routes = [(h, methods) for p, h, methods, _ in context.registered_web_apis if p == path]
    assert len(routes) == 1, "missing privileged login API"
    handler, methods = routes[0]
    assert methods == [method]

    async def receive():
        return {"type": "http.request", "body": json.dumps(data).encode()}

    req = Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": query.encode(),
            "headers": [],
        },
        receive,
    )
    with bind_request_context(PluginRequest(req, username=username)):
        response = await handler()
    assert response.headers["cache-control"] == "no-store"
    return response.status_code, json.loads(response.body)


@pytest.mark.parametrize("username", [None, "viewer", "api_key:admin"])
async def test_login_web_api_requires_actual_dashboard_account(username):
    from test_connection_view import plugin

    _, context = plugin([robot()])
    context._config["dashboard"] = {"username": "admin"}
    for endpoint in ("start", "status", "qr"):
        status, result = await web_login(
            context, endpoint, username=username, method="POST" if endpoint == "start" else "GET"
        )
        assert status in (401, 403)
        assert "image" not in result


async def test_login_web_api_fail_closed_and_rejects_disabled_bots_and_control_parameters():
    from test_connection_view import plugin

    _, context = plugin([robot(enable=False)])
    status, _ = await web_login(context, "start", data={"bot_id": "native-1"}, method="POST")
    assert status == 403
    context._config["dashboard"] = {"username": "admin"}
    for data in (
        {"bot_id": "native-1"},
        {"bot_id": "unknown"},
        {"bot_id": "native-1", "api_url": "http://untrusted.invalid"},
    ):
        status, _ = await web_login(context, "start", data=data, method="POST")
        assert status in (400, 404)


async def test_login_web_success_returns_only_safe_metadata_and_png(serve, tmp_path):
    from test_connection_view import plugin

    bot, state = await login_server(serve, tmp_path)
    instance, context = plugin([bot])
    instance.login_sessions.kv = state["kv"]
    context._config["dashboard"] = {"username": "admin"}
    status, result = await web_login(context, "start", data={"bot_id": "native-1"}, method="POST")
    assert status == 200
    assert result == {"state": "awaiting_scan", "qr_available": True, "qr_generation": 1}
    status, result = await web_login(context, "qr", query="bot_id=native-1&generation=1")
    assert status == 200 and result["generation"] == 1
    assert set(result) == {"generation", "image"}
    for query in (
        "bot_id=native-1&generation=true",
        "bot_id=native-1&generation=1&generation=2",
        "bot_id=native-1&generation=1&operationId=foreign",
    ):
        status, _ = await web_login(context, "qr", query=query)
        assert status == 400


async def test_login_identity_change_does_not_resume_old_operation(serve, tmp_path):
    bot, state = await login_server(serve, tmp_path)
    manager = module("login_session").LoginSessionManager(state["kv"])
    await manager.start(bot)
    changed = {**bot, "account_id": "new-account"}
    assert await manager.status(changed) == {"state": "idle", "qr_available": False}
    assert state["gets"] == 0


async def test_missing_admin_token_never_uses_read_token(serve, tmp_path):
    bot, state = await login_server(serve, tmp_path)
    bot["admin_token_file"] = str(tmp_path / "missing-admin-token")
    login = module("login_session")
    with pytest.raises(login.LoginError, match="Admin Token"):
        await login.LoginSessionManager(state["kv"]).start(bot)
    assert state["posts"] == []


@pytest.mark.parametrize("status", [401, 403, 503])
async def test_unavailable_native_never_starts_login(serve, status, tmp_path):
    calls = []

    async def handler(req):
        calls.append(req.method)
        return web.Response(status=status, text="SENSITIVE RAW RESPONSE")

    login = module("login_session")
    with pytest.raises(login.LoginError):
        await login.LoginSessionManager(MemoryKV()).start(robot(await serve(handler)))
    assert calls == ["GET"]


async def test_login_conflict_reuses_original_key_without_cancelling_unknown_task(serve, tmp_path):
    keys, methods = [], []
    admin = tmp_path / "admin"
    admin.write_text("admin-token")

    async def handler(req):
        methods.append(req.method)
        if req.method == "POST":
            keys.append(req.headers["Idempotency-Key"])
            return web.json_response({"error": {"code": "operation_in_progress"}}, status=409)
        return web.json_response(
            {"state": "ready"}
            if req.path.endswith("status")
            else {
                "id": "a",
                "authenticated": False,
                "connection": "disconnected",
            }
        )

    bot = robot(await serve(handler), admin_token_file=str(admin))
    login, kv = module("login_session"), MemoryKV()
    for retry in (False, True):
        with pytest.raises(login.LoginError) as error:
            await login.LoginSessionManager(kv).start(bot, retry=retry)
        assert error.value.status == 409
    assert len(keys) == 2 and len(set(keys)) == 1
    assert "DELETE" not in methods


async def test_failed_login_logs_native_code_without_sensitive_result(serve, tmp_path, caplog):
    bot, state = await login_server(serve, tmp_path)
    state["operation"] = operation(
        "failed",
        result={
            "code": "profile_network_routing_unavailable",
            "message": "SENSITIVE PROFILE PATH AND TOKEN",
            "qr": "PRIVATE QR",
        },
    )
    manager = module("login_session").LoginSessionManager(state["kv"])
    result = await manager.start(bot)
    assert result == {"state": "failed", "qr_available": False}
    assert "stage=start state=failed code=profile_network_routing_unavailable" in caplog.text
    assert "SENSITIVE" not in caplog.text and "PRIVATE QR" not in caplog.text
    assert "synthetic-admin-token" not in caplog.text
    assert state["gets"] == 0


@pytest.mark.parametrize("code", ["SENSITIVE TOKEN", {"private": "SENSITIVE"}, ["SENSITIVE"], None])
async def test_login_diagnostics_redact_unknown_native_codes(serve, tmp_path, caplog, code):
    bot, state = await login_server(serve, tmp_path)
    state["operation"] = operation("failed", result={"code": code, "message": "PRIVATE BODY"})
    await module("login_session").LoginSessionManager(state["kv"]).start(bot)
    assert "stage=start state=failed code=unknown" in caplog.text
    assert "SENSITIVE" not in caplog.text and "PRIVATE BODY" not in caplog.text


async def test_login_status_logs_failure_transition_once_without_poll_noise(
    serve, tmp_path, caplog
):
    bot, state = await login_server(serve, tmp_path)
    manager = module("login_session").LoginSessionManager(state["kv"])
    await manager.start(bot)
    caplog.clear()
    state["operation"] = operation("failed", result={"code": "login_failed"})
    assert (await manager.status(bot))["state"] == "failed"
    assert "stage=status state=failed code=login_failed" in caplog.text
    assert (await manager.status(bot))["state"] == "failed"
    assert caplog.text.count("stage=status state=failed") == 1
    assert state["gets"] == 1


@pytest.mark.parametrize(
    "stage,status,code",
    [
        ("start", 403, "insufficient_scope"),
        ("status", 503, "timeout"),
        ("qr", 409, "login_challenge_superseded"),
    ],
)
async def test_login_http_diagnostics_identify_failed_stage(
    serve,
    tmp_path,
    caplog,
    stage,
    status,
    code,
):
    bot, state = await login_server(serve, tmp_path)
    login = module("login_session")
    manager = login.LoginSessionManager(state["kv"])
    if stage != "start":
        await manager.start(bot)
    state["http_errors"] = {stage: (status, code)}
    with pytest.raises(login.LoginError):
        if stage == "qr":
            await manager.qr(bot, 1)
        elif stage == "status":
            await manager.status(bot)
        else:
            await manager.start(bot)
    assert f"stage={stage} http={status} code={code}" in caplog.text
    assert "SENSITIVE SERVER BODY" not in caplog.text
    assert "synthetic-admin-token" not in caplog.text


@pytest.mark.parametrize(
    "body,mime,reason",
    [
        (b"not png", "text/html", "invalid_content_type"),
        (b"not png", "image/png", "invalid_signature"),
        (b"x" * (1024 * 1024 + 1), "image/png", "size_limit"),
        (b"\x89PNG\r\n\x1a\nSENSITIVE", "image/png", "invalid_png"),
    ],
)
async def test_qr_validation_diagnostics_do_not_log_image_bytes(serve, caplog, body, mime, reason):
    async def handler(req):
        return web.Response(body=body, content_type=mime)

    native = module("native_client")
    async with native.NativeClient(await serve(handler) + "/v1", "a", "admin") as client:
        with pytest.raises(native.NativeAPIError):
            await client.login_qr("op-1", 1)
    assert f"stage=qr reason={reason}" in caplog.text
    assert "SENSITIVE" not in caplog.text and "not png" not in caplog.text
