// SPDX-License-Identifier: MIT
package io.github.nixpurifiers.maven;

import javax.inject.Named;
import javax.inject.Singleton;
import org.apache.maven.AbstractMavenLifecycleParticipant;
import org.apache.maven.MavenExecutionException;
import org.apache.maven.execution.MavenSession;

/** Run online-marked goals against the already-created offline repository session. */
@Named("purifier-locked-replay")
@Singleton
public final class LockedReplayParticipant extends AbstractMavenLifecycleParticipant {
  @Override
  public void afterProjectsRead(MavenSession session) throws MavenExecutionException {
    if (!Boolean.parseBoolean(System.getProperty("maven-purifier.replay"))) return;
    try {
      // Maven 3.0 uses Sonatype Aether; newer Maven uses Eclipse Aether.
      // Reflect only across that API boundary, without changing either session.
      Object repository = MavenSession.class.getMethod("getRepositorySession").invoke(session);
      if (!Boolean.TRUE.equals(repository.getClass().getMethod("isOffline").invoke(repository))) {
        throw new IllegalStateException("the locked repository session must remain offline");
      }
      // Maven snapshots the resolver's offline flag before this hook. The
      // execution request is a separate flag: leaving it offline silently
      // skips requiresOnline goals such as CycloneDX, losing locked outputs.
      session.getRequest().setOffline(false);
    } catch (Exception error) {
      throw new MavenExecutionException("maven-purifier: cannot enable locked goal replay", error);
    }
  }
}
