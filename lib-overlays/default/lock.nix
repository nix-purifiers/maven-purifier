# SPDX-License-Identifier: MIT
#
# readLock: the lock in memory of the purifier contract, read from a
# maven-purifier lockfile. The flake publishes it as
# `lib.maven-purifier.readLock`:
#
#   {
#     purifier = "maven-purifier";
#     entries  = [ <download entry> ];   # every locked download
#     outputs  = { <artifactId> = <project>; };
#     problems = [ <string> ];           # each naming the command that fixes it
#     maven    = { ... };                # what buildMavenProject reads besides
#   }
#
# A problem is a lockfile that is stale, tampered, malformed or of another
# format version. The reader returns the problems rather than failing, and
# the builder passes the result to the purifier library's checkLock, which
# fails with them. A lockfile that is not JSON at all fails in Nix's own
# JSON parser, which a reader cannot catch.
#
# The lockfile is JSON with these fields:
#
#   version              the lock format version (lock-version.nix)
#   lockHash             "sha256:" and the SHA-256, in hex, of the lockfile
#                        without this field, as builtins.toJSON writes it:
#                        keys sorted, no spaces. A lockfile someone edited
#                        by hand no longer matches it.
#   maven.version        the Maven version of the lock run; its distribution
#                        archive is the entry of `downloads` named
#                        apache-maven-<version>-bin.tar.gz or .zip
#   javaMajor            the Java major version of the JDK that ran the
#                        lock run
#   goals                the goals and options the lock run ran
#   settings             the settings the lock run used, without servers and
#                        proxies, as XML text; null when it used none
#   definitionHash       a hash of the build definition (every pom.xml and
#                        .mvn/) and the goals; see definition-hash.nix
#   projects             one entry per module the build installs: groupId,
#                        artifactId, version, and its artifacts, each with
#                        `key` (<artifactId>[-<classifier>].<extension>),
#                        classifier, extension and its local repository path
#   downloads            the download entries of the purifier library, less
#                        what Nix derives: file entries ({ path, url, hash })
#                        for the files Maven downloaded, at their paths in
#                        a Maven local repository, and inline entries for
#                        the metadata files it read. The reader adds the
#                        `mirrors` of every file entry: the other hosts of
#                        Maven Central for a file of Central, and none for
#                        any other. An entry with `development = true` is
#                        one that only development needs (source jars,
#                        plugins the build's goals do not run, the default
#                        plugins of an editor's Maven); `maven-purifier
#                        prefetch` fetches it and a sandboxed build leaves
#                        it out.
#
# Needs no nixpkgs.
{
  # The root of the Maven build.
  src,
  # The lockfile. It may lie outside `src`.
  lockFile ? src + "/maven-purifier.lock.json",
  # Compares the build definition with the one the lock was made from. The
  # comparison reads `src` at evaluation time, so it applies only when `src`
  # is a path.
  checkDefinition ? true,
}:
let
  inherit (builtins)
    isAttrs
    isList
    isString
    filter
    all
    ;

  relock = "Run `maven-purifier lock --force` to write it again.";
  invalid =
    problem:
    "maven-purifier: ${toString lockFile} is not a lockfile that maven-purifier wrote: ${problem}. ${relock}";

  formatVersion = import ./lock-version.nix;

  text = builtins.readFile lockFile;
  raw = if builtins.substring 0 1 text == "{" then builtins.fromJSON text else null;

  downloadProblem =
    i: d:
    let
      where = "downloads[${toString i}]";
    in
    if !isAttrs d then
      invalid "${where} is not an object"
    else if !isString (d.path or null) then
      invalid "${where} has no `path`"
    else if d ? contents then
      if isString d.contents then null else invalid "the `contents` of ${d.path} is not a string"
    else if !isString (d.url or null) then
      invalid "${d.path} has neither `url` nor `contents`"
    else if !isString (d.hash or null) then
      invalid "${d.path} has no `hash`"
    else
      null;

  projectProblem =
    p:
    if !(isAttrs p && isString (p.artifactId or null)) then
      invalid "a project has no `artifactId`"
    else if !isString (p.groupId or null) then
      invalid "project ${p.artifactId} has no `groupId`"
    else if !isString (p.version or null) then
      invalid "project ${p.artifactId} has no `version`"
    else if
      !(
        isList (p.artifacts or null)
        && all (a: isString (a.key or null) && isString (a.path or null)) p.artifacts
      )
    then
      invalid "project ${p.artifactId} has an artifact without `key` or `path`"
    else
      null;

  distributionName =
    name:
    name == "apache-maven-${raw.maven.version}-bin.tar.gz"
    || name == "apache-maven-${raw.maven.version}-bin.zip";
  distributions = filter (
    d: !(d.development or false) && distributionName (baseNameOf d.path)
  ) raw.downloads;

  # The first problem that keeps the lockfile from being read at all.
  shapeProblem =
    if !isAttrs raw then
      invalid "it is not a JSON object"
    else if (raw.version or null) != formatVersion then
      "maven-purifier: ${toString lockFile} has lock format version ${
        builtins.toJSON (raw.version or null)
      }, and this maven-purifier reads version ${toString formatVersion}. ${relock}"
    else if
      (raw.lockHash or null)
      != "sha256:${builtins.hashString "sha256" (builtins.toJSON (removeAttrs raw [ "lockHash" ]))}"
    then
      "maven-purifier: ${toString lockFile} does not match its lockHash, so it was changed after `maven-purifier lock` wrote it. ${relock}"
    else if !isList (raw.downloads or null) then
      invalid "it has no `downloads` list"
    else if !isList (raw.projects or null) then
      invalid "it has no `projects` list"
    else if !isList (raw.goals or null) then
      invalid "it has no `goals` list"
    else if !isString (raw.javaMajor or null) then
      invalid "it has no `javaMajor`"
    else if !isString (raw.definitionHash or null) then
      invalid "it has no `definitionHash`"
    else if !(isAttrs (raw.maven or null) && isString (raw.maven.version or null)) then
      invalid "it has no `maven.version`"
    else
      let
        problems =
          filter (p: p != null) (
            builtins.genList (i: downloadProblem i (builtins.elemAt raw.downloads i)) (
              builtins.length raw.downloads
            )
          )
          ++ filter (p: p != null) (map projectProblem raw.projects);
      in
      if problems != [ ] then
        builtins.head problems
      else if builtins.length distributions != 1 then
        invalid "`downloads` has no single entry for the distribution of Maven ${raw.maven.version}"
      else
        null;

  staleProblem =
    if
      checkDefinition
      && builtins.isPath src
      &&
        raw.definitionHash != import ./definition-hash.nix {
          inherit src;
          inherit (raw) goals;
        }
    then
      "maven-purifier: the build definition (a pom.xml, .mvn/ or the goals) changed since ${toString lockFile} was made. Run `maven-purifier lock` at the build root to lock it again."
    else
      null;

  problems =
    if shapeProblem != null then [ shapeProblem ] else filter (p: p != null) [ staleProblem ];
  usable = shapeProblem == null;

  # Maven Central serves the same files under each of these URLs (as
  # CENTRAL_HOSTS in cli/maven_purifier.py); the lock records the first.
  centralHosts = [
    "https://repo1.maven.org/maven2/"
    "https://repo.maven.apache.org/maven2/"
    "https://maven-central.storage-download.googleapis.com/maven2/"
  ];
  canonical = builtins.head centralHosts;
  withMirrors =
    d:
    if d ? contents then
      d
    else
      d
      // {
        mirrors =
          if builtins.substring 0 (builtins.stringLength canonical) d.url == canonical then
            map (host: host + builtins.substring (builtins.stringLength canonical) (-1) d.url) (
              builtins.tail centralHosts
            )
          else
            [ ];
      };

  entries = if usable then map withMirrors raw.downloads else [ ];
in
{
  purifier = "maven-purifier";
  inherit entries problems;
  outputs =
    if usable then
      builtins.listToAttrs (
        map (p: {
          name = p.artifactId;
          value = p;
        }) raw.projects
      )
    else
      { };
  maven =
    if usable then
      {
        inherit (raw)
          goals
          javaMajor
          projects
          definitionHash
          ;
        inherit (raw.maven) version;
        settings = raw.settings or null;
        # The path of the Maven distribution archive in the local repository.
        distribution = (builtins.head distributions).path;
      }
    else
      { };
}
