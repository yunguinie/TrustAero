# TPC-H SF1 official-query support audit

This is a semantic and execution audit, not a performance result.
DuckDB execution does not imply TrustAero IR support.

| Query | DuckDB SF1 | TrustAero IR v1 | Exact blockers | Output rows |
|---|---|---|---|---:|
| Q01 | PASS | SUPPORTED | none | 4 |
| Q02 | PASS | BLOCKED | like_predicate, correlated_scalar_subquery, order_by, limit | 100 |
| Q03 | PASS | BLOCKED | computed_aggregate_expression, order_by, limit | 10 |
| Q04 | PASS | BLOCKED | correlated_exists, order_by | 5 |
| Q05 | PASS | BLOCKED | computed_aggregate_expression, order_by | 5 |
| Q06 | PASS | SUPPORTED | none | 1 |
| Q07 | PASS | BLOCKED | derived_projection, from_subquery, computed_aggregate_expression, order_by | 4 |
| Q08 | PASS | BLOCKED | case_expression, derived_projection, from_subquery, division, order_by | 2 |
| Q09 | PASS | BLOCKED | like_predicate, derived_projection, from_subquery, order_by | 175 |
| Q10 | PASS | BLOCKED | computed_aggregate_expression, order_by, limit | 20 |
| Q11 | PASS | BLOCKED | computed_aggregate_expression, having, scalar_subquery, order_by | 1048 |
| Q12 | PASS | BLOCKED | case_expression, in_predicate, field_field_filter, order_by | 2 |
| Q13 | PASS | BLOCKED | join_predicate_filter, from_subquery, nested_aggregate, order_by | 42 |
| Q14 | PASS | BLOCKED | case_expression, like_predicate, division | 1 |
| Q15 | PASS | BLOCKED | common_table_expression, computed_aggregate_expression, scalar_subquery, order_by | 1 |
| Q16 | PASS | BLOCKED | distinct_aggregate, like_predicate, in_predicate, subquery, order_by | 18314 |
| Q17 | PASS | BLOCKED | division, correlated_scalar_subquery, computed_predicate | 1 |
| Q18 | PASS | BLOCKED | in_subquery, having, order_by, limit | 57 |
| Q19 | PASS | BLOCKED | computed_aggregate_expression, in_predicate, nested_boolean, computed_predicate | 1 |
| Q20 | PASS | BLOCKED | nested_subqueries, like_predicate, computed_aggregate_expression, order_by | 186 |
| Q21 | PASS | BLOCKED | correlated_exists, correlated_not_exists, field_field_filter, order_by, limit | 100 |
| Q22 | PASS | BLOCKED | string_function, in_predicate, scalar_subquery, correlated_not_exists, order_by | 7 |

Exact IR support: 2/22 (Q01, Q06).

Q1 is supported through a bounded fixed-point product formula and explicit sort keys; Q6 uses explicit filters, a temporal range, and one non-nested numeric product. No query uses a raw-SQL bypass.
