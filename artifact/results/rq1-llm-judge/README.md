# LLM-as-a-Judge approval baseline

This directory contains the aggregate result used in the paper. The frozen
evaluation uses 120 real-agent plan inputs, three repeated judgments per input,
and the four-way decision space `ACCEPT`, `REWRITE`, `CLARIFY`, and `REJECT`.

The committed file excludes free-form model text and transport records. It
retains the model identifier, frozen-protocol hash, denominators, status counts,
approval errors, and repeat-consistency statistics needed to check the paper's
table. External inference is not required for artifact verification.

Run the offline check with:

```bash
python scripts/verify_llm_judge_approval_result.py
```