# Security

## Reporting a vulnerability

Please do **not** open a public issue. Use GitHub's private reporting instead: the **Security** tab
of this repository → **Report a vulnerability**. We answer within a week and publish a fix and an
advisory as soon as possible.

## Supported versions

Only the latest release gets security fixes while GeoLoupe is below 1.0.

## Security model: what to know before you expose a server

GeoLoupe is built for one person or a trusted group exploring data. Keep this in mind:

* **There is no login.** Anyone who can reach the server sees all its data. The server listens on
  `127.0.0.1` by default; to share it, put it behind a proxy with authentication (or JupyterHub).
* **"Add data" is off by default.** `--allow-open` lets every visitor make the server fetch any URL
  (including addresses inside your network), and `--open-root DIR` lets them open any file below
  `DIR`. Never enable them on a public server.
* **Requests are bounded** (cells read per tile, time-series length, export size, vector size), but
  there is no rate limit. A public demo should run behind a proxy that limits request rates.
* **Configuration files are trusted input**: they can point to any file or URL the server can read.
