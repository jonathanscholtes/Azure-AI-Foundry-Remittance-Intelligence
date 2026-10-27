# Load Testing

> Back to the [README](../README.md)

[test/load/load_test.py](../test/load/load_test.py) sends remittance messages to a deployed environment through the real intake path (API → Blob `incoming/` → Service Bus → Durable orchestration), so you can see how the solution behaves under volume: queue depth, throughput, processing time, model throttling and cost.

It creates no messages of its own. It picks `.msg` / `.eml` files at random from a folder you supply, and every setting (how many, how fast, how many at once) is a parameter.

> **Billable.** Every message calls Content Understanding, and review runs call the Foundry agents. Runs above 50 messages require `--yes`.

## Before you start

- **Messages.** Point `--source-dir` at a folder of `.msg` / `.eml` files. The repository includes a synthetic set in [samples/synthetic/](../samples/synthetic/README.md): 20 scenarios (body tables, PDF, scanned PDF, image, Excel, a 1,200-line table, short pay, credit memo, several payments, dispute thread, portal link, Spanish, EDI 820, duplicates) with invented companies, PROs and amounts, one `.msg` per subfolder. Regenerate it with `scripts/synthetic_samples/generate.py` (Windows with Outlook). Keep real remittances outside the repository; the rest of `samples/` is git-ignored.
- **Network.** With a private UI (`-PrivateUi`), run the test from a machine that can reach it (for example over the landing-zone VPN).
- **Capacity.** The default Function plan (`B1`) suits single uploads. For volume, deploy with `-FunctionPlanSku S1` or `P1v3`, and check the `cu-gpt-55` deployment's capacity: it is shared by Content Understanding and two agents. `-MaxConcurrentActivities` (default 8) limits how many activities, and so how many model calls, run at once on each instance.
- **Python 3.11+.** The script uses only the standard library.

## Commands

```powershell
# Show the plan without sending anything
python test/load/load_test.py --source-dir <folder> --count 20 --dry-run

# Smoke test: 5 messages, one every 20 seconds
python test/load/load_test.py --api https://<ui-fqdn> --source-dir <folder> --count 5 --interval-seconds 20

# Every file in the folder once, in random order
python test/load/load_test.py --api https://<ui-fqdn> --source-dir <folder> --each-once

# Steady flow with irregular gaps: one every ~30 seconds, +/- 50%
python test/load/load_test.py --api https://<ui-fqdn> --source-dir <folder> --count 120 --interval-seconds 30 --jitter 0.5 --yes

# A day's volume compressed into one hour
python test/load/load_test.py --api https://<ui-fqdn> --source-dir <folder> --count 1200 --duration-minutes 60 --concurrency 8 --yes

# Burst: 400 messages as fast as 8 senders allow
python test/load/load_test.py --api https://<ui-fqdn> --source-dir <folder> --count 400 --concurrency 8 --yes
```

`--api` is the UI's address (`https://ach-remit-ui.<environment-domain>`); its proxy forwards `/api`. The API app's own `.internal.` address only accepts traffic from inside the Container Apps environment and returns 404 from anywhere else. Before sending, the script calls `/api/health` and stops if it doesn't answer 200.

### With the synthetic set

The synthetic messages sit one per subfolder, so add `--recursive`:

```powershell
# Check the selection: all 20 scenarios, no sending
python test/load/load_test.py --source-dir samples/synthetic --recursive --each-once --dry-run

# Every scenario once, 15 seconds apart (about 5 minutes)
python test/load/load_test.py --api https://<ui-fqdn> --source-dir samples/synthetic --recursive --each-once --interval-seconds 15 --results test/load/results/synthetic-once.json

# 200 messages drawn at random from the 20 scenarios over 20 minutes, 8 senders
python test/load/load_test.py --api https://<ui-fqdn> --source-dir samples/synthetic --recursive --count 200 --duration-minutes 20 --jitter 0.3 --concurrency 8 --yes
```

Each scenario's expected status is in [samples/synthetic/README.md](../samples/synthetic/README.md) (12 complete, 8 go to review), so a run of every scenario once also shows whether each one ended as intended. Repeats in the random run are made unique (`--unique pad`), so every message is processed in full.

## Parameters

| Parameter | Default | Meaning |
|---|---|---|
| `--source-dir` | required | Folder of `.msg` / `.eml` messages |
| `--pattern` | `*.msg,*.eml` | Comma-separated file patterns within the folder |
| `--recursive` | off | Also search subfolders |
| `--count` | `10` | Messages to send, picked at random (a file can be picked more than once) |
| `--each-once` | off | Send every file exactly once in random order; overrides `--count` |
| `--interval-seconds` | `0` | Wait between sends; `0` = no pacing |
| `--duration-minutes` | `0` | Alternative to the interval: spread the sends evenly over this window |
| `--jitter` | `0` | Vary each wait by up to +/- this fraction (0-1) |
| `--concurrency` | `4` | Parallel senders |
| `--unique` | `pad` | `pad` makes every send a new message; `none` sends files unchanged (see below) |
| `--stream` | `loadtest` | Intake stream recorded with each run: `loadtest`, `upload` or `email` |
| `--timeout` | `60` | Seconds to wait for each upload |
| `--seed` | random | Fixes the selection and the gaps, to repeat a run exactly |
| `--results` | none | Write one JSON record per message to this file |
| `--dry-run` | off | Print the plan; send nothing |
| `--yes` | off | Confirm a run above 50 messages |

Pacing sets when each send *starts*. If uploads take longer than the gap, raise `--concurrency` or the sends fall behind the schedule.

## Sending the same message more than once

A run's `processingId` is a hash of the file's bytes, so the same file sent twice is the same message:

| The repeat arrives | Result |
|---|---|
| Within 10 minutes | Service Bus duplicate detection drops it; nothing runs |
| While the first is still processing | The intake starter skips it |
| After the first completed cleanly | A short run that ends **Duplicate skipped**; no second CSV |
| After the first went to review or failed | Processed again in full (and billed again) |

With the default `--unique pad`, every send is made unique so it is processed in full: a `.msg` gets 512 random bytes added after the end of the file, which the reader ignores (subject, sender, body and attachments parse the same), and an `.eml` gets an `X-Load-Test-Id` header. Use `--unique none` to test duplicate handling itself.

## What to watch

- **Live Pipeline** page: queue depth, runs in flight, throughput, p95 processing time, Content Understanding throttle retries, dead-letter count.
- **ACH Remittance Processing** dashboard (Azure Monitor → Dashboards with Grafana): outcomes, cost and usage per run, capacity.
- **Script output:** messages sent by status, distinct files, upload time p50 / p95. Upload time covers only the API accepting the message; processing time is on the Live Pipeline page and the dashboard.

Files are reported by a short content hash, never by name, so results can be shared even when the file names are sensitive.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `API check failed: ... returned HTTP 404` | `--api` is the API app's internal address. Use the UI's address. |
| `API check failed: ... unreachable` | No network path to the UI. With a private UI, connect the VPN; a secure-access client that tunnels Azure traffic must be off or bypass `*.azurecontainerapps.io`. |
| `no .msg / .eml files ... match` | The messages are in subfolders (as in `samples/synthetic/`); add `--recursive`. |
| All messages `queued`, but Live Pipeline shows them under *Queued (Service Bus)* with nothing *In flight* | The Function app isn't reading the queue, most often because **Always On** is off. See [Deployment Guide, Troubleshooting](deployment_steps.md#troubleshooting). |
| Many `http_429` / `http_5xx` | The API or its storage is saturated; lower `--concurrency` or add pacing. |

## Interpreting results

- The mix of message types follows your folder, not production. Use load tests for **capacity** (does the queue drain, where does throttling start) and real traffic for **cost per message**.
- Rising throttle retries with a stable queue mean the model deployment is the limit: raise its capacity or lower `-MaxConcurrentActivities`. A growing queue with no throttling means the Function plan is the limit: scale it up or out.
- Load-test runs appear in the UI and the ledger like any other run, labelled with the `loadtest` stream.
