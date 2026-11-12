# Top-50 upstream validation

The target is working Maven capture, prefetched developer builds, release
artifacts and sandboxed tests for two ranked sets of 50 Maven-using projects:
GitHub and Maven Central. The sets and their executions remain incomplete.
Passing one upstream or a synthetic fixture does not establish either set.

Freeze ranking evidence, eligibility decisions, repository revisions and build
roots before aggregating passes. GitHub stars supply the GitHub ranking. A
root POM identifies a candidate; repositories with nested builds still need
review. A POM used only as a test fixture does not by itself establish a
project's Maven build. Keep excluded candidates and reasons, rather than
replacing difficult builds with easier projects.

Maven Central needs a separately recorded popularity metric, dated ranking,
Central coordinates and source mappings. Deduplicate modules of the same
upstream project and continue through the ranking until there are 50 eligible
projects. A consumer resolving 50 dependencies does not prove that those
projects build through maven-purifier.

## Individual project runner

Run from this checkout, keeping execution evidence outside it:

```sh
python3 -B tests/real-world/run-project.py \
  --purifier "$PWD" \
  --repository https://github.com/TheAlgorithms/Java.git \
  --revision c46808958406c3b74726542fc9ad7e8038fe924e \
  --results "$TMPDIR/algorithms-java-observation"
```

The results directory must be new. The runner creates a fresh HOME and source
checkout, including submodules, pins the purifier's immutable source with Nix,
and records commands, durations, logs, hashes, lock digest and output paths.
It runs `clean install` by default. Explicit trailing goals change the tested
workflow and are recorded in the report; use upstream's documented workflow
and retain the reason for any project-specific arguments or build root.
`--settings` can name private task-local Maven proxy settings. Maven strips
proxy/server credentials when recording settings in the lock. Keep private
settings files outside the checkout and shared artifacts. Plugins that use
Java's URL protocol handlers may additionally need JVM proxy properties;
Maven Resolver itself uses the settings proxies.

The phases are networked capture, prefetch, the generated offline Maven
launcher, and Nix release and test builds with `sandbox=true`. Maven offline
mode is distinct from enforced host network denial. Nix may substitute an
existing identical output; inspect the execution log before claiming a fresh
build. The test result includes Surefire/Failsafe report counts, executed
tests, skips, errors and failures. An empty report directory does not count
as executed tests. For a publication-only parent with no tests, explicitly
record that scope with `--minimum-tests 0`.

`selectedProjectPassed` covers the selected revision, build root and goals.
It does not prove ranking eligibility, all SDKs, publication to a remote
repository, independent rebuilds or the complete top-50 sets. Failed or
timed-out phases retain partial evidence and return nonzero. Classify native
upstream failures against an ordinary native baseline before attributing them
to purifier.

The `bundle` fixture retains the capture regression found with Guava: Felix
generates a local `repository.xml` index during install. Only an index of
artifacts installed by that same reactor is treated as generated metadata;
unknown downloads, external resources and invalid indexes remain errors.
The `bundle-release` check builds the captured fixture offline, and native CI
relocks it from a fresh local repository.
