# Experiment definitions

This directory contains only the experiment definitions used by the public
artifact.

- `configs/` contains executable configurations for the primary evaluation.
- `frozen/` records protocols and expected outcomes fixed for reported runs.
- `frozen/models/` contains the registered cost models used by held-out
  planning experiments.
- `blackbox_exp4/` contains the standard-library-only generator and objectives
  for the independent black-box plan corpus.

Generated measurements are written under the ignored top-level `results/`
directory. The selected measurements distributed with the paper are immutable
copies under `artifact/results/` and are covered by
`artifact/checksums.sha256`.

For the complete paper-to-protocol-to-command map, see
[`artifact/README.md`](../artifact/README.md).
