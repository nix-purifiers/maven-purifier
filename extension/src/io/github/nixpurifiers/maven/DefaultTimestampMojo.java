// SPDX-License-Identifier: MIT
package io.github.nixpurifiers.maven;

import java.lang.reflect.InvocationTargetException;
import java.util.Properties;
import org.apache.maven.execution.MavenSession;
import org.apache.maven.plugin.AbstractMojo;
import org.apache.maven.plugin.MojoExecutionException;
import org.apache.maven.project.MavenProject;

/** Supplies a reproducible default without replacing the project's own timestamp choices. */
public final class DefaultTimestampMojo extends AbstractMojo {
  static final String PROPERTY = "project.build.outputTimestamp";
  static final String DEFAULT = "1980-01-01T00:00:02Z";
  private MavenSession session;
  private MavenProject project;

  @Override public void execute() throws MojoExecutionException {
    if (session == null) throw new MojoExecutionException("The Maven session is unavailable");
    Properties systemProperties;
    try {
      // Maven 2 supplies a combined execution-properties view instead.
      systemProperties = (Properties) session.getClass().getMethod("getSystemProperties").invoke(session);
    } catch (NoSuchMethodException olderMaven) {
      systemProperties = session.getExecutionProperties();
    } catch (IllegalAccessException | InvocationTargetException error) {
      throw new MojoExecutionException("Cannot read Maven system properties", error);
    }
    if (project == null) throw new MojoExecutionException("The Maven project is unavailable");
    applyDefault(project, session.getUserProperties(), systemProperties);
  }

  static boolean present(Properties properties) {
    // getProperty also observes inherited Properties defaults, including empty strings.
    return properties != null
        && (properties.containsKey(PROPERTY) || properties.getProperty(PROPERTY) != null);
  }

  static boolean applyDefault(MavenProject project, Properties user, Properties system)
      throws MojoExecutionException {
    Properties effective = project.getProperties();
    if (present(user) || present(system) || present(effective)) return false;
    if (project.getModel() == project.getOriginalModel()) {
      throw new MojoExecutionException("Cannot default a timestamp on an aliased original Maven model");
    }
    // A copied map also protects original/sibling models if a builder shares properties.
    Properties selected = (Properties) effective.clone();
    selected.setProperty(PROPERTY, DEFAULT);
    project.getModel().setProperties(selected);
    return true;
  }
}
