"""HTTPS certificates: let Python trust the same certificate authorities as the system.

Python verifies HTTPS servers against a list of trusted certificate authorities. Some Python
installations have no such list (Python from python.org on macOS until one runs its "Install
Certificates" step), and some networks re-sign HTTPS traffic with their own authority, which only
the operating system trusts. Both make every remote dataset fail with CERTIFICATE_VERIFY_FAILED,
while GDAL (with its own list) still works. The `geoloupe` command calls `use_system_certificates`
first; Python users call it themselves before opening remote data, or set SSL_CERT_FILE.
"""

from __future__ import annotations

import logging
import os
import ssl
import sys

log = logging.getLogger(__name__)

CERT_HINT = (
    "Python cannot verify HTTPS certificates on this computer: set "
    'SSL_CERT_FILE="$(python3 -m certifi)", or with Python from python.org on macOS run its '
    "'Install Certificates.command' once"
)


def use_system_certificates() -> str:
    """Make HTTPS verification work; returns what was done (for the log).

    Call it before anything opens an HTTPS connection: aiohttp (used by fsspec) builds its
    default SSL context when it is first imported.

    - SSL_CERT_FILE or SSL_CERT_DIR set: the user chose; nothing changes ("environment").
    - macOS and Windows: the system certificate store through truststore ("system store").
    - Elsewhere, if Python finds no certificate authorities: certifi's list ("certifi").
    - Otherwise Python's default, which on Linux is the system list ("default").
    """
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return "environment"
    if sys.platform in ("darwin", "win32"):
        try:
            import truststore

            truststore.inject_into_ssl()
            return "system store"
        except Exception as e:  # a broken system store must not stop the server
            log.debug("truststore failed (%s); using Python's certificates", e)
    if not _has_default_authorities():
        try:
            import certifi

            os.environ["SSL_CERT_FILE"] = certifi.where()
            return "certifi"
        except ImportError:
            pass
    return "default"


def _has_default_authorities() -> bool:
    paths = ssl.get_default_verify_paths()
    if paths.cafile:  # None when the file does not exist
        return True
    capath = paths.capath
    return bool(capath and os.path.isdir(capath) and any(os.scandir(capath)))


def explain(message: str) -> str:
    """Add the fix to a message about a certificate error."""
    if "CERTIFICATE_VERIFY_FAILED" in message and CERT_HINT not in message:
        return f"{message}. {CERT_HINT}"
    return message
