"""HTTPS certificates: system store on macOS/Windows, certifi without system list, clear errors."""

from __future__ import annotations

import os
import ssl
import sys
import types
import urllib.error
import urllib.request

import pytest

from geoloupe import certs
from geoloupe.readers import OpenError, detect_engine

CERT_ERROR = ssl.SSLCertVerificationError(
    1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer"
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        monkeypatch.setenv(name, "")  # records the old state, so a value set by the test is undone
        monkeypatch.delenv(name)
    return monkeypatch


def _fake_truststore(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A truststore whose inject_into_ssl only records the call (the real one patches ssl)."""
    calls: list[str] = []
    fake = types.ModuleType("truststore")
    fake.inject_into_ssl = lambda: calls.append("inject")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "truststore", fake)
    return calls


def test_user_setting_wins(clean_env: pytest.MonkeyPatch) -> None:
    calls = _fake_truststore(clean_env)
    clean_env.setattr(sys, "platform", "darwin")
    clean_env.setenv("SSL_CERT_FILE", "/my/ca.pem")
    assert certs.use_system_certificates() == "environment"
    assert calls == []


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_macos_and_windows_use_the_system_store(
    clean_env: pytest.MonkeyPatch, platform: str
) -> None:
    calls = _fake_truststore(clean_env)
    clean_env.setattr(sys, "platform", platform)
    assert certs.use_system_certificates() == "system store"
    assert calls == ["inject"]


def test_linux_without_authorities_uses_certifi(clean_env: pytest.MonkeyPatch) -> None:
    import certifi

    calls = _fake_truststore(clean_env)
    clean_env.setattr(sys, "platform", "linux")
    clean_env.setattr(certs, "_has_default_authorities", lambda: False)
    assert certs.use_system_certificates() == "certifi"
    assert calls == []
    assert os.environ["SSL_CERT_FILE"] == certifi.where()


def test_linux_with_authorities_keeps_the_default(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setattr(sys, "platform", "linux")
    clean_env.setattr(certs, "_has_default_authorities", lambda: True)
    assert certs.use_system_certificates() == "default"


def test_explain_adds_the_fix_once() -> None:
    msg = certs.explain(f"cannot open x: {CERT_ERROR}")
    assert "SSL_CERT_FILE" in msg and "Install Certificates" in msg
    assert certs.explain(msg) == msg
    assert certs.explain("cannot open x: timeout") == "cannot open x: timeout"


def test_format_probe_reports_a_failed_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """A certificate error is not "cannot tell the format" (the gallery's ocean model on a Mac)."""

    def urlopen(req: urllib.request.Request, timeout: float = 0) -> None:
        raise urllib.error.URLError(CERT_ERROR)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(OpenError, match=r"cannot reach https://zarr\.test/tos") as e:
        detect_engine("https://zarr.test/tos")
    assert "CERTIFICATE_VERIFY_FAILED" in str(e.value) and "SSL_CERT_FILE" in str(e.value)


def test_format_probe_reports_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def urlopen(req: urllib.request.Request, timeout: float = 0) -> None:
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(OpenError, match=r"cannot reach .*timed out"):
        detect_engine("https://zarr.test/tos")
