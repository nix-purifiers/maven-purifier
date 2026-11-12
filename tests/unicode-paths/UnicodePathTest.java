// SPDX-License-Identifier: MIT
package example.multi.app;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.jupiter.api.Test;

class UnicodePathTest {
  @Test
  void readsAndWritesUnicodePath() throws Exception {
    Path path = Files.createTempFile("maven-unicode-\u00e5", ".txt");
    try {
      Files.write(path, new byte[] {42});
      assertEquals(42, Files.readAllBytes(path)[0]);
    } finally {
      Files.delete(path);
    }
  }
}
