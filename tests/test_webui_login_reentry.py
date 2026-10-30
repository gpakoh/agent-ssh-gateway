"""Regression: module-scoped browser auth must not force a second navigation.

The full Selenium E2E suite is the authoritative acceptance test. These
lightweight unit cases exercise its actual _login function without Chromium.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path(__file__).with_name("test_webui_e2e.py")


class Element:
    def __init__(self, driver, name):
        self.driver = driver
        self.name = name

    def is_displayed(self):
        return self.driver.visible.get(self.name, False)

    def send_keys(self, text):
        self.driver.typed[self.name] = text

    def click(self):
        self.driver.visible["appShell"] = True


class Driver:
    def __init__(self, current_url, *, shell=False, login=False):
        self.current_url = current_url
        self.visible = {"appShell": shell, "loginUsername": login, "loginPassword": login, "loginBtn": login}
        self.typed = {}
        self.navigations = []

    def get(self, url):
        self.navigations.append(url)
        self.current_url = url

    def find_elements(self, _by, name):
        return [Element(self, name)] if name in self.visible else []

    def find_element(self, _by, name):
        return self.find_elements(_by, name)[0]


class Wait:
    def __init__(self, driver, _seconds):
        self.driver = driver

    def until(self, predicate):
        result = predicate(self.driver)
        assert result, "Required UI state not reached"
        return result


def login_function():
    source = ast.parse(SOURCE.read_text(encoding="utf-8"))
    method = next(
        node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "_login"
    )
    env = {
        "WebDriverWait": Wait,
        "By": SimpleNamespace(ID="id"),
        "EC": SimpleNamespace(
            element_to_be_clickable=lambda locator: (
                lambda driver: driver.find_element("id", locator[1])
                if driver.visible.get(locator[1]) else False
            ),
            visibility_of_element_located=lambda locator: (
                lambda driver: driver.find_element("id", locator[1])
                if driver.visible.get(locator[1]) else False
            ),
        ),
    }
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(SOURCE), "exec"), env)
    return env["_login"]


def test_reused_authenticated_browser_does_not_navigate_again():
    driver = Driver("http://127.0.0.1:12345/", shell=True)
    login_function()(driver, "http://127.0.0.1:12345")
    assert driver.navigations == []
    assert driver.typed == {}


def test_login_screen_on_current_fixture_is_not_reloaded():
    driver = Driver("http://127.0.0.1:12345/", login=True)
    login_function()(driver, "http://127.0.0.1:12345")
    assert driver.navigations == []
    assert driver.typed["loginUsername"] == "e2e-admin"
    assert driver.visible["appShell"] is True


def test_new_browser_navigates_to_the_fixture_once():
    driver = Driver("about:blank", login=True)
    login_function()(driver, "http://127.0.0.1:12345")
    assert driver.navigations == ["http://127.0.0.1:12345/"]
    assert driver.visible["appShell"] is True
