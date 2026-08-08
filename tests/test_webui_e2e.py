"""E2E Web UI tests (issue #3) via Selenium + headless Chromium.

These tests boot a real uvicorn server on a temp port with an isolated
AUTH_DB_PATH and drive the browser through the auth flow, session list,
file browser and terminal panels.

Marked ``e2e`` — excluded from the default CI run (``-m "not host_smoke
and not e2e"``) because the CI container may not ship Selenium/Chromium.
"""

import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.e2e

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:  # pragma: no cover
    webdriver = None

_DRIVER = shutil.which("chromedriver")
_CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")

if not (webdriver and _DRIVER and _CHROMIUM):
    pytest.skip(
        "Selenium/chromedriver/Chromium not available — skipping Web UI E2E",
        allow_module_level=True,
    )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("X-API-Key", "e2e-master-key")
            urllib.request.urlopen(req, timeout=2)
            return
        except urllib.error.HTTPError:
            return  # server is up (any HTTP status proves it)
        except urllib.error.URLError as err:
            last_err = err
            time.sleep(0.5)
    raise AssertionError(f"Server at {url} did not become ready: {last_err}")


@pytest.fixture(scope="module")
def server():
    port = _free_port()
    tmpdir = tempfile.mkdtemp(prefix="webui-e2e-")
    auth_db = os.path.join(tmpdir, "auth.sqlite3")
    env = dict(os.environ)
    env.update(
        {
            "AUTH_DB_PATH": auth_db,
            "API_KEY": "e2e-master-key",
            "JWT_SECRET": "e2e-jwt-secret-not-for-prod",
            "API_AUTH_ENABLED": "true",
            "SETUP_TOKEN": "e2e-setup-token-123",
        }
    )
    proc = subprocess.Popen(
        [
            "python",
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_http(f"{base}/api/health")
        yield base, auth_db
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def driver():
    opts = ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.binary_location = _CHROMIUM
    drv = webdriver.Chrome(options=opts)
    drv.set_window_size(1400, 1000)
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
