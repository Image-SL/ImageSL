# Hosting capacity

ImageSL is CPU-bound on one thing: measuring a slide. Everything else — decoding,
encoding, zipping — is a rounding error beside it. So capacity planning is a
single question: **how many vCPUs does the container have?**

## Measured cost per slide

Timings below are one slide at working resolution (long edge 1024) on one full
modern core, after the July 2026 optimisation pass:

| stage | seconds | notes |
|---|---|---|
| decode + downsample | 0.05 | scales with source file size |
| **measurement** | **1.14** | the whole cost; pure numpy/scikit-image |
| lossless original (WebP) | 0.07 | the browser's master copy |
| level map (PNG) | 0.03 | carries every sensitivity + the denominator |
| **upload path total** | **1.29** | what a user waits for, per slide |
| overlay + comparison + TIFF | 0.16 | export only |

A batch of 200 slides is therefore **~4.3 CPU-minutes of measurement** and
**~32 CPU-seconds of export**, plus roughly **400 MB** of archive to transfer.

## What that means for the Lightsail power

Real Lightsail powers and prices (us-east-2, `aws lightsail get-container-service-powers`):

| power | vCPU | RAM | $/mo | 200-slide measurement | verdict |
|---|---|---|---|---|---|
| `nano` | 0.25 | 0.5 GB | 7 | ~17 min | unusable |
| `micro` | 0.25 | 1 GB | 10 | ~17 min | **unusable** — the service ran here until 2026-07-30, and this is what produced batches reporting half their slides as "Analysis failed" |
| `small` | 0.5 | 1 GB | 15 | ~8.6 min | tolerable to ~50 slides; still only 1 GB |
| `medium` | 1 | 2 GB | 40 | ~4.3 min | **in use now** — the realistic minimum for 100–200 file batches |
| `large` | 2 | 4 GB | 80 | ~2.2 min | headroom to export while uploads continue |
| `xlarge` | 4 | 8 GB | 160 | ~1.1 min | more than this workload needs |

Note the RAM does not track the vCPU count the way the AWS docs' older tables
suggest — `small` has the same 1 GB as `micro`, so it buys CPU only. Memory
matters here: a measurement peaks in the low hundreds of megabytes, and two
concurrent ones plus the cache write is what pushed the 1 GB tiers into the
OOM killer.

Set it with:

```bash
aws lightsail update-container-service --region us-east-2 --service-name imagesl --power medium --scale 1
```

Check what it is now with:

```bash
aws lightsail get-container-services --region us-east-2 --service-name imagesl --query 'containerServices[0].{power:power,scale:scale,state:state}'
```

## Keep scale at 1

Analyses live in the container's own filesystem (`/data` if mounted, otherwise
the temp dir). With two nodes behind the load balancer, a slide analysed on node
A does not exist on node B, so roughly half of every export would come back
short and roughly half of every sensitivity change would 404. Scaling out needs
shared storage first; scaling *up* is the correct lever here.

## Why the concurrency is deliberately small

`IMAGESL_MAX_CONCURRENCY` (default 2) bounds how many slides are measured at
once, and the browser sends at most 3 uploads in flight. numpy and
scikit-image already spread a single measurement across the available cores, so
running more at once buys no throughput — it only multiplies peak memory and
lengthens every individual request until something times out. The two numbers
are set to keep the measurement gate fed with one request queued behind it, and
no more.

## The health check is load-bearing

`/api/health` is `async` and does no work, deliberately. The load balancer gives
it 5 seconds and restarts the container after five consecutive failures, and a
restart destroys every analysis in the batch. If it ever goes back to being a
synchronous handler it will queue behind measurement work under load and take
the whole session down with it.
