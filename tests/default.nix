# SPDX-License-Identifier: MIT
#
# The flake's checks. Every one builds in the sandbox; the only network
# access is the fixed-output fetches of locked downloads. The conformance-*
# checks are those of the purifier contract; conformance-relock needs the
# network and runs as a CI job of that name.
{
  pkgs,
  # The flake's default package overlay, which adds the maven-purifier scope.
  overlay,
  cli,
  extension,
}:
let
  inherit (pkgs) lib;

  # Every fixture builds the way a consumer's package does: through
  # buildMavenProject of the scope in a package set the overlay applies to.
  withOverlay = pkgs.extend overlay;
  build = withOverlay.maven-purifier.buildMavenProject;

  # A reactor of a parent POM and two modules. core depends on
  # commons-lang3 and installs a sources jar beside its jar; app depends on
  # core, and its JUnit 5 test runs in `tests`.
  multi = build { src = ./fixtures/multi; };

  onlineGoal = build { src = ./fixtures/online-goal; };

  # Felix writes repository.xml while installing bundles. It is generated
  # local metadata, and must not be mistaken for a missing download.
  bundle = build { src = ./fixtures/bundle; };

  toolchainJdk17 = pkgs.javaPackages.compiler.openjdk17.headless;
  toolchainJdk21 = pkgs.temurin-bin-21;
  toolchainProbe = build {
    src = ./fixtures/multi;
    jdkToolchains = [
      {
        jdk = toolchainJdk17;
        provides = {
          version = "17";
          vendor = "A&B <vendor>";
        };
      }
      {
        jdk = toolchainJdk21;
        provides.version = "21";
      }
    ];
  };
  invalidToolchainsRejected =
    lib.all
      (
        value:
        !((builtins.tryEval (
          (build {
            src = ./fixtures/multi;
            jdkToolchains = value;
          }).prefetch.drvPath
        )).success
        )
      )
      [
        "not-a-list"
        [
          {
            jdk = toolchainJdk17;
            provides.version = 17;
          }
        ]
        [
          {
            jdk = toolchainJdk17;
            provides = {
              version = "17";
              "bad<tag" = "x";
            };
          }
        ]
      ];
  toolchainCheck =
    assert invalidToolchainsRejected;
    pkgs.runCommand "maven-jdk-toolchains" { nativeBuildInputs = [ pkgs.python3 ]; } ''
      python3 - ${toolchainProbe.prefetch}/toolchains.xml ${toolchainJdk17.home} ${toolchainJdk21.home} <<'PYTHON'
      import sys, xml.etree.ElementTree as ET
      root = ET.parse(sys.argv[1]).getroot()
      chains = root.findall('toolchain')
      assert len(chains) == 2
      assert [c.findtext('type') for c in chains] == ['jdk', 'jdk']
      assert [c.findtext('provides/version') for c in chains] == ['17', '21']
      assert chains[0].findtext('provides/vendor') == 'A&B <vendor>'
      assert [c.findtext('configuration/jdkHome') for c in chains] == sys.argv[2:]
      PYTHON
      touch $out
    '';

  # The lock names the artifacts of each package, so a consumer reaches
  # them without building first. app runs from the jars of the packages
  # and the locked commons-lang3.
  multiRuns =
    pkgs.runCommand "maven-purifier-test-multi-runs"
      {
        nativeBuildInputs = [ multi.jdk ];
      }
      ''
        test -f ${multi.packages.core.artifacts."core-sources.jar"}
        test -f ${multi.packages.core.artifacts."core.pom"}
        cp=${multi.packages.app.artifacts."app.jar"}
        cp=$cp:${multi.packages.core.artifacts."core.jar"}
        cp=$cp:${multi.repository}/org/apache/commons/commons-lang3/3.17.0/commons-lang3-3.17.0.jar
        out_text=$(java -cp "$cp" example.multi.app.Main nix)
        if [ "$out_text" != "Hello, Nix!" ]; then
          echo "app printed '$out_text'" >&2
          exit 1
        fi
        touch $out
      '';

  # The publish script deploys the files of the release to a file:
  # repository, with the maven-deploy-plugin of the lock.
  multiPublish = pkgs.runCommand "maven-purifier-test-multi-publish" { } ''
    export HOME=$TMPDIR/home
    mkdir -p $HOME
    ${multi.release.publish}/bin/maven-purifier-publish --repository-id local --url file://$TMPDIR/published
    ${pkgs.python3}/bin/python - ${multi.projections.maven.catalog} "$TMPDIR/published" <<'PYTHON'
    import json, pathlib, sys
    modules = json.load(open(sys.argv[1]))['modules']
    root = pathlib.Path(sys.argv[2])
    for module in modules:
        directory = root / module['group'].replace('.', '/') / module['name'] / module['version']
        prefix = module['name'] + '-' + module['version']
        assert (directory / (prefix + '.pom')).is_file()
        if module['name'] != 'multi-parent':
            assert (directory / (prefix + '.jar')).is_file()
        if module['name'] == 'core':
            assert (directory / (prefix + '-sources.jar')).is_file()
    PYTHON
    touch $out
  '';

  # A build with every kind of dependency source; see the comment in its
  # pom.xml. Its lock took the SNAPSHOT from tests/snapshot-repository on
  # localhost:18080, so the check fetches those files from the committed
  # repository instead, by their locked hashes, through `fetchers`.
  snapshotUrl = "http://localhost:18080/";
  fromSnapshotRepository =
    {
      name,
      url ? null,
      urls ? [ url ],
      hash,
      ...
    }@args:
    let
      first = builtins.head urls;
    in
    if lib.hasPrefix snapshotUrl first then
      pkgs.runCommand name
        {
          outputHash = hash;
          outputHashMode = "flat";
        }
        ''
          cp ${./snapshot-repository}/${lib.removePrefix snapshotUrl first} $out
        ''
    else
      pkgs.fetchurl args;
  sources = build {
    src = ./fixtures/sources;
    fetchers.fetchurl = fromSnapshotRepository;
  };

  # A build whose Maven wrapper names Maven 3.9.11 as a zip: the lock run
  # and the sandboxed build both use that distribution.
  wrapper = build { src = ./fixtures/wrapper; };
  wrapperVersion =
    assert wrapper.maven.version == "3.9.11";
    pkgs.runCommand "maven-purifier-test-wrapper-version" { JAVA_HOME = wrapper.jdk.home; } ''
      ${wrapper.maven}/bin/mvn --version | grep -q 'Apache Maven 3.9.11'
      test -f ${wrapper.packages.wrapped.artifacts."wrapped.jar"}
      touch $out
    '';

  # Locks edited in Nix, each with a lockHash that matches it, so that the
  # check meant for it is the one that fails.
  multiLock = builtins.fromJSON (builtins.readFile ./fixtures/multi/maven-purifier.lock.json);
  rehash =
    l:
    l
    // {
      lockHash = "sha256:${
        builtins.hashString "sha256" (builtins.toJSON (removeAttrs l [ "lockHash" ]))
      }";
    };
  lockFileOf = l: builtins.toFile "maven-purifier.lock.json" (builtins.toJSON l);
  isLang3 = d: lib.hasSuffix "commons-lang3-3.17.0.jar" d.path;
  evaluates = value: (builtins.tryEval (builtins.seq value true)).success;

  # A lockfile older than the build definition fails at evaluation; the
  # build is the wrapper fixture's lock with another pom.xml.
  conformanceStale =
    assert
      !evaluates
        (build {
          src = ./negative/stale;
          lockFile = ./fixtures/wrapper/maven-purifier.lock.json;
        }).release.drvPath;
    pkgs.runCommand "maven-purifier-conformance-stale" { } "touch $out";

  # A lockfile with a hash changed after locking no longer matches its
  # lockHash, and fails at evaluation.
  conformanceTampered =
    let
      tampered = multiLock // {
        downloads = map (
          d: if isLang3 d then d // { hash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="; } else d
        ) multiLock.downloads;
      };
    in
    assert
      !evaluates
        (build {
          src = ./fixtures/multi;
          lockFile = lockFileOf tampered;
        }).release.drvPath;
    pkgs.runCommand "maven-purifier-conformance-tampered" { } "touch $out";

  # A valid lockfile without an entry the build needs: Maven fails offline,
  # and the build names the command to run. The check runs the release
  # build and requires that failure.
  conformanceMissing =
    let
      missing = build {
        src = ./fixtures/multi;
        lockFile = lockFileOf (
          rehash (multiLock // { downloads = builtins.filter (d: !isLang3 d) multiLock.downloads; })
        );
      };
    in
    missing.release.overrideAttrs (old: {
      name = "maven-purifier-conformance-missing";
      buildPhase = ''
        if ( set -e; ${old.buildPhase} ) > $TMPDIR/build.log 2>&1; then
          echo "the build without commons-lang3 in its lock succeeded" >&2
          exit 1
        fi
        grep -F 'run `maven-purifier lock --force`' $TMPDIR/build.log
      '';
      installPhase = "touch $out";
    });

  # Lockfiles that are not valid fail at evaluation, naming the field and
  # the command. Each has a lockHash that matches it.
  invalidLocks = {
    wrong-version = rehash (multiLock // { version = 999; });
    no-downloads = rehash (removeAttrs multiLock [ "downloads" ]);
    no-projects = rehash (removeAttrs multiLock [ "projects" ]);
    no-maven = rehash (removeAttrs multiLock [ "maven" ]);
    no-java = rehash (removeAttrs multiLock [ "javaMajor" ]);
    entry-without-hash = rehash (
      multiLock
      // {
        downloads = map (d: if isLang3 d then removeAttrs d [ "hash" ] else d) multiLock.downloads;
      }
    );
    entry-without-url = rehash (
      multiLock
      // {
        downloads = map (d: if isLang3 d then removeAttrs d [ "url" ] else d) multiLock.downloads;
      }
    );
    no-distribution = rehash (
      multiLock
      // {
        maven = multiLock.maven // {
          version = "0.0.0";
        };
      }
    );
    project-without-artifact-id = rehash (
      multiLock // { projects = map (p: removeAttrs p [ "artifactId" ]) multiLock.projects; }
    );
    not-an-object = [ 1 ];
  };
  # readLock returns each problem rather than failing, and the builder
  # fails with them.
  problemsOf =
    l:
    (import ../lib-overlays/default/lock.nix {
      src = ./fixtures/multi;
      lockFile = lockFileOf l;
    }).problems;
  conformanceMalformed =
    assert problemsOf multiLock == [ ];
    assert lib.all (
      name:
      let
        problems = problemsOf invalidLocks.${name};
      in
      problems != [ ] && lib.all (lib.hasInfix "maven-purifier lock") problems
    ) (builtins.attrNames invalidLocks);
    assert
      !evaluates
        (build {
          src = ./fixtures/multi;
          lockFile = lockFileOf invalidLocks.entry-without-hash;
        }).release.drvPath;
    pkgs.runCommand "maven-purifier-conformance-malformed" { } "touch $out";

  # The release, built a second time, gives the same bytes: the jars get a
  # fixed timestamp.
  conformanceRebuild =
    let
      again = multi.release.overrideAttrs { MAVEN_PURIFIER_REBUILD = "1"; };
    in
    pkgs.runCommand "maven-purifier-conformance-rebuild" { } ''
      diff -r ${multi.release} ${again}
      touch $out
    '';

  # After `maven-purifier prefetch`, the everyday loop works with no
  # network, which the sandbox has none of: plain `mvn` builds and tests
  # the multi fixture through the lines that prefetch wrote into
  # .mvn/maven.config. The prefetch is the one this build made, which
  # `--prefetched` hands to the command in place of building it.
  conformanceOffline =
    pkgs.runCommand "maven-purifier-conformance-offline"
      {
        nativeBuildInputs = [
          pkgs.coreutils
          multi.jdk
        ];
      }
      ''
        export HOME=$TMPDIR/home JAVA_HOME=${multi.jdk.home}
        mkdir -p $HOME
        cp -r ${./fixtures/multi} $TMPDIR/multi
        chmod -R u+w $TMPDIR/multi
        cd $TMPDIR/multi
        ${cli}/bin/maven-purifier prefetch --prefetched ${multi.prefetch}
        grep -qx -- '-Dmaven-purifier.prefetched=true' .mvn/maven.config
        .maven-purifier/bin/mvn -B -ntp clean install 2>&1 | tee $TMPDIR/mvn.log
        grep -q 'Tests run: 2, Failures: 0' $TMPDIR/mvn.log
        test -f app/target/app-1.0.0.jar
        touch $out
      '';

  # A build whose source holds no lockfile, built from one kept outside it.
  external = build {
    src = ./fixtures/external/src;
    lockFile = ./fixtures/external/maven-purifier.lock.json;
  };
  conformanceExternal =
    assert !builtins.pathExists ./fixtures/external/src/maven-purifier.lock.json;
    external.packages.external;

  # A real project: Gson at its release gson-parent-2.15.0, a reactor of a
  # library, its extras, a protobuf module that runs a protoc of each
  # system, and tests of JPMS and code shrinking. Its source comes from
  # GitHub unchanged, and its lockfile lies here, outside it.
  gsonSource = pkgs.fetchFromGitHub {
    owner = "google";
    repo = "gson";
    rev = "fcd416909db2bea96eb916bcfb2d0a83cb9b86c3";
    hash = "sha256-Z6Ciixpi415JzF96ZACH9YR2Aq15uS/oPz1TxhQGRUQ=";
  };
  gson = build {
    src = gsonSource;
    lockFile = ./real-world/gson/maven-purifier.lock.json;
  };

  # Execute the ordinary Maven workflow in a networkless derivation too.
  # The networked capture/relock job prepares the same committed lock.
  gsonOffline =
    pkgs.runCommand "maven-purifier-gson-offline"
      {
        nativeBuildInputs = [
          pkgs.python3
          gson.jdk
        ];
      }
      ''
        export HOME=$TMPDIR/home JAVA_HOME=${gson.jdk.home}
        mkdir -p "$HOME" "$out"
        cp -r ${gsonSource} $TMPDIR/gson
        chmod -R u+w $TMPDIR/gson
        cd $TMPDIR/gson
        ${cli}/bin/maven-purifier prefetch \
          --lockfile ${./real-world/gson/maven-purifier.lock.json} \
          --prefetched ${gson.prefetch}
        export PATH=$PWD/.maven-purifier/bin:$PATH
        mvn --version > "$out/toolchain.txt"
        mvn -B -ntp clean install > "$out/maven.log" 2>&1
        python3 ${./real-world/gson/check-reports.py} . | tee "$out/tests.txt"
        junit=$(python3 - <<'PY'
        from pathlib import Path
        import xml.etree.ElementTree as ET
        managed = Path('.mvn/maven.config').read_text().split('-Dmaven-purifier.prefetched=true\n', 1)[1].splitlines()
        settings = Path(next(line.split('=', 1)[1] for line in managed if line.startswith('--settings=')))
        repository = Path(next(e.text for e in ET.parse(settings).getroot().iter() if e.tag.rsplit('}', 1)[-1] == 'localRepository'))
        print(repository / 'junit/junit/4.13.2/junit-4.13.2.jar')
        PY
        )
        test -f "$junit"
        mv "$junit" $TMPDIR/withheld-junit.jar
        if mvn -B -ntp -pl gson test > "$out/missing-artifact.log" 2>&1; then
          echo 'Maven accepted a missing locked test dependency' >&2
          exit 1
        fi
        grep -i 'offline' "$out/missing-artifact.log"
        grep 'junit:junit' "$out/missing-artifact.log"
      '';

  # The unit tests of the lock tool: settings, tracking files, mirrors,
  # paths, the lockfile and definition hashes, declared downloads and the
  # deploy target.
  unitCli =
    pkgs.runCommand "maven-purifier-test-unit-cli"
      {
        nativeBuildInputs = [ pkgs.python3 ];
        MAVEN_PURIFIER_CLI = "${../cli}/maven_purifier.py";
      }
      ''
        python3 -B -m unittest discover -s ${./cli} -p 'test_*.py'
        python3 -B -m unittest discover -s ${./real-world} -p 'test_*.py'
        touch $out
      '';

  # The unit tests of the extension: the remote path of an artifact or
  # metadata file, and the URL it joins with its repository.
  unitExtension =
    pkgs.runCommand "maven-purifier-test-unit-extension"
      {
        nativeBuildInputs = [ pkgs.jdk_headless ];
      }
      ''
        mkdir classes src
        # javac wants each public class in a file of its name.
        cp ${../extension/src/io/github/nixpurifiers/maven/RecordingEventSpy.java} src/RecordingEventSpy.java
        cp ${./extension/io/github/nixpurifiers/maven/RecordingEventSpyTest.java} src/RecordingEventSpyTest.java
        cp ${../extension/src/io/github/nixpurifiers/maven/LockedReplayParticipant.java} src/LockedReplayParticipant.java
        cp ${../extension/src/io/github/nixpurifiers/maven/DocumentationCapture.java} src/DocumentationCapture.java
        cp ${../extension/src/io/github/nixpurifiers/maven/DocumentationLinks.java} src/DocumentationLinks.java
        cp ${./extension/io/github/nixpurifiers/maven/LockedReplayParticipantTest.java} src/LockedReplayParticipantTest.java
        cp ${./extension/io/github/nixpurifiers/maven/DocumentationLinksTest.java} src/DocumentationLinksTest.java
        cp ${./extension/io/github/nixpurifiers/maven/DocumentationCaptureTest.java} src/DocumentationCaptureTest.java
        cp ${../extension/src/io/github/nixpurifiers/maven/ProjectedVersions.java} src/ProjectedVersions.java
        cp ${./extension/io/github/nixpurifiers/maven/ProjectedVersionsTest.java} src/ProjectedVersionsTest.java
        javac -Xlint:-deprecation -d classes -cp "${pkgs.maven}/maven/lib/*" src/*.java
        java -cp "classes:${pkgs.maven}/maven/lib/*" io.github.nixpurifiers.maven.RecordingEventSpyTest
        java -cp "classes:${pkgs.maven}/maven/lib/*" io.github.nixpurifiers.maven.LockedReplayParticipantTest
        java -cp "classes:${pkgs.maven}/maven/lib/*" io.github.nixpurifiers.maven.DocumentationLinksTest
        java -cp "classes:${pkgs.maven}/maven/lib/*:${pkgs.maven}/maven/boot/*" io.github.nixpurifiers.maven.DocumentationCaptureTest
        java -cp "classes:${pkgs.maven}/maven/lib/*" io.github.nixpurifiers.maven.ProjectedVersionsTest
        touch $out
      '';

  # The extension jar holds the class Maven loads and the index that names
  # it, and the command answers --help.
  packaging =
    pkgs.runCommand "maven-purifier-test-packaging"
      {
        nativeBuildInputs = [ pkgs.jdk_headless ];
      }
      ''
        jar=${extension}/share/java/maven-purifier-extension.jar
        jar tf $jar | grep -qx 'io/github/nixpurifiers/maven/RecordingEventSpy.class'
        jar tf $jar | grep -qx 'io/github/nixpurifiers/maven/CompatibleEventSpy.class'
        index=$(jar xf $jar META-INF/sisu/javax.inject.Named && cat META-INF/sisu/javax.inject.Named)
        grep -qx io.github.nixpurifiers.maven.CompatibleEventSpy <<<"$index"
        grep -qx io.github.nixpurifiers.maven.ProjectedModelProcessor <<<"$index"
        grep -qx io.github.nixpurifiers.maven.ProjectedLifecycleParticipant <<<"$index"
        ${cli}/bin/maven-purifier --help | grep -q 'maven-purifier prefetch'
        touch $out
      '';

  # A package set with a consumer's package, written for callPackage, that
  # takes the builder by name, as a package in an overlay does. It builds
  # the same derivation as multi-app.
  withConsumer = withOverlay.extend (
    final: prev: {
      example = final.callPackage (
        { maven-purifier }: (maven-purifier.buildMavenProject { src = ./fixtures/multi; }).packages.app
      ) { };
    }
  );

  # buildMavenProject.override replaces what the builder takes from the
  # scope: here the JDKs, whose JDK of the lock's Java major version
  # becomes the build's JDK.
  markerJdk = pkgs.runCommand "marker-jdk" { } "mkdir $out";
  javaPackagesWithMarker = {
    compiler."openjdk${multi.lock.maven.javaMajor}".headless = markerJdk;
  };
  overridden = withOverlay.maven-purifier.buildMavenProject.override {
    javaPackages = javaPackagesWithMarker;
  };

  # overrideScope replaces what the scope shares: here the fetcher, which
  # the builder and the other packages of the scope all take.
  markerFetchurl = args: pkgs.runCommand args.name { } "mkdir $out";
  scopeOverridden =
    (withOverlay.maven-purifier.overrideScope (final: prev: { fetchurl = markerFetchurl; }))
    .buildMavenProject;

  nixLibrary =
    assert withConsumer.example.drvPath == multi.packages.app.drvPath;
    assert (overridden { src = ./fixtures/multi; }).jdk.outPath == markerJdk.outPath;
    assert (scopeOverridden { src = ./fixtures/multi; }).repository.drvPath != multi.repository.drvPath;
    pkgs.runCommand "maven-purifier-test-nix-library" { } "touch $out";
in
{
  # A producer needs a complete runtime view even with no Nix project inputs.
  standalone-projection = pkgs.runCommand "maven-standalone-projection" { } ''
    test -n "$(find ${multi.projections.maven}/org/apache/commons/commons-lang3 -name '*.jar')"
    touch $out
  '';
  jvm-projection = multi.projections.maven;

  composition = import ./composition.nix {
    inherit pkgs build extension;
    purifier = withOverlay.lib.purifier;
  };
  multi-app = multi.packages.app;
  multi-core = multi.packages.core;
  multi-tests = multi.tests;
  unicode-paths = multi.tests.overrideAttrs (old: {
    name = "maven-unicode-path-tests";
    preBuild = (old.preBuild or "") + ''
      cp ${./unicode-paths/UnicodePathTest.java} app/src/test/java/example/multi/app/UnicodePathTest.java
    '';
  });
  bundle-release = bundle.packages.bundle;
  jdk-toolchains = toolchainCheck;
  online-goal = pkgs.runCommand "maven-purifier-online-goal" { } ''
    bom=${onlineGoal.packages.online-goal.artifacts."online-goal-cyclonedx.json"}
    grep -q 'commons-lang3' "$bom"
    test -f ${onlineGoal.packages.online-goal.artifacts."online-goal-cyclonedx.xml"}
    touch $out
  '';
  multi-runs = multiRuns;
  multi-publish = multiPublish;
  sources = sources.packages.sources;
  sources-tests = sources.tests;
  # The download that .mvn/maven-purifier.json declares is in the local
  # repository, at the path it declares.
  sources-declared = pkgs.runCommand "maven-purifier-test-sources-declared" { } ''
    grep -q '<artifactId>slf4j-api</artifactId>' ${sources.repository}/declared/notice/1.0/notice-1.0.xml
    touch $out
  '';
  wrapper-version = wrapperVersion;
  real-world-gson = gson.packages.gson;
  real-world-gson-proto = gson.packages.proto;
  real-world-gson-tests = gson.tests;
  real-world-gson-offline = gsonOffline;
  conformance-stale = conformanceStale;
  conformance-tampered = conformanceTampered;
  conformance-missing = conformanceMissing;
  conformance-malformed = conformanceMalformed;
  conformance-rebuild = conformanceRebuild;
  conformance-offline = conformanceOffline;
  conformance-external = conformanceExternal;
  unit-cli = unitCli;
  unit-extension = unitExtension;
  nix-library = nixLibrary;
  inherit packaging;
}
