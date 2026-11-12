// SPDX-License-Identifier: MIT
package io.github.nixpurifiers.maven;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.Arrays;
import org.apache.maven.execution.DefaultMavenExecutionRequest;
import org.apache.maven.execution.DefaultMavenExecutionResult;
import org.apache.maven.execution.MavenSession;
import org.apache.maven.model.Build;
import org.apache.maven.model.Model;
import org.apache.maven.model.Plugin;
import org.apache.maven.model.PluginExecution;
import org.apache.maven.model.PluginManagement;
import org.apache.maven.model.Dependency;
import org.apache.maven.project.MavenProject;
import org.codehaus.plexus.util.xml.Xpp3Dom;
import org.eclipse.aether.DefaultRepositorySystemSession;

/** Protect the native model projection independently of capture's Python representation. */
public final class DocumentationLinksTest {
  private static Xpp3Dom child(Xpp3Dom parent, String name, String value) {
    Xpp3Dom node = new Xpp3Dom(name);
    node.setValue(value);
    parent.addChild(node);
    return node;
  }

  private static MavenProject project(Path root, String name) throws Exception {
    Path pom = root.resolve(name).resolve("pom.xml");
    Files.createDirectories(pom.getParent());
    Files.write(pom, "<project/>\n".getBytes(StandardCharsets.UTF_8));
    Model model = new Model();
    model.setGroupId("example");
    model.setArtifactId(name);
    model.setVersion("1");
    model.setBuild(new Build());
    model.getProperties().setProperty("docs.base", "https://docs.example.invalid/" + name);
    Plugin plugin = new Plugin();
    plugin.setGroupId("org.apache.maven.plugins");
    plugin.setArtifactId("maven-javadoc-plugin");
    Xpp3Dom config = new Xpp3Dom("configuration");
    child(child(config, "links", null), "link", "${docs.base}");
    child(config, "doclint", "all");
    plugin.setConfiguration(config);
    PluginExecution execution = new PluginExecution();
    execution.setId("attach-docs");
    plugin.addExecution(execution);
    // Populate the cache before projection: adding default-cli must flush it.
    plugin.getExecutionsAsMap();
    model.getBuild().addPlugin(plugin);
    MavenProject project = new MavenProject(model);
    project.setFile(pom.toFile());
    return project;
  }

  private static String digest(byte[] data) throws Exception {
    StringBuilder result = new StringBuilder();
    for (byte value : MessageDigest.getInstance("SHA-256").digest(data)) result.append(String.format("%02x", value & 255));
    return result.toString();
  }

  private static void check(MavenProject project, Path repository, String name) throws Exception {
    Plugin plugin = project.getBuildPlugins().get(0);
    if (plugin.getExecutionsAsMap().get("default-cli") == null) throw new AssertionError("cached execution lookup lost default-cli");
    for (String id : Arrays.asList("attach-docs", "default-cli")) {
      Xpp3Dom config = (Xpp3Dom) plugin.getExecutionsAsMap().get(id).getConfiguration();
      if (config.getChild("links").getChildCount() != 0) throw new AssertionError("remote metadata still requested");
      if (!"all".equals(config.getChild("doclint").getValue())) throw new AssertionError("doclint configuration changed");
      Xpp3Dom[] links = config.getChild("offlineLinks").getChildren("offlineLink");
      if (links.length != 1) throw new AssertionError("another module's metadata leaked into this execution");
      if (!("https://docs.example.invalid/" + name).equals(links[0].getChild("url").getValue())) throw new AssertionError("public href changed");
      if (!repository.resolve("downloads/javadoc/" + name).toFile().getCanonicalPath().equals(links[0].getChild("location").getValue())) throw new AssertionError("metadata location changed");
    }
    if (!"${docs.base}".equals(((Xpp3Dom) plugin.getConfiguration()).getChild("links").getChild("link").getValue())) throw new AssertionError("original plugin configuration changed");
    if (!"<project/>\n".equals(new String(Files.readAllBytes(project.getFile().toPath()), StandardCharsets.UTF_8))) throw new AssertionError("source POM changed");
  }

  public static void main(String[] args) throws Exception {
    Path root = Files.createTempDirectory("documentation-model-");
    Path repository = root.resolve("repository");
    Files.createDirectories(repository.resolve("downloads/javadoc"));
    MavenProject a = project(root, "a");
    MavenProject b = project(root, "b");
    MavenProject managedProject = project(root, "managed");
    Plugin managed = managedProject.getBuild().getPlugins().remove(0);
    managed.setVersion("3.12.0");
    managed.getExecutions().get(0).setPhase("validate");
    managed.getExecutions().get(0).addGoal("uncaptured-must-not-run");
    PluginExecution cli = new PluginExecution();
    cli.setId("default-cli");
    Xpp3Dom cliConfig = new Xpp3Dom("configuration");
    child(cliConfig, "doctitle", "managed CLI title");
    cli.setConfiguration(cliConfig);
    managed.addExecution(cli);
    Dependency dependency = new Dependency();
    dependency.setGroupId("example");
    dependency.setArtifactId("doclet-support");
    dependency.setVersion("1");
    managed.addDependency(dependency);
    PluginManagement management = new PluginManagement();
    management.addPlugin(managed);
    managedProject.getBuild().setPluginManagement(management);
    managedProject.getBuild().getPluginsAsMap();
    // This managed plugin has no captured execution and must stay inactive.
    MavenProject untouched = project(root, "untouched");
    Plugin uncaptured = untouched.getBuild().getPlugins().remove(0);
    PluginManagement untouchedManagement = new PluginManagement();
    untouchedManagement.addPlugin(uncaptured);
    untouched.getBuild().setPluginManagement(untouchedManagement);
    byte[] content = "org.junit\n".getBytes(StandardCharsets.UTF_8);
    StringBuilder index = new StringBuilder("<javadoc version=\"1\">");
    for (String name : Arrays.asList("a", "b", "managed")) {
      Path directory = repository.resolve("downloads/javadoc/" + name);
      Files.createDirectories(directory);
      Files.write(directory.resolve("package-list"), content);
      index.append("<project id=\"").append(name).append("\">");
      for (String id : Arrays.asList("attach-docs", "default-cli")) {
        if ("managed".equals(name) && !"default-cli".equals(id)) continue;
        index.append("<execution id=\"").append(id).append("\"><link url=\"https://docs.example.invalid/")
            .append(name).append("\" directory=\"downloads/javadoc/").append(name)
            .append("\"><source name=\"package-list\" sha256=\"").append(digest(content))
            .append("\"/></link></execution>");
      }
      index.append("</project>");
    }
    index.append("</javadoc>");
    Files.write(repository.resolve("downloads/javadoc/index.xml"), index.toString().getBytes(StandardCharsets.UTF_8));
    DefaultMavenExecutionRequest request = new DefaultMavenExecutionRequest();
    request.setLocalRepositoryPath(repository.toFile());
    DefaultRepositorySystemSession resolver = new DefaultRepositorySystemSession();
    resolver.setOffline(true);
    MavenSession session = new MavenSession(null, resolver, request, new DefaultMavenExecutionResult());
    session.setProjects(Arrays.asList(a, b, managedProject, untouched));
    String previous = System.getProperty("maven-purifier.root");
    System.setProperty("maven-purifier.root", root.toString());
    try {
      DocumentationLinks.apply(session);
      check(a, repository, "a");
      check(b, repository, "b");
      Plugin materialized = managedProject.getBuildPlugins().get(0);
      if (!"3.12.0".equals(materialized.getVersion()) || materialized.getDependencies().size() != 1) throw new AssertionError("managed plugin identity changed");
      if (materialized.getExecutions().size() != 1 || materialized.getExecutionsAsMap().get("attach-docs") != null) throw new AssertionError("uncaptured managed execution activated");
      PluginExecution projected = materialized.getExecutionsAsMap().get("default-cli");
      Xpp3Dom projectedConfig = (Xpp3Dom) projected.getConfiguration();
      if (!"managed CLI title".equals(projectedConfig.getChild("doctitle").getValue())) throw new AssertionError("managed CLI configuration lost");
      if (projected.getPhase() != null || !projected.getGoals().isEmpty()) throw new AssertionError("managed CLI bound a lifecycle phase");
      if (projectedConfig.getChild("links").getChildCount() != 0 || projectedConfig.getChild("offlineLinks").getChildCount() != 1) throw new AssertionError("managed CLI link not projected");
      if (managed.getExecutions().size() != 2 || ((Xpp3Dom) managed.getConfiguration()).getChild("links").getChildCount() != 1) throw new AssertionError("original pluginManagement changed");
      if (!untouched.getBuildPlugins().isEmpty()) throw new AssertionError("uncaptured managed plugin activated");
      Files.write(repository.resolve("downloads/javadoc/a/package-list"), "changed\n".getBytes(StandardCharsets.UTF_8));
      try {
        DocumentationLinks.apply(session);
        throw new AssertionError("corrupted metadata was accepted");
      } catch (java.io.IOException expected) {
        if (!expected.getMessage().contains("metadata changed")) throw expected;
      }
      if (!resolver.isOffline()) throw new AssertionError("resolver network access enabled");
    } finally {
      if (previous == null) System.clearProperty("maven-purifier.root");
      else System.setProperty("maven-purifier.root", previous);
    }
  }
}
