"""Run the shipped page in Chromium with only the AstrBot bridge replaced."""

import json
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

VIEW = Path(__file__).resolve().parents[1] / "views" / "connection"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=shutil.which("chromium"))
        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    page = browser.new_page(viewport={"width": 420, "height": 640})

    def asset(route):
        name = Path(urlsplit(route.request.url).path).name or "index.html"
        path = VIEW / name
        if not path.is_file():
            route.fulfill(status=404, body="not found")
        else:
            mime = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}
            route.fulfill(path=path, content_type=mime[path.suffix])

    page.route("http://plugin.test/**", asset)
    yield page
    page.close()


def open_view(page, bots):
    assert (VIEW / "index.html").is_file(), "missing connection Plugin View"
    page.add_init_script(
        """
        window.calls = [];
        window.pendingChecks = [];
        window.pendingQrs = [];
        window.AstrBotPluginView = {
            ready: () => new Promise(resolve => { window.releaseReady = resolve; }),
            apiPost: (endpoint, params) => {
                window.calls.push([endpoint, params || {}]);
                if (endpoint !== 'connection/login/start') throw new Error('unexpected endpoint');
                return Promise.resolve(window.loginStartResult || {
                    state: 'awaiting_scan', qr_available: true, qr_generation: 1
                });
            },
            apiGet: (endpoint, params) => {
                window.calls.push([endpoint, params || {}]);
                if (endpoint === 'connection/login/status') return Promise.resolve(
                    window.loginStatusResult || {
                        state: 'awaiting_scan', qr_available: true, qr_generation: 1
                    }
                );
                if (endpoint === 'connection/login/qr') {
                    if (window.deferQr) {
                        return new Promise(resolve => window.pendingQrs.push(resolve));
                    }
                    return Promise.resolve({
                        image: 'data:image/png;base64,' +
                            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8A' +
                            'AwMCAO+aS1UAAAAASUVORK5CYII=', generation: params.generation
                    });
                }
                if (endpoint === 'connection/bots') return Promise.resolve({bots: BOTS});
                if (endpoint === 'connection/panel') {
                    if (window.panelError) return Promise.reject(new Error('PRIVATE DETAILS'));
                    return Promise.resolve(window.panelResult || {
                        statistics: {available: false}, config: {}
                    });
                }
                if (endpoint !== 'connection/check') throw new Error('unexpected endpoint');
                return new Promise((resolve, reject) => {
                    window.pendingChecks.push({resolve, reject});
                });
            }
        };
    """.replace("BOTS", json.dumps(bots))
    )
    page.goto("http://plugin.test/connection/index.html")
    page.wait_for_function("typeof window.releaseReady === 'function'")


def start_check(page):
    page.evaluate("window.releaseReady({})")
    page.wait_for_function("window.pendingChecks.length === 1")


def complete(page, result, index=0):
    page.evaluate(
        "([index, result]) => window.pendingChecks[index].resolve(result)", [index, result]
    )


def test_four_states_single_automatic_check_and_manual_retry(page):
    open_view(page, [{"id": "native-1"}])
    expect(page.get_by_role("heading", name="WeChat Native 面板")).to_be_visible()
    expect(page.locator("#connection-status")).to_have_text("未检测")
    start_check(page)
    expect(page.locator("#connection-status")).to_have_text("检测中")
    expect(page.get_by_role("button", name="刷新")).to_be_disabled()
    complete(page, {"state": "connected", "wechat_online": True, "reason": ""})
    expect(page.locator("#connection-status")).to_have_text("已连接")
    expect(page.locator("#connection-status")).to_have_css("color", "rgb(21, 128, 61)")
    expect(page.locator("#wechat-status")).to_have_text("在线")
    expect(page.locator("#failure-reason")).to_be_hidden()
    expect(page.locator("#robot-picker")).to_be_hidden()
    page.wait_for_timeout(100)
    assert page.evaluate("window.calls") == [
        ["connection/bots", {}],
        ["connection/panel", {"bot_id": "native-1"}],
        ["connection/check", {"bot_id": "native-1"}],
    ]
    page.get_by_role("button", name="刷新").click()
    page.wait_for_function("window.pendingChecks.length === 2")
    complete(
        page, {"state": "failed", "wechat_online": None, "reason": "Native Token 无效或已过期"}, 1
    )
    expect(page.locator("#connection-status")).to_have_text("连接失败")
    expect(page.locator("#connection-status")).to_have_css("color", "rgb(185, 28, 28)")
    expect(page.locator("#wechat-status")).to_have_text("未检测")
    expect(page.locator("#failure-reason")).to_have_text("Native Token 无效或已过期")
    expect(page.get_by_role("button", name="刷新")).to_be_enabled()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_multiple_bots_selects_saved_id_and_tests_selected_bot(page):
    open_view(page, [{"id": "native-1"}, {"id": "native-2"}])
    start_check(page)
    expect(page.get_by_label("机器人")).to_be_disabled()
    complete(page, {"state": "connected", "wechat_online": True, "reason": ""})
    expect(page.get_by_label("机器人")).to_be_enabled()
    page.get_by_label("机器人").select_option("native-2")
    page.wait_for_function("window.pendingChecks.length === 2")
    assert page.evaluate("window.calls.at(-1)") == ["connection/check", {"bot_id": "native-2"}]
    complete(page, {"state": "connected", "wechat_online": True, "reason": ""}, 1)
    expect(page.locator("#connection-status")).to_have_text("已连接")


def test_no_configured_robot_has_useful_empty_state(page):
    open_view(page, [])
    page.evaluate("window.releaseReady({})")
    expect(page.locator("#connection-status")).to_have_text("连接失败")
    expect(page.locator("#failure-reason")).to_have_text(
        "请先在机器人配置中保存 WeChat Native 机器人"
    )
    assert page.evaluate("window.pendingChecks.length") == 0


def test_bridge_failure_does_not_render_raw_exception(page):
    open_view(page, [{"id": "native-1"}])
    start_check(page)
    page.evaluate("window.pendingChecks[0].reject(new Error('SENSITIVE RAW ERROR'))")
    expect(page.locator("#failure-reason")).to_have_text(
        "检测请求失败，请确认 AstrBot 登录有效后重试"
    )
    expect(page.locator("body")).not_to_contain_text("SENSITIVE RAW ERROR")
    expect(page.get_by_role("button", name="刷新")).to_be_enabled()


def offline_result():
    return {"state": "connected", "wechat_online": False, "reason": "微信账号尚未登录",
            "health": "offline", "auth_state": "unauthenticated"}


def test_qr_scan_approval_and_account_online_confirmation(page):
    open_view(page, [{"id": "native-1"}])
    start_check(page)
    complete(page, offline_result())
    expect(page.locator("#login-qr")).to_be_visible()
    expect(page.locator("#login-status")).to_contain_text("扫码")
    assert page.locator("#login-qr").get_attribute("src").startswith("data:image/png;base64,")
    page.evaluate("window.loginStatusResult = {state:'awaiting_approval', qr_available:false}")
    expect(page.locator("#login-status")).to_contain_text("手机微信中确认", timeout=4000)
    expect(page.locator("#login-qr")).to_be_hidden()
    page.evaluate("window.loginStatusResult = {state:'succeeded', qr_available:false}")
    page.wait_for_function("window.pendingChecks.length === 2", timeout=4000)
    complete(page, {"state": "connected", "wechat_online": True,
                    "health": "online", "auth_state": "authenticated"}, 1)
    expect(page.locator("#login-panel")).to_be_hidden()
    expect(page.locator("#wechat-status")).to_have_text("在线")
    assert len(page.evaluate("window.calls.filter(c => c[0] === 'connection/login/start')")) == 1


def test_terminal_login_requires_explicit_retry(page):
    open_view(page, [{"id": "native-1"}])
    page.evaluate("window.loginStartResult = {state:'expired',qr_available:false}")
    start_check(page)
    complete(page, offline_result())
    expect(page.locator("#login-retry")).to_be_visible()
    expect(page.locator("#login-status")).to_contain_text("过期")
    page.get_by_role("button", name="刷新", exact=True).click()
    page.wait_for_function("window.pendingChecks.length === 2")
    complete(page, offline_result(), 1)
    page.wait_for_timeout(2200)
    assert len(page.evaluate("window.calls.filter(c => c[0] === 'connection/login/start')")) == 1
    page.evaluate("""window.loginStartResult = {
        state:'awaiting_scan',qr_available:true,qr_generation:2
    }""")
    page.locator("#login-retry").click()
    expect(page.locator("#login-qr")).to_be_visible()
    assert page.evaluate("""window.calls.filter(
        c => c[0] === 'connection/login/start'
    ).at(-1)[1]""") == {
        "bot_id": "native-1", "retry": True,
    }


def test_qr_generation_replaced_and_visibility_resumes_same_operation(page):
    open_view(page, [{"id": "native-1"}])
    start_check(page)
    complete(page, offline_result())
    expect(page.locator("#login-qr")).to_be_visible()
    page.evaluate("""() => {
        Object.defineProperty(document, 'hidden', {configurable:true, value:true});
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    before = len(page.evaluate("window.calls"))
    page.wait_for_timeout(2200)
    assert len(page.evaluate("window.calls")) == before
    page.evaluate("""() => {
        window.loginStatusResult = {state:'awaiting_scan',qr_available:true,qr_generation:2};
        Object.defineProperty(document, 'hidden', {configurable:true, value:false});
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    page.wait_for_function("""window.calls.some(
        c => c[0] === 'connection/login/qr' && c[1].generation === 2
    )""")
    assert len(page.evaluate("window.calls.filter(c => c[0] === 'connection/login/start')")) == 1


def test_bot_switch_discards_late_qr_response(page):
    open_view(page, [{"id": "native-1"}, {"id": "native-2"}])
    page.evaluate("window.deferQr = true")
    start_check(page)
    complete(page, offline_result())
    page.wait_for_function("window.pendingQrs.length === 1")
    page.get_by_label("机器人").select_option("native-2")
    page.wait_for_function("window.pendingChecks.length === 2")
    complete(page, offline_result(), 1)
    page.wait_for_function("window.pendingQrs.length === 2")
    page.evaluate("window.pendingQrs[0]({image:'data:image/png;base64,OLDQR',generation:1})")
    expect(page.locator("#login-qr")).to_be_hidden()
    page.evaluate("window.pendingQrs[1]({image:'data:image/png;base64,NEWQR',generation:1})")
    expect(page.locator("#login-qr")).to_have_attribute("src", "data:image/png;base64,NEWQR")
    assert "OLDQR" not in page.content()


def test_switching_back_to_expired_bot_keeps_manual_retry_visible(page):
    open_view(page, [{"id": "native-1"}, {"id": "native-2"}])
    page.evaluate("window.loginStartResult = {state:'expired',qr_available:false}")
    start_check(page)
    complete(page, offline_result())
    expect(page.locator("#login-retry")).to_be_visible()
    page.get_by_label("机器人").select_option("native-2")
    page.wait_for_function("window.pendingChecks.length === 2")
    complete(page, {"state": "connected", "wechat_online": True,
                    "health": "online", "auth_state": "authenticated"}, 1)
    page.get_by_label("机器人").select_option("native-1")
    page.wait_for_function("window.pendingChecks.length === 3")
    complete(page, offline_result(), 2)
    expect(page.locator("#login-retry")).to_be_visible()
    expect(page.locator("#login-status")).to_contain_text("过期")


def test_late_qr_cannot_reappear_after_account_becomes_online(page):
    open_view(page, [{"id": "native-1"}])
    page.evaluate("window.deferQr = true")
    start_check(page)
    complete(page, offline_result())
    page.wait_for_function("window.pendingQrs.length === 1")
    page.get_by_role("button", name="刷新", exact=True).click()
    page.wait_for_function("window.pendingChecks.length === 2")
    complete(page, {"state": "connected", "wechat_online": True,
                    "health": "online", "auth_state": "authenticated"}, 1)
    expect(page.locator("#login-panel")).to_be_hidden()
    page.evaluate("window.pendingQrs[0]({image:'data:image/png;base64,LATEQR',generation:1})")
    page.wait_for_timeout(100)
    assert not page.locator("#login-qr").get_attribute("src")


@pytest.mark.parametrize("result", [
    {"state": "connected", "wechat_online": False, "health": "offline",
     "auth_state": "authenticated"},
    {"state": "failed", "wechat_online": None, "health": "unavailable",
     "auth_state": "unknown"},
])
def test_ineligible_health_never_starts_login(page, result):
    open_view(page, [{"id": "native-1"}])
    start_check(page)
    complete(page, result)
    expect(page.get_by_role("button", name="刷新", exact=True)).to_be_enabled()
    expect(page.locator("#login-panel")).to_be_hidden()
    assert not page.evaluate("window.calls.some(c => c[0].startsWith('connection/login/'))")


def test_login_authorization_failure_has_safe_manual_retry(page):
    open_view(page, [{"id": "native-1"}])
    page.evaluate("""() => {
        window.AstrBotPluginView.apiPost = () =>
            Promise.reject(new Error('SENSITIVE TOKEN RAW ERROR'));
    }""")
    start_check(page)
    complete(page, offline_result())
    expect(page.locator("#login-status")).to_contain_text("管理员权限")
    expect(page.locator("#login-retry")).to_be_visible()
    expect(page.locator("body")).not_to_contain_text("SENSITIVE")


@pytest.mark.parametrize("reason", ["微信账号尚未登录", "微信账号当前不在线"])
def test_native_connected_and_wechat_offline_are_shown_separately(page, reason):
    open_view(page, [{"id": "native-1"}])
    start_check(page)
    complete(page, {"state": "connected", "wechat_online": False, "reason": reason})
    expect(page.locator("#connection-status")).to_have_text("已连接")
    expect(page.locator("#connection-status")).to_have_css("color", "rgb(21, 128, 61)")
    expect(page.locator("#wechat-status")).to_have_text("离线")
    expect(page.locator("#failure-reason")).to_be_visible()
    expect(page.locator("#failure-reason")).to_have_text(reason)
    expect(page.get_by_role("button", name="刷新")).to_be_enabled()


def panel_result():
    from datetime import date, timedelta

    days = [
        {
            "date": (date.today() - timedelta(days=i)).isoformat(),
            "received": 12 - i,
            "replied": 6 - i,
            "failed": 1,
            "partial": 0,
        }
        for i in reversed(range(7))
    ]
    return {
        "statistics": {
            "available": True,
            "days": days,
            "today": days[-1],
            "pending_review": 2,
            "since": days[0]["date"] + "T12:00:00+08:00",
            "timezone": "CST",
        },
        "config": {
            "enable_private_reply": True,
            "group_reply_mode": "mention",
            "enable_media": True,
            "allowed_users": 3,
            "allowed_groups": 0,
        },
    }


def test_compact_panel_chart_config_and_mobile_dark_mode(page):
    open_view(page, [{"id": "native-1"}])
    page.evaluate("value => window.panelResult = value", panel_result())
    start_check(page)
    complete(page, {"state": "connected", "wechat_online": True})
    expect(page.locator("#today-received")).to_have_text("12")
    expect(page.locator("#today-replied")).to_have_text("6")
    expect(page.locator("#today-failed")).to_have_text("1")
    expect(page.locator("#pending-review")).to_have_text("2")
    expect(page.locator("#daily-chart [data-day]")).to_have_count(7)
    last = page.locator("#daily-chart [data-day]").last
    last.focus()
    expect(page.locator("#chart-detail")).to_contain_text("接收 12")
    expect(page.locator("#chart-detail")).to_contain_text("已回复 6")
    expect(page.locator("#config-content")).to_be_hidden()
    page.get_by_text("配置概览", exact=True).click()
    expect(page.locator("#config-content")).to_contain_text("仅 @回复")
    expect(page.locator("#config-content")).to_contain_text("3 人")
    for width in (320, 420, 900):
        page.set_viewport_size({"width": width, "height": 720})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.evaluate("document.documentElement.dataset.theme = 'dark'")
    expect(page.locator("body")).to_have_css("background-color", "rgb(20, 27, 23)")


def test_statistics_failure_clears_previous_bot_and_keeps_connection(page):
    open_view(page, [{"id": "native-1"}, {"id": "native-2"}])
    page.evaluate("value => window.panelResult = value", panel_result())
    start_check(page)
    complete(page, {"state": "connected", "wechat_online": True})
    expect(page.locator("#today-received")).to_have_text("12")
    page.evaluate("window.panelError = true")
    page.get_by_label("机器人").select_option("native-2")
    page.wait_for_function("window.pendingChecks.length === 2")
    complete(page, {"state": "connected", "wechat_online": True}, 1)
    expect(page.locator("#today-received")).to_have_text("—")
    expect(page.locator("#panel-note")).to_contain_text("统计请求失败")
    expect(page.locator("#daily-chart [data-day]")).to_have_count(0)
    expect(page.locator("#connection-status")).to_have_text("已连接")
    expect(page.locator("body")).not_to_contain_text("PRIVATE DETAILS")


def test_zero_counts_and_uncollected_days_are_distinct(page):
    open_view(page, [{"id": "native-1"}])
    result = panel_result()
    for day in result["statistics"]["days"]:
        day.update(received=None, replied=None, failed=None, partial=None)
    result["statistics"]["days"][-1].update(received=0, replied=0, failed=0, partial=0)
    page.evaluate("value => window.panelResult = value", result)
    start_check(page)
    complete(page, {"state": "failed", "wechat_online": None, "reason": "服务不可达"})
    expect(page.locator("#today-received")).to_have_text("0")
    expect(page.locator("#daily-chart [data-day]")).to_have_count(7)
    page.locator("#daily-chart [data-day]").first.focus()
    expect(page.locator("#chart-detail")).to_contain_text("未采集")
    page.locator("#daily-chart [data-day]").last.focus()
    expect(page.locator("#chart-detail")).to_contain_text("接收 0")
    expect(page.locator("#connection-status")).to_have_text("连接失败")


def test_unreadable_statistics_has_an_explicit_error_state(page):
    open_view(page, [{"id": "native-1"}])
    message = "无法读取统计，请检查机器人状态库是否可用"
    page.evaluate(
        "message => window.panelResult = {statistics: {available: false, reason: message}}",
        message,
    )
    start_check(page)
    complete(page, {"state": "connected", "wechat_online": True})
    expect(page.locator("#panel-note")).to_have_text(message)
    expect(page.locator("#today-received")).to_have_text("—")


def test_automatic_refresh_is_non_overlapping_and_pauses_when_hidden(page):
    page.clock.install()
    open_view(page, [{"id": "native-1"}])
    page.evaluate("value => window.panelResult = value", panel_result())
    start_check(page)
    page.clock.fast_forward(31000)
    assert page.evaluate("window.pendingChecks.length") == 1
    complete(page, {"state": "connected", "wechat_online": True})
    expect(page.get_by_role("button", name="刷新")).to_be_enabled()
    last_day = page.locator("#daily-chart [data-day]").last
    last_day.focus()
    focused_day = last_day.get_attribute("data-day")
    page.clock.fast_forward(30000)
    page.wait_for_function("window.pendingChecks.length === 2")
    complete(page, {"state": "connected", "wechat_online": True}, 1)
    expect(page.get_by_role("button", name="刷新")).to_be_enabled()
    expect(page.locator(f'#daily-chart [data-day="{focused_day}"]')).to_be_focused()
    page.evaluate("Object.defineProperty(document, 'hidden', {get: () => true})")
    page.clock.fast_forward(60000)
    assert page.evaluate("window.pendingChecks.length") == 2
