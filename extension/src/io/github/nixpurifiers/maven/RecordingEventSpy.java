// SPDX-License-Identifier: MIT
package io.github.nixpurifiers.maven;

import java.io.IOException;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.FileAlreadyExistsException;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.util.LinkedHashSet;
import java.util.IdentityHashMap;
import java.util.Map;
import java.util.Properties;
import java.util.Set;
import org.apache.maven.eventspy.AbstractEventSpy;
import org.apache.maven.execution.MavenExecutionResult;
import org.eclipse.aether.RepositoryEvent;
import org.eclipse.aether.artifact.Artifact;
import org.eclipse.aether.metadata.Metadata;
import org.eclipse.aether.repository.ArtifactRepository;
import org.eclipse.aether.repository.RemoteRepository;

/**
 * Records what a Maven build downloads and what it installs, for the lock run of maven-purifier.
 *
 * <p>Maven loads this class as a core extension when the lock run passes the extension jar in
 * {@code -Dmaven.ext.class.path}. Maven forwards every repository event of its resolver to the
 * event spies it loads. When the environment variable {@code MAVEN_PURIFIER_RECORD} names a file,
 * this spy appends one JSON object per line to it:
 *
 * <ul>
 *   <li>{@code {"event":"download","repository":…,"repositoryUrl":…,"url":…,"file":…}} for each
 *       artifact or metadata file the resolver downloaded, with the id and URL of the repository it
 *       came from, its own URL, and the file it was stored at in the local repository;
 *   <li>{@code {"event":"resolved","file":…}} for each metadata file the build read from the local
 *       repository;
 *   <li>{@code {"event":"install","groupId":…,"artifactId":…,"version":…,"classifier":…,
 *       "extension":…,"file":…}} for each artifact the build installed into the local repository.
 * </ul>
 *
 * <p>After {@code maven-purifier prefetch}, Maven loads the spy for every build through the lines
 * that the command writes into {@code .mvn/maven.config}, with the system property {@code
 * maven-purifier.maven} naming the Maven version of the lock. The spy then guards the offline
 * build: it warns when another Maven version runs, whose default plugin versions differ from the
 * locked ones, and when the build fails after files could not be resolved offline, it names them
 * and the commands that add them.
 */
public class RecordingEventSpy extends AbstractEventSpy {

  private Writer out;
  private Path downloadDirectory;
  private String lockedMaven;
  private final Set<String> unresolved = new LinkedHashSet<>();
  private final Map<org.apache.maven.plugin.MojoExecution, DocumentationCapture.Invocation> documentation = new IdentityHashMap<>();

  @Override
  public void init(Context context) throws IOException {
    String record = System.getenv("MAVEN_PURIFIER_RECORD");
    if (record != null && !record.isEmpty()) {
      Path recordPath = Paths.get(record).toAbsolutePath();
      downloadDirectory = recordPath.resolveSibling(recordPath.getFileName() + ".downloads");
      out =
          Files.newBufferedWriter(
              recordFile(recordPath, System.getenv("MAVEN_PURIFIER_PHASE")),
              StandardCharsets.UTF_8,
              StandardOpenOption.CREATE,
              StandardOpenOption.APPEND);
      return;
    }
    lockedMaven = System.getProperty("maven-purifier.maven");
    if (lockedMaven == null) {
      return;
    }
    Map<String, Object> data = context.getData();
    Object versions = data == null ? null : data.get("versionProperties");
    String running = versions instanceof Properties ? ((Properties) versions).getProperty("version") : null;
    if (running != null && !running.equals(lockedMaven)) {
      System.err.println(
          "maven-purifier: this is Maven "
              + running
              + ", and the lock was made with Maven "
              + lockedMaven
              + ". Their default plugin versions differ, so a plugin may be missing offline; run"
              + " .maven-purifier/bin/mvn, or lock with this Maven's version in"
              + " .mvn/wrapper/maven-wrapper.properties.");
    }
  }

  // Each recorder owns its writer. Maven can initialize multiple spies, and
  // Invoker children inherit the record destination; synchronized methods do
  // not serialize either independent instances or JVMs. Keep the first
  // recorder in the original file; subsequent instances get private files
  // grouped by capture phase. The separate runtime recorder goal still
  // appends its root-project runtime events to the original file.
  static Path recordFile(Path record, String phase) throws IOException {
    try {
      return Files.createFile(record);
    } catch (FileAlreadyExistsException existing) {
      String group = "development".equals(phase) ? "development" : "build";
      Path parts = record.resolveSibling(record.getFileName() + ".parts").resolve(group);
      Files.createDirectories(parts);
      return Files.createTempFile(parts, "record-", ".jsonl");
    }
  }

  @Override
  public synchronized void onEvent(Object event) throws IOException {
    prepareReplay(event);
    if (out == null) {
      guard(event);
      return;
    }
    if (event instanceof org.apache.maven.execution.ExecutionEvent) {
      org.apache.maven.execution.ExecutionEvent execution = (org.apache.maven.execution.ExecutionEvent) event;
      try {
        if (execution.getType() == org.apache.maven.execution.ExecutionEvent.Type.MojoStarted) {
          DocumentationCapture.Invocation invocation = DocumentationCapture.started(execution);
          if (invocation != null) documentation.put(execution.getMojoExecution(), invocation);
        } else if (execution.getType() == org.apache.maven.execution.ExecutionEvent.Type.MojoSucceeded) {
          DocumentationCapture.Invocation invocation = documentation.remove(execution.getMojoExecution());
          if (invocation != null) invocation.write(out);
        } else if (execution.getType() == org.apache.maven.execution.ExecutionEvent.Type.MojoFailed
            || "MojoSkipped".equals(execution.getType().name())) {
          documentation.remove(execution.getMojoExecution());
        }
      } catch (Exception error) {
        out.write("{\"event\":\"javadoc-error\",\"message\":" + DocumentationCapture.json(error.toString()) + "}\n");
        out.flush();
      }
      return;
    }
    if (!(event instanceof RepositoryEvent)) {
      return;
    }
    RepositoryEvent e = (RepositoryEvent) event;
    if (e.getException() != null || e.getFile() == null) {
      return;
    }
    try {
      switch (e.getType()) {
        case ARTIFACT_DOWNLOADED:
          if (remote(e.getRepository()) != null) {
            download(remote(e.getRepository()), artifactPath(e.getArtifact()), e);
          }
          break;
        case METADATA_DOWNLOADED:
          if (remote(e.getRepository()) != null) {
            download(remote(e.getRepository()), metadataPath(e.getMetadata()), e);
          }
          break;
        case METADATA_RESOLVED:
          resolved(e);
          break;
        case ARTIFACT_INSTALLED:
          install(e.getArtifact(), e);
          break;
        default:
          break;
      }
    } catch (IOException error) {
      out.write("{\"event\":\"record-error\",\"message\":" + json(error.toString()) + "}\n");
      out.flush();
    }
  }

  // Both Maven 3 and Maven 4 dispatch the request before creating its
  // repository session. A nested Invoker request can be online even though
  // its parent resolver is offline: afterProjectsRead clears only the
  // parent's execution flag so requiresOnline goals can run. Establish the
  // child's resolver flag before Maven snapshots this request too.
  static void prepareReplay(Object event) {
    if (event instanceof org.apache.maven.execution.MavenExecutionRequest
        && Boolean.parseBoolean(System.getProperty("maven-purifier.replay"))) {
      ((org.apache.maven.execution.MavenExecutionRequest) event).setOffline(true);
    }
  }

  @Override
  public synchronized void close() throws IOException {
    if (out != null) {
      out.close();
      out = null;
    }
  }

  // Notes each artifact or metadata file that could not be resolved, and when
  // the build has failed, names them and the commands that add them.
  private void guard(Object event) {
    if (lockedMaven == null) {
      return;
    }
    if (event instanceof RepositoryEvent) {
      RepositoryEvent e = (RepositoryEvent) event;
      boolean failed = e.getException() != null;
      if (failed && e.getType() == RepositoryEvent.EventType.ARTIFACT_RESOLVED && e.getArtifact() != null) {
        unresolved.add(e.getArtifact().toString());
      } else if (failed && e.getType() == RepositoryEvent.EventType.METADATA_RESOLVED && e.getMetadata() != null) {
        unresolved.add(e.getMetadata().toString());
      }
    } else if (event instanceof MavenExecutionResult
        && ((MavenExecutionResult) event).hasExceptions()
        && !unresolved.isEmpty()) {
      StringBuilder names = new StringBuilder();
      int shown = 0;
      for (String name : unresolved) {
        if (shown++ == 5) {
          names.append(", and ").append(unresolved.size() - 5).append(" more");
          break;
        }
        names.append(shown == 1 ? "" : ", ").append(name);
      }
      System.err.println(
          "maven-purifier: Maven could not resolve "
              + names
              + " offline, because the lock does not hold "
              + (unresolved.size() == 1 ? "it" : "them")
              + ". Run `maven-purifier lock --force` with the goals and -P options you build with,"
              + " then `maven-purifier prefetch`.");
    }
  }

  private static RemoteRepository remote(ArtifactRepository repository) {
    return repository instanceof RemoteRepository ? (RemoteRepository) repository : null;
  }

  private void download(RemoteRepository repository, String path, RepositoryEvent e)
      throws IOException {
    // An install later in this reactor can replace this very repository path.
    // Preserve the successful transfer's bytes before returning to Maven.
    Path snapshot;
    try {
      Files.createDirectories(downloadDirectory);
      snapshot = Files.createTempFile(downloadDirectory, "download-", ".bin");
      Files.copy(e.getFile().toPath(), snapshot, StandardCopyOption.REPLACE_EXISTING);
    } catch (IOException error) {
      out.write("{\"event\":\"download-error\",\"url\":" + json(url(repository, path))
          + ",\"message\":" + json(error.toString()) + "}\n");
      out.flush();
      return;
    }
    out.write(
        "{\"event\":\"download\",\"repository\":"
            + json(repository.getId())
            + ",\"repositoryUrl\":"
            + json(repository.getUrl())
            + ",\"url\":"
            + json(url(repository, path))
            + ",\"file\":"
            + json(e.getFile().getAbsolutePath())
            + repositoryFields(e)
            + ",\"snapshot\":"
            + json(snapshot.toAbsolutePath().toString())
            + "}\n");
    out.flush();
  }

  // A metadata file the build read, whether it was downloaded now or lay in
  // the local repository already, as the metadata of the previous lock does.
  private void resolved(RepositoryEvent e) throws IOException {
    out.write("{\"event\":\"resolved\",\"file\":" + json(e.getFile().getAbsolutePath())
        + repositoryFields(e) + "}\n");
    out.flush();
  }

  private void install(Artifact a, RepositoryEvent e) throws IOException {
    out.write(
        "{\"event\":\"install\",\"groupId\":"
            + json(a.getGroupId())
            + ",\"artifactId\":"
            + json(a.getArtifactId())
            + ",\"version\":"
            + json(a.getVersion())
            + ",\"classifier\":"
            + json(a.getClassifier())
            + ",\"extension\":"
            + json(a.getExtension())
            + ",\"file\":"
            + json(e.getFile().getAbsolutePath())
            + repositoryFields(e)
            + "}\n");
    out.flush();
  }

  private static String repositoryFields(RepositoryEvent event) throws IOException {
    org.eclipse.aether.repository.LocalRepository local =
        event.getRepository() instanceof org.eclipse.aether.repository.LocalRepository
            ? (org.eclipse.aether.repository.LocalRepository) event.getRepository()
            : event.getSession().getLocalRepository();
    if (local == null) {
      throw new IOException("repository event has no local repository: " + event.getFile());
    }
    Path repository = local.getBasedir().toPath().toAbsolutePath().normalize();
    Path file = event.getFile().toPath().toAbsolutePath().normalize();
    if (!file.startsWith(repository)) {
      throw new IOException("repository event file is outside its local repository: " + file);
    }
    return ",\"localRepository\":" + json(repository.toString());
  }

  static String url(RemoteRepository repository, String path) {
    String base = repository.getUrl();
    return (base.endsWith("/") ? base : base + "/") + path;
  }

  // The path of an artifact in the Maven 2 repository layout. A SNAPSHOT
  // artifact's directory is its base version and its file name carries the
  // resolved timestamp version.
  static String artifactPath(Artifact a) {
    StringBuilder path = new StringBuilder();
    path.append(a.getGroupId().replace('.', '/')).append('/');
    path.append(a.getArtifactId()).append('/');
    path.append(a.getBaseVersion()).append('/');
    path.append(a.getArtifactId()).append('-').append(a.getVersion());
    if (!a.getClassifier().isEmpty()) {
      path.append('-').append(a.getClassifier());
    }
    if (!a.getExtension().isEmpty()) {
      path.append('.').append(a.getExtension());
    }
    return path.toString();
  }

  // The path of a metadata file in the Maven 2 repository layout: the
  // group, artifact and version directories it applies to, each only when
  // set, then its type, such as maven-metadata.xml.
  static String metadataPath(Metadata m) {
    StringBuilder path = new StringBuilder();
    if (!m.getGroupId().isEmpty()) {
      path.append(m.getGroupId().replace('.', '/')).append('/');
    }
    if (!m.getArtifactId().isEmpty()) {
      path.append(m.getArtifactId()).append('/');
    }
    if (!m.getVersion().isEmpty()) {
      path.append(m.getVersion()).append('/');
    }
    path.append(m.getType());
    return path.toString();
  }

  private static String json(String s) {
    StringBuilder b = new StringBuilder("\"");
    for (int i = 0; i < s.length(); i++) {
      char c = s.charAt(i);
      switch (c) {
        case '"':
          b.append("\\\"");
          break;
        case '\\':
          b.append("\\\\");
          break;
        default:
          if (c < 0x20) {
            b.append(String.format("\\u%04x", (int) c));
          } else {
            b.append(c);
          }
      }
    }
    return b.append('"').toString();
  }
}
