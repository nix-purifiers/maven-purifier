// SPDX-License-Identifier: MIT
package io.github.nixpurifiers.maven;

import org.apache.maven.MavenExecutionException;
import org.apache.maven.execution.DefaultMavenExecutionRequest;
import org.apache.maven.execution.DefaultMavenExecutionResult;
import org.apache.maven.execution.MavenSession;
import org.eclipse.aether.DefaultRepositorySystemSession;

public final class LockedReplayParticipantTest {
  public static void main(String[] args) throws Exception {
    DefaultRepositorySystemSession repository = new DefaultRepositorySystemSession();
    repository.setOffline(true);
    DefaultMavenExecutionRequest request = new DefaultMavenExecutionRequest();
    request.setOffline(true);
    MavenSession session = new MavenSession(null, repository, request, new DefaultMavenExecutionResult());
    LockedReplayParticipant participant = new LockedReplayParticipant();
    participant.afterProjectsRead(session);
    if (!request.isOffline()) throw new AssertionError("ordinary offline Maven changed");
    System.setProperty("maven-purifier.replay", "true");
    participant.afterProjectsRead(session);
    if (request.isOffline()) throw new AssertionError("online-marked goals still skipped");
    if (!repository.isOffline()) throw new AssertionError("resolver network access enabled");
    repository.setOffline(false);
    try {
      participant.afterProjectsRead(session);
      throw new AssertionError("accepted an online resolver");
    } catch (MavenExecutionException expected) {
      if (!expected.getMessage().contains("locked goal replay")) throw expected;
    } finally {
      System.clearProperty("maven-purifier.replay");
    }
  }
}
