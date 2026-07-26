# Optimizer V4 profile audit and expanded January protocol

## Profile audit result

The January profile run `20260721T100730786864Z` passed its structural gate:
12 families, 24 candidate profiles, and 72 raw DuckDB plans are complete.  All
candidate results are equivalent, all early/late physical plans are distinct,
all repeated shapes are stable, and no profile spills to disk.

The audit deliberately keeps the 20-block paired wall-clock result as the
authoritative direction label.  Profile medians agree for 9 of 11 stable
families and disagree for `bts-jan-w192-target0.95` and
`bts-jan-w384-target0.25`.  The 192-byte conflict includes a strong
1110-to-566 ms profile warm-up drift, demonstrating why three instrumented
profiles cannot replace paired timing.

Summed operator CPU divided by profile wall-clock time has median 2.843 and
maximum 3.594.  DuckDB pipeline parallelism therefore makes operator timings
non-additive.  They may inform a compact interaction structure but cannot be
summed as causal candidate costs or used as inference-time features.

## Frozen expanded development matrix

The next run contains four non-overlapping January groups:

- `jan01-07`: `[2024-01-01, 2024-01-08)`;
- `jan08-14`: `[2024-01-08, 2024-01-15)`;
- `jan15-21`: `[2024-01-15, 2024-01-22)`;
- `jan22-31`: `[2024-01-22, 2024-02-01)`.

Each group crosses four sensitive widths (192, 384, 768, and 1,536 bytes)
with three target Join rates (0.25, 0.70, and 0.95), producing 48 complete
families.  Every family uses two balanced warm-up blocks and 20 balanced
measured blocks.  The expected output is 1,920 measured candidate executions;
physical preflights and warm-ups bring the visible progress counter to 2,208.

The runner records exact controlled statistics, both V4 candidate-work
records, every paired latency, result checksums, physical-plan fingerprints,
memory, spill state, and strict-policy feasibility.  It fits no model.

## Scientific boundary

All members of one time window remain together during later cross-validation.
Different widths, rates, or candidate executions from the same window may not
be split between training and validation.  February--December are unopened.

After the run, unstable or tied families are retained and explicitly labeled;
they are not deleted to make V4 easier to fit.  Model structures and fallback
gates may be compared only on this January development matrix.  A complete V4
must be frozen before any external-month evaluation.
