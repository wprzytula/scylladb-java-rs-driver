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
package com.datastax.oss.driver.internal.core.util;

/**
 * Marks functionality that the Rust core does not bridge yet.
 *
 * <p>The driver's transport layer was removed in favor of the Rust driver, but the surrounding
 * classes are kept so that the public API stays intact. Every code path that used to reach the old
 * transport fails with {@link #error(String)} until it is reimplemented on top of the Rust core.
 * This is deliberately distinct from functionality that is <em>not going to be</em> supported,
 * which fails fast with a dedicated message of its own.
 */
public class NotYetImplemented {

  public static final String MESSAGE = "NOT YET IMPLEMENTED (java-rs)";

  /**
   * Returns the exception to throw from a not-yet-bridged code path, mentioning what is missing.
   *
   * @param what a short description of the missing functionality, e.g. {@code "session
   *     initialization"}.
   */
  public static UnsupportedOperationException error(String what) {
    return new UnsupportedOperationException(MESSAGE + ": " + what);
  }

  private NotYetImplemented() {}
}
