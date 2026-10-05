"""TLS settings for every outgoing HTTPS connection.

Certificates are checked against the operating system's trust store (macOS Keychain, the
Windows certificate store, the system bundle on Linux) via `truststore`. That's what makes
`rip` work behind a corporate TLS-inspecting proxy such as Zscaler, whose root certificate IT
installs into the OS store, while still verifying every certificate. `SSL_CERT_FILE` (a PEM
bundle) or `SSL_CERT_DIR` overrides it, as they do for httpx itself.
"""

from __future__ import annotations

import os
import ssl

import truststore


class TLSConfigError(RuntimeError):
    """SSL_CERT_FILE / SSL_CERT_DIR points at something that can't be loaded."""


def ssl_context() -> ssl.SSLContext:
    bundle = os.environ.get("SSL_CERT_FILE")
    directory = os.environ.get("SSL_CERT_DIR")
    try:
        if bundle:
            return ssl.create_default_context(cafile=bundle)
        if directory:
            return ssl.create_default_context(capath=directory)
    except (OSError, ssl.SSLError) as exc:
        name, value = ("SSL_CERT_FILE", bundle) if bundle else ("SSL_CERT_DIR", directory)
        raise TLSConfigError(
            f"{name}={value} could not be loaded ({exc}). "
            f"Point it at a PEM certificate bundle, or unset it to use the system trust store."
        ) from exc
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
