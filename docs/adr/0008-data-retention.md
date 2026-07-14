# Data retention: raw 30d, hourly rollups 1y, daily forever, incidents forever

Check Results are high-volume append-only time-series; without a retention policy the database fills the disk. We layer retention with TimescaleDB's native primitives:

| Data | Retention | Mechanism |
|---|---|---|
| Raw **Check** rows | **30 days** | TimescaleDB `drop_chunks` policy on the raw hypertable. |
| **Hourly rollups** (uptime %, p50/p95 latency, fail count, per Monitor per hour) | **1 year** | Continuous aggregate, `drop_chunks` after 12 months. |
| **Daily rollups** | **forever** | Continuous aggregate, no drop policy. Cheap; powers long-term trend Reports. |
| **Incidents** | **forever** | Plain table, low volume (only transitions), human-meaningful. Never pruned. |
| **Scheduled Reports** metadata | forever | Rendered body kept 30 days then GC'd. |

Why this shape:

- **30 days of raw** is enough to investigate any recent incident in full detail; beyond that, the hourly rollup answers "was there an outage?" with sufficient fidelity.
- **Hourly → daily rollups** let us compute the monthly uptime Scheduled Report (Report kind #1) cheaply, indefinitely, without scanning raw rows. The daily aggregate is small enough to keep forever — that's the long-term memory of the system.
- **Incidents are forever** because they are the canonical, low-volume, human-readable history. Losing them would silently rewrite a user's understanding of their own reliability.

Rejected:

- **Raw forever, no rollups:** storage grows unbounded and monthly-uptime queries get slow at scale — the exact problem Timescale rollups exist to solve.
- **Raw 7d only:** too short — the monthly Scheduled Report becomes uncomputable or lossy.
- **Raw 90d + 2y hourly:** a reasonable paid-tier upgrade path, but overkill for launch storage budgets.
