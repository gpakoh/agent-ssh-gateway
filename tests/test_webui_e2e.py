"""E2E Web UI tests (issue #3) via Selenium + headless Chromium.

These tests boot a real uvicorn server on a temp port with an isolated
AUTH_DB_PATH and drive the browser through the auth flow, session list,
file browser and terminal panels.

Marked ``e2e`` — excluded from the default unit-test run (``-m "not host_smoke
and not e2e"``). The dedicated E2E CI job provisions an explicit browser
runtime; local runs may use either SELENIUM_REMOTE_URL or local Chrome/Chromium.
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.e2e

try:
    from selenium import webdriver
    from selenium.common.exceptions import SessionNotCreatedException, TimeoutException
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:  # pragma: no cover
    webdriver = None

_DRIVER = shutil.which("chromedriver")
_CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
_REMOTE_URL = os.environ.get("SELENIUM_REMOTE_URL", "").strip()
# Outer E2E harness budget only; the app's production startup behavior is unchanged.
E2E_SERVER_READY_TIMEOUT_SECONDS = 180.0
# Explicit page-load budget. Selenium's implicit default is 300s, which is the
# same window as the Grid idle-session reaper, so a slow `drv.get` under runner
# load could lose the race and have its session reaped mid-command. Declaring
# it here keeps the client's timeout strictly below the reaper configured by
# the CI sidecar (`--session-timeout 840`), so a genuinely slow load fails as
# one honest timeout instead of poisoning the remaining tests.
E2E_PAGE_LOAD_TIMEOUT_SECONDS = 600.0
E2E_SCRIPT_TIMEOUT_SECONDS = 120.0
E2E_REMOTE_SESSION_ATTEMPTS = 3
E2E_REMOTE_SESSION_RETRY_DELAY_SECONDS = 2.0
E2E_REMOTE_FIXTURE_READY_TIMEOUT_SECONDS = 20.0

if not webdriver or (not _REMOTE_URL and not (_DRIVER and _CHROMIUM)):
    pytest.skip(
        "Selenium runtime not available — configure SELENIUM_REMOTE_URL or install local Chrome/Chromium + chromedriver",
        allow_module_level=True,
    )


def _reserved_loopback_socket() -> socket.socket:
    """Reserve the exact TCP listener uvicorn will inherit.

    Binding an ephemeral port and closing it before ``Popen`` creates a TOCTOU
    window on shared CI runners: another process may claim the port before
    uvicorn binds it. Keep the listener open and pass its fd to the child.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.set_inheritable(True)
    return listener


def _startup_log_tail(path: str, limit: int = 8192) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit), os.SEEK_SET)
            return fh.read(limit).decode("utf-8", "replace")
    except OSError as exc:
        return f"<startup log unavailable: {type(exc).__name__}>"


def _wait_http(
    url: str,
    *,
    proc: subprocess.Popen,
    startup_log_path: str,
    timeout: float = 30.0,
) -> None:
    deadline = time.monotonic() + timeout
    last_err = None
    while time.monotonic() < deadline:
        return_code = proc.poll()
        if return_code is not None:
            tail = _startup_log_tail(startup_log_path)
            raise AssertionError(
                f"Server process exited before readiness (rc={return_code}); "
                f"startup log tail:\n{tail}"
            )
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("X-API-Key", "e2e-master-key")
            urllib.request.urlopen(req, timeout=2)
            return
        except urllib.error.HTTPError:
            return  # server is up (any HTTP status proves it)
        except (urllib.error.URLError, TimeoutError) as err:
            last_err = err
            time.sleep(0.5)
    tail = _startup_log_tail(startup_log_path)
    raise AssertionError(
        f"Server at {url} did not become ready: {last_err}; "
        f"process_rc={proc.poll()}; startup log tail:\n{tail}"
    )


@pytest.fixture(scope="module")
def server():
    tmpdir = tempfile.mkdtemp(prefix="webui-e2e-")
    auth_db = os.path.join(tmpdir, "auth.sqlite3")
    startup_log_path = os.path.join(tmpdir, "uvicorn-startup.log")
    listener = _reserved_loopback_socket()
    port = listener.getsockname()[1]
    # Keep the child hermetic: a push workflow may expose additional runner
    # secrets/configuration that a pull-request workflow does not. The Web UI
    # fixture must exercise the same local app profile in both cases.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": tmpdir,
        "AUTH_DB_PATH": auth_db,
        "API_KEY": "e2e-master-key",
        "JWT_SECRET": "e2e-jwt-secret-not-for-prod",
        "API_AUTH_ENABLED": "true",
        "SETUP_TOKEN": "e2e-setup-token-123",
        "REDIS_URL": "redis://127.0.0.1:1/0",
        "REDIS_JOB_QUEUE_ENABLED": "false",
        "PERSISTENT_SESSIONS_ENABLED": "false",
        "EVENT_HOOKS_ENABLED": "false",
        "AUDIT_LOG_PERSIST_ENABLED": "false",
        "ACCESS_CONTROL_ENABLED": "false",
    }
    startup_log = open(startup_log_path, "wb")
    proc = None
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--fd",
                str(listener.fileno()),
                "--log-level",
                "warning",
            ],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=env,
            stdout=startup_log,
            stderr=subprocess.STDOUT,
            pass_fds=(listener.fileno(),),
        )
        listener.close()
        base = f"http://127.0.0.1:{port}"
        _wait_http(
            f"{base}/api/health",
            proc=proc,
            startup_log_path=startup_log_path,
            timeout=E2E_SERVER_READY_TIMEOUT_SECONDS,
        )
        yield base, auth_db
    finally:
        listener.close()
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        startup_log.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


def _new_remote_driver(opts, base):
    """Create a remote session and prove it can render the loopback fixture.

    A Grid can report ready and accept a session while the newly-started
    Chromium renderer is still wedged. Treat that as startup failure only when
    the browser cannot see the fixture's static ``#appShell`` marker; real UI
    assertions remain outside this retry loop and stay fail-closed.
    """
    last_error = None
    for attempt in range(1, E2E_REMOTE_SESSION_ATTEMPTS + 1):
        drv = None
        try:
            drv = webdriver.Remote(command_executor=_REMOTE_URL, options=opts)
            drv.get(f"{base}/")
            WebDriverWait(drv, E2E_REMOTE_FIXTURE_READY_TIMEOUT_SECONDS).until(
                EC.presence_of_element_located((By.ID, "appShell"))
            )
            return drv
        except (SessionNotCreatedException, TimeoutException) as exc:
            last_error = exc
            if drv is not None:
                try:
                    drv.quit()
                except Exception:
                    pass
            if attempt >= E2E_REMOTE_SESSION_ATTEMPTS:
                raise
            time.sleep(E2E_REMOTE_SESSION_RETRY_DELAY_SECONDS)
    raise AssertionError(f"remote Selenium session retry exhausted: {last_error}")


@pytest.fixture(scope="module")
def driver(server):
    opts = ChromeOptions()
    # Every navigation below is followed by an explicit wait for the DOM state
    # the test actually needs. Even Selenium's "eager" strategy can leave a
    # remote Chrome navigation blocked inside the renderer lifecycle until the
    # page-load timeout under shared-runner pressure. Do not wait for browser
    # load milestones here; the explicit element waits below are the real test
    # contract and provide the bounded readiness signal we need.
    opts.page_load_strategy = "none"
    opts.add_argument("--headless=new")
    # These E2E tests only navigate to the loopback uvicorn fixture. Make that
    # contract explicit at the browser layer so inherited runner/Docker proxy
    # settings cannot route 127.0.0.1 through an external package proxy.
    opts.add_argument("--no-proxy-server")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1400,1000")
    base, _ = server
    if _REMOTE_URL:
        drv = _new_remote_driver(opts, base)
    else:
        opts.binary_location = _CHROMIUM
        drv = webdriver.Chrome(options=opts)
        drv.set_window_size(1400, 1000)
    # Must stay below the Grid reaper window; see E2E_PAGE_LOAD_TIMEOUT_SECONDS.
    drv.set_page_load_timeout(E2E_PAGE_LOAD_TIMEOUT_SECONDS)
    drv.set_script_timeout(E2E_SCRIPT_TIMEOUT_SECONDS)
    yield drv
    drv.quit()


@pytest.fixture(scope="module")
def admin_created(server):
    """Create the admin account once via the API so UI tests can sign in."""
    base, _ = server
    import json as _json

    payload = _json.dumps(
        {
            "username": "e2e-admin",
            "password": "Str0ng!Pass123",
            "password_confirm": "Str0ng!Pass123",
            "setup_token": "e2e-setup-token-123",
        }
    ).encode()
    req = urllib.request.Request(
        f"{base}/api/auth/register",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as err:
        if err.code != 403:  # 403 = already registered
            raise


def _login(drv, base):
    drv.get(f"{base}/")
    wait = WebDriverWait(drv, 15)
    # Already authenticated (httpOnly auth cookie present) → shell is visible.
    try:
        wait.until(EC.visibility_of_element_located((By.ID, "appShell")))
        return
    except Exception:
        pass
    wait.until(EC.element_to_be_clickable((By.ID, "loginUsername")))
    drv.find_element(By.ID, "loginUsername").send_keys("e2e-admin")
    drv.find_element(By.ID, "loginPassword").send_keys("Str0ng!Pass123")
    drv.find_element(By.ID, "loginBtn").click()
    wait.until(EC.visibility_of_element_located((By.ID, "appShell")))
    assert drv.find_element(By.ID, "appShell").is_displayed()


def _register(drv, base):
    drv.get(f"{base}/")
    wait = WebDriverWait(drv, 15)
    # Fresh install: register form is shown directly when users_count == 0.
    wait.until(EC.element_to_be_clickable((By.ID, "regUsername")))
    drv.find_element(By.ID, "regUsername").send_keys("e2e-admin")
    drv.find_element(By.ID, "regPassword").send_keys("Str0ng!Pass123")
    drv.find_element(By.ID, "regPasswordConfirm").send_keys("Str0ng!Pass123")
    drv.find_element(By.ID, "regSetupToken").send_keys("e2e-setup-token-123")
    drv.find_element(By.ID, "registerBtn").click()
    wait.until(EC.presence_of_element_located((By.ID, "appShell")))
    assert drv.find_element(By.ID, "appShell").is_displayed()


class TestWebUiE2E:
    """End-to-end Web UI flows via Selenium."""

    def test_auth_register_and_app_shell(self, server, driver, admin_created):
        base, _ = server
        _login(driver, base)
        assert driver.current_url.startswith(base)

    def test_session_panel_and_file_browser_present(self, server, driver, admin_created):
        base, _ = server
        _login(driver, base)
        # Session manager panel renders in the left column.
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".sessions-section"))
        )
        # File browser panel renders in the right column with a breadcrumb.
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "fbSection"))
        )
        assert driver.find_element(By.ID, "fbBreadcrumb").is_displayed()
        # PTY buttons exist in the terminal panel header.
        assert driver.find_element(By.ID, "ptyBtn").is_displayed()
        # ptyCloseBtn lives inside #ptyContainer (hidden until PTY opens) — presence only.
        driver.find_element(By.ID, "ptyCloseBtn")

    def test_xterm_vendor_loaded(self, server, driver, admin_created):
        base, _ = server
        _login(driver, base)
        loaded = driver.execute_script("return typeof Terminal !== 'undefined'")
        assert loaded, "xterm.js global Terminal is not defined"

    def test_append_line_system_type_escapes_html(self, server, driver, admin_created):
        """Regression: appendLine(..., 'system') used to build its DOM node
        via unescaped innerHTML. Every one of its ~15 call sites in app.js is
        a plain-text template built from values the user typed into the
        connect form or their own submitted command (host, username, job
        command) — a hostname/username containing e.g.
        <img src=x onerror=...> executed verbatim in the terminal view.
        Session/job cards elsewhere already escaped these same fields
        (escapeHtml(s.host), escapeHtml(job.command)) — this was the one
        path that didn't. Drives the real function in a real browser rather
        than re-simulating the DOM.
        """
        base, _ = server
        _login(driver, base)
        driver.execute_script(
            "appendLine('Connected to <img src=x onerror=\"window.__xssFired=true\">@evil', 'system');"
        )
        line = driver.find_element(By.CSS_SELECTOR, ".terminal-line.system:last-child")
        # The line exists in the DOM (innerHTML is set synchronously by
        # appendLine), but WebDriver .text reflects the *rendered* text: in
        # headless Chrome the layout pass runs asynchronously after
        # execute_script, so .text can transiently read "" even though the
        # node's textContent is already correct. Wait for the renderer to
        # catch up instead of racing it.
        WebDriverWait(driver, 5).until(
            lambda d: d.find_element(By.CSS_SELECTOR, ".terminal-line.system:last-child").text
        )
        line = driver.find_element(By.CSS_SELECTOR, ".terminal-line.system:last-child")
        # The deterministic proof: if the markup were still live HTML, the
        # <img> element would have been parsed out of textContent entirely
        # (it isn't text, it's a child element) — its presence *as text*
        # means the browser never parsed it as an element in the first
        # place, i.e. it was actually escaped.
        assert "<img" in line.text, "escaped markup should still render as literal text"
        assert line.find_elements(By.TAG_NAME, "img") == [], "markup must not be parsed as a real element"
