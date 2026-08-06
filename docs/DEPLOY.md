# ImageSL — Deploying

The live site is **<https://imagesl.online>**, running on an **AWS Lightsail
container service** in `us-east-2`. Deployment is automatic: every push to `main`
runs [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml), which
builds the `Dockerfile`, pushes the image to ECR, and rolls out a new deployment.

There is nothing to click. If you have pushed to `main`, you have deployed.

## What the pipeline needs (already set up)

| Thing | Value |
| --- | --- |
| AWS account | `581586866061` |
| Region | `us-east-2` |
| ECR repository | `imagesl` |
| Lightsail service | `imagesl` |
| GitHub OIDC role | `arn:aws:iam::581586866061:role/imagesl-github-deploy` |

Authentication is OIDC — there are no stored AWS keys in the repository or in
GitHub secrets.

## Capacity

**`SCALE` must stay at 1.** Analyses live in the container's own memory, so a
second node cannot see a slide analysed on the first and every second export
would come back half empty.

`POWER` is the setting that matters for speed: a slide is about a second of numpy
on a full core, so on a 0.25-vCPU `nano`/`micro` it takes ten to twenty seconds
and a large batch cannot finish inside a sane timeout. Power and scale belong to
the *service*, not to a deployment, so `deploy.yml` does not carry them:

```bash
aws lightsail update-container-service --region us-east-2 --service-name imagesl --power medium --scale 1
```

See [CAPACITY.md](CAPACITY.md) for the sizing argument.

## Environment variables

Set in the `environment` block of `deploy.yml` — that file is the source of
truth. Changing a variable in the Lightsail console works until the next push to
`main`, which overwrites it.

| Variable | Purpose |
| --- | --- |
| `IMAGESL_VERSION` | Version reported by `/api/health` and `/api/downloads` |
| `IMAGESL_MAX_CONCURRENCY` | Slides measured at once; track the container's vCPU count |
| `IMAGESL_CACHE_MAX` / `_MB` / `_TTL` | Analysis cache size, memory budget, idle lifetime |
| `IMAGESL_DOWNLOAD_URL_WINDOWS` / `_MACOS` | Where `/download/<platform>` redirects — see below |
| `IMAGESL_DOWNLOAD_DIR` | Serve installers from the container's own disk instead |
| `IMAGESL_MAX_UPLOAD_MB` | Upload cap (default 256) |
| `IMAGESL_ACCESS_TOKENS` | Comma-separated license keys; omit to run open |

Full annotated list in [`.env.example`](../.env.example).

## Publishing the desktop installers

The installers are **not** served from GitHub Releases. The repository is
private, so anonymous release links 404 — and they 404 as an HTML page, which
navigates the visitor away instead of downloading. This is why `/download/` is
served by the application itself.

A build is ~86 MB and is gitignored, so it is not in the image. It lives in S3
and the app redirects to it. The full procedure — bucket, IAM policy, CI upload
job, and the one-time bootstrap of an already-built installer — is in
**[../desktop/BUILD.md](../desktop/BUILD.md)**, which owns the whole build and
publish path.

## Verify a deploy

```bash
curl -s https://imagesl.online/api/health
curl -s https://imagesl.online/api/downloads
```

`/api/health` returns `{"status":"ok","version":"..."}`. `/api/downloads` reports
per platform whether an installer is actually reachable; the landing page greys
out any platform that is not, rather than handing the visitor a 404.

## Local development

```bash
pip install -r server/requirements.txt
uvicorn --app-dir server app:app --port 8000
```

Then <http://localhost:8000>. To exercise the download buttons locally, drop the
built installers into `downloads/` at the repo root (that path is the default
outside the container) and reload.
