# Log4j native Docker proof

The dedicated `log4j-native` job runs on ordinary PR **opened** and
**synchronize** events only when the PR changes its dedicated workflow, script
or this document, with a 45-minute job limit. Each additional head/retry needs
separate authorization; a result covers only its recorded head. Unrelated PRs
do not start this job. Existing repository PR checks continue normally.

The harness records the PR checkout independently from the tested purifier,
which is pinned to public commit `301ee83db0a6ddde7d4ea4f385fa8d33ee4af5be`
and source NAR `sha256-rB8d3s58bog0Q436qLdx9+oe3wrgNjjiUtB8+xbYluM=`.
It builds an exact-revision local Git flake reference, rather than assuming
that a metadata store path is already materialized. It preserves the source
NAR check and never rewrites the lockfile, and binds the CLI
output/derivation/NAR to the source, and supplies explicit Zulu Java 17 plus an
owned writable JVM home.

The native capture uses Log4j commit
`28117e55f69892d3454672f9399e53fb7b74a321`, `CI=true`, and the full
`-DtrimStackTrace=false -DinstallAtEnd=true clean install` reactor. It requires
all 31 reactor successes, exact recorded goals/runtime, real Fabric8 starts,
executed successful Logstash/Mongo integration suites, and actual linux/amd64
image identities. Upstream profiles, services and readiness limits stay intact.

Docker must already work on the hosted runner. The job does not start or
reconfigure its daemon, change sysctls, skip tests, or manually substitute
services. Actual upstream image pulls occur through Fabric8. Logs, the native
lock and identity receipts are uploaded even on failure. Runner capacity and
Docker API/image compatibility are observations, not assumed guarantees.

This job proves only native capture. The separate upstream `site` step,
native offline replay and a pure Nix Docker fixture remain separate obligations.
No sandboxed/pure success is inferred from access to the hosted Docker daemon.
