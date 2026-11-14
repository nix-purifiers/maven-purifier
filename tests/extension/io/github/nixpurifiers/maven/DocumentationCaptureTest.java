// SPDX-License-Identifier: MIT
package io.github.nixpurifiers.maven;

import java.io.StringWriter;
import java.lang.reflect.Field;
import java.lang.reflect.Proxy;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import org.apache.maven.execution.DefaultMavenExecutionRequest;
import org.apache.maven.execution.DefaultMavenExecutionResult;
import org.apache.maven.execution.ExecutionEvent;
import org.apache.maven.execution.MavenSession;
import org.apache.maven.model.Build;
import org.apache.maven.model.Model;
import org.apache.maven.model.Plugin;
import org.apache.maven.plugin.MojoExecution;
import org.apache.maven.project.MavenProject;
import org.codehaus.plexus.util.xml.Xpp3Dom;
import org.eclipse.aether.DefaultRepositorySystemSession;

/** Drive real spy events: current options are evidence, unrelated target files are not. */
public final class DocumentationCaptureTest {
  private static MavenProject project(Path root, String name) throws Exception {
    Path pom = root.resolve(name).resolve("pom.xml");
    Files.createDirectories(pom.getParent());
    Files.write(pom, "<project/>".getBytes(StandardCharsets.UTF_8));
    Model model = new Model();
    model.setGroupId("example");
    model.setArtifactId(name);
    model.setVersion("1");
    Build build = new Build();
    build.setDirectory(pom.getParent().resolve("build").toString());
    model.setBuild(build);
    MavenProject result = new MavenProject(model);
    result.setFile(pom.toFile());
    return result;
  }

  private static MojoExecution mojo(String goal, String id) {
    Plugin plugin = new Plugin();
    plugin.setGroupId("org.apache.maven.plugins");
    plugin.setArtifactId("maven-javadoc-plugin");
    plugin.setVersion("3.12.0");
    MojoExecution result = new MojoExecution(plugin, goal, id);
    Xpp3Dom config = new Xpp3Dom("configuration");
    Xpp3Dom output = new Xpp3Dom("outputDirectory");
    output.setAttribute("default-value", "${project.build.directory}/custom-reports");
    config.addChild(output);
    Xpp3Dom stale = new Xpp3Dom("staleDataPath");
    stale.setValue("${project.build.directory}/custom-cache.txt");
    config.addChild(stale);
    result.setConfiguration(config);
    return result;
  }

  private static ExecutionEvent event(MavenSession session, MavenProject project, MojoExecution mojo, ExecutionEvent.Type type) {
    session.setCurrentProject(project);
    return (ExecutionEvent) Proxy.newProxyInstance(ExecutionEvent.class.getClassLoader(), new Class<?>[] {ExecutionEvent.class},
        (proxy, method, args) -> {
          if ("getType".equals(method.getName())) return type;
          if ("getSession".equals(method.getName())) return session;
          if ("getProject".equals(method.getName())) return project;
          if ("getMojoExecution".equals(method.getName())) return mojo;
          return null;
        });
  }

  private static Path options(MavenProject project) {
    return project.getBasedir().toPath().resolve("build/custom-reports/apidocs/options");
  }

  private static void write(Path file, String text) throws Exception {
    Files.createDirectories(file.getParent());
    Files.write(file, text.getBytes(StandardCharsets.UTF_8));
  }

  private static void empty(StringWriter out, String label) {
    if (!out.toString().isEmpty()) throw new AssertionError(label + ": " + out);
  }

  public static void main(String[] args) throws Exception {
    Path root = Files.createTempDirectory("documentation-invocations-");
    MavenProject a = project(root, "a");
    MavenProject b = project(root, "b");
    MavenSession session = new MavenSession(null, new DefaultRepositorySystemSession(),
        new DefaultMavenExecutionRequest(), new DefaultMavenExecutionResult());
    RecordingEventSpy spy = new RecordingEventSpy();
    StringWriter out = new StringWriter();
    Field writer = RecordingEventSpy.class.getDeclaredField("out");
    writer.setAccessible(true);
    writer.set(spy, out);
    String previous = System.getProperty("maven-purifier.root");
    System.setProperty("maven-purifier.root", root.toString());
    try {
      MojoExecution first = mojo("javadoc", "first");
      spy.onEvent(event(session, a, first, ExecutionEvent.Type.MojoStarted));
      write(options(a), "-link https://current.example/api\n");
      spy.onEvent(event(session, a, first, ExecutionEvent.Type.MojoSucceeded));
      if (!out.toString().contains("https://current.example/api") || !out.toString().contains("\"project\":\"a\"")) {
        throw new AssertionError("current invocation options were not captured: " + out);
      }
      out.getBuffer().setLength(0);

      // Neither an unchanged options file nor another module's fresh file is evidence.
      MojoExecution stale = mojo("javadoc", "stale");
      spy.onEvent(event(session, a, stale, ExecutionEvent.Type.MojoStarted));
      spy.onEvent(event(session, a, stale, ExecutionEvent.Type.MojoSucceeded));
      empty(out, "unchanged stale options captured");
      MojoExecution other = mojo("javadoc", "other");
      spy.onEvent(event(session, b, other, ExecutionEvent.Type.MojoStarted));
      write(options(a), "-link https://other-module.example/api\n");
      spy.onEvent(event(session, b, other, ExecutionEvent.Type.MojoSucceeded));
      empty(out, "another module's options captured");

      Path cache = a.getBasedir().toPath().resolve("build/custom-cache.txt");
      write(cache, "-link https://old-cache.example/api\n");
      MojoExecution fresh = mojo("jar", "fresh");
      spy.onEvent(event(session, a, fresh, ExecutionEvent.Type.MojoStarted));
      write(options(a), "-link https://fresh-before-cache.example/api\n");
      spy.onEvent(event(session, a, fresh, ExecutionEvent.Type.MojoSucceeded));
      if (!out.toString().contains("fresh-before-cache") || out.toString().contains("old-cache")) throw new AssertionError("old cache preferred to fresh invocation");
      out.getBuffer().setLength(0);

      MojoExecution help = mojo("help", "not-a-doclet");
      spy.onEvent(event(session, a, help, ExecutionEvent.Type.MojoStarted));
      write(options(a), "-link https://uncaptured-goal.example/api\n");
      spy.onEvent(event(session, a, help, ExecutionEvent.Type.MojoSucceeded));
      empty(out, "non-doclet goal captured");
      MojoExecution skipped = mojo("javadoc", "skip");
      Xpp3Dom skip = new Xpp3Dom("skip");
      skip.setValue("true");
      skipped.getConfiguration().addChild(skip);
      spy.onEvent(event(session, a, skipped, ExecutionEvent.Type.MojoStarted));
      spy.onEvent(event(session, a, skipped, ExecutionEvent.Type.MojoSucceeded));
      empty(out, "skipped doclet captured");

      MojoExecution failed = mojo("javadoc", "failed");
      spy.onEvent(event(session, a, failed, ExecutionEvent.Type.MojoStarted));
      write(options(a), "-link https://failed-goal.example/api\n");
      spy.onEvent(event(session, a, failed, ExecutionEvent.Type.MojoFailed));
      spy.onEvent(event(session, a, failed, ExecutionEvent.Type.MojoSucceeded));
      empty(out, "failed invocation retained");

      MojoExecution cached = mojo("javadoc", "cached");
      spy.onEvent(event(session, a, cached, ExecutionEvent.Type.MojoStarted));
      spy.onEvent(event(session, a, cached, ExecutionEvent.Type.MojoSucceeded));
      if (!out.toString().contains("old-cache.example")) throw new AssertionError("declared native cache lost");
      System.out.println("DocumentationCaptureTest: all checks passed");
    } finally {
      if (previous == null) System.clearProperty("maven-purifier.root");
      else System.setProperty("maven-purifier.root", previous);
    }
  }
}
