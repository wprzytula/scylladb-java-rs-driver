# java-rs-driver: complexity assessment & preliminary design
## (Java driver 4.x API on a scylla-rust-driver core)

## Context

ScyllaDB is converging its drivers on a single Rust core. Four bindings already exist —
`cpp-rs-driver` (2021), `nodejs-rs-driver` (2024, npm 0.6.1), `python-rs-driver` (2025, pre-alpha),
`csharp-rs-driver` (2025, unreleased) — **no Java binding exists; this would be the first, and no
public RFC/design doc exists to align with.** The constraint: preserve the public API of
scylladb/java-driver 4.x (scylla-4.x branch, this repo) so existing apps migrate with minimal change.

**Deliverable of this task: this assessment/design document** (no code changes).

**Bottom line: this is a large, multi-quarter program (~18–30 engineer-months to a usable beta),
comparable to but larger than nodejs-rs-driver (14 months to first 0.x, still incomplete) because
Java's public API surface is the largest of all bindings. It is feasible and well-precedented —
csharp-rs-driver is the direct architectural template — but success depends on 3 early decisions
(marshalling design, policy-SPI story, scope cuts) and a de-risking spike before commitment.**

---

## 1. What we're porting: java-driver 4.x surface (measured in this repo)

- ~1,270 production .java files; **core = 831** (269 public API — 202 OSS + 67 DSE — vs 562 internal).
- Satellites: query-builder 220 files (113 touch internals), mapper 114 (annotation processor —
  targets public API, mostly portable), metrics backends 10 (100% internal-SPI coupled), test-infra 33,
  **integration-tests: 226 test files** (the compat-progress metric).
- Coupling hazards:
  - `api/core/session/SessionBuilder` (1,057 lines, public) hard-wires `internal/.../DefaultDriverContext`
    (1,262-line service locator); `buildContext()` override is a documented extension path.
  - `reference.conf` (2,591 lines) — every option is a behavioral contract (incl. Netty knobs that
    become no-ops).
  - ~10 policy SPIs reflectively loaded by FQCN with `(DriverContext, String)` ctor: LoadBalancingPolicy,
    RetryPolicy, SpeculativeExecutionPolicy, ReconnectionPolicy, AddressTranslator, TimestampGenerator,
    AuthProvider, SslEngineFactory (JSSE `SSLEngine`-based — unimplementable on a Rust TLS stack),
    RequestThrottler, NodeDistanceEvaluator; plus programmatic TypeCodec, SchemaChangeListener,
    NodeStateListener, RequestTracker, custom DriverConfigLoader.
- Netty: 70/831 core files, all internal, **zero Netty types in public API signatures** — clean cut is possible.
- **Codecs decode from `ByteBuffer`** (`TypeCodec.decode(ByteBuffer, ProtocolVersion)`,
  `Row.getBytesUnsafe(i)`) → the entire Java type-system/codec layer survives unchanged if Rust hands
  over raw cell bytes. This is the single biggest compat asset.
- Scylla fork extensions (shard-awareness, tablets, LWT routing, ScyllaCloud, CDC partitioner):
  ~25 files in the hottest subsystems — **all already implemented natively in scylla-rust-driver → free**.
- Baseline: **JDK 11** (`<release>11</release>`), GraalVM native-image support in-tree, OSGi bundles,
  no JPMS module-info.

## 2. What the siblings teach us

| | nodejs-rs | python-rs | **csharp-rs (the template)** |
|---|---|---|---|
| Model | fat JS shim, DataStax-API compat | greenfield API, fat Rust | **fork managed driver, gut transport, keep API+tests** |
| Binding | napi-rs 3 | PyO3 + maturin | **hand-written C ABI + P/Invoke** |
| Host:Rust ratio | 25:1 | ~0 host code | **26:1** |
| Policies | Rust built-ins as config descriptors | same | same, but interfaces survive as **silent no-ops (trap!)** |
| Published | 0.6.1, Linux x64/arm64 only | never | never (packaging unsolved) |

- scylla-rust-driver carries **per-binding unstable feature flags** (`unstable-csharp-rs` etc.) so each
  binding upstreams internal hooks → plan an **`unstable-java-rs`** flag + upstream PRs from day one.
- nodejs perf lesson (their `docs/source/internal/napi.md`): first version **4–6× slower** than pure-JS
  because Rust constructed host-language objects across the boundary; fixed by returning raw
  buffers and materializing objects host-side. Post-fix ≥ parity, shard-awareness gave +46% insert throughput.
- csharp patterns to copy directly (details in their `rust/src/ffi.rs`, `task.rs`, `row_set.rs`):
  1. **TCB async pattern**: `#[repr(C)]` struct {handle to managed future, complete-fn-ptr, fail-fn-ptr}
     → Rust spawns on a global `LazyLock<Runtime>`, settles the future from the Tokio task, with
     `catch_unwind` + `panic="abort"`. Maps 1:1 to `CompletableFuture` + JNI global ref + cached jmethodIDs.
  2. **Zero-copy row marshalling with codecs kept host-side**: Rust walks the frame, passes borrowed
     `(ptr,len)` cell slices to a managed decode callback. Preserves the TypeCodec SPI.
  3. **Sync fast-path**: single `noop_waker` poll of the pager; only falls back to async machinery on
     page boundary → avoids per-row future allocation.
  4. **BrokenTests quarantine**: keep the whole DataStax test suite, move failures to a parallel
     module, migrate back incrementally — the compat-progress metric.
  5. Self-describing handles (pointer + destructor fn-ptr); exception constructors passed into Rust
     so Rust raises the exact DataStax exception hierarchy (their `error_conversion.rs` is 32 KB — budget for it).
- csharp pitfalls to avoid: hand-mirrored `#[repr(C)]` structs with "keep in sync" comments (top
  defect source — generate or layout-assert instead); policy interfaces compiling but never invoked;
  packaging deferred (→ zero releases in 10 months); 24-byte architecture-doc stub.

## 3. Binding technology: JNI and its alternatives

### Full survey of Java↔native mechanisms (mid-2026)

| Mechanism | Min JDK | Hot-path perf | Async/upcalls | Verdict for this driver |
|---|---|---|---|---|
| **JNI (via jni-rs crate)** | any (baseline 11 OK) | excellent if boundary designed right | manual (attach Tokio threads, cached jmethodIDs) | **viable now** — what valkey-glide (Java 11, Rust core), RocksDB, DataFusion Comet, Netty all ship |
| **FFM API / Project Panama (`java.lang.foreign`, JEP 454)** — *the* modern JNI replacement | preview 19–21, **final in JDK 22; first LTS = 25** | downcalls ~12% faster than JNI (parity+ since JDK 24); **upcalls 3.5–4× faster** (key for async completions); struct access slightly slower unless flat buffers used | first-class: MethodHandle downcalls, upcall stubs, Arena/MemorySegment memory mgmt, `jextract` from C headers | **the strategic target** — pure-Java binding code (no hand-written native registration), safer memory model; blocked only by the JDK floor |
| JNA | 8 | ~13× JNI overhead | weak | reject (init-time calls only) |
| JNR-FFI (already used by this driver for `getpid` in `internal/core/os`) | 8 | ~84% of JNI at best | weak | fine where it is; not for the data path |
| uniffi-bindgen-java (UniFFI → Panama bindings, async → CompletableFuture) | 22 | unproven; not zero-copy-capable | generated | watch only — unstable, inherits FFM's JDK floor, can't express DirectByteBuffer zero-copy |
| Diplomat Java backend / flapigen / robusta_jni / duchess | — | — | — | WIP / low-activity / wrong direction (duchess = Rust-calls-Java) |
| Rust→Wasm on a JVM Wasm runtime (Chicory/GraalWasm) | 11 | poor for I/O-heavy code | n/a | rejected — no mature socket/threading story for a Tokio network driver |

### Facts that shape the choice
- **JDK reality**: FFM requires JDK 22+; the first LTS carrying it is **25** (Sep 2025). 2026 adoption
  (Azul survey): 8≈23%, 17≈34%, 21≈31%, 25≈10% → an FFM-only driver is unavailable to ~90% of users today.
  The repo baseline is JDK 11.
- **JEP 472**: from JDK 24, *JNI too* is a restricted operation heading toward requiring
  `--enable-native-access` — the flag is not a point against FFM. Document it from day one either way.
- **The decisive variable is boundary crossings per request, not JNI vs FFM.** At 100k req/s,
  2 crossings/req ≈ 0.01 cores (free); per-cell JNI marshalling ≈ 2.5+ cores (fatal). Design the
  buffer protocol first; then the JNI-vs-FFM delta is noise on downcalls (upcalls still favor FFM).

### Strategy options
- **(i) JNI-first, FFM later — RECOMMENDED.** Rust core exposes a **plain C ABI** (like cpp/csharp
  siblings) + a thin JNI shim now; a thin FFM shim over the *same C ABI* ships later as a separate
  `-ffm` artifact (cleaner than a multi-release jar), auto-selected on JDK 22+/25+. Flip the default
  once the ecosystem baseline reaches JDK 25; retire JNI eventually. Keeps JDK 11 users covered.
- **(ii) FFM-first.** Simpler and safer binding layer (no hand-written JNI glue, no
  AttachCurrentThread management, Arena-scoped memory), best upcall path — but forces a JDK 25 floor
  (or 22 non-LTS). Only viable if stakeholders accept that java-rs-driver targets "current Java"
  and the existing pure-Java 4.x driver remains the offering for older JDKs. This is a legitimate
  simplification worth pricing: it removes an entire ABI layer and its maintenance.
- **(iii) Dual-ABI from day one.** Both shims over the C ABI from the start. Most coverage, most
  up-front cost; only worth it if Phase 0 shows FFM upcall wins matter at target load.

### Row-marshalling sub-decision (settle by measurement in the Phase 0 spike)
- (a) **flat-buffer**: Rust copies frame body + offset index into a pooled DirectByteBuffer; Java Row =
  view over slices; 1–2 crossings/page (best theoretical JNI economics), vs
- (b) **per-cell callback** (csharp model): borrowed slices, no copy, but one upcall per cell — JNI
  upcalls are pricier than P/Invoke reverse calls, so (a) is the likely winner for Java (under FFM,
  (b) becomes ~3.5–4× cheaper and could win); verify empirically.

## 4. Options

### Option A — Fork-and-gut this repo, JNI bridge («csharp model») — RECOMMENDED
Fork scylla-4.x → `java-rs-driver`. Keep `api/` trees, codecs, data/metadata model classes, exceptions,
query-builder, mapper, test suites. Delete `internal/core/{channel,protocol,pool,control}` + Netty +
native-protocol dep. Add `rust/` cdylib (deps: `scylla` with `unstable-java-rs`, tokio) + a
`RustBridge`-equivalent Java package. Re-point `DefaultDriverContext` at bridged components.
- ✔ Maximum API/test reuse; drop-in story; single repo like csharp; fastest path to running ITs.
- ✘ Carries the whole legacy surface incl. parts that can't work (see scope cuts); DefaultDriverContext
  surgery is invasive.

### Option B — Greenfield thin wrapper («python model»)
New rust-driver-shaped Java API; no compat.
- ✘ Fails the stated constraint (4.x API compat). Only relevant as a long-term "modern API" companion. Rejected.

### Option C — Two artifacts: thin core binding + compat facade
`scylla-java-rs-core` (thin, rust-shaped, JNI) + `java-driver-4x-compat` implementing the 4.x API on top.
- ✔ Clean layering; new API for new users; compat layer is "just a client" of the core.
- ✘ Two API surfaces to design/maintain from day one; compat fidelity harder at arm's length (exception
  hierarchy, execution profiles, config semantics need core hooks anyway); no sibling precedent (nodejs's
  shim is internal, not a public second API). Defensible, but higher total cost; revisit after A ships.

## 5. Scope tiers (proposed cuts — need product sign-off)

| Tier | Content |
|---|---|
| **T1 (v1.0)** | `CqlSession` sync+async, statements (simple/bound/batch), prepared statements, paging, full type system/codecs (incl. UDT/tuple/vector), metadata (nodes/schema/token/tablet maps), execution profiles, built-in policies as config, auth (plain + SNI/ScyllaCloud), TLS, query-builder, mapper, request tracker + node/schema listeners (event upcalls), reference.conf-compatible config loader |
| **T2** | reactive module (thin adapter over async), metrics (Rust-core counters exported → Java `Metrics`/micrometer), speculative execution, custom-policy story (see §6), OSGi |
| **Drop/never** | DSE graph (TinkerPop), DSE continuous paging & geometry, Netty tuning options (accepted-but-warn), JSSE `SslEngineFactory` (replace: rustls/openssl config; keystore-path options honored), GraalVM substitutions (native lib changes the story — needs its own design later) |

## 6. The policy-SPI decision (biggest compat risk — decide explicitly, up front)

All three siblings dropped host-language pluggable policies (Rust built-ins configured by descriptors).
Java's LBP/RetryPolicy SPIs are far more widely implemented by users than C#'s.
Recommended stance for v1:
- Built-in policies (default/DC-aware/token-aware LBP, default retry, constant/exponential reconnection,
  throttlers) → **map config FQCNs of the known built-in classes to Rust equivalents**; keep the Java
  classes as descriptor shells.
- Unknown user-provided policy class in config → **fail fast at session build with a clear error**
  (never silent no-op — the csharp trap).
- Post-v1 (T2): evaluate genuine upcall-based SPI for RetryPolicy (low frequency — only on error) and
  possibly LBP query-plan (hot path — likely unacceptable; nodejs "casync" data suggests cost).
  AddressTranslator and TimestampGenerator are low-frequency → upcall-able early if demanded.

## 7. Phased roadmap

**Phase 0 — de-risk spike (4–6 weeks, 1–2 people; go/no-go gate)**
1. Verify scylla-rust-driver can surrender raw cell bytes / frame slices without full deserialization
   (csharp proves slices work via their feature flag; confirm what `unstable-java-rs` must expose). **Load-bearing.**
2. Throwaway JNI spike: connect + execute + paged select end-to-end; TCB async pattern; measure both
   marshalling designs (flat-buffer vs per-cell upcall) vs current pure-Java driver p50/p99/throughput.
3. Benchmark the current driver's cost structure — if Netty+codecs+GC aren't a material share of request
   cost, the project's perf ROI is compat/maintenance-convergence only; size accordingly.

**Phase 1 — skeleton (≈1 quarter)**: repo fork; `rust/` cdylib with C ABI + JNI shim (session build/
connect/execute/prepare, TCB, error-constructor table, logging bridge); Java `rustbridge` package
(handle table, Cleaner safety net, AsyncRegistry); `DefaultDriverContext` variant returning bridged
components; config translation layer (Typesafe reference.conf → Rust SessionBuilder); quarantine
non-compiling tests (BrokenTests pattern). Exit: smoke ITs green against CCM/Scylla.

**Phase 2 — data & metadata (≈1 quarter)**: result marshalling per spike winner; Row/ColumnDefinitions
as buffer views; full codec matrix; metadata/token/tablet map bridging; schema/node event upcall channel;
prepared-statement metadata; execution profiles per-request.

**Phase 3 — compat grind (1–2 quarters, parallelizable)**: migrate quarantined unit+integration tests
back; query-builder/mapper validation (should be near-free); policies/auth/TLS mapping; error-hierarchy
completeness; packaging matrix (linux x64/arm64 glibc + x64 musl + macOS arm64 must-have; classifier
jars + fat jar, os-maven-plugin, extract-and-load with noexec-/tmp escape hatch); ASAN + layout-assert CI;
benchmarks vs baseline driver in driver-matrix.
**Phase 4 — beta & docs**: migration guide (dropped features table), architecture doc (differentiator —
siblings have 25-byte stubs), driver-matrix integration, 0.x releases.

## 8. Effort & risk summary

Effort (rough, excluding Phase 0): Rust FFI core 4–6 em; Java bridge + context/config rewiring 4–6 em;
results/codecs/metadata 3–4 em; test migration 4–8 em; packaging/CI 1–2 em; docs/bench 1–2 em →
**≈18–30 engineer-months to beta; calendar ~12–18 months at 2 FTE** (consistent with nodejs's 14 months
to 0.1.0 on a smaller surface).

Top risks:
1. Raw-bytes access not exposed by rust driver → upstream `unstable-java-rs` work; scope unknown until Phase 0.
2. Marshalling design wrong → unrecoverable perf; mitigated by Phase 0 measurement.
3. Silent behavior drift (reference.conf semantics, retry timing, LBP plan order) → mitigate via IT suite
   migration + driver-matrix; document every intentional divergence.
4. Native-lib support burden: segfault kills customer JVM → handle tables, catch_unwind everywhere,
   ASAN CI; **pure-Java fallback** (keep old driver artifact selectable) is the safety valve.
5. Ecosystem breakage outside the API: OSGi, GraalVM native-image, app-server classloaders, JPMS —
   each needs an explicit supported/unsupported statement.
6. Maintenance-economics counter-signal (Kafka/Neo4j/Influx kept pure-Java clients) is neutralized by
   ScyllaDB's shared-core strategy — but only if the core hooks are upstreamed, not forked.

## 9. Usage examples for reference (what migration actually looks like)

Legend: ✔ works unchanged · ⚠ works with behavior/config differences · ✘ unsupported in v1 (fail-fast)

### 9.1 Simple application — zero changes

```java
try (CqlSession session = CqlSession.builder()                          // ✔ same builder API
    .addContactPoint(new InetSocketAddress("scylla.example.com", 9042)) // ✔
    .withLocalDatacenter("dc1")                                         // ✔ → Rust DefaultPolicy prefer_datacenter
    .withAuthCredentials("cassandra", "cassandra")                      // ✔ → Rust plain-text auth
    .build()) {

  // Synchronous execution
  ResultSet rs = session.execute("SELECT cluster_name, release_version FROM system.local");
  Row row = rs.one();
  System.out.println(row.getString("release_version"));                 // ✔ codec decodes a ByteBuffer
                                                                        //    slice of the Rust-owned frame
  // Prepared statements
  PreparedStatement ps = session.prepare(
      "INSERT INTO ks.users (id, name, created) VALUES (?, ?, ?)");     // ✔ prepared cache lives in Rust
  session.execute(ps.bind(userId, "Ada", Instant.now()));               // ✔ values serialized by Java
                                                                        //    codecs, passed as one buffer
  // Asynchronous execution
  CompletionStage<AsyncResultSet> stage =
      session.executeAsync(ps.bind(userId2, "Grace", Instant.now()));   // ✔ CompletableFuture completed
                                                                        //    from Tokio via TCB upcall
}
```

Under the hood everything changed (no Netty, Rust connection pool, shard-aware routing); at the
source level nothing did. This is the target experience for the majority of applications — and they
silently gain shard/tablet awareness perf (nodejs measured +46% insert throughput from shard-awareness).

### 9.2 Complex application — the compat-sensitive touchpoints

`application.conf`:
```hocon
datastax-java-driver {
  basic {
    contact-points = ["10.0.0.1:9042", "10.0.0.2:9042"]
    load-balancing-policy {
      class = DefaultLoadBalancingPolicy        # ✔ FQCN mapped to Rust DefaultPolicy (token-aware, DC-aware)
      local-datacenter = dc1
    }
  }
  advanced {
    retry-policy { class = DefaultRetryPolicy } # ✔ mapped to Rust default retry
    # retry-policy { class = com.acme.MyRetry } # ✘ v1: session build fails fast with a clear error
    ssl-engine-factory {
      class = DefaultSslEngineFactory           # ⚠ keystore/truststore options honored, but TLS is
      truststore-path = /etc/ssl/client.jks     #    terminated by rustls/openssl, not JSSE; a custom
    }                                           #    SslEngineFactory implementation is ✘
    netty.io-group.size = 8                     # ⚠ accepted but a no-op (warning logged)
    connection.pool.local.size = 2              # ⚠ re-interpreted: Rust pools per-shard, not per-node
  }
  profiles {
    analytics {                                 # ✔ execution profiles map to Rust ExecutionProfile
      basic.request.timeout = 30 seconds
      basic.request.consistency = LOCAL_ONE
    }
  }
}
```

Java:
```java
CqlSession session = CqlSession.builder()
    .withConfigLoader(DriverConfigLoader.fromClasspath("application"))  // ✔ Typesafe loader kept in Java;
                                                                        //    translated to Rust config
    .addTypeCodecs(new MoneyCodec(), ExtraTypeCodecs.BLOB_TO_ARRAY)     // ✔ custom codecs — decode/encode
                                                                        //    ByteBuffers exactly as today
    .withNodeStateListener(new LoggingNodeStateListener())              // ✔ node events upcalled from Rust
    .withSchemaChangeListener(new MySchemaListener())                   // ✔ schema events upcalled
    .withRequestTracker(new LatencyTracker())                           // ✔ per-request completion upcall
    .build();

// Metadata, token map, UDTs — bridged read-only views over Rust cluster state
UserDefinedType addressType = session.getMetadata()
    .getKeyspace("ks").flatMap(ks -> ks.getUserDefinedType("address"))
    .orElseThrow();                                                     // ✔
UdtValue addr = addressType.newValue().setString("city", "Warsaw");     // ✔ pure-Java data types
TokenMap tokenMap = session.getMetadata().getTokenMap().orElseThrow();  // ✔ incl. tablet-aware fork APIs

// Query builder emits SimpleStatements → near-free compat
Statement<?> stmt = QueryBuilder.insertInto("ks", "users")
    .value("id", bindMarker()).value("addr", bindMarker())
    .build()
    .setExecutionProfileName("analytics")                               // ✔ per-request profile switch
    .setConsistencyLevel(ConsistencyLevel.LOCAL_QUORUM)                 // ✔
    .setIdempotent(true);                                               // ✔ gates Rust retry/specex

// Async paging across pages
session.executeAsync(stmt).thenCompose(rs -> {
  for (Row r : rs.currentPage()) process(r);                            // ✔ rows are views over the
  return rs.hasMorePages()                                              //    Rust-owned page buffer
      ? rs.fetchNextPage() : CompletableFuture.completedFuture(rs);     // ✔ next page fetched by Rust pager
});

// Object mapper — generated code targets only the public API
InventoryMapper mapper = new InventoryMapperBuilder(session).build();   // ✔
ProductDao dao = mapper.productDao(CqlIdentifier.fromCql("ks"));        // ✔

// --- The parts that change or break ---
session.getMetrics();                            // ⚠ empty/limited until T2 (Rust metrics → Java bridge)
session.executeReactive("SELECT ...");           // ⚠ T2 (thin adapter over executeAsync)
new MyCustomLbp();                               // ✘ custom LoadBalancingPolicy never invoked → v1
                                                 //    stance: fail fast at build, never silent no-op
class MyBuilder extends CqlSessionBuilder {      // ✘ overriding buildContext() to reach internals
  @Override protected DriverContext buildContext(...) { ... }           //    (Netty tuning etc.) breaks
}
GraphStatement.newInstance(...);                 // ✘ DSE graph dropped (scope tier "never")
```

The annotations above are the concrete rendering of §5 (scope tiers) and §6 (policy stance): the goal
is that 9.1-style apps recompile-and-run, 9.2-style apps need a short migration checklist, and nothing
fails silently.

## 10. Decision points to confirm with stakeholders

1. **Scope cuts** (§5): drop DSE graph/continuous-paging/geometry? mapper+query-builder in T1?
2. **Policy SPI stance** (§6): fail-fast on custom policies acceptable for v1?
3. **JDK floor & binding strategy** (§3): stay at 11 with JNI-first + FFM later (i), go FFM-first with a
   JDK 25 floor (ii — simplest binding layer, smallest audience), or dual-ABI from day one (iii)?
4. Compat bar: source-compat (recompile) vs strict binary compat (drop-in jar swap)? (Affects how much
   of SessionBuilder/DriverContext plumbing must be preserved vs replaced.)
5. Repo strategy: new `scylladb/java-rs-driver` repo forked from this one (sibling naming convention).
6. GraalVM/OSGi: supported in v1, later, or never?
