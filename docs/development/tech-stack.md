# Tech stack

Extends the workspace `docs/development/tech-defaults.md`.

- **The recording extension** (`extension/`) is one Java class, an
  `org.apache.maven.eventspy.EventSpy`, compiled for Java 8 against the
  Maven API jars of nixpkgs' Maven. Maven discovers it through the Sisu
  index `META-INF/sisu/javax.inject.Named` when the lock run passes the jar
  in `-Dmaven.ext.class.path`. It reads `RepositoryEvent`s, whose API is
  the same in Maven 3.9 (Maven Resolver 1.9) and Maven 3.10 (Maven
  Resolver 2).
- **The command** (`cli/maven_purifier.py`) is Python with the standard
  library only, run by nixpkgs' Python through the wrapper that
  `pkgs/cli` makes. It calls `nix store prefetch-file`, `nix-build`,
  `nix-instantiate` and `nix-store` from the user's Nix installation.
- **The library overlay** (`lib-overlays/default/`) provides the lock reader
  and its pure Nix helpers.
- **The project builder** (`pkgs/build-maven-project/`) builds locked Maven
  projects. Its private Python helpers live beside the builder that runs them.
- **caisson** composes the flake, library overlays and package sets.
