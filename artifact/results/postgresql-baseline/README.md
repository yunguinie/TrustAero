# PostgreSQL conventional-governance baseline

This experiment compares Direct SQL, native row-level security (RLS), and RLS
with a masking view on one million deterministic rows. Each method uses 20
warm-up transactions followed by five blocks of 100 single-client measured
transactions.

The committed summary contains correctness checks, all five block-level timing
measurements, medians, ranges, and overhead relative to Direct SQL. The frozen
protocol, SQL definitions, and executable runner are included in the repository.

Run the experiment with Docker available:

```bash
python scripts/run_postgres_conventional_baseline.py --docker docker --root . --output results/postgresql-baseline/run
```