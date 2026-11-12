// SPDX-License-Identifier: MIT
package io.github.nixpurifiers.maven;

import java.io.IOException;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.nio.file.StandardOpenOption;
import java.util.LinkedHashSet;
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
  private String lockedMaven;
  private final Set<String> unresolved = new LinkedHashSet<>();

  @Override
  public void init(Context context) throws IOException {
    String record = System.getenv("MAVEN_PURIFIER_RECORD");
    if (record != null && !record.isEmpty()) {
      out =
          Files.newBufferedWriter(
              Paths.get(record),
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

  @Override
  public synchronized void onEvent(Object event) throws IOException {
    if (out == null) {
      guard(event);
      return;
    }
    if (event instanceof org.apache.maven.execution.ExecutionEvent) {
      org.apache.maven.execution.ExecutionEvent execution = (org.apache.maven.execution.ExecutionEvent) event;
      if (execution.getType() == org.apache.maven.execution.ExecutionEvent.Type.MojoSucceeded
          && execution.getMojoExecution() != null
          && "org.apache.maven.plugins".equals(execution.getMojoExecution().getGroupId())
          && "maven-javadoc-plugin".equals(execution.getMojoExecution().getArtifactId())) {
        try {
          java.io.File stale = new java.io.File(execution.getProject().getBuild().getDirectory(),
              "maven-javadoc-plugin-stale-data.txt");
          org.codehaus.plexus.util.xml.Xpp3Dom config = execution.getMojoExecution().getConfiguration();
          if (config != null && config.getChild("staleDataPath") != null) {
            Object value = new org.apache.maven.plugin.PluginParameterExpressionEvaluator(
                execution.getSession(), execution.getMojoExecution()).evaluate(config.getChild("staleDataPath").getValue());
            if (value != null) stale = value instanceof java.io.File ? (java.io.File) value : new java.io.File(value.toString());
          }
          DocumentationCapture.write(out, execution.getProject(), execution.getMojoExecution().getExecutionId(), stale);
        } catch (Exception error) {
          out.write("{\"event\":\"javadoc-error\",\"message\":" + DocumentationCapture.json(error.toString()) + "}\n");
          out.flush();
        }
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
    out.write(
        "{\"event\":\"download\",\"repository\":"
            + json(repository.getId())
            + ",\"repositoryUrl\":"
            + json(repository.getUrl())
            + ",\"url\":"
            + json(url(repository, path))
            + ",\"file\":"
            + json(e.getFile().getAbsolutePath())
            + "}\n");
    out.flush();
  }

  // A metadata file the build read, whether it was downloaded now or lay in
  // the local repository already, as the metadata of the previous lock does.
  private void resolved(RepositoryEvent e) throws IOException {
    out.write("{\"event\":\"resolved\",\"file\":" + json(e.getFile().getAbsolutePath()) + "}\n");
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
            + "}\n");
    out.flush();
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
