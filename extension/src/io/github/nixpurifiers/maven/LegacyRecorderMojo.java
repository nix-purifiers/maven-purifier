// SPDX-License-Identifier: MIT
package io.github.nixpurifiers.maven;

import java.io.File;
import java.io.IOException;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.List;
import org.apache.maven.artifact.Artifact;
import org.apache.maven.artifact.repository.ArtifactRepository;
import org.apache.maven.plugin.AbstractMojo;
import org.apache.maven.plugin.MojoExecutionException;
import org.apache.maven.project.MavenProject;

/** Captures the effective reactor for Maven versions predating repository event spies. */
public final class LegacyRecorderMojo extends AbstractMojo {
  private List<MavenProject> projects;
  private MavenProject project;
  private ArtifactRepository localRepository;
  private boolean perProject;

  @Override
  public void execute() throws MojoExecutionException {
    String record = System.getenv("MAVEN_PURIFIER_RECORD");
    if (record == null) throw new MojoExecutionException("MAVEN_PURIFIER_RECORD is unset");
    if (perProject && project == null) throw new MojoExecutionException("The Maven project is unavailable");
    StandardOpenOption[] options = perProject
        ? new StandardOpenOption[] {StandardOpenOption.WRITE}
        : new StandardOpenOption[] {StandardOpenOption.CREATE, StandardOpenOption.APPEND};
    try (Writer output = Files.newBufferedWriter(recordFile(Paths.get(record), perProject),
        StandardCharsets.UTF_8, options)) {
      List<MavenProject> reactor = perProject ? null : projects;
      if (reactor == null || reactor.isEmpty()) {
        reactor = new ArrayList<>();
        reactor.add(project);
      }
      for (MavenProject module : reactor) {
        for (ArtifactRepository repository : module.getRemoteArtifactRepositories()) {
          repository(output, repository);
        }
        for (ArtifactRepository repository : module.getPluginArtifactRepositories()) {
          repository(output, repository);
        }
        if ("1".equals(System.getenv("MAVEN_PURIFIER_CAPTURE_RUNTIME"))) {
          runtime(output, module);
        }
        installed(output, module, "", "pom");
        if (!"pom".equals(module.getPackaging())) {
          Artifact main = module.getArtifact();
          installed(output, module, main.getClassifier(), main.getArtifactHandler().getExtension());
        }
        for (Artifact attached : module.getAttachedArtifacts()) {
          installed(output, module, attached.getClassifier(), attached.getArtifactHandler().getExtension());
        }
      }
    } catch (IOException error) {
      throw new MojoExecutionException("Cannot record the legacy Maven reactor", error);
    }
  }

  static Path recordFile(Path record, boolean perProject) throws IOException {
    if (!perProject) return record;
    Path parts = record.resolveSibling(record.getFileName() + ".parts").resolve("build");
    Files.createDirectories(parts);
    return Files.createTempFile(parts, "record-", ".jsonl");
  }

  private void runtime(Writer output, MavenProject module) throws IOException {
    if (module.getArtifacts() == null) return;
    for (Artifact artifact : module.getArtifacts()) {
      String scope = artifact.getScope();
      if (!("compile".equals(scope) || "runtime".equals(scope))) continue;
      // Resolve the managed repository path even if Maven canonicalized a
      // cache symlink to its store object. Reactor classes are separate.
      File file = new File(localRepository.getBasedir(), localRepository.pathOf(artifact));
      if (!file.isFile()) continue;
      // Tool/plugin classpaths are never part of MavenProject.getArtifacts().
      output.write("{\"event\":\"runtime\",\"projectGroupId\":" + json(module.getGroupId())
          + ",\"projectArtifactId\":" + json(module.getArtifactId())
          + ",\"groupId\":" + json(artifact.getGroupId()) + ",\"artifactId\":" + json(artifact.getArtifactId())
          + ",\"version\":" + json(artifact.getVersion()) + ",\"file\":" + json(file.getAbsolutePath()) + "}\n");
    }
  }

  private static void repository(Writer output, ArtifactRepository repository) throws IOException {
    output.write("{\"event\":\"repository\",\"repository\":" + json(repository.getId())
        + ",\"repositoryUrl\":" + json(repository.getUrl()) + "}\n");
  }

  private void installed(Writer output, MavenProject module, String classifier, String extension)
      throws IOException {
    classifier = classifier == null ? "" : classifier;
    String name = module.getArtifactId() + "-" + module.getVersion()
        + (classifier.isEmpty() ? "" : "-" + classifier) + "." + extension;
    File file = new File(localRepository.getBasedir(), module.getGroupId().replace('.', '/') + "/"
        + module.getArtifactId() + "/" + module.getVersion() + "/" + name);
    if (!file.isFile()) return;
    output.write("{\"event\":\"install\",\"groupId\":" + json(module.getGroupId())
        + ",\"artifactId\":" + json(module.getArtifactId()) + ",\"version\":" + json(module.getVersion())
        + ",\"classifier\":" + json(classifier) + ",\"extension\":" + json(extension)
        + ",\"file\":" + json(file.getAbsolutePath()) + "}\n");
  }

  private static String json(String value) {
    return "\"" + value.replace("\\", "\\\\").replace("\"", "\\\"")
        .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + "\"";
  }
}
