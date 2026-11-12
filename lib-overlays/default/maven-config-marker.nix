# The line of .mvn/maven.config from which on `maven-purifier prefetch`
# writes lines of its own (PREFETCH_MARKER in cli/maven_purifier.py). It is
# a property definition that nothing reads, since maven.config has no
# comments.
"-Dmaven-purifier.prefetched=true"
