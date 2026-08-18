# ImageSL — Deploying

> `<AWS_ACCOUNT_ID>` throughout this document is the AWS account the stack is
> deployed into. It is not written into the workflows: set it once as the
> repository variable `AWS_ACCOUNT_ID` (Settings -> Secrets and variables ->
> Actions -> Variables) and both workflows read it from there. A fork deploying
> into its own account sets its own value and changes no files.

The live site is **<https://imagesl.com>**, running on an **AWS Lightsail
container service** in `us-east-2`. Deployment is automatic: every push to `main`
runs [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml), which
builds the `Dockerfile`, pushes the image to ECR, and rolls out a new deployment.

There is nothing to click. If you have pushed to `main`, you have deployed.

## What the pipeline needs (already set up)

| Thing | Value |
| --- | --- |
| AWS account | `<AWS_ACCOUNT_ID>` |
| Region | `us-east-2` |
| ECR repository | `imagesl` |
| Lightsail service | `imagesl` |
| GitHub OIDC role | `arn:aws:iam::<AWS_ACCOUNT_ID>:role/imagesl-github-deploy` |

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

### The bucket

`imagesl-downloads-<AWS_ACCOUNT_ID>`, in `us-east-2`. Deliberately **not** the
existing `imagesl-build-<AWS_ACCOUNT_ID>`, which holds `imagesl-source.zip`: a bucket
that serves anonymous downloads should not also hold the source, and one
mis-scoped policy would be the whole difference.

Public read is granted by bucket policy to `latest/*` and `v/*` only — not to the
bucket. ACL-based public access stays blocked (`BlockPublicAcls`,
`IgnorePublicAcls`); only `BlockPublicPolicy` / `RestrictPublicBuckets` are off,
which is the minimum that lets that policy apply.

```
latest/ImageSL-Setup-Windows.exe   overwritten every release, max-age=300
latest/VERSION                     the version those bytes are, max-age=60
v/<version>/...                    immutable archive copy, max-age=31536000
```

> **`IMAGESL_DOWNLOAD_BUCKET` must be set as a repository variable**
> (Settings → Secrets and variables → Actions → Variables) to
> `imagesl-downloads-<AWS_ACCOUNT_ID>`. Until it is, `deploy.yml` resolves no
> installer URLs, omits them from the deployment, and the download buttons go
> back to "coming soon" on the next successful deploy — silently, because that is
> the honest state as far as the app can tell. The workflow prints a warning when
> the variable is missing; it is not an error, so it does not fail the run.

The Windows installer was uploaded by hand on 2026-08-07 to get the button
working while the CI pipeline was broken, so the objects above exist but nothing
in CI has ever written them. The first tagged release will overwrite `latest/`
normally.

## Verify a deploy

```bash
curl -s https://imagesl.com/api/health
curl -s https://imagesl.com/api/downloads
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
