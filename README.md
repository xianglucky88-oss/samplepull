# samplepull

A small offline data-processing service with a local HTTP fixture and SQLite storage. It is self-contained and does not require network access.

## Usage

```text
make run       # process the bundled source
make run_inc   # process using the incremental command-line mode
make test      # run the test suite
make mock      # start the local fixture service
```

The package exposes a Python entry point under `src/samplepull`. The fixture service supports the source formats used by the package, and the command-line entry point writes results to the local SQLite database. Re-running commands is safe for an existing database.

## Quality conclusions (质量结论)

Samples may receive several quality notices — `initial` 初检、`recheck` 复检、`revoke` 撤销 — from different sources, repeated, reordered or revised after the fact. The quality engine (`samplepull.quality`) evaluates the whole notice set of a sample into one conclusion whose state is always distinguishable:

| State | 含义 | 触发要点 |
| --- | --- | --- |
| `PROVISIONAL` 暂定 | 有初检但未经复检确认 | rule R7 |
| `PUBLISHED` 已发布 | 复检与初检一致，结论稳定 | rule R8 |
| `CONFLICT` 存在冲突 | 初复检不一致 / 同阶段多来源冲突 / 撤销无目标 | R5, R9, R11 |
| `INCOMPLETE` 缺少关键信息 | 通知缺字段、只有复检无初检、有效通知全被撤销 | R1, R6, R10, R12 |

Every stored conclusion keeps its full basis: participating notices, the role of each notice (`active` / `duplicate` / `superseded` / `revoked` / `revoke` / `invalid` …), the fired rule ids and rule-catalog versions (`quality.RULES`, versioned so historical bases stay interpretable), anomalies and a `basis_hash`. Conclusions are append-only and versioned; a changed basis creates a new version and never overwrites history.

Determinism guarantees:

* Repeated receipt of the same notice (same source/kind/id/version and business content) is deduplicated on a stable **identity hash** and cannot move a published conclusion; receipt-only metadata changes are ignored by the decision hash.
* A newer `notice_version` supersedes the older one (R3); ties are broken by a total, order-independent ranking, so arrival order and out-of-order delivery never change the result.
* A revoke voids its target notice (R4); an unresolvable revoke is reported as a conflict (R5).

Collection (`samplepull.qpipeline`) supports full and incremental runs against `/quality/items`. Incremental runs use a separate `quality_watermark` on server receipt time, the run ledger (`quality_runs`) records success/failure, and on failure the watermark and all published conclusions remain untouched until the next idempotent recovery run. Legacy `records`/`meta` tables and the original CLI commands are unchanged; quality data lives in its own `quality_*` tables.

```text
make quality            # full quality collection
make quality_inc        # incremental quality collection
make quality_list       # list the latest conclusion of every sample
make quality_show SAMPLE=Q-S03   # full traceability view for one sample
```
