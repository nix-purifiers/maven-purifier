// SPDX-License-Identifier: MIT
package example.multi.app;

import static org.junit.jupiter.api.Assertions.assertEquals;

import example.multi.core.Greeting;
import example.multi.results.Result;
import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.jupiter.api.Test;

class MainTest {
  @Test
  void greetsCapitalized() throws Exception {
    assertEquals("Hello, Maven!", Greeting.greet("maven"));
    Path unicode = Files.createTempFile("maven-unicode-\u00e5", ".txt");
    try {
      Files.write(unicode, new byte[] {42});
      assertEquals(42, Files.readAllBytes(unicode)[0]);
    } finally {
      Files.delete(unicode);
    }
  }

  @Test
  void keepsResultsPackage() {
    assertEquals("retained", Result.value());
  }
}
