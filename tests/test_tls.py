import ast
import ssl
from pathlib import Path

import certifi
import pytest
import truststore

from reinvent_planner import tls


def _strict(ctx: ssl.SSLContext) -> None:
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_uses_the_os_trust_store_by_default(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    ctx = tls.ssl_context()
    assert isinstance(ctx, truststore.SSLContext)
    _strict(ctx)


def test_ssl_cert_file_overrides(monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", certifi.where())
    ctx = tls.ssl_context()
    assert not isinstance(ctx, truststore.SSLContext)
    _strict(ctx)


def test_ssl_cert_dir_overrides(monkeypatch, tmp_path):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
    ctx = tls.ssl_context()
    assert not isinstance(ctx, truststore.SSLContext)
    _strict(ctx)


def test_a_missing_bundle_names_the_variable(monkeypatch, tmp_path):
    missing = tmp_path / "nope.pem"
    monkeypatch.setenv("SSL_CERT_FILE", str(missing))
    with pytest.raises(tls.TLSConfigError, match=r"SSL_CERT_FILE=.*nope\.pem"):
        tls.ssl_context()


def test_every_client_uses_it():
    # A new HTTP client that forgot verify=ssl_context() would silently fall back to certifi
    # and break again behind Zscaler.
    src = Path(tls.__file__).parent
    clients = 0
    for path in src.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("Client", "AsyncClient")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "httpx"
            ):
                clients += 1
                assert any(k.arg == "verify" for k in node.keywords), f"{path.name}:{node.lineno}"
    assert clients >= 3


def test_cli_reports_a_bad_bundle_without_a_traceback(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from reinvent_planner.cli import app

    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "nope.pem"))
    result = CliRunner().invoke(app, ["events"])
    assert result.exit_code == 1
    assert "SSL_CERT_FILE" in result.output
    assert not isinstance(result.exception, tls.TLSConfigError)
