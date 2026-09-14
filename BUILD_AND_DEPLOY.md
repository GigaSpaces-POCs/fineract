# Build and Deploy Instructions

## Prerequisites

- **Java 21+** (Current environment has Java 17, needs upgrade)
- **Gradle** (included in repository)

## Step 1: Install Java 21

```bash
# Using Homebrew on macOS
brew install java@21

# Or download from:
# https://jdk.java.net/21/

# Verify installation:
java -version
```

## Step 2: Build Fineract

From the project root (`/Users/yoramweinreb/work/fineract/`):

```bash
# Build only the provider (faster than full build)
./gradlew :fineract-provider:build -x test

# Or full build with all modules:
./gradlew build -x test
```

Expected build time: 5-15 minutes depending on your machine

## Step 3: Verify Build Success

Look for output like:
```
BUILD SUCCESSFUL in XXXs
```

The WAR file will be at:
```
fineract-provider/build/libs/fineract-provider-1.x.x-SNAPSHOT.war
```

## Step 4: Deploy

### Option A: Docker Compose (Recommended)

If using docker-compose:

```bash
# Stop running container
docker-compose down

# Rebuild the Docker image
docker-compose build

# Start with new image
docker-compose up -d
```

### Option B: Direct Tomcat Deployment

```bash
# Copy WAR to Tomcat webapps
cp fineract-provider/build/libs/fineract-provider-1.x.x-SNAPSHOT.war \
   /path/to/tomcat/webapps/fineract-provider.war

# Restart Tomcat
./path/to/tomcat/bin/shutdown.sh
./path/to/tomcat/bin/startup.sh
```

### Option C: Gradle Task

```bash
# Some configurations support direct deployment
./gradlew :fineract-provider:bootRun
```

## Step 5: Verify Deployment

Once deployed, check that Prometheus metrics show normalized URIs:

```bash
# Query Prometheus metrics
curl -s "http://localhost:8443/fineract-provider/actuator/prometheus" | grep "http_server_requests_seconds_count"

# Expected output (normalized URIs, no UNKNOWN):
# http_server_requests_seconds_count{application="fineract",method="GET",status="200",uri="/api/v1/clients/{id}"}
# http_server_requests_seconds_count{application="fineract",method="GET",status="200",uri="/api/v1/loans/{id}"}
# etc.
```

Or query Prometheus:

```bash
curl "http://localhost:9090/api/v1/query?query=http_server_requests_seconds_count" | jq .
```

## Troubleshooting

### Build fails with "Dependency requires JVM runtime version 21"

**Solution**: Install Java 21
```bash
brew install java@21
export JAVA_HOME=$(/usr/libexec/java_home -v 21)
./gradlew --version  # Verify gradle sees Java 21
```

### Build fails with other errors

Clear Gradle cache and rebuild:
```bash
./gradlew clean
./gradlew :fineract-provider:build -x test
```

### Application won't start after deployment

Check logs:
```bash
# If using Docker
docker-compose logs fineract-server

# If using Tomcat
tail -f /path/to/tomcat/logs/catalina.out
```

Look for errors related to `MetricsConfig` class loading.

## Changes Made

This build includes the following improvement:

**File**: `fineract-provider/src/main/java/org/apache/fineract/infrastructure/core/config/MetricsConfig.java`

**Change**: Added URI normalization for Prometheus metrics
- Replaces numeric IDs in URIs with `{id}` placeholders
- Prevents "UNKNOWN" URIs in metrics
- Example: `/api/v1/clients/123` → `/api/v1/clients/{id}`

See [CHANGES_LOG.md](CHANGES_LOG.md) for full details.
