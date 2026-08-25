/*
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package com.datastax.oss.driver.internal.core.session;

import com.datastax.oss.driver.api.core.CqlIdentifier;
import com.datastax.oss.driver.api.core.CqlSession;
import com.datastax.oss.driver.api.core.context.DriverContext;
import com.datastax.oss.driver.api.core.metadata.EndPoint;
import com.datastax.oss.driver.api.core.metadata.Metadata;
import com.datastax.oss.driver.api.core.metrics.Metrics;
import com.datastax.oss.driver.api.core.session.Request;
import com.datastax.oss.driver.api.core.type.reflect.GenericType;
import com.datastax.oss.driver.internal.core.context.InternalDriverContext;
import com.datastax.oss.driver.internal.core.metadata.MetadataManager;
import com.datastax.oss.driver.internal.core.metrics.SessionMetricUpdater;
import com.datastax.oss.driver.internal.core.util.NotYetImplemented;
import com.datastax.oss.driver.internal.core.util.concurrent.CompletableFutures;
import edu.umd.cs.findbugs.annotations.NonNull;
import edu.umd.cs.findbugs.annotations.Nullable;
import java.nio.ByteBuffer;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.ConcurrentMap;
import net.jcip.annotations.ThreadSafe;

/**
 * The session implementation.
 *
 * <p>Its transport — connection pools, the control connection, node state tracking and request
 * execution — was removed along with the driver's own protocol layer; a session is meant to become
 * a handle onto a Rust-core session. Until that bridge exists, {@link #init} fails with {@link
 * NotYetImplemented}, so no instance is ever handed out, and the accessors below are kept only so
 * that the internal API around the session stays intact.
 */
@ThreadSafe
public class DefaultSession implements CqlSession {

  public static CompletionStage<CqlSession> init(
      InternalDriverContext context, Set<EndPoint> contactPoints, CqlIdentifier keyspace) {
    // TODO(java-rs): open a Rust-core session from the resolved configuration.
    return CompletableFutures.failedFuture(NotYetImplemented.error("session initialization"));
  }

  private final InternalDriverContext context;
  private final MetadataManager metadataManager;
  private final SessionMetricUpdater metricUpdater;

  // TODO(java-rs): the session-leak diagnostic (advanced.session-leak.threshold) was driven by
  // an instance counter incremented here and decremented on close. Both ends went with the
  // transport; the option stays in reference.conf and is inert until sessions exist again.
  //
  // Unused for as long as init() cannot produce a session; kept for the bridge to come.
  @SuppressWarnings("UnusedMethod")
  private DefaultSession(InternalDriverContext context) {
    this.context = context;
    this.metadataManager = context.getMetadataManager();
    this.metricUpdater = context.getMetricsFactory().getSessionUpdater();
  }

  @NonNull
  @Override
  public String getName() {
    return context.getSessionName();
  }

  @NonNull
  @Override
  public Metadata getMetadata() {
    return metadataManager.getMetadata();
  }

  @Override
  public boolean isSchemaMetadataEnabled() {
    return metadataManager.isSchemaEnabled();
  }

  @NonNull
  @Override
  public CompletionStage<Metadata> setSchemaMetadataEnabled(@Nullable Boolean newValue) {
    return metadataManager.setSchemaEnabled(newValue);
  }

  @NonNull
  @Override
  public CompletionStage<Metadata> refreshSchemaAsync() {
    return CompletableFutures.failedFuture(NotYetImplemented.error("schema metadata refresh"));
  }

  @NonNull
  @Override
  public CompletionStage<Boolean> checkSchemaAgreementAsync() {
    return CompletableFutures.failedFuture(NotYetImplemented.error("schema agreement check"));
  }

  @NonNull
  @Override
  public DriverContext getContext() {
    return context;
  }

  @NonNull
  @Override
  public Optional<CqlIdentifier> getKeyspace() {
    throw NotYetImplemented.error("session keyspace");
  }

  @NonNull
  @Override
  public Optional<Metrics> getMetrics() {
    return context.getMetricsFactory().getMetrics();
  }

  /**
   * <b>INTERNAL USE ONLY</b> -- switches the session to a new keyspace.
   *
   * <p>This is called by the driver when a {@code USE} query is successfully executed through the
   * session. Calling it from anywhere else is highly discouraged, as an invalid keyspace would
   * wreak havoc (close all connections and make the session unusable).
   */
  @NonNull
  public CompletionStage<Void> setKeyspace(@NonNull CqlIdentifier newKeyspace) {
    return CompletableFutures.failedFuture(NotYetImplemented.error("keyspace switch"));
  }

  @Nullable
  @Override
  public <RequestT extends Request, ResultT> ResultT execute(
      @NonNull RequestT request, @NonNull GenericType<ResultT> resultType) {
    throw NotYetImplemented.error("request execution");
  }

  /**
   * The routing information the driver used to associate prepared statements with the connections
   * they were prepared on. Kept for the internal API; re-preparation is the Rust core's business.
   */
  @NonNull
  public ConcurrentMap<ByteBuffer, RepreparePayload> getRepreparePayloads() {
    throw NotYetImplemented.error("prepared statement re-preparation");
  }

  @NonNull
  public SessionMetricUpdater getMetricUpdater() {
    return metricUpdater;
  }

  @NonNull
  @Override
  public CompletionStage<Void> closeFuture() {
    // A failed stage rather than a throw: AsyncAutoCloseable.close() calls this from a
    // try-with-resources scope exit, where a synchronous throw would either escape a block the
    // caller did not ask to fail or arrive as a suppressed exception on the real failure.
    return CompletableFutures.failedFuture(NotYetImplemented.error("session shutdown"));
  }

  @NonNull
  @Override
  public CompletionStage<Void> closeAsync() {
    return CompletableFutures.failedFuture(NotYetImplemented.error("session shutdown"));
  }

  @NonNull
  @Override
  public CompletionStage<Void> forceCloseAsync() {
    return CompletableFutures.failedFuture(NotYetImplemented.error("session shutdown"));
  }
}
