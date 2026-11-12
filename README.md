# maven-purifier

maven-purifier keeps Maven's development workflow on locked,
Nix-resolved dependencies. Use the normal build and test commands in your
working tree; building the project itself in the Nix sandbox is an
optional next step.

## Quickstart

From the project root:

```zsh
nix shell 'github:nix-purifiers/maven-purifier'
maven-purifier lock
maven-purifier prefetch
mvn test
mvn package
```

Commit the lockfiles that the lock command writes, including the build
tool's own lockfile when it has one. Keep using the project's usual build,
test and editor commands with this setup. Nix provides the locked
dependencies; the development commands above run outside the Nix sandbox.
No project Nix files are needed for this workflow.

Sandboxed builds of the whole project are an optional
[next step](#building-the-project-with-nix).

For persistent use, add these packages through your usual Nix configuration.


## Development loop

After `maven-purifier prefetch` (see [Working offline](#working-offline)), you run
Maven as usual: `mvn clean install`, `mvn test`, `mvn package`, `./mvnw
verify`. Maven resolves from the locked files, offline.

To add or upgrade a dependency or a plugin, edit the POM and lock again:

```zsh
nix shell 'github:nix-purifiers/maven-purifier'
maven-purifier lock
maven-purifier prefetch
```

A relock keeps what version ranges and SNAPSHOTs resolved to in the
previous lock: the lock run starts from the metadata files the lockfile
recorded. To move them to what the repositories hold now, update instead,
for every package or for the ones you name:

```zsh
nix shell 'github:nix-purifiers/maven-purifier'
maven-purifier update
maven-purifier update \
  org.slf4j:slf4j-api
```

`maven-purifier update` locks with the goals that the lockfile recorded.
Maven keeps no lockfile of its own, so `maven-purifier.lock.json` is the one
file that `update` changes.

maven-purifier runs Maven with a recording core extension. Maven 2 and 3.0
use a recording goal instead, as described under [Maven versions](#maven-versions).

`maven-purifier lock` copies the build to a temporary directory and runs
Maven there with an empty local repository and an empty home directory. The
default goals are `clean install`; pass others as arguments, with any Maven
options they need, such as `maven-purifier lock clean deploy -Prelease`.
The lock holds what those goals need, so lock the goals you run. The lock
run also runs the tests, so the lock holds what they need even when a Nix
build skips them. It locks again only when a `pom.xml`, anything under
`.mvn/`, the goals or the settings changed; `--force` locks regardless. A
relock of an unchanged build writes the same lockfile, byte for byte. It
keeps the recorded resolution of version ranges and SNAPSHOTs. It also
keeps the recorded Java major version, and runs on the nixpkgs JDK of that
version whatever `JAVA_HOME` names. A first lock runs on `JAVA_HOME`, or on
the JDK that maven-purifier's nixpkgs provides (Java 8 for Maven 2 and 3.0). To move to another Java
version, delete the lockfile and lock again.

## Maven versions

Maven 3 and Maven 4 are supported, along with Maven 2.2.1. CI builds and
runs tests with 2.2.1, 3.0.5, 3.3.9, 3.8.8, 3.9.11 and 4.0.0-rc-7,
including relocking, offline development and Nix sandbox builds.
The Maven wrapper's distribution URL selects the version to lock.

For Maven older than 3.3.1, use the prefetched launcher, since those
versions do not read `.mvn/maven.config`:

```zsh
export PATH="$PWD/.maven-purifier/bin:$PATH"
mvn clean install
```

That launcher selects the locked JDK too. A first lock defaults to Java 8
for Maven 2 and 3.0; `JAVA_HOME` can select another JDK. The plugins in your
POM must also support the Maven and Java versions you select.

Maven 2 and 3.0 lack repository download events. Their lock runs use a
recorder goal and verify each downloaded file against its remote origin.
Repositories must permit that verification; authenticated repositories
requiring Maven-specific transports are not yet supported by this legacy
recording path. Maven 3.1 and newer use the core extension's events.

## Published recording extension

The [Maven repository](https://nix-purifiers.github.io/maven-purifier/maven/) publishes
`io.github.nix-purifiers:maven-purifier-extension` at each release version.
This is the download recorder used during locking. The `maven-purifier`
wrapper in the Quickstart supplies the complete locked development workflow.

## Building the project with Nix

Once the native development workflow is working, you can also build and
test the whole project in the Nix sandbox. For this step, keep a flake or
another Nix build definition in the project's repository, or maintain an
external packaging repository that points at its sources and lockfiles.
The builder consumes the same lockfiles as the development workflow.

A project whose authors want a flake that packages it can add one, in the
project's repository or in another one. The flake calls the builder
directly:

```nix
{
  inputs.maven-purifier.url =
    "github:nix-purifiers/maven-purifier";
  inputs.nixpkgs.follows = "maven-purifier/nixpkgs";

  outputs =
    { nixpkgs, maven-purifier, ... }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs {
        inherit system;
        overlays = [ maven-purifier.overlays.default ];
      };
      project = pkgs.maven-purifier.buildMavenProject {
        src = ./.;
      };
    in
    {
      packages.${system}.app = project.packages.app;
      checks.${system}.tests = project.tests;
    };
}
```

`nix build '.#app'` builds module `app`. A package in an overlay takes the
scope by name, as the argument `maven-purifier`.

The builder's result has:

- `packages.<artifactId>`: the files the module installs, in Maven layout.
  `artifacts.<key>` names each file, where the key is
  `<artifactId>[-<classifier>].<extension>`, such as
  `packages.core.artifacts."core-sources.jar"`. The lockfile lists them, so
  Nix knows every output at evaluation time, without building first.
- `tests`: the build with its tests, whose output holds the test reports.
- `release`: every artifact the build installs, built with the tests
  skipped.
- `projections.maven`: an immutable Maven repository with Nix-derived
  versions and matching dependency metadata; `maven-purifier publish`
  deploys this repository, including supplied Nix dependencies.
- `fetches`: the download entries the build fetches, for SBOM, license and
  vulnerability tools.

The builder also takes `fetchers` (a company mirror or credentials go into
`fetchers.fetchurl`), `lockFile` (a lockfile outside `src`), `jdk` (the
default is nixpkgs' headless JDK of the Java major version the lock was made
with), `mavenFlags`, `srcFilter` and `checkDefinition`;
`pkgs/build-maven-project/default.nix` documents them.
`maven-purifier.buildMavenProject.override` replaces the packages the
builder takes, such as `javaPackages` or `stdenvNoCC`, and
`maven-purifier.overrideScope` replaces what the scope shares, such as
`fetchurl`.

To use another purified JVM package without downloading its artifacts:

```zsh
maven-purifier lock --input github:your-org/library#library
maven-purifier prefetch
mvn test
```

Native input activation requires Maven 3 or newer. Maven before 3.3.1
uses the prefetched `mvn` wrapper because it does not read `.mvn/maven.config`.
Repeat `--input` for more packages. The lock records Nix's pinned source
identity; `update` refreshes it. Maven discovers the supplied modules from
their Maven or translated Ivy publications and selects them even when the
POM requests a different version. Source POMs remain unchanged. Published
POMs name the actual selections so ordinary Maven downstreams can consume
the deployed repository. A Nix builder can instead pass package derivations
as `projectDependencies = [ library ];`.

To deploy what Nix built, with the target from the root POM's
`distributionManagement` and the credentials from `~/.m2/settings.xml`:

```zsh
nix shell 'github:nix-purifiers/maven-purifier'
maven-purifier publish
```

`--repository-id ID --url URL` names another target.

A build whose repository cannot hold the lockfile, such as a project you
package without being able to commit to it, can keep the lockfile outside
it. The builder then takes the lockfile as `lockFile`. Every command takes
`--lockfile FILE`, or reads the path from the environment variable
`MAVEN_PURIFIER_LOCKFILE`; `--lockfile` takes precedence. A dev shell can
set the variable to the path it passes to the builder as `lockFile`.

### One-off builds without a project Nix file

For an ad hoc build, the builder can also be evaluated directly from the
project root. Replace the example package name with one from your project:

```zsh
nix build --impure --expr 'let
  purifier = builtins.getFlake "github:nix-purifiers/maven-purifier";
  pkgs = import purifier.inputs.nixpkgs {
    system = builtins.currentSystem;
    overlays = [ purifier.overlays.default ];
  };
in
(pkgs.maven-purifier.buildMavenProject { src = ./.; })
.packages.app'
```

This uses the purifier's pinned nixpkgs, matching the lock command above.
To build with ambient `<nixpkgs>` from your Nix search path instead,
replace `import purifier.inputs.nixpkgs` with `import <nixpkgs>`.
Both forms need no Nix files in your project. The build tool and language
versions must match the lockfile; with ambient nixpkgs, lock with the
purifier from that package set or pass a compatible toolchain to the builder.

## Working offline

On Maven 3.1 and newer (including Maven 4), locked replay keeps Maven's repository resolver offline while
allowing goals marked `requiresOnline` to execute. Otherwise Maven silently
skips goals such as CycloneDX and SPDX generation, changing the published
artifacts. Missing repository downloads still fail offline; a plugin that uses
its own network client must also work without that network in a Nix build.
The extension refuses replay if its repository session is online. This replay
adapter does not change Maven 2 or 3.0's execution behavior.

```zsh
nix shell 'github:nix-purifiers/maven-purifier'
maven-purifier prefetch
```

`maven-purifier prefetch` fetches everything the lockfile lists into the
Nix store and keeps it there with a garbage-collector root in
`.maven-purifier/` at the build root, where Maven reads the locked
repository. For a lockfile outside the build root, the root stays in the
build root rather than beside the lockfile. Besides what the build downloads, the lock holds what
day-to-day development asks for: every plugin the build declares, the source
jars of its dependencies, and the default plugins of the Maven inside the
editor. The command copies the locked repository to
`.maven-purifier/repository`, writes `.maven-purifier/settings.xml` with
offline mode on, and adds lines of its own at the end of
`.mvn/maven.config`, after the line `-Dmaven-purifier.prefetched=true`.
Maven reads that file for every build, so `mvn`, `./mvnw` and an editor
that reads it resolve from the locked files. The lines of `prefetch` take
no part in the build definition, and a lock run or a Nix build leaves them
out; delete them to resolve online again.

The lines also load maven-purifier's extension, which warns when a Maven of
another version runs: its default plugin versions differ from the locked
ones. The locked Maven is `.maven-purifier/bin/mvn`, and nixpkgs' JDK of the
locked Java major version is `.maven-purifier/jdk`. Put
`.maven-purifier/bin` on `PATH`, or use a Maven wrapper that names the
locked version.

## Editor

An editor that imports a Maven build, such as VS Code or Neovim through
jdtls, or IntelliJ IDEA, runs a Maven of its own. After a prefetch, make
`.maven-purifier/settings.xml` the editor's Maven user settings,
`java.configuration.maven.userSettings` for jdtls or *User settings file*
in IntelliJ IDEA, and the editor resolves from the locked files without the
network: dependencies, their source jars, and the plugins of its own Maven.

The lock holds the default plugins of the Maven that jdtls embeds, which
the lock run reads from the jdtls of maven-purifier's nixpkgs. An editor whose Maven has other
default plugin versions finds them missing offline, unless the build pins
the version of every plugin it uses in `<pluginManagement>`, as we
recommend; then every Maven asks for the locked versions.

## Troubleshooting

Each common mistake fails with a message that says what went wrong and what
to run.

- When a pom.xml, `.mvn/` or the lock goals changed after locking, a Nix
  build fails at evaluation, and `maven-purifier prefetch` and `publish`
  fail, saying that the build definition changed and to run
  `maven-purifier lock`.
- When a build needs a dependency or plugin that the lock does not hold, a
  Nix build fails with Maven's offline error and adds that the lock does
  not hold everything the build needs, and to run `maven-purifier lock
  --force` with the goals and `-P` options of the build. After a prefetch,
  Maven itself names the files it could not resolve and the same command.
- When someone edited the lockfile, it no longer matches its `lockHash`,
  and a Nix build and every command fail, saying so and asking for
  `maven-purifier lock --force`. A lockfile that is valid JSON but misses a
  field names the field. A lockfile that is not JSON fails the commands
  with the parse error and the same command; for a Nix build, Nix's own JSON
  parser reports it.
- When a lock run sees a file reach the local repository without a download
  that Maven reported, it fails with the list of such files: a plugin
  downloaded them itself. Declare them in `.mvn/maven-purifier.json`, as
  [Supported language repositories](#supported-language-repositories)
  shows.

## Supported language repositories

Each source of dependencies reaches the lock as follows.

- **Repositories declared in POMs, plugins and their dependencies,
  profiles and version ranges.** Maven Resolver downloads them during the
  lock run, and the extension records each file with its URL. Profiles
  count as they were active in the lock run; `-P` options belong among the
  lock goals.
- **Repositories, mirrors and profiles in `settings.xml`.** The lock run
  uses `~/.m2/settings.xml`, or the file that `--settings FILE` names. The
  lockfile records the settings without `<servers>`, `<proxies>`,
  `<localRepository>` and `<offline>`, and a Nix build and a prefetched
  Maven use the recorded settings, whose repository and mirror ids the names
  of metadata files depend on.
- **Core extensions** (`.mvn/extensions.xml`). Maven resolves them before it
  loads the recording extension, so the lock takes each of their URLs from
  Maven Resolver's tracking file, which names the repository a file came
  from.
- **Metadata files** (`maven-metadata.xml`), which version ranges,
  SNAPSHOTs and plugin prefixes read. Their URLs serve new contents
  whenever a version is published, so the lockfile holds the contents the
  lock run read.
- **SNAPSHOT dependencies.** The lockfile holds the timestamped files the
  lock run resolved, under both names Maven Resolver stores them under.
- **Files a plugin downloads itself**, outside Maven Resolver, such as the
  Node.js of frontend-maven-plugin. Maven reports nothing about them, so the
  build declares each with its URL and its path in the local repository, in
  `.mvn/maven-purifier.json`, and the lock run fetches it:

  ```json
  { "downloads": [ { "url": "https://…/node-v22.0.0-linux-x64.tar.gz",
                     "path": "com/github/eirslett/node/22.0.0/node-22.0.0-linux-x64.tar.gz" } ] }
  ```

- **Maven itself.** The version the build's Maven wrapper names in
  `.mvn/wrapper/maven-wrapper.properties`, or else the Maven of
  maven-purifier's nixpkgs. Its distribution archive is locked like any
  other download, so the sandboxed build runs the same Maven.
- **Repositories that need credentials.** The lock run authenticates with
  the `<servers>` of the settings, which the lockfile leaves out. A Nix
  build fetches their files through `fetchers.fetchurl`, such as nixpkgs'
  `fetchurl` with `netrcPhase`, or through Nix's own `netrc-file` setting.

A file of Maven Central is recorded under repo1.maven.org, whichever of
Central's hosts served it, and Nix fetches it from repo.maven.apache.org or
Google's copy of Central when repo1 fails. `MAVEN_PURIFIER_CENTRAL_MIRROR`
makes a lock run download through another copy of Central without changing
the lockfile. The lock run copies the proxy from `https_proxy` and
`http_proxy` into its settings when they name none.

The sandboxed build runs the locked goals with
`deploy` replaced by `install`, since Maven refuses to deploy offline, and
`release.publish` deploys the Maven projection. JAR bytes stay unchanged;
publication versions include the Nix producer identity, and POM references
use the selected versions. The local
repository holds copies rather than links into the store, since Maven 3.10
treats a file as absent when its real path differs from its path in the
repository. Every run turns Maven Resolver 2's prefix filter off, since its
list of groups changes as groups are published and offline it saves
nothing. The jars of a Nix build get a fixed timestamp, so a rebuild gives
the same bytes.

## Examples

For day-to-day work on these projects, use the native-tool setup and
commands in [Quickstart](#quickstart) and [Development loop](#development-loop).
The Nix expressions below show optional sandboxed packaging of those
projects.

### Gson

[Gson](https://github.com/google/gson) is Google's JSON library, a Maven
build of several modules. Its `proto` module runs protoc, a native binary
that Maven downloads for the machine it runs on; the lock holds protoc for
both Linux systems and aarch64-darwin.

From a clone of Gson, lock the build and prefetch it:

```zsh
nix shell 'github:nix-purifiers/maven-purifier'
maven-purifier lock
maven-purifier prefetch
```

Commit `maven-purifier.lock.json`. The locked Maven now builds and tests
Gson offline on the locked JDK, and an editor that reads
`.maven-purifier/settings.xml` imports it offline:

```zsh
JAVA_HOME=$PWD/.maven-purifier/jdk .maven-purifier/bin/mvn \
  clean install
```

From the same directory, Nix builds the `gson` module with this command:

```zsh
nix build --impure --expr 'let
  purifier = builtins.getFlake "github:nix-purifiers/maven-purifier";
  pkgs = import purifier.inputs.nixpkgs {
    system = builtins.currentSystem;
    overlays = [ purifier.overlays.default ];
  };
in
(pkgs.maven-purifier.buildMavenProject { src = ./.; })
.packages.gson'
```

`packages.gson-extras` and `packages.proto` select the other modules,
`release` selects every artifact Gson installs, and `tests` runs Gson's
tests in the Nix sandbox. After a change to a POM, lock and prefetch again.

## Credits

The purifier idea was inspired by nixcloud's
[cargo (libnix)](https://github.com/nixcloud/cargo).
