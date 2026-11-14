// SPDX-License-Identifier: MIT
package io.github.nixpurifiers.maven;

import java.io.File;
import java.io.IOException;
import java.io.Reader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.Set;
import org.apache.maven.model.BuildBase;
import org.apache.maven.model.Dependency;
import org.apache.maven.model.DependencyManagement;
import org.apache.maven.model.Model;
import org.apache.maven.model.ModelBase;
import org.apache.maven.model.Parent;
import org.apache.maven.model.Plugin;
import org.apache.maven.model.Profile;

/** Inferred native module selections, shared by the Maven 3 and Maven 4 model adapters. */
public final class ProjectedVersions {
  private ProjectedVersions() {}

  public static Properties forSource(File source) throws IOException {
    Properties versions = new Properties();
    if (source == null) return versions;
    if (Boolean.parseBoolean(System.getProperty("maven-purifier.externalInputs"))) {
      return external(source);
    }
    File absolute = source.getAbsoluteFile();
    for (File directory = absolute.getParentFile(); directory != null; directory = directory.getParentFile()) {
      File configuration = new File(directory, ".maven-purifier/project-inputs.properties");
      if (!configuration.isFile()) continue;
      // Do not rewrite artifact-descriptor models in the local repository:
      // build plugins have their own dependency realms. The project's managed
      // dependencies select its transitive graph without altering those realms.
      File repository = new File(directory, ".maven-purifier/repository");
      if (absolute.toPath().normalize().startsWith(repository.toPath().normalize())) return versions;
      try (Reader input = Files.newBufferedReader(configuration.toPath(), StandardCharsets.UTF_8)) {
        versions.load(input);
      }
      return versions;
    }
    return versions;
  }

  private static File absoluteProperty(String name) throws IOException {
    String value = System.getProperty(name);
    if (value == null || !new File(value).isAbsolute()) {
      throw new IOException("missing absolute external Maven view association: " + name);
    }
    return new File(value).getCanonicalFile();
  }

  private static Properties external(File source) throws IOException {
    File root = absoluteProperty("maven-purifier.root");
    File configuration = absoluteProperty("maven-purifier.inputs");
    File repository = absoluteProperty("maven-purifier.repository");
    if (configuration.toPath().startsWith(root.toPath())
        || Files.isSymbolicLink(new File(System.getProperty("maven-purifier.inputs")).toPath())) {
      throw new IOException("external project inputs must be a frozen file outside the source tree");
    }
    byte[] bytes = Files.readAllBytes(configuration.toPath());
    try {
      byte[] hash = java.security.MessageDigest.getInstance("SHA-256").digest(bytes);
      StringBuilder hex = new StringBuilder();
      for (byte value : hash) {
        hex.append(Character.forDigit((value & 255) >>> 4, 16));
        hex.append(Character.forDigit(value & 15, 16));
      }
      if (!hex.toString().equals(System.getProperty("maven-purifier.inputsHash"))) {
        throw new IOException("external project input metadata has drifted");
      }
    } catch (java.security.NoSuchAlgorithmException error) {
      throw new IOException("SHA-256 is unavailable", error);
    }
    Properties versions = new Properties();
    java.nio.file.Path path = source.getCanonicalFile().toPath();
    if (!path.startsWith(root.toPath()) || path.startsWith(repository.toPath())) return versions;
    // Invoker can select another local repository inside the build tree.
    // Maven CLI sets these user properties before models are constructed.
    String activeRepository = System.getProperty("maven.repo.local");
    if (activeRepository != null && path.startsWith(new File(activeRepository).getCanonicalFile().toPath())) {
      return versions;
    }
    try (Reader input = new java.io.StringReader(new String(bytes, StandardCharsets.UTF_8))) {
      versions.load(input);
    }
    return versions;
  }

  public static File source(Map<String, ?> options, File fallback) {
    if (fallback != null) return fallback;
    Object source = options == null ? null : options.get("org.apache.maven.model.building.source");
    if (source instanceof org.apache.maven.model.building.ModelSource) {
      String location = ((org.apache.maven.model.building.ModelSource) source).getLocation();
      if (location != null && !location.contains("://")) return new File(location);
    }
    return null;
  }

  private static String expand(String value, Properties properties) {
    if (value == null) return "";
    for (int iteration = 0; iteration < 32; iteration++) {
      String previous = value;
      for (String name : properties.stringPropertyNames()) {
        value = value.replace("${" + name + "}", properties.getProperty(name));
      }
      if (previous.equals(value)) break;
    }
    return value;
  }

  private static String version(Properties versions, String group, String artifact, Properties properties) {
    return versions.getProperty(expand(group, properties) + ":" + expand(artifact, properties));
  }

  private static List<Dependency> dependencies(List<Dependency> original, Properties versions, Properties properties) {
    List<Dependency> selected = new ArrayList<>();
    for (Dependency dependency : original) {
      Dependency copy = dependency.clone();
      String selectedVersion = version(versions, copy.getGroupId(), copy.getArtifactId(), properties);
      if (selectedVersion != null) copy.setVersion(selectedVersion);
      selected.add(copy);
    }
    return selected;
  }

  private static void base(ModelBase model, Properties versions, Properties properties) {
    model.setDependencies(dependencies(model.getDependencies(), versions, properties));
    DependencyManagement management = model.getDependencyManagement();
    if (management == null) management = new DependencyManagement();
    List<Dependency> managed = dependencies(management.getDependencies(), versions, properties);
    Set<String> existing = new HashSet<>();
    for (Dependency dependency : managed) existing.add(dependency.getManagementKey());
    for (String coordinate : new java.util.TreeSet<>(versions.stringPropertyNames())) {
      int separator = coordinate.indexOf(':');
      if (separator < 1 || separator == coordinate.length() - 1) {
        throw new IllegalArgumentException("maven-purifier: invalid Nix module coordinate " + coordinate);
      }
      Dependency dependency = new Dependency();
      dependency.setGroupId(coordinate.substring(0, separator));
      dependency.setArtifactId(coordinate.substring(separator + 1));
      dependency.setVersion(versions.getProperty(coordinate));
      if (existing.add(dependency.getManagementKey())) managed.add(dependency);
    }
    management.setDependencies(managed);
    model.setDependencyManagement(management);
  }

  private static void build(BuildBase build, Properties versions, Properties properties) {
    if (build == null) return;
    List<Plugin> plugins = new ArrayList<>();
    for (Plugin original : build.getPlugins()) {
      Plugin plugin = original.clone();
      String group = plugin.getGroupId() == null ? "org.apache.maven.plugins" : plugin.getGroupId();
      String selected = version(versions, group, plugin.getArtifactId(), properties);
      if (selected != null) plugin.setVersion(selected);
      plugin.setDependencies(dependencies(plugin.getDependencies(), versions, properties));
      plugins.add(plugin);
    }
    build.setPlugins(plugins);
    if (build.getPluginManagement() != null) {
      // Use the same operation for managed plugins without disturbing their
      // execution/configuration metadata.
      BuildBase managed = new BuildBase();
      managed.setPlugins(build.getPluginManagement().getPlugins());
      build(managed, versions, properties);
      build.getPluginManagement().setPlugins(managed.getPlugins());
    }
  }

  public static Model select(Model original, Properties versions, Properties inherited) {
    if (versions.isEmpty()) return original;
    Model model = original.clone();
    Properties properties = new Properties();
    if (inherited != null) properties.putAll(inherited);
    properties.putAll(model.getProperties());
    String group = model.getGroupId();
    if (group == null && model.getParent() != null) group = model.getParent().getGroupId();
    if (group != null) properties.setProperty("project.groupId", group);
    if (model.getVersion() != null) properties.setProperty("project.version", model.getVersion());
    if (model.getParent() != null) {
      Parent parent = model.getParent().clone();
      String selected = version(versions, parent.getGroupId(), parent.getArtifactId(), properties);
      if (selected != null) {
        parent.setVersion(selected);
        parent.setRelativePath("");
        model.setParent(parent);
      }
    }
    base(model, versions, properties);
    build(model.getBuild(), versions, properties);
    List<Profile> profiles = new ArrayList<>();
    for (Profile originalProfile : model.getProfiles()) {
      Profile profile = originalProfile.clone();
      Properties profileProperties = new Properties();
      profileProperties.putAll(properties);
      profileProperties.putAll(profile.getProperties());
      base(profile, versions, profileProperties);
      build(profile.getBuild(), versions, profileProperties);
      profiles.add(profile);
    }
    model.setProfiles(profiles);
    return model;
  }
}
