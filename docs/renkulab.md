# GeoLoupe on RenkuLab

[RenkuLab](https://renkulab.io) (by the Swiss Data Science Center) runs GeoLoupe in the browser
without installing anything. A **public** project can be opened by anyone with its launch link:
every visitor gets their own session.

## What you need

* The GeoLoupe container image, published with every release:
  `ghcr.io/salmanmohebi/geoloupe:latest` (or a version, e.g. `:0.3.1`). By default it serves the
  gallery of open public data on port 8000. In a RenkuLab session it finds its URL path (Renku runs
  every session under `/sessions/<id>`) in the variable `RENKU_BASE_URL_PATH`, so it needs no
  extra settings.
* A RenkuLab account (log in at <https://renkulab.io> with SWITCH edu-ID or another supported login).

## Set it up once

1. **Create a project:** *Create* → *Project*. Name it (e.g. *GeoLoupe*) and set the visibility to
   **Public**: only public projects can be launched by everyone.
2. **Add a session launcher:** in the project, *Sessions* → *Add session launcher* →
   *External image*, and fill in:

   | Field | Value |
   |---|---|
   | Container image | `ghcr.io/salmanmohebi/geoloupe:latest` |
   | Port | `8000` |
   | Default URL | `/` |
   | UID / GID | `1000` / `100` (any non-root user works) |
   | Command, arguments | leave empty (the image starts the gallery) |
   | Resource class | the smallest one is enough (GeoLoupe needs about 1 GB of memory) |

3. **Launch** it once to check: the gallery opens inside the session after the image is pulled
   (about a minute the first time on a node, a few seconds later).
4. **Share it:** next to *Launch*, open the menu → *Share session launch link*. The link (or the
   Markdown badge) takes anyone straight to a new session, without the project page.

## Your own data in the session

Use the arguments to serve something else than the gallery, e.g. *Command* `["sh", "-c"]` and
*Arguments* `["geoloupe serve s3://bucket/data.zarr --anon --host 0.0.0.0 --port 8000"]`, or a
configuration file from the project's repository (mounted in the session's working directory).

*Add data* in the browser is **off** in these sessions, because the server listens on the session's
network: a public project should not let visitors make it read any URL. To turn it on for a
project of your own, add `--allow-open` to the arguments.

## Notes

* Sessions stop when idle, and each visitor starts their own: data read by one session is not
  cached for the next.
* The image runs on x86-64 (linux/amd64).
