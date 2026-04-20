# Postmortem: hl-trades-recorder Data Loss, 2026-04-18 → 2026-04-19

**Status:** Resolved
**Severity:** SEV-2 — partial product-data loss, no customer impact
**Service:** `hl-trades-recorder` (the Hyperliquid side; `extended-trades-recorder` unaffected)
**Author:** Marco Lavagnino
**Date written:** 2026-04-20

---

## Summary

Starting at **13:44 UTC on Saturday 2026-04-18** (09:44 ET), the Hyperliquid trades recorder entered a crashloop and stopped writing data for the remainder of the weekend collection window. The two daily Parquet files that should have held Saturday + Sunday `xyz:CL` / `xyz:BRENTOIL` trades — `trades_2026-04-18.parquet` and `trades_2026-04-19.parquet` — were left corrupted (no `PAR1` footer) and cannot be read. Approximately **29 hours** of Hyperliquid trading data was lost across the remainder of the window.

The Extended Exchange recorder ran on an identical image but wrote to a different filename prefix and never corrupted its file; all three Extended parquet files for the weekend are intact.

The incident was discovered on **Monday 2026-04-20 at ~12:15 ET** when the operator tried to rsync the weekend's data locally and `visualize.py` raised `ArrowInvalid: Parquet magic bytes not found in footer`.

## Impact

| File | Status | Data lost |
|---|---|---|
| `trades_2026-04-17.parquet` (Friday HL) | ✅ intact, 67,415 rows | none |
| `trades_2026-04-18.parquet` (Saturday HL) | ❌ corrupt, no footer | full day |
| `trades_2026-04-19.parquet` (Sunday HL) | ❌ corrupt, no footer | full day |
| `trades_extended_2026-04-17/18/19.parquet` | ✅ intact (558 + 2,618 + 1,961 rows) | none |

No downstream consumer was affected — this is first-week data from a new recorder and no models or dashboards depended on it yet. If this had happened after the data was wired into a model, the blast radius would have been significantly wider.

## Detection

**Passive.** There was no alert. The operator discovered the issue manually while inspecting the weekend's files. Instrumentation gaps are the first action item below.

## Timeline

All times UTC. Evidence: Datadog log search across `service:hl-trades-recorder` and `docker.mem.rss` metric.

| Time (UTC) | Event |
|---|---|
| **2026-04-17 20:00** | Collection window opens (Fri 16:00 ET). HL recorder starts writing `trades_2026-04-17.parquet`. |
| 2026-04-18 04:04 UTC | Friday data flush finalizes `trades_2026-04-17.parquet` at 4.3 MB / 67,415 rows. |
| 2026-04-18 00:00 ET | Saturday file `trades_2026-04-18.parquet` begins. |
| 2026-04-18 13:34:23 UTC | Successful flush — 132,110 cumulative rows. Container RSS: 220 MB. |
| 2026-04-18 13:39:24 UTC | Successful flush — 136,164 cumulative rows. Container RSS: 244 MB. |
| **2026-04-18 13:44:00 UTC** | **RSS peaks at 245.5 MB (95.9% of 256 MB limit). Kernel OOM-killer fires SIGKILL during `pq.write_table`. File is left with data pages but no footer.** |
| 2026-04-18 13:44:25 UTC | Docker restart policy starts a new container. Subscribes, begins buffering fresh trades in memory. |
| 2026-04-18 13:49:26 UTC | First `ArrowInvalid` — flush attempts to read the now-corrupt existing file, `TaskGroup` re-raises the exception, `main()` exits, container restarts again. Buffered trades since 13:44:25 are lost. |
| 2026-04-18 13:49 → 2026-04-19 23:03 UTC | **271 restart cycles over ~33 hours.** Every cycle: subscribe → buffer ~5 min of trades → flush tries to read the torn file → crash → restart → empty buffer, repeat. |
| 2026-04-19 ~00:00 ET | Container's in-container clock rolls over to Sunday. New flush attempts target `trades_2026-04-19.parquet`, which doesn't exist yet — the first flush after rollover creates it, but subsequent flushes OOM the same way (same growth pattern) and the file is corrupted too. |
| 2026-04-19 23:00 UTC (Sun 19:00 ET) | Collection window closes. Buffer is empty (nothing inside the window), so `flush()` early-returns without touching the torn file — crashloop stops, container appears stable. |
| **2026-04-20 16:00 UTC** | Operator rsyncs data locally. `visualize.py` raises `ArrowInvalid`. Incident discovered. |
| 2026-04-20 16:02 UTC | Fix deployed — atomic writes + corrupt-file quarantine. Both containers running healthy on patched image. Corrupt files quarantined on server and locally as `*.parquet.broken.manual.*`. |

## Root Cause

A single OOM kill was enough to wedge the HL container for the rest of the weekend because the Parquet sink had **three compounding design flaws**. Removing any one of them would have contained the incident.

### 1. Per-flush memory growth unbounded in file size (the trigger)

Every flush ran:

```python
existing = pq.read_table(path, schema=SCHEMA)        # load whole file
table = pa.concat_tables([existing, pending])         # materialize concat
pq.write_table(table, path, compression="snappy")    # rewrite whole file
```

As the daily file accumulated row groups, each flush re-read the **entire** file into an Arrow in-memory table. Compressed Parquet expands roughly 5–10× when loaded (Snappy decode + Arrow column overhead), and Python doesn't reliably return freed heap to the OS for many small allocations. The measured RSS growth on Saturday was:

| Time (UTC) | RSS | Event |
|---|---:|---|
| 13:29 | 195 MB | steady state |
| 13:34 (after flush) | 220 MB | +25 MB not returned |
| 13:39 (after flush) | 244 MB | +24 MB not returned |
| 13:44 (next flush) | 246 MB → SIGKILL | blew past 256 MB cap |

This was a **staircase to OOM** that scaled with file size, so it was guaranteed to fail on any sufficiently-large daily file, and it would fail earlier as trading volume increased.

**Why the 256 MB limit was chosen:** the spec for the original recorder (single coin, single exchange) showed flush-time memory usage well under 100 MB. When the service grew to two coins + the mark/oracle price cache + a second venue's worth of activity, the limit was not revisited. This is a classic capacity-drift bug.

### 2. Non-atomic write (the corruption mechanism)

`pq.write_table(table, path, ...)` writes directly to the target path. `SIGKILL` during write leaves an arbitrary prefix on disk, with no footer. The universal safe pattern — write to a temp file in the same directory, then `os.replace(tmp, path)` — gives atomic publish on POSIX: either the reader sees the old complete file or the new complete file, never a torn one.

The recorder was written without this pattern because the original spec assumed a single-threaded, never-killed process. That assumption held for one weekend and then it didn't.

### 3. Fatal on unreadable existing file (the loss multiplier)

The flush began with `existing = pq.read_table(path, schema=SCHEMA)` with no try/except. A single corrupt file turned every subsequent flush into a process-exiting exception, and because Docker's `restart: unless-stopped` dutifully restarted the container, the new container started with an **empty** buffer — it had no memory of trades collected by the previous instance. With a 5-minute flush interval and a ~25-second crash-to-restart latency, the crashloop dropped essentially 100% of in-flight trades.

### Why `OOMKilled=false`

The Docker daemon reports `OOMKilled=true` based on kernel cgroup events, which are more reliable under cgroup-v1 than cgroup-v2. The host is a DigitalOcean droplet (`ubuntu-s-2vcpu-8gb-160gb-intel-sgp1-01`) running a cgroup-v2 kernel, where the kill manifests as a plain SIGKILL from the OOM-killer and the daemon's flag isn't always propagated. Additionally, `OOMKilled` is a *current-state* field: it is reset on every restart, and by the time the incident was investigated the container had restarted 271 times. The authoritative evidence is the `docker.mem.rss` time series, which shows the staircase unambiguously.

## Mitigations applied (2026-04-20 ~16:02 UTC)

Shipped in commit [`6e5191e`](https://github.com/0xmetropolis/hl-trades-recorder/commit/6e5191e) on the `hl-trades-recorder` repo:

1. **Atomic write:** `pq.write_table(table, tmp)` followed by `os.replace(tmp, path)`. If the process is killed during write, the target path still points at the last good file; the `.tmp` is cleaned on the next flush start.
2. **Corrupt-file quarantine on read:** if `pq.read_table(existing_path)` raises, log the exception, rename the file to `<path>.broken.<ts>`, and continue by writing the buffer out as a fresh file. A single bad file no longer crashloops the container.
3. **Flush-level try/except:** any failure during the full flush path keeps the in-memory buffer for the next attempt instead of discarding it with the process.

The corrupt files on the server were renamed to `*.parquet.broken.manual.<ts>` so they remain archived (in case we ever attempt byte-level recovery) but won't interfere with the new fix's quarantine logic. They are not deletable data — we just don't have a tool to read them.

The window guard in CI prevented this deploy from being blocked (today is Monday, outside Fri 16:00 ET → Sun 19:00 ET), so the fix went out cleanly.

## Action items

| # | Item | Owner | Priority |
|---|---|---|---|
| 1 | **Root-cause the memory pattern.** Replace read-back-and-rewrite with an open `pq.ParquetWriter` held for the lifetime of the daily file, appending each flush as a new row group. Memory then scales with buffer size, not file size. | TBA | High |
| 2 | **Raise container memory to 512 MB** as a belt-and-braces hedge until item 1 ships. Current cap is an artifact of the single-coin / single-venue era. | TBA | High |
| 3 | **Datadog monitor on RSS headroom.** Alert when `docker.mem.rss / docker.mem.limit > 0.85` for 5 min on any `trades-recorder`. Would have paged ~9 minutes before the first corruption. | TBA | High |
| 4 | **Datadog monitor on restart count.** Alert when `docker.containers.restarts > 3 in 15 min`. Would have paged within the first hour of the crashloop. | TBA | Medium |

Ownership left as TBA so it can be assigned in standup.

## Lessons

- **Memory limits are a capacity decision that must be revisited whenever the workload shape changes.** The 256 MB cap was fine for the first-version workload and silently wrong for the second. No one re-evaluated it when `HL_COINS` became two coins, a mark/oracle cache was added, and (separately) Extended landed.
- **"Never interrupt collection" is a distributed-systems property, not a deploy policy.** The CI window-freeze prevents *us* from stopping collection, but it doesn't prevent the kernel or the container runtime from doing so. Robustness to unexpected SIGKILL is not optional for a recorder.
- **`Docker restart: unless-stopped` + in-memory buffers is a data-loss pattern** unless the process can durably fence off bad state on startup. A new process must not discover an irrecoverable file and die — it must either fix it, quarantine it, or keep writing somewhere valid.
- **Cgroup-v2 erases the `OOMKilled` signal.** Future debugging should look at RSS time series first, not Docker's post-hoc flags.
- **The window guard worked as intended** — once the fix was ready on Monday, the deploy went out without manual overrides. Sunday-evening hotfixing would have been operationally painful, but it's now a known pattern. The only thing the freeze did not do (and was never supposed to do) was stop the kernel from OOM-killing us while collection was actively running; there is no CI-level control for that.

## Appendix A — Evidence

### OOM confirmation query

```bash
pup metrics query \
  --query='avg:docker.mem.rss{container_name:hl-trades-recorder}' \
  --from='2026-04-18T13:00:00Z' --to='2026-04-18T14:30:00Z'
```

Peak RSS **245.5 MB / 256 MB cap (95.9%)** at 13:44:00 UTC, exactly when the first crash was logged.

### Crash volume

```bash
pup logs search \
  --query='service:hl-trades-recorder "Parquet magic bytes"' \
  --from='2026-04-17T00:00:00Z' --to='2026-04-20T00:00:00Z' --limit 500
```

**477 log lines** matching the footer-missing exception across the window, corresponding to 271 container restarts (~1.8 lines per restart due to multi-line traceback split).

### Why the Extended recorder survived

`extended-trades-recorder` runs the same image with the same 256 MB limit, but three structural differences saved it:

1. **Smaller daily file.** Extended's 2,618-row Saturday file is ~50× smaller than HL's; the read-back-and-rewrite never crossed the RSS cap.
2. **Different filename prefix.** `trades_extended_*` vs `trades_*` means the two recorders don't share files — a corrupt HL file couldn't poison Extended's flush.
3. **Separate container process.** An OOM in one container has no effect on the other.

Point 2 was a deliberate design decision (to avoid multi-writer contention on a single parquet file). That decision also turned out to be a fault-isolation win.
