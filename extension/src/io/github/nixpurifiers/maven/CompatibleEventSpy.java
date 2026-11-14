// SPDX-License-Identifier: MIT
package io.github.nixpurifiers.maven;

import javax.inject.Named;
import javax.inject.Singleton;
import org.apache.maven.eventspy.AbstractEventSpy;
import org.apache.maven.eventspy.EventSpy;

/** Loads the Eclipse Aether recorder without linking it into older Maven containers. */
@Named("maven-purifier")
@Singleton
public final class CompatibleEventSpy extends AbstractEventSpy {
  private EventSpy delegate;

  @Override
  public void init(Context context) throws Exception {
    ClassLoader loader = getClass().getClassLoader();
    try {
      Class.forName("org.eclipse.aether.RepositoryEvent", false, loader);
    } catch (ClassNotFoundException legacyAether) {
      // Maven 3.0 uses Sonatype Aether and the CLI's legacy recorder goal.
      return;
    }
    delegate =
        (EventSpy)
            Class.forName("io.github.nixpurifiers.maven.RecordingEventSpy", true, loader)
                .getDeclaredConstructor()
                .newInstance();
    delegate.init(context);
  }

  @Override
  public void onEvent(Object event) throws Exception {
    if (delegate != null) delegate.onEvent(event);
  }

  @Override
  public void close() throws Exception {
    if (delegate != null) delegate.close();
  }
}
