// SPDX-License-Identifier: MIT
package example.sources;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class SourcesTest {
  @Test
  void usesEverySource() throws Exception {
    // os-maven-plugin names macOS osx.
    String os = System.getProperty("os.name").startsWith("Mac") ? "osx" : "linux";
    assertEquals("Lang3 txt Text snapshot LoggerFactory " + os, Sources.describe());
  }
}
