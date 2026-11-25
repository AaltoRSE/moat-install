"""PyInstaller runtime hook: point OpenSSL at the CA bundle inside the frozen binary.

``urllib`` builds its TLS context from OpenSSL's default verify paths
(``ssl.SSLContext.load_default_certs`` -> ``set_default_verify_paths``), which
know nothing about the frozen archive: the ``certifi`` CA bundle collected by
PyInstaller's built-in ``hook-certifi`` would otherwise sit unused in
``sys._MEIPASS``. This hook runs at start-up and exports ``SSL_CERT_FILE`` so
TLS validation works on hosts without a system CA store (e.g. minimal
containers). An ``SSL_CERT_FILE`` already present in the environment is left
untouched.

The source package stays stdlib-only (``moat_install.httpcheck`` uses
``urllib``); ``certifi`` is needed only in the build environment (dev
dependency group) and inside the frozen binary.
"""

import os

try:
    import certifi
except ImportError:  # fall back to OpenSSL's default verify paths
    certifi = None

if certifi is not None:
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
