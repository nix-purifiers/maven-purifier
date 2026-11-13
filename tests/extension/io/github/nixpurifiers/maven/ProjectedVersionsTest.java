// SPDX-License-Identifier: MIT
package io.github.nixpurifiers.maven;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

public final class ProjectedVersionsTest {
  public static void main(String[] args) throws Exception {
    Path temporary = Files.createTempDirectory("external-project-inputs-");
    Path root = temporary.resolve("source");
    Path module = root.resolve("module/pom.xml");
    Files.createDirectories(module.getParent());
    Files.write(module, "<project/>".getBytes(StandardCharsets.UTF_8));
    Path repository = temporary.resolve("repository");
    Files.createDirectories(repository);
    Path descriptor = repository.resolve("descriptor.pom");
    Files.write(descriptor, "<project/>".getBytes(StandardCharsets.UTF_8));
    Path inputs = temporary.resolve("inputs.properties");
    byte[] content = "example\\:library=2\n".getBytes(StandardCharsets.UTF_8);
    Files.write(inputs, content);
    StringBuilder hash = new StringBuilder();
    for (byte value : java.security.MessageDigest.getInstance("SHA-256").digest(content)) {
      hash.append(Character.forDigit((value & 255) >>> 4, 16));
      hash.append(Character.forDigit(value & 15, 16));
    }
    String[] names = {"maven-purifier.externalInputs", "maven-purifier.root", "maven-purifier.inputs",
        "maven-purifier.inputsHash", "maven-purifier.repository", "maven.repo.local"};
    java.util.Map<String, String> previous = new java.util.HashMap<>();
    for (String name : names) previous.put(name, System.getProperty(name));
    try {
      System.setProperty(names[0], "true");
      System.setProperty(names[1], root.toString());
      System.setProperty(names[2], inputs.toString());
      System.setProperty(names[3], hash.toString());
      System.setProperty(names[4], repository.toString());
      if (!"2".equals(ProjectedVersions.forSource(module.toFile()).getProperty("example:library"))) {
        throw new AssertionError("module source must use its frozen external inputs");
      }
      if (!ProjectedVersions.forSource(descriptor.toFile()).isEmpty()) {
        throw new AssertionError("plugin descriptor models must not use project input selections");
      }
      Path nested = root.resolve("target/test-repository");
      Files.createDirectories(nested);
      Path nestedDescriptor = nested.resolve("input.pom");
      Files.write(nestedDescriptor, "<project/>".getBytes(StandardCharsets.UTF_8));
      System.setProperty("maven.repo.local", nested.toString());
      if (!ProjectedVersions.forSource(nestedDescriptor.toFile()).isEmpty()) {
        throw new AssertionError("the actual nested resolver repository must be excluded");
      }
      Path alias = root.resolve("descriptor-alias.pom");
      Files.createSymbolicLink(alias, descriptor);
      if (!ProjectedVersions.forSource(alias.toFile()).isEmpty()) {
        throw new AssertionError("physical repository models must not project through a source symlink");
      }
      Path legacy = root.resolve(".maven-purifier/project-inputs.properties");
      Files.createDirectories(legacy.getParent());
      Files.write(legacy, "example\\:library=wrong\n".getBytes(StandardCharsets.UTF_8));
      Files.write(inputs, "changed metadata".getBytes(StandardCharsets.UTF_8));
      try {
        ProjectedVersions.forSource(module.toFile());
        throw new AssertionError("drifted external metadata must not fall back to another selection");
      } catch (java.io.IOException expected) {
        if (!expected.getMessage().contains("drifted")) throw expected;
      }
    } finally {
      for (String name : names) {
        String value = previous.get(name);
        if (value == null) System.clearProperty(name); else System.setProperty(name, value);
      }
    }
  }
}
