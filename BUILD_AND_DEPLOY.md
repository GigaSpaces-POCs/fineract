# Build and Deploy Instructions

Builds the `fineract` container image and runs it alongside the local
observability stack (Prometheus, Tempo, Grafana) and a Kafka broker.

## Prerequisites

- **JDK 25.** `build.gradle` pins the Gradle toolchain to Java 25, and Gradle
  itself must *run* on 21+ because two buildscript plugins
  (`cucumber-runner`, `swagger-brake`) require it at configuration time. There
  is no toolchain resolver configured, so Gradle will not download a JDK for
  you - it must already be installed.
- **Docker** with the Compose plugin (`docker compose`, v2+). The image is
  built straight into the Docker daemon, so a daemon is required even for the
  build step.
- Gradle itself is provided by the wrapper in this repository.

Verify the toolchain is visible before building:

```bash
java -version            # expect 25.x
./gradlew javaToolchains # expect an entry for JDK 25
```

If your system default is an older JDK, either point Gradle at 25 for this
user:

```bash
echo 'org.gradle.java.home=/path/to/jdk-25' >> ~/.gradle/gradle.properties
```

or export `JAVA_HOME=/path/to/jdk-25` for the build.

## Step 1: Build the image

From the repository root:

```bash
./gradlew :fineract-provider:jibDockerBuild -x test
```

This uses [jib](https://github.com/GoogleContainerTools/jib) - there is no
Dockerfile in this repository - and produces `fineract:latest` plus a
version-tagged image in the local Docker daemon. Expect roughly 3-5 minutes on
a warm Gradle cache.

Confirm the image exists:

```bash
docker images fineract
```

## Step 2: Prepare the log bind mount

The compose files run the container as `${FINERACT_USER:-1000}` and bind-mount
`build/fineract/logs`. If your host UID is not 1000, that directory is not
writable by the container. This only matters once file logging is switched on
(`config/docker/env/debug.env` sets `-Dlogging.config`), but it costs nothing
to get right up front:

```bash
mkdir -p build/fineract/logs
printf 'FINERACT_USER=%s\nFINERACT_GROUP=%s\n' "$(id -u)" "$(id -g)" > .env
```

Compose reads `.env` from the repository root automatically. Note that
`FINERACT_USER` in `config/docker/env/fineract-common.env` does **not** control
this - that file is an `env_file`, which only injects variables into the
running container and is never consulted for Compose's own `${...}`
substitution.

## Step 3: Start the stack

```bash
docker compose up -d
```

`docker-compose.override.yml` is picked up automatically alongside
`docker-compose.yml` and adds Prometheus, Tempo, Grafana and Kafka.

First start runs the full Liquibase migration; the application needs roughly
two minutes before it answers. Watch progress with:

```bash
docker compose logs -f fineract
```

It is ready when the log reports `Started ServerApplication`.

## Step 4: Verify

```bash
docker compose ps                       # all services up, fineract healthy
curl -sk https://localhost:8443/fineract-provider/actuator/prometheus | head
```

Check that request URIs are normalized rather than reported as `UNKNOWN`:

```bash
curl -sk https://localhost:8443/fineract-provider/actuator/prometheus \
  | grep '^http_server_requests_seconds_count'
```

Expect entries such as `uri="/api/v1/offices/{id}"`, and no `uri="UNKNOWN"`.

Endpoints:

| Service    | URL                                            |
| ---------- | ---------------------------------------------- |
| Fineract   | https://localhost:8443/fineract-provider/api/v1 |
| Grafana    | http://localhost:3000 (admin/admin)            |
| Prometheus | http://localhost:9090                          |
| Tempo      | http://localhost:3200                          |

API credentials are `mifos` / `password` with the header
`Fineract-Platform-TenantId: default`.

## Troubleshooting

**`Dependency requires at least JVM runtime version 21. This build uses a Java 17 JVM.`**
Gradle is running on too old a JDK. See Prerequisites - this is about the JVM
Gradle runs on, not the toolchain it compiles with.

**`No matching toolchain found for JavaLanguageVersion 25`**
JDK 25 is installed but Gradle cannot see it. Check `./gradlew javaToolchains`
and set `org.gradle.java.home` as above.

**Application will not start.** `docker compose logs fineract`. If it exits
during migration, check that `db` is healthy first - `fineract` waits on its
healthcheck.

**Kafka topic stays empty.** External event *types* are all disabled by
default (`m_external_event_configuration`), independently of
`FINERACT_EXTERNAL_EVENTS_KAFKA_ENABLED`. Enable the ones you need:

```bash
curl -sk -X PUT https://localhost:8443/fineract-provider/api/v1/externalevents/configuration \
  -H 'Fineract-Platform-TenantId: default' -H 'Content-Type: application/json' \
  -u mifos:password \
  -d '{"externalEventConfigurations":{"ClientCreateBusinessEvent":true}}'
```

## Changes in this branch

See [CHANGES_LOG.md](CHANGES_LOG.md). In short: HTTP server request URIs are
resolved and normalized in
`FineractServerRequestObservationConvention`, which feeds both metrics and
traces, with `MetricsConfig` applying a cardinality backstop.
