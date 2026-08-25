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

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import com.datastax.oss.driver.api.core.config.DefaultDriverOption;
import com.datastax.oss.driver.api.core.config.DriverExecutionProfile;
import com.datastax.oss.driver.api.core.specex.SpeculativeExecutionPolicy;
import com.datastax.oss.driver.internal.core.config.typesafe.TypesafeDriverConfig;
import com.datastax.oss.driver.internal.core.context.InternalDriverContext;
import com.datastax.oss.driver.internal.core.specex.ConstantSpeculativeExecutionPolicy;
import com.datastax.oss.driver.internal.core.specex.NoSpeculativeExecutionPolicy;
import com.typesafe.config.ConfigFactory;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;
import org.junit.Test;

public class ReflectionTest {

  @Test
  public void should_build_policies_per_profile() {
    String configSource =
        "advanced.speculative-execution-policy {\n"
            + "  class = ConstantSpeculativeExecutionPolicy\n"
            + "  max-executions = 3\n"
            + "  delay = 100 milliseconds\n"
            + "}\n"
            + "profiles {\n"
            // Inherits from default profile
            + "  profile1 {}\n"
            // Inherits but changes one option
            + "  profile2 { \n"
            + "    advanced.speculative-execution-policy.max-executions = 2"
            + "  }\n"
            // Same as previous profile, should share the same policy instance
            + "  profile3 { \n"
            + "    advanced.speculative-execution-policy.max-executions = 2"
            + "  }\n"
            // Completely overrides default profile
            + "  profile4 { \n"
            + "    advanced.speculative-execution-policy.class = NoSpeculativeExecutionPolicy\n"
            + "  }\n"
            + "}\n";
    InternalDriverContext context = mock(InternalDriverContext.class);
    TypesafeDriverConfig config = new TypesafeDriverConfig(ConfigFactory.parseString(configSource));
    when(context.getConfig()).thenReturn(config);

    Map<String, SpeculativeExecutionPolicy> policies =
        Reflection.buildFromConfigProfiles(
            context,
            DefaultDriverOption.SPECULATIVE_EXECUTION_POLICY_CLASS,
            DefaultDriverOption.SPECULATIVE_EXECUTION_POLICY,
            SpeculativeExecutionPolicy.class,
            "com.datastax.oss.driver.internal.core.specex");

    assertThat(policies).hasSize(5);
    SpeculativeExecutionPolicy defaultPolicy = policies.get(DriverExecutionProfile.DEFAULT_NAME);
    SpeculativeExecutionPolicy policy1 = policies.get("profile1");
    SpeculativeExecutionPolicy policy2 = policies.get("profile2");
    SpeculativeExecutionPolicy policy3 = policies.get("profile3");
    SpeculativeExecutionPolicy policy4 = policies.get("profile4");
    assertThat(defaultPolicy)
        .isInstanceOf(ConstantSpeculativeExecutionPolicy.class)
        .isSameAs(policy1);
    assertThat(policy2).isInstanceOf(ConstantSpeculativeExecutionPolicy.class).isSameAs(policy3);
    assertThat(policy4).isInstanceOf(NoSpeculativeExecutionPolicy.class);
  }

  /**
   * The custom-{@code ClassLoader} path used to be covered only by the OSGi tests (through {@code
   * SessionBuilder.withClassLoader}); they are gone with OSGi support, so the fallback is pinned
   * here instead. A loader that resolves only part of the application's classes must still work,
   * because the driver retries with its own loader.
   */
  @Test
  public void should_fall_back_to_driver_class_loader_when_user_loader_cannot_load_class() {
    Class<?> loaded =
        Reflection.loadClass(blindLoader(), ConstantSpeculativeExecutionPolicy.class.getName());

    assertThat(loaded).isEqualTo(ConstantSpeculativeExecutionPolicy.class);
  }

  @Test
  public void should_use_user_class_loader_when_it_can_load_class() {
    AtomicBoolean used = new AtomicBoolean();
    ClassLoader recordingLoader =
        new ClassLoader(ReflectionTest.class.getClassLoader()) {
          @Override
          public Class<?> loadClass(String name) throws ClassNotFoundException {
            used.set(true);
            return super.loadClass(name);
          }
        };

    Class<?> loaded =
        Reflection.loadClass(recordingLoader, ConstantSpeculativeExecutionPolicy.class.getName());

    assertThat(loaded).isEqualTo(ConstantSpeculativeExecutionPolicy.class);
    assertThat(used).isTrue();
  }

  @Test
  public void should_return_null_when_no_class_loader_can_load_class() {
    // Returns at the classLoader == null branch, without the fallback recursion.
    assertThat(Reflection.loadClass(null, "com.datastax.oss.driver.NoSuchClass")).isNull();
  }

  @Test
  public void should_return_null_when_neither_user_nor_driver_class_loader_can_load_class() {
    // Both levels are exercised here: the user loader throws, the driver loader is tried and also
    // fails. This is what a misspelled policy class name looks like after withClassLoader.
    assertThat(Reflection.loadClass(blindLoader(), "com.datastax.oss.driver.NoSuchClass")).isNull();
  }

  /**
   * Every real consumer goes through the default-packages overload ({@code DefaultDriverContext},
   * {@code Reflection.buildFromConfig}), which is also what the deleted OSGi tests drove, so the
   * fallback is pinned on that path too.
   */
  @Test
  public void should_resolve_unqualified_name_from_default_packages_through_fallback() {
    Class<?> loaded =
        Reflection.loadClass(
            blindLoader(),
            "ConstantSpeculativeExecutionPolicy",
            "com.datastax.oss.driver.internal.core.metadata",
            "com.datastax.oss.driver.internal.core.specex");

    assertThat(loaded).isEqualTo(ConstantSpeculativeExecutionPolicy.class);
  }

  @Test
  public void should_return_null_when_unqualified_name_is_in_no_default_package() {
    assertThat(
            Reflection.loadClass(
                null, "NoSuchPolicy", "com.datastax.oss.driver.internal.core.specex"))
        .isNull();
  }

  /** A class loader that resolves nothing, to force the fallback to the driver's own loader. */
  private static ClassLoader blindLoader() {
    return new ClassLoader(null) {
      @Override
      public Class<?> loadClass(String name) throws ClassNotFoundException {
        throw new ClassNotFoundException(name);
      }
    };
  }

  @Test
  public void should_fall_back_to_driver_class_loader_when_user_loader_throws_linkage_error() {
    // The other arm of catch (LinkageError | Exception e): a loader that fails to link rather
    // than failing to find.
    ClassLoader brokenLoader =
        new ClassLoader(null) {
          @Override
          public Class<?> loadClass(String name) {
            throw new LinkageError(name);
          }
        };

    Class<?> loaded =
        Reflection.loadClass(brokenLoader, ConstantSpeculativeExecutionPolicy.class.getName());

    assertThat(loaded).isEqualTo(ConstantSpeculativeExecutionPolicy.class);
  }
}
