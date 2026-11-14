// SPDX-License-Identifier: MIT
package io.github.nixpurifiers.maven;

import java.util.Properties;
import org.apache.maven.model.Model;
import org.apache.maven.plugin.MojoExecutionException;
import org.apache.maven.project.MavenProject;

public final class DefaultTimestampMojoTest {
  private static MavenProject project(Properties effective, Properties original) {
    Model model = new Model();
    model.setProperties(effective);
    MavenProject project = new MavenProject(model);
    Model source = new Model();
    source.setProperties(original);
    project.setOriginalModel(source);
    return project;
  }

  private static Properties properties(String value) {
    Properties properties = new Properties();
    properties.setProperty(DefaultTimestampMojo.PROPERTY, value);
    return properties;
  }

  private static void check(boolean value, String message) {
    if (!value) throw new AssertionError(message);
  }

  public static void main(String[] args) throws Exception {
    String key = DefaultTimestampMojo.PROPERTY;
    Properties shared = new Properties();
    MavenProject absent = project(shared, shared);
    MavenProject sibling = project(shared, shared);
    check(DefaultTimestampMojo.applyDefault(absent, null, null), "absent value not defaulted");
    check(DefaultTimestampMojo.DEFAULT.equals(absent.getProperties().getProperty(key)), "wrong default");
    check(!shared.containsKey(key), "shared/source properties mutated");
    check(!sibling.getProperties().containsKey(key), "sibling properties mutated");
    check(!absent.getOriginalModel().getProperties().containsKey(key), "original model polluted");
    check(!DefaultTimestampMojo.applyDefault(absent, null, null), "default changed on second call");

    for (String value : new String[] {"2026-08-26T21:49:35Z", "${git.commit.time}", "", "x", "315532802"}) {
      Properties authored = properties(value);
      MavenProject selected = project(authored, new Properties());
      check(!DefaultTimestampMojo.applyDefault(selected, null, null), "authored value replaced: " + value);
      check(value.equals(selected.getProperties().getProperty(key)), "authored value changed");
      MavenProject cli = project(new Properties(), new Properties());
      check(!DefaultTimestampMojo.applyDefault(cli, authored, null), "user property ignored");
      check(!DefaultTimestampMojo.applyDefault(cli, null, authored), "system property ignored");
      check(!cli.getProperties().containsKey(key), "global value copied into project");
      Properties inherited = new Properties(authored);
      check(!DefaultTimestampMojo.applyDefault(project(inherited, new Properties()), null, null),
          "Properties defaults ignored");
      check(!DefaultTimestampMojo.applyDefault(project(new Properties(), new Properties()), inherited, null),
          "user Properties defaults ignored");
    }

    Model aliased = new Model();
    MavenProject unsafe = new MavenProject(aliased);
    unsafe.setOriginalModel(aliased);
    try {
      DefaultTimestampMojo.applyDefault(unsafe, null, null);
      throw new AssertionError("aliased original model was mutated");
    } catch (MojoExecutionException expected) {
      check(!aliased.getProperties().containsKey(key), "aliased model was mutated before rejection");
    }
    System.out.println("Default timestamp property/original-model controls passed");
  }
}
