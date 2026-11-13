// SPDX-License-Identifier: MIT
package io.github.nixpurifiers.maven;

import java.io.StringWriter;
import java.lang.reflect.Field;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.stream.Collectors;
import java.util.stream.Stream;
import org.eclipse.aether.DefaultRepositorySystemSession;
import org.eclipse.aether.RepositoryEvent;
import org.eclipse.aether.artifact.DefaultArtifact;
import org.eclipse.aether.metadata.DefaultMetadata;
import org.eclipse.aether.metadata.Metadata;
import org.eclipse.aether.repository.RemoteRepository;

/**
 * Unit tests of the URL-to-path logic of the recording extension. Run with the Maven Resolver API
 * on the class path; it exits non-zero on the first failure.
 */
public final class RecordingEventSpyTest {
  private RecordingEventSpyTest() {}

  private static void check(String expected, String actual) {
    if (!expected.equals(actual)) {
      throw new AssertionError("expected " + expected + " but got " + actual);
    }
  }

  private static void snapshotsSurviveInstall() throws Exception {
    Path root = Files.createTempDirectory("download-origin-");
    Path artifact = root.resolve("same-tests.jar");
    Path snapshots = root.resolve("snapshots");
    Files.write(artifact, "published tests".getBytes(StandardCharsets.UTF_8));
    RecordingEventSpy spy = new RecordingEventSpy();
    StringWriter output = new StringWriter();
    Field writer = RecordingEventSpy.class.getDeclaredField("out");
    writer.setAccessible(true);
    writer.set(spy, output);
    Field directory = RecordingEventSpy.class.getDeclaredField("downloadDirectory");
    directory.setAccessible(true);
    directory.set(spy, snapshots);
    RemoteRepository remote = new RemoteRepository.Builder("origin", "default", "https://example.org/repo").build();
    DefaultArtifact coordinate = new DefaultArtifact("example:same:jar:tests:1");
    DefaultRepositorySystemSession session = new DefaultRepositorySystemSession();
    session.setLocalRepositoryManager(
        new org.eclipse.aether.internal.impl.SimpleLocalRepositoryManagerFactory().newInstance(
            session, new org.eclipse.aether.repository.LocalRepository(root.toFile())));
    spy.onEvent(new RepositoryEvent.Builder(session, RepositoryEvent.EventType.ARTIFACT_DOWNLOADED)
        .setArtifact(coordinate).setRepository(remote).setFile(artifact.toFile()).build());
    List<Path> copies;
    try (Stream<Path> paths = Files.list(snapshots)) {
      copies = paths.collect(Collectors.toList());
    }
    if (copies.size() != 1) throw new AssertionError("one successful transfer needs one snapshot");
    Files.write(artifact, "locally built tests".getBytes(StandardCharsets.UTF_8));
    spy.onEvent(new RepositoryEvent.Builder(session, RepositoryEvent.EventType.ARTIFACT_INSTALLED)
        .setArtifact(coordinate).setFile(artifact.toFile()).build());
    check("published tests", new String(Files.readAllBytes(copies.get(0)), StandardCharsets.UTF_8));
    if (!output.toString().contains("\"snapshot\":" + DocumentationCapture.json(copies.get(0).toString()))
        || !output.toString().contains("https://example.org/repo/example/same/1/same-1-tests.jar")
        || !output.toString().contains("\"event\":\"install\"")) {
      throw new AssertionError("remote input and local install must remain distinct observations");
    }
    if (!output.toString().contains("\"localRepository\":" + DocumentationCapture.json(root.toString()))) {
      throw new AssertionError("capture must name the actual resolver session repository");
    }
    Path staged = root.resolve("staging");
    Files.createDirectories(staged);
    Path stagedArtifact = staged.resolve("same-tests.jar");
    Files.copy(artifact, stagedArtifact);
    spy.onEvent(new RepositoryEvent.Builder(session, RepositoryEvent.EventType.ARTIFACT_INSTALLED)
        .setArtifact(coordinate).setFile(stagedArtifact.toFile())
        .setRepository(new org.eclipse.aether.repository.LocalRepository(staged.toFile())).build());
    if (!output.toString().contains("\"localRepository\":" + DocumentationCapture.json(staged.toString()))) {
      throw new AssertionError("an explicit staging repository takes precedence over the parent session");
    }
    Path metadata = root.resolve("maven-metadata.xml");
    Files.write(metadata, "<metadata>downloaded</metadata>".getBytes(StandardCharsets.UTF_8));
    spy.onEvent(new RepositoryEvent.Builder(session, RepositoryEvent.EventType.METADATA_DOWNLOADED)
        .setMetadata(new DefaultMetadata("example", "same", "maven-metadata.xml", Metadata.Nature.RELEASE))
        .setRepository(remote).setFile(metadata.toFile()).build());
    Files.write(metadata, "<metadata>changed</metadata>".getBytes(StandardCharsets.UTF_8));
    try (Stream<Path> paths = Files.list(snapshots)) {
      if (paths.noneMatch(path -> {
        try {
          return "<metadata>downloaded</metadata>".equals(new String(Files.readAllBytes(path), StandardCharsets.UTF_8));
        } catch (Exception e) {
          throw new AssertionError(e);
        }
      })) throw new AssertionError("metadata also needs its observed bytes");
    }
    Files.delete(artifact);
    spy.onEvent(new RepositoryEvent.Builder(session, RepositoryEvent.EventType.ARTIFACT_DOWNLOADED)
        .setArtifact(coordinate).setRepository(remote).setFile(artifact.toFile()).build());
    if (!output.toString().contains("\"event\":\"download-error\"")) {
      throw new AssertionError("snapshot failure must be recorded for the CLI to reject the lock");
    }
    spy.close();
  }

  public static void main(String[] args) throws Exception {
    Path records = Files.createTempDirectory("nested-records-");
    Path main = records.resolve("record.jsonl");
    if (!main.equals(RecordingEventSpy.recordFile(main, "build"))) {
      throw new AssertionError("first process must retain the main record");
    }
    Path child = RecordingEventSpy.recordFile(main, "build");
    Path sibling = RecordingEventSpy.recordFile(main, "build");
    Path development = RecordingEventSpy.recordFile(main, "development");
    if (child.equals(sibling) || !child.getParent().endsWith("build")
        || !development.getParent().endsWith("development")) {
      throw new AssertionError("process writers and capture phases must remain separate");
    }
    org.apache.maven.execution.DefaultMavenExecutionRequest request =
        new org.apache.maven.execution.DefaultMavenExecutionRequest();
    RecordingEventSpy.prepareReplay(request);
    if (request.isOffline()) throw new AssertionError("ordinary capture became offline");
    System.setProperty("maven-purifier.replay", "true");
    try {
      RecordingEventSpy.prepareReplay(request);
      if (!request.isOffline()) throw new AssertionError("nested replay request remained online");
      RecordingEventSpy.prepareReplay(new Object());
    } finally {
      System.clearProperty("maven-purifier.replay");
    }
    check(
        "org/apache/commons/commons-lang3/3.17.0/commons-lang3-3.17.0.jar",
        RecordingEventSpy.artifactPath(
            new DefaultArtifact("org.apache.commons:commons-lang3:jar:3.17.0")));
    check(
        "example/core/1.0/core-1.0-sources.jar",
        RecordingEventSpy.artifactPath(new DefaultArtifact("example:core:jar:sources:1.0")));
    check(
        "example/core/1.0/core-1.0.pom",
        RecordingEventSpy.artifactPath(new DefaultArtifact("example:core:pom:1.0")));
    // A timestamped SNAPSHOT lives in the directory of its base version.
    check(
        "example/snaplib/1.0-SNAPSHOT/snaplib-1.0-20261101.082447-1.jar",
        RecordingEventSpy.artifactPath(
            new DefaultArtifact("example:snaplib:jar:1.0-20261101.082447-1")));

    check(
        "org/slf4j/slf4j-api/maven-metadata.xml",
        RecordingEventSpy.metadataPath(
            new DefaultMetadata(
                "org.slf4j", "slf4j-api", "maven-metadata.xml", Metadata.Nature.RELEASE)));
    check(
        "example/snaplib/1.0-SNAPSHOT/maven-metadata.xml",
        RecordingEventSpy.metadataPath(
            new DefaultMetadata(
                "example",
                "snaplib",
                "1.0-SNAPSHOT",
                "maven-metadata.xml",
                Metadata.Nature.SNAPSHOT)));
    check(
        "org/apache/maven/plugins/maven-metadata.xml",
        RecordingEventSpy.metadataPath(
            new DefaultMetadata(
                "org.apache.maven.plugins", "maven-metadata.xml", Metadata.Nature.RELEASE)));

    RemoteRepository withSlash =
        new RemoteRepository.Builder("central", "default", "https://repo.maven.apache.org/maven2/")
            .build();
    RemoteRepository withoutSlash =
        new RemoteRepository.Builder("google", "default", "https://maven.google.com").build();
    check(
        "https://repo.maven.apache.org/maven2/a/b/1/b-1.jar",
        RecordingEventSpy.url(withSlash, "a/b/1/b-1.jar"));
    check("https://maven.google.com/a/b/1/b-1.jar", RecordingEventSpy.url(withoutSlash, "a/b/1/b-1.jar"));

    snapshotsSurviveInstall();
    System.out.println("RecordingEventSpyTest: all checks passed");
  }
}
