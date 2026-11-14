# Log4j native Maven offline replay

This proposed follow-up consumes the validated native artifact13963552297 from
run43257513258/head75c7886, verifies its API metadata and exact ZIP/lock hashes,
and checks fresh Log4j28117e55 source against the captured build-definition hash.
It never relocks. Tested purifier301ee83d and its metadata NAR, actual CLI
output/derivation/NAR, Java17 vendor/version/build and source inputs remain joined.
The original Java binary hash was not captured: runtime matching is version/build
matching, not an assertion of identical original JDK bytes.

Online preparation pulls the three recorded linux/amd64 image digests, verifies
their original image IDs, restores the upstream tag aliases, and runs actual
`maven-purifier prefetch` against the captured lock. No POM/profile changes or
service substitutes are permitted. Prefetch's generated Maven configuration is
expected and its actual product freshness helper ignores only those generated
lines; original POMs and the lock must remain unchanged.

The test invokes the generated `-o` launcher with the full captured clean-install
goals and CI=true. It requires all31 reactor modules, real Fabric8 starts/stops,
executed successful Logstash/Mongo suites, stable image identities, no observed
image-pull event during replay, and no remaining containers. TERM/INT handling tracks owned command groups and keeps leaders unreaped until
bounded cleanup. The event collector is owned/stopped by the proof; failures retain evidence and rely on disposable
runner teardown rather than global Docker cleanup.

This is native **Maven offline-mode** replay with cached real services. It does
not deny all plugin/daemon network traffic at kernel level and establishes no
site, pure Nix, or complete-project qualification. Each future synchronization
job needs separate authorization; cumulative PR path filters are not one-shot.
