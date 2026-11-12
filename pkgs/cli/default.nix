# SPDX-License-Identifier: MIT
#
# The `maven-purifier` command (cli/maven_purifier.py). Its wrapper names
#
#   MAVEN_PURIFIER_EXTENSION          the Maven extension jar, which records
#                                     the downloads of a lock run and guards
#                                     an offline Maven after a prefetch
#   MAVEN_PURIFIER_FLAKE              this flake's source, whose
#                                     overlays.default adds the scope
#                                     whose buildMavenProject `prefetch`
#                                     and `publish` build with
#   MAVEN_PURIFIER_DEFAULT_MAVEN      the Maven version a build without a
#                                     Maven wrapper locks
#   MAVEN_PURIFIER_EDITOR_MAVEN       the Maven version that jdtls embeds,
#                                     whose default plugins the lock holds
#                                     for the editor
#   MAVEN_PURIFIER_DEFAULT_JAVA_HOME  the JDK of a lock run when JAVA_HOME
#                                     is unset
{
  lib,
  stdenvNoCC,
  makeWrapper,
  unzip,
  python3,
  jdk_headless,
  jdk8_headless,
  jdt-language-server,
  maven,
  version,
  extension,
  purifier ? lib.purifier,
}:
let
  # The flake, as the command evaluates it with builtins.getFlake.
  flake = lib.fileset.toSource {
    root = ../..;
    fileset = lib.fileset.unions [
      ../../flake.nix
      ../../flake.lock
      ../../configs
      ../../lib-overlays
      ../../pkg-overlays
      ../../pkgs
      ../../cli
      ../../extension
      ../../tests
    ];
  };
in
stdenvNoCC.mkDerivation {
  pname = "maven-purifier";
  inherit version;
  src = lib.fileset.toSource {
    root = ../..;
    fileset = ../../cli;
  };
  nativeBuildInputs = [
    makeWrapper
    unzip
  ];
  dontConfigure = true;
  dontBuild = true;
  installPhase = ''
    runHook preInstall
    # The Maven of the editor: the maven-core inside jdtls's m2e runtime.
    runtime=$(echo ${jdt-language-server}/share/java/jdtls/plugins/org.eclipse.m2e.maven.runtime_*.jar)
    editorMaven=$(unzip -Z1 "$runtime" | sed -n 's|^jars/maven-core-\(.*\)\.jar$|\1|p')
    if [ -z "$editorMaven" ]; then
      echo "cannot find the Maven version of jdtls in $runtime" >&2
      exit 1
    fi
    install -Dm644 cli/maven_purifier.py $out/libexec/maven-purifier/maven_purifier.py
    makeWrapper ${python3.interpreter} $out/bin/maven-purifier \
      --add-flags $out/libexec/maven-purifier/maven_purifier.py \
      --prefix PYTHONPATH : ${purifier.projectInputsTool} \
      --set MAVEN_PURIFIER_EXTENSION ${extension}/share/java/maven-purifier-extension.jar \
      --set MAVEN_PURIFIER_FLAKE ${flake} \
      --set MAVEN_PURIFIER_DEFAULT_MAVEN ${maven.version} \
      --set MAVEN_PURIFIER_EDITOR_MAVEN "$editorMaven" \
      --set MAVEN_PURIFIER_DEFAULT_JAVA_HOME ${jdk_headless.home} \
      --set MAVEN_PURIFIER_LEGACY_JAVA_HOME ${jdk8_headless.home}
    runHook postInstall
  '';
  meta = {
    description = "Lock the downloads of a Maven build, prefetch them for offline work, and publish what Nix built";
    mainProgram = "maven-purifier";
  };
}
