# Releasing GeoLoupe

Releases go to PyPI from GitHub Actions (`.github/workflows/release.yml`) when a tag `vX.Y.Z` is
pushed. PyPI trusts the workflow through "trusted publishing", so no password or token is stored
anywhere.

## One-time setup

1. **PyPI account**: register at <https://pypi.org/account/register/> and turn on two-factor
   authentication.
2. **Trusted publisher**: at <https://pypi.org/manage/account/publishing/>, add a *pending
   publisher* (GitHub tab): PyPI project name `geoloupe`, owner `salmanmohebi`, repository
   `geoloupe`, workflow `release.yml`, environment `pypi`. The first release then creates the
   project.
3. **GitHub environment**: repository → Settings → Environments → New environment `pypi`. Adding
   yourself as a *required reviewer* makes every upload wait for one click.
4. **Security**: Settings → Code security → turn on *Private vulnerability reporting* (used by
   SECURITY.md), *Dependabot alerts* and *Dependabot security updates*.
5. **Visibility**: Settings → General → Danger zone → *Change visibility* → public (needed for the
   README images and badges on PyPI, and for free CI minutes).

## Each release

1. `CHANGELOG.md`: rename "Unreleased" to `[X.Y.Z] - YYYY-MM-DD` and start a new empty
   "Unreleased" section; update the links at the bottom.
2. Set the version in **one** place, `src/geoloupe/__init__.py` (`__version__ = "X.Y.Z"`), and the
   `version` and `date-released` in `CITATION.cff`.
3. Run every check on a fresh clone: `GALLERY=1 scripts/ci_local.sh`.
4. Commit ("Release X.Y.Z"), push to `main`, and wait for a green CI run.
5. Tag and push the tag:

   ```bash
   git tag vX.Y.Z && git push origin vX.Y.Z
   ```

   The workflow checks that the tag equals `__version__`, rebuilds the web client from the tagged
   source, builds and checks the wheel, installs it in a clean environment and starts a server,
   uploads to PyPI, and creates the GitHub release with the files and generated notes.
6. Check from a clean environment: `pip install "geoloupe[all]==X.Y.Z" && geoloupe --version`.
7. Rebuild the public demo (below).

A dry run without upload: Actions → Release → *Run workflow*.

## The public demo (Hugging Face Space)

`deploy/huggingface/` holds a Docker Space that installs GeoLoupe from PyPI and serves the gallery
(free CPU hardware is enough).

1. At <https://huggingface.co/new-space>: a name (e.g. `geoloupe`), SDK *Docker*, template *Blank*,
   hardware *CPU basic*, public.
2. Push the two files:

   ```bash
   git clone https://huggingface.co/spaces/<user>/geoloupe hf-space
   cp deploy/huggingface/* hf-space/ && cd hf-space
   git add . && git commit -m "GeoLoupe gallery" && git push
   ```

3. The Space builds (a few minutes) and starts. After a new release, rebuild it: Settings →
   *Factory rebuild*.

The demo has "Add data" off (the default); keep it that way on any public server (SECURITY.md).

## Container image

`Dockerfile` at the repository root builds an image from the source:

```bash
docker build -t geoloupe .
docker run --rm -p 8000:8000 geoloupe        # the gallery → http://localhost:8000
```
