// SPDX-License-Identifier: MIT
package io.github.nixpurifiers.maven;

import java.io.File;
import java.io.IOException;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.Arrays;
import java.util.Objects;
import org.apache.maven.execution.ExecutionEvent;
import org.apache.maven.plugin.PluginParameterExpressionEvaluator;
import org.apache.maven.project.MavenProject;
import org.codehaus.plexus.util.xml.Xpp3Dom;

/** Snapshot each executed doclet's native options before another execution overwrites them. */
final class DocumentationCapture {
  private DocumentationCapture() {}

  /** One actual mojo invocation, including its argument file before execution. */
  static final class Invocation {
    final MavenProject project;
    final String execution;
    final File options;
    final File stale;
    final BasicFileAttributes before;
    final byte[] contents;

    Invocation(MavenProject project, String execution, File options, File stale) throws IOException {
      this.project = project;
      this.execution = execution;
      this.options = options;
      this.stale = stale;
      before = options != null && options.isFile() ? Files.readAttributes(options.toPath(), BasicFileAttributes.class) : null;
      contents = before == null ? null : Files.readAllBytes(options.toPath());
    }

    void write(Writer out) throws IOException {
      if (options != null && options.isFile()) {
        BasicFileAttributes after = Files.readAttributes(options.toPath(), BasicFileAttributes.class);
        byte[] current = Files.readAllBytes(options.toPath());
        if (before == null || !before.lastModifiedTime().equals(after.lastModifiedTime())
            || before.size() != after.size() || !Objects.equals(before.fileKey(), after.fileKey())
            || !Arrays.equals(contents, current)) {
          // A tolerated doclet error can leave fresh options/HTML but no cache
          // file. Prefer this invocation's arguments to an older cache file.
          writeOptions(out, project, execution, current);
          return;
        }
      }
      // An unchanged invocation can legitimately reuse the plugin's cache.
      DocumentationCapture.write(out, project, execution, stale);
    }
  }

  private static Object value(ExecutionEvent event, String name) throws Exception {
    Xpp3Dom config = event.getMojoExecution().getConfiguration();
    Xpp3Dom parameter = config == null ? null : config.getChild(name);
    if (parameter == null) return null;
    String expression = parameter.getValue();
    if (expression == null || expression.isEmpty()) expression = parameter.getAttribute("default-value");
    return expression == null ? null : new PluginParameterExpressionEvaluator(
        event.getSession(), event.getMojoExecution()).evaluate(expression);
  }

  private static File file(ExecutionEvent event, Object value) {
    if (value == null) return null;
    File result = value instanceof File ? (File) value : new File(value.toString());
    return result.isAbsolute() ? result : new File(event.getProject().getBasedir(), result.toString());
  }

  static Invocation started(ExecutionEvent event) throws Exception {
    if (event.getMojoExecution() == null || event.getProject() == null
        || !"org.apache.maven.plugins".equals(event.getMojoExecution().getGroupId())
        || !"maven-javadoc-plugin".equals(event.getMojoExecution().getArtifactId())) return null;
    String goal = event.getMojoExecution().getGoal();
    if (!Arrays.asList("javadoc", "javadoc-no-fork", "jar", "jar-no-fork", "aggregate", "aggregate-no-fork",
        "aggregate-jar", "aggregate-jar-no-fork", "test-javadoc", "test-javadoc-no-fork", "test-jar",
        "test-jar-no-fork", "test-aggregate", "test-aggregate-no-fork", "test-aggregate-jar",
        "test-aggregate-jar-no-fork").contains(goal)) return null;
    if (Boolean.parseBoolean(String.valueOf(value(event, "skip")))) return null;
    File output = file(event, value(event, "outputDirectory"));
    // The standard plugin appends [test]apidocs to this configured directory.
    // Do not search target/ or another module for a plausible options file.
    File options = output == null ? null : new File(output, (goal.startsWith("test-") ? "test" : "") + "apidocs/options");
    File stale = file(event, value(event, "staleDataPath"));
    if (stale == null) stale = new File(event.getProject().getBuild().getDirectory(), "maven-javadoc-plugin-stale-data.txt");
    return new Invocation(event.getProject(), event.getMojoExecution().getExecutionId(), options, stale);
  }

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
    writeOptions(out, project, execution, Files.readAllBytes(stale.toPath()));
  }

  private static void writeOptions(Writer out, MavenProject project, String execution, byte[] contents) throws IOException {
    String id = projectKey(project);
    String options = new String(contents, StandardCharsets.UTF_8);
    out.write("{\"event\":\"javadoc\",\"project\":" + json(id)
        + ",\"execution\":" + json(execution) + ",\"options\":" + json(options) + "}\n");
    out.flush();
  }
}
