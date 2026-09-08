# Public API tracker

The driver's public API is being re-implemented on the Rust core, and the API-preservation rule says
every member under `com.datastax.oss.driver.api.**` survives the move. This directory records that
surface member by member and tracks how much of it the Rust bridge actually serves yet — the Java
counterpart of the sibling project's `C# over Rust driver API.xlsx`.

The tracked state is the **CSVs in this directory**, one per sheet, so progress is diffable and shows
up in `git log`. The `.xlsx` is a render of them into `out/`, which is gitignored.

## Using it

```bash
make api-report    # render out/*.csv and the workbook -- no build needed
make api-import    # fold your workbook edits back into the CSVs (then commit them)
make api-baseline  # refresh the generated columns from the compiled tree
make api-check     # fail if the compiled tree has drifted from the CSVs
```

`api-report` and `api-import` work on a fresh clone. `api-baseline` and `api-check` depend on
`compile-all`, because the member list comes from `javap` over `*/target/classes`.

Python 3.11+, standard library only. `javap` is taken from `JAVA_HOME` if set, otherwise from `PATH`;
any JDK 11 or newer can read this driver's class files.

### Editing

`Github issue?`, `Implemented and waiting as PRs`, `Merged to master` and `Comment` on the Function
list, plus `Disposition` and `Comment` on Config options, are yours. Everything else is regenerated.
Edit them in the workbook and run `make api-import`, or edit the CSVs directly — `api-baseline`
preserves them either way. On the Error mapping sheet the whole right-hand side is yours: `Java
exception`, `Explicitly implemented`, `All variants mapped` and `Comment` are carried, never
regenerated, on Rust, Java and wire rows alike.

If a member is renamed or removed, its annotations cannot be matched: `baseline` and `check` report
them as *orphaned*, and `import` refuses a workbook row that matches nothing as `UNMATCHED` rather
than dropping it silently. Re-render the workbook and redo those rows.

### Two columns this repo cannot recompute

They are carried in the CSVs and only refreshed when you ask:

```bash
make api-baseline API_TRACKER_ARGS="--upstream-repo ../java-driver --rust-src ../scylla-rust-driver"
```

- **`Scylla-only?`** needs a diff against the upstream DataStax driver, and this repo has no upstream
  remote. Point `--upstream-repo` at a clone that has one (`--upstream-ref` defaults to `apache/4.x`).
- **The Rust half of `Error mapping`** is parsed from `scylla/src/errors.rs`; point `--rust-src` at a
  scylla-rust-driver checkout to re-snapshot it. Which commit it came from is recorded in
  `provenance.json` — repo name and short sha only, never the checkout path — and shown on Notes.

Without those flags the values are carried forward unchanged, and new members get a blank plus a
warning — never a silent zero.

## The sheets

| Sheet | Rows | |
|---|---|---|
| Function list | 2,897 | one row per public/protected API member — the backlog |
| Categories | 389 | per-package and per-class rollup, live `COUNTIFS`/`SUMIFS` formulas |
| Integration tests | 138 | one row per IT class; the quarantine count is the compat metric |
| Error mapping | 113 | Rust `scylla::errors` × Java exceptions × the wire error-code table |
| Config options | 170 | every `DefaultDriverOption` and what becomes of it on a Rust core |
| Notes | — | provenance, every derivation rule, warnings, orphaned annotations |

`Categories` and `Notes` are pure functions of the other four, so they are rendered into `out/` and
never committed. Two columns of the other sheets are the same kind of thing and are likewise
rendered rather than committed: `ITs` on the Function list, and `Passed`/`Failed`/`Skipped` on
Integration tests. Both describe the machine you are on, not the tree — committing them would churn
hundreds of rows and make `api-check` fail for anyone who has run the suite.

### What is counted

Every `public` or `protected` member of every type under `com.datastax.oss.driver.api.**` in
**core, query-builder and mapper-runtime** — the same modules revapi guards. Excluded: `test-infra`
(test scaffolding, not driver API), `examples`, `mapper-processor` and `metrics/*` (no `api`
package), `package-info.java`, anonymous inner classes, non-public nested types, and
synthetic/bridge methods. DSE is gone from this driver, so no DSE row exists.

### Reading the columns

- **Class / Method-Property name** — members are listed under the type that **declares** them, the
  convention javadoc uses. `execute(String)` is on `SyncCqlSession`, not `CqlSession`;
  `whereColumn(...)` is on `OngoingWhereClause`, not `Select`. Search by member, not by the type you
  call it on.
- **Kind** — `method`, `constructor`, `field`, `enum-constant`, or `type` for a marker interface or
  element-less annotation that declares nothing but is still API.
- **Priority** — `T1` by default; `T2` for `api.core.cql.reactive`, `api.core.metrics` and
  `api.core.specex`. Edit `PRIORITY_BY_PACKAGE` in `generate.py` to move a package. A third tier,
  `Never`, drops a package out of `Counted to total sum`; nothing uses it today, so every row counts.
- **Status** — `host-side` means the declaring package survives the transport cut untouched (the
  codec/type matrix, data, config, detach, uuid, time, and all of query-builder and mapper-runtime),
  so it works today and is not part of the bridging backlog. `needs-bridge` means it is reachable
  only through the session/request/metadata path.
- **Stub anchor** — the internal choke point that currently throws `NOT YET IMPLEMENTED (java-rs)`
  for this type, where the mapping is unambiguous. `SyncCqlSession` and `AsyncCqlSession` are split
  per member: `prepare*` is gated by `CqlPrepareAsyncProcessor`, everything else by
  `CqlRequestAsyncProcessor`.
- **ITs** — how many integration tests import this type. Use it to order the backlog by real usage.

**Why `Status` is not a per-member stub scan:** `NOT YET IMPLEMENTED (java-rs)` lives in a handful of
`internal` files and in no `api/` source file at all. It marks choke points — `DefaultSession`,
`MetadataManager`, `CqlRequestAsyncProcessor` — not individual members, so grepping a member's own
body for it would mark every row identically and say nothing. The transport-dependency split is the
honest mechanical signal; per-member progress is what the hand-maintained columns record.

## Known limits

- `@Test (declared)` counts declarations, not executions: some IT classes use `@DataProviderRunner`,
  and a class showing 0 inherits its tests from a `*ITBase`. `Passed`/`Failed`/`Skipped` are filled
  in the workbook only when `integration-tests/target/failsafe-reports` exists locally.
- Row order follows javap, i.e. declaration order — except an enum's `values()`/`valueOf(String)`,
  which javac does not place consistently, so they are pinned to the end of their type. Without that
  a plain rebuild reorders rows and `api-baseline` produces a diff that changed nothing.
- `Scylla-only?` is file-level: a Scylla-added member inside a file DataStax also ships is not
  flagged.
- Config defaults come from a light flattening of `reference.conf`; options with no literal default
  there (commented out or set programmatically) show blank.
- `Disposition` is seeded only for the families already decided — Netty knobs become warn-no-ops,
  pool sizing is reinterpreted per shard, TLS moves to the Rust stack, metrics are T2. The rest are
  blank on purpose.
- The Rust↔Java correspondence in `Error mapping` is not inferred; the `Java exception` cell on a
  Rust row is yours to fill.

## Next

`make api-check` is not yet wired into CI — a follow-up PR adds it as a job alongside `Full verify`,
together with a ratchet asserting the quarantined-IT count never increases. Until then it is a local
command, and `tools/api-tracker/*.csv` and `*.md` are in the workflow's `paths-ignore` so annotation
commits do not burn the integration-test matrices. `generate.py` deliberately is *not* ignored: it is
code, and until `api-check` runs in CI nothing else would build it.
