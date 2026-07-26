# Optimizer V3 real-pipeline root-cause diagnosis

## Why every final decision was late Mask

The all-late result has two separate causes.

First, the primary interaction model itself points to late Mask in 11 of the
12 BTS families.  It is not merely an uncertainty-guard artifact: eight
families use confident direct cost ranking, while three use ridge-disagreement
fallback and one uses uncertainty fallback.  On the 11 stable families, the
primary model's direction is correct in only four.

Second, every fallback goes to V1, whose fixed early-materialization setup term
is 256 MiB.  At 106,251 Join-input rows, V1 can choose early only when

`match_rate * sensitive_width > 64 + 256 MiB / 106251 = 2590.43 bytes`.

The largest observed product is only 1459.79 bytes.  Early Mask is therefore
mathematically unreachable for V1 anywhere in this real-data grid, regardless
of the measured runtime.

## Assessment of the four proposed explanations

### Feature distortion: partially confirmed

This is not a simple range or cardinality error.  Post-filter rows, controlled
sensitive width, and achieved Join match rate are measured from the real input
and all lie inside the frozen training support.  DuckDB profiles confirm the
same 106,251 Join-input rows and the expected 28,129 versus 76,266 outputs for
the representative 25% and 70% families.

The semantic meaning of the three features is nevertheless insufficient.  V3
does not describe total carried row width, Parquet/expression path, dimension
and output schema width, sort shape, or the effect of an explicit pipeline
breaker.

### Cost omissions: confirmed for the physical pipeline

V3 directly predicts one complete early/late ratio from three logical
statistics.  It does not separately estimate:

- hashing all input rows versus hashing only matched rows;
- raw versus masked bytes carried through the Join;
- writing and reading the early masked boundary;
- downstream projection and sort work;
- pipeline-breaker and parallel-execution effects.

Policy filters are common upstream work in this query.  Source-lineage capture
is outside the timed candidate difference.  Adding either to explain the
current relative failure would be unsupported, although future candidate-
specific lineage strategies will need their own cost term.

### Decision-boundary collapse: confirmed

The learned surface is dominated by match-rate terms and its intercept; the
row-count contribution is nearly zero at the BTS point.  High-match real
families lie near the synthetic model's tie boundary even though early Mask is
30--40% faster.  Uncertainty and stability fallback then reinforce the all-
late outcome through V1's unreachable early boundary.

### Estimate-versus-measurement deviation: ratio calibration confirmed

The three input statistics are actual pre-execution measurements, not faulty
estimates, and the physical candidates remain distinct.  The error is between
predicted and measured candidate cost ratios.  One 192-byte/70%-target family
has isolated timing instability, but the wrong-sign predictions remain across
all six stable early-preferred families.

## Consequence for V4

V3 remains frozen as a baseline.  V4 must estimate the two legal candidate
costs from decomposed physical work and real operator statistics.  January may
be used for development; February--December must remain unopened until V4,
its fallback, feature extractor, and acceptance gates are frozen.  Refitting
the same polynomial, lowering uncertainty, or changing one threshold is not an
acceptable fix.
