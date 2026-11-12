// SPDX-License-Identifier: MIT
package io.github.nixpurifiers.maven;

import java.io.File;
import java.io.IOException;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import org.apache.maven.project.MavenProject;

/** Snapshot each executed doclet's native options before another execution overwrites them. */
final class DocumentationCapture {
  private DocumentationCapture() {}

  static String projectKey(MavenProject project) {
    java.nio.file.Path root = java.nio.file.Paths.get(System.getProperty(
        "maven-purifier.root", project.getBasedir().getAbsolutePath())).toAbsolutePath().normalize();
    String key = root.relativize(project.getBasedir().toPath().toAbsolutePath().normalize())
        .toString().replace(File.separatorChar, '/');
    return key.isEmpty() ? "." : key;
  }

  static String json(String value) {
    if (value == null) return "null";
    StringBuilder result = new StringBuilder("\"");
    for (char c : value.toCharArray()) {
      if (c == '\\' || c == '"') result.append('\\').append(c);
      else if (c < 32) result.append(String.format("\\u%04x", (int) c));
      else result.append(c);
    }
    return result.append('"').toString();
  }

  static void write(Writer out, MavenProject project, String execution, File stale) throws IOException {
    if (!stale.isFile()) return;
    String id = projectKey(project);
    String options = new String(Files.readAllBytes(stale.toPath()), StandardCharsets.UTF_8);
    out.write("{\"event\":\"javadoc\",\"project\":" + json(id)
        + ",\"execution\":" + json(execution) + ",\"options\":" + json(options) + "}\n");
    out.flush();
  }
}
