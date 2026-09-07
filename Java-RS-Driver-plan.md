# java-rs-driver: execution plan

Task-focused successor to [`Java-RS-Driver-design.md`](Java-RS-Driver-design.md) (the background
assessment). That document surveyed options; this one records the decisions taken and the concrete
work plan, starting with preparing this repo for the PoC. Where a rationale is omitted here, it is
in the design doc.

---

## 1. Decisions locked in

Stakeholder answers to design-doc §10, plus carried-over design choices:

| # | Decision |
|---|---|
| D1 | **DSE support is dropped entirely.** `com.datastax.dse.**` is *deleted*, not stubbed — this is the **only** exception to API preservation. |
| D2 | **Custom user-provided policies are out of scope for v1.** Built-in policy FQCNs in config map to Rust equivalents; an unknown policy class fails fast at session build with a clear error (never a silent no-op). |
| D3 | **FFM-first.** JDK floor is **25** for early development (v1). Later, a JNI shim over the same C ABI lowers the floor to JDK 11 (dual-ABI period); JNI is deleted once FFM is ecosystem-spread. |
| D4 | **Drop-in jar swap is desired but not committed** — feasibility assessed in §6. |
| D5 | **Repo strategy: operate as a fork of this repo** (branch `rust-poc`) for now. |
| D6 | **GraalVM native-image and OSGi: unsupported in v1** (explainer in §5). |
| D7 | **Fork-and-gut (design-doc Option A)**, csharp-rs-driver as the architectural template. |
| D8 | Rust core exposes a **plain C ABI** so FFM and JNI shims are peers over the same surface. |
| D9 | Internal Rust-driver hooks go behind an **`unstable-java-rs`** feature flag, upstreamed to scylladb/scylla-rust-driver from day one. |
| D10 | **Codecs and the whole Java type system stay host-side**, decoding `ByteBuffer` slices of Rust-owned frame data (the single biggest compat asset). |

### The API-preservation rule (governs everything below)

> **All public API surface is preserved — every class and signature under
> `com.datastax.oss.driver.api.**` and the Scylla fork's public additions stays, byte-for-byte at the
> source level. The only exception is DSE (`com.datastax.dse.**`), which is deleted outright.**

Consequences:
- "Cutting internals" means deleting/stubbing `com.datastax.oss.driver.internal.**` only, and only
  where nothing public depends on the *type* existing.
- Public interfaces whose v1 *behavior* is unsupported (e.g. `SslEngineFactory`, custom
  `LoadBalancingPolicy`) keep their API classes; unsupported behavior surfaces as a **fail-fast
  error at session build**, or `UnsupportedOperationException("NOT YET IMPLEMENTED (java-rs)")`
  for not-yet-bridged functionality. Two distinct messages: *"not yet"* vs *"not in v1"*.
- Internal classes referenced from public signatures or documented extension points (e.g.
  `DefaultDriverContext` via `SessionBuilder.buildContext()`) survive at least as stubs.

## 2. Target architecture (FFM-first deltas vs the design doc)

The design doc's recommended shape stands (C ABI cdylib + thin shim + host-side codecs + TCB async
pattern + handle tables). Going FFM-first changes:

- **Binding layer is pure Java**: `java.lang.foreign` — `Linker` downcalls, upcall stubs for TCB
  future completion, `Arena`/`MemorySegment` for memory. No hand-written native glue on the Java
  side; bindings generated with `jextract` from the C ABI header (kills the csharp "keep-in-sync
  mirrored structs" defect class).
- **`--enable-native-access`** is required and documented from day one (JEP 472 makes JNI need it
  too, so this costs nothing long-term).
- **Swappable shim boundary**: driver internals call a small Java-side bridge interface package
  (working name `com.datastax.oss.driver.internal.core.nativebridge`), with the FFM implementation
  first and a JNI implementation later. Nothing outside that package may touch
  `java.lang.foreign` directly.
- **Row marshalling** (flat-buffer copy vs per-cell upcall over borrowed slices) remains an open
  question to settle by measurement in the PoC — but FFM upcalls are 3.5–4× cheaper than JNI
  upcalls, so the csharp per-cell model is a genuine contender, not just the fallback.
- Build: `core` compiles with `<release>25</release>` on the `rust-poc` branch. The JDK 11 floor
  returns only with the JNI shim phase.

## 3. Repo surgery — module level

| Module | Action |
|---|---|
| `core` | Gut internals per §4; delete `com.datastax.dse.**` from main, test, and resources |
| `query-builder` | Keep (emits statements against public API); delete DSE-specific builders (graph, geo) |
| `mapper-runtime`, `mapper-processor` | Keep (target public API only); delete DSE-specific processing |
| `metrics/micrometer`, `metrics/microprofile` | Remove from build (100% internal-SPI coupled; returns in T2 when Rust-core metrics are bridged) |
| `guava-shaded` | Keep (used by public API surface) |
| `core-shaded` | Drop (its purpose is shading Netty; Netty is gone) |
| `osgi-tests` | Delete (D6) |
| `test-infra` | Keep (CCM/simulacron harness used by kept ITs); prune DSE parts |
| `integration-tests` | Keep, triage per §7 (161 OSS test files kept/quarantined, 66 DSE deleted) |
| `distribution`, `distribution-source`, `distribution-tests`, `bom`, `examples` | Keep building; prune DSE and dropped modules from poms/examples |
| **new** `rust/` | Placeholder layout for the cdylib crate (PoC work happens here) |

Legacy 3.x-era directories present at repo root (`driver-core`, `driver-dist`, `driver-examples`,
`driver-extras`, `driver-mapping`, `driver-tests`) are not in the 4.x reactor — left untouched.

## 4. Repo surgery — `core` internals cut line

Per-package plan for `com.datastax.oss.driver.internal.core.*` (packages verified in repo):

### Delete (pure transport/protocol; no public type depends on them)
- `channel`, `protocol`, `pool`, `control`, `adminrequest` — Netty pipeline, frame codecs,
  connection pooling, control connection, admin queries. All replaced by the Rust core.
- Dependencies removed from `core/pom.xml`: the LZ4 and Snappy compressors. **Two corrections made
  while executing this step:**
  - `java-driver-native-protocol` **stays**. 18 files under `api/**` use it (`ProtocolConstants`,
    `RawType`, `ProtocolVersion` codes, protocol-level enums behind `ConsistencyLevel`, `WriteType`,
    the data types), so removing it would break public API — which the API-preservation rule
    forbids. It is a pure data/constants library with no I/O, so keeping it costs nothing.
  - **Netty stays for now.** Deleting the pipeline removed most of it, but ~20 files still use it as
    the *scheduling* layer (`NettyOptions.adminEventExecutorGroup()`, `RunOrSchedule`, `Debouncer`,
    `Reconnection`, throttlers, metrics timers, config reload) plus `FastThreadLocal` in two codecs
    and the BlockHound integration. What replaces that admin executor is the threading question the
    `nativebridge` PoC (§8.5) settles — where the Rust core's callback threads are defined — so the
    swap belongs there rather than to a guess made now.
- GraalVM substitution classes (`protocol/CompressorSubstitutions.java`, graal-specific parts of
  `Uuids` support) go with them (D6).

### Stub (public API or `DriverContext` reaches them; bodies throw `NOT YET IMPLEMENTED (java-rs)`)
- `session` — `DefaultSession` and session lifecycle: `CqlSession.builder().build()` must compile
  and fail with the stub exception, not a linkage error.
- `cql` — request execution internals, `DefaultPreparedStatement`/result-set plumbing. Statement
  *data* classes (immutable statement impls) are kept working; only execution paths are stubbed.
- `context` — `DefaultDriverContext` survives (it is a documented extension point via
  `buildContext()`), slimmed: components that moved to Rust return stubs; the service-locator
  shape and public getters keep their signatures.
- `metadata` — kept as stubs/empty views; later becomes bridged read-only views over Rust cluster
  state (token map, tablets, schema).

### Keep working (host-side by design, D10)
- `type` (full codec matrix), `data` (UDT/tuple values), `util`, `time`, `os` (JNR `getpid`;
  revisit later — FFM can replace JNR).
- `config` — Typesafe-config loader and `reference.conf` stay; the config→Rust translation layer
  is PoC/Phase-1 work. Netty-specific options become accepted-but-warn no-ops.
- Policy packages (`loadbalancing`, `retry`, `specex`, `reconnection` parts of `connection`,
  `addresstranslation`, `time`) — **classes kept** (API rule), reduced to descriptor shells: their
  FQCNs select Rust built-ins; their Java logic is never invoked (D2). Note: the reconnection
  policy API classes survive even though the `connection` package's transport scheduling is deleted.
- `auth`, `ssl` — public interfaces and built-in impl classes kept; JSSE-based
  `SslEngineFactory` *behavior* replaced by Rust-side TLS (keystore-path config options honored);
  a custom `SslEngineFactory` implementation fails fast.
- `tracker`, listener plumbing — interfaces kept; event upcalls arrive with bridging.
- `servererrors`, `clientroutes`, `metrics` (internal) — keep types referenced publicly, stub the rest.

**File-level principle** during execution: anything reachable from `api/**` signatures,
`reference.conf` semantics, or documented extension points survives at least as a stub; anything
reachable only from deleted transport code is deleted. When in doubt → stub, don't delete.

## 5. GraalVM & OSGi — what they are, and the v1 stance (D6)

- **GraalVM native-image** ahead-of-time compiles a Java application into a single native binary
  (fast startup, low memory — popular with Quarkus/Micronaut). It requires build-time metadata for
  anything dynamic (reflection, resources, JNI/FFM), and libraries often ship "substitutions" —
  alternate class bodies used only during native-image builds. This repo has such support in-tree.
  With a Rust cdylib the whole story changes (the native library must be bundled or statically
  linked into the image, FFM needs registration) and deserves its own design later.
  **v1: unsupported; substitutions deleted with the internals.**
- **OSGi** is a legacy Java module/plugin framework (Eclipse platform, Apache Karaf and older app
  servers): jars carry bundle manifests declaring imported/exported packages and can be
  hot-(un)loaded. The driver ships OSGi manifests and an `osgi-tests` module. The ecosystem is
  niche and shrinking, and native-library loading under OSGi classloaders is its own can of worms.
  **v1: unsupported; `osgi-tests` deleted, bundle-manifest machinery stripped from the build.**

Your instinct was right: neither is crucial for v1. Both get an explicit "unsupported" line in the
migration guide rather than silent breakage.

## 6. Drop-in jar swap feasibility (D4)

Verdict: **source-compatible drop-in for typical applications: yes — that is the design goal.
Strict universal binary drop-in: no.** Known hazards:

1. `SessionBuilder.buildContext()` overrides reaching Netty/internal components — breaks (documented).
2. Applications importing `internal.**` classes directly — may break; internal is not API.
3. DSE users (`com.datastax.dse.**`) — gone by decision D1.
4. `core-shaded` consumers — artifact discontinued.
5. JVM invocation changes: JDK 25 floor (v1) and `--enable-native-access=...` flag.
6. Packaging: native lib per platform (classifier jars + fat jar, extract-and-load with a
   noexec-`/tmp` escape hatch) — a *deployment* change even when code is unchanged.
7. Behavior drift: Netty tuning options become warn-no-ops; pool sizing reinterpreted per-shard;
   retry/LBP timing differs. Mitigated by IT-suite migration; every divergence documented.

Deliverable when nearing release: a migration checklist enumerating exactly these.

## 7. Test triage

Measured baseline: unit tests — 226 files under `oss/driver/internal/core`, 21 under
`oss/driver/api/core`, 50 DSE; integration tests — 161 OSS, 66 DSE files.

- **Delete**: all DSE tests (unit + IT, 116 files); unit tests of deleted packages
  (`channel`, `protocol`, `pool`, `control`, `adminrequest` — incl. protocol frame fixtures in
  `core/src/test/resources`); OSGi tests.
- **Keep green from day one**: `api`-level unit tests, codec/type/data tests, config-loader tests,
  query-builder and mapper module tests (they compile against public API only), statement-data tests.
- **Quarantine** (csharp `BrokenTests` pattern — the compat-progress metric): tests of stubbed
  subsystems (session/cql/metadata internals) and the whole `integration-tests` suite.
  *Implemented as:* a JUnit category `com.datastax.oss.driver.categories.BrokenTests` in
  `test-infra`, carried by all 125 IT classes and named in `<excludedGroups>` of the three failsafe
  executions in `integration-tests/pom.xml`. The ITs still compile (so they cannot rot) but run
  zero tests and need no CCM cluster. Un-quarantining is deleting one annotation argument per
  class, and the set of classes still carrying the category is the progress metric. The CI IT jobs
  are gated on `workflow_dispatch` for as long as the quarantine is total.
- Exit criterion for the cut: **`mvn test` green on `rust-poc`** with the exclusions in place;
  `mvn verify` ITs stay quarantined until bridging lands.

## 8. Task sequence (starts now; each step one commit on `rust-poc`)

1. **Delete DSE** — `com.datastax.dse.**` from `core` main/test/resources, `integration-tests`,
   `query-builder`/`mapper` DSE parts, `examples`, pom references. (Done; the reactive API was
   relocated to the `oss` namespace rather than deleted.)
2. **Delete OSGi + GraalVM machinery, drop `metrics/*` and `core-shaded` from the reactor.** (Done.)
3. **Cut transport**: delete packages per §4; stub survivors until `core` compiles.
   (Done. Netty and `native-protocol` deliberately kept — see the corrections in §4.)
   *Correction to D3's timing:* the toolchain stays on **JDK 17 with `<release>11</release>`** for
   now. The Error Prone bump that JDK 25 needs was reverted in review, because the CI matrix still
   builds on 11 and 17; the move to a JDK 25 floor (and `<release>25</release>`) belongs to step 5,
   together with the CI matrix change.
4. **Test triage** per §7; quarantine mechanism in place; `mvn test` green. (Done: `BrokenTests`
   category over all 125 ITs, excluded in failsafe, IT CI jobs dispatch-only.)
5. **PoC handoff point**: `nativebridge` interface skeleton + empty `rust/` crate layout.
   → Wojciech writes the PoC (connect + execute + paged select through the Rust core, TCB async
   pattern, both row-marshalling designs measured).

Post-PoC phases (skeleton → data/metadata → compat grind → beta) follow the design doc §7,
re-based on FFM.
