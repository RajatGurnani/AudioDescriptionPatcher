# Architecture decision records

One file per decision that shaped the code and isn't obvious from it.
Format: context → decision → consequences. Status is Accepted unless
noted. Earlier decisions live in the iteration plans
([../plan/README.md](../plan/README.md)); these start where they stop.

| # | Decision | Date |
|---|---|---|
| [0001](0001-ssd-staging-for-hard-drives.md) | Stage hard-drive videos on the SSD; one reader per HDD | 2026-10-29 |
| [0002](0002-batch-pipeline-one-writer.md) | Batch pipeline: 3 items aligning, one writing | 2026-10-29 |
| [0003](0003-process-pool-pairing.md) | Pairing scores in worker processes, not threads | 2026-10-29 |
| [0004](0004-split-on-wobbly-sync.md) | Try piecewise when one offset's sync wobbles (> 120 ms) | 2026-10-29 |
