# Fineract Changes Log

This document tracks all modifications made to the Fineract codebase, including the rationale and date for each change.

---

## Change #1: URI Normalization to Prevent "UNKNOWN" in Prometheus Metrics

**Date**: 2026-09-01 (Revised 2026-09-02)  
**Files Modified**: 
- `fineract-provider/src/main/java/org/apache/fineract/infrastructure/core/config/MetricsConfig.java`
- `fineract-provider/src/main/resources/application.properties`

**Type**: Enhancement  

### Problem
Fineract was reporting `uri="UNKNOWN"` in Prometheus `http_server_requests_seconds_count` metrics:
- `GET /UNKNOWN` (401) → 7 requests
- `GET /UNKNOWN` (200) → 78 requests  
- `POST /UNKNOWN` (400) → 23 requests
- `POST /UNKNOWN` (200) → 351 requests

This was causing a major monitoring blind spot - no visibility into which API endpoints were being called.

### Root Cause
- REST API endpoints have dynamic parameters (client IDs, loan IDs, etc.)
- Each unique ID creates a unique URI: `/api/v1/clients/123`, `/api/v1/clients/456`, etc.
- Spring Boot/Micrometer detects this high cardinality and masks URIs as "UNKNOWN" to protect Prometheus
- Result: No observability into actual API patterns

### Solution
Implemented **URI normalization** using a `MeterFilter.map()` that transforms meter IDs:

1. **Normalize URIs** by replacing numeric IDs with `{id}` placeholder:
   - `/api/v1/clients/123` → `/api/v1/clients/{id}`
   - `/api/v1/clients/456/loans/789` → `/api/v1/clients/{id}/loans/{id}`
   - `/api/v1/loans/123/transactions/456` → `/api/v1/loans/{id}/transactions/{id}`

2. **Apply cardinality limit** of 100 unique URI patterns as a safety measure

3. **Filter out remaining UNKNOWNs** that couldn't be normalized

### Why This Works
The key is using `MeterFilter.map()` which transforms the meter ID **before** it's recorded:
- Micrometer sees normalized URIs with much lower cardinality
- Doesn't mark them as "UNKNOWN" because cardinality is manageable
- First 100 unique normalized patterns are recorded
- Any remaining high-cardinality values are denied

### Expected Result After Deployment
**Before**:
```
uri="UNKNOWN" (GET 200)    → 78 requests
uri="UNKNOWN" (POST 200)   → 351 requests
uri="UNKNOWN" (POST 400)   → 23 requests
uri="UNKNOWN" (GET 401)    → 7 requests
uri="/actuator/prometheus" → 176 requests
```

**After**:
```
uri="/actuator/prometheus"         → 176 requests
uri="/api/v1/authentication"       → 50 requests
uri="/api/v1/clients/{id}"         → 45 requests
uri="/api/v1/loans/{id}"           → 40 requests
uri="/api/v1/loans/{id}/transactions/{id}" → 35 requests
uri="/api/v1/clients/{id}/accounts/{id}"   → 28 requests
... (up to 100 normalized patterns, no UNKNOWN values)
```

### Technical Implementation
**MetricsConfig.java**:
- `httpServerRequestsUriNormalization()` - Uses regex to replace `/\d+` with `/{id}`
- `httpServerRequestsCardinalityLimit()` - Limits to 100 unique URIs

**Regex Pattern**: `Pattern.compile("/\\d+(?=/|\\?|$)")`
- Matches `/` followed by one or more digits
- Lookahead ensures it's followed by `/`, `?`, or end of string
- Replaces with `/{id}`

### Verification
Query Prometheus after deployment:
```bash
curl "http://localhost:9090/api/v1/query?query=http_server_requests_seconds_count"
```

Should see actual URIs like `/api/v1/clients/{id}` instead of `UNKNOWN`.

### Impact
- **Observability**: ✅ Can now see which API endpoints are being called
- **Cardinality**: ✅ Reduced from unlimited → ~100 normalized patterns
- **Prometheus Storage**: ✅ Prevents unbounded growth
- **Debuggability**: ✅ Can identify performance hotspots by endpoint pattern

---

## Change #2: Repository-Wide OpenRewrite Support

**Date**: 2026-09-08  
**Files Modified**:
- `build.gradle`
- `README.md`

**Type**: Enhancement

### Reason
Enable repository-wide source parsing and opt-in automated refactoring across
the multi-module Gradle build.

### Solution
Applied the OpenRewrite Gradle plugin to the root project and documented the
discovery, dry-run, and run tasks. No recipes are active by default. Version
7.39.0 is pinned because its runtime dependencies remain publicly available
from Maven Central without repository credentials.

### Impact
- Adds `rewriteDiscover`, `rewriteDryRun`, and `rewriteRun` tasks.
- Does not alter source or participate in normal build tasks unless invoked.
- Covers all subprojects from the root plugin configuration.

---

## Change #3: Normalize Request URIs for Traces as Well as Metrics

**Date**: 2026-09-14
**Files Modified**:
- `fineract-provider/src/main/java/org/apache/fineract/infrastructure/core/config/FineractServerRequestObservationConvention.java`
- `fineract-provider/src/main/java/org/apache/fineract/infrastructure/core/config/MetricsConfig.java`

**Type**: Fix

### Problem
After Change #1, `http_server_requests` reported normalized URIs such as
`/api/v1/users/{id}`, but the corresponding **spans did not**. A span for the same
request carried `uri=/api/v1/users/1` - the raw id - and was named just `http get`,
with no route at all.

The cause is that the normalization lived in a `MeterFilter`. A `MeterFilter` is
applied by the Micrometer **meter registry** and is never consulted when a span is
recorded, so only metrics benefited. Three consequences:

1. Span tag cardinality stayed unbounded in the tracing backend - the very problem
   Change #1 set out to prevent, still present on the trace side.
2. Metric-to-trace correlation by `uri` was impossible: the metric said
   `/api/v1/users/{id}` while the span said `/api/v1/users/1`.
3. Every Fineract span was named `http get` / `http post`, making the trace list
   unreadable and `{span.uri=...}` TraceQL queries return nothing useful.

### Solution
Moved normalization into `FineractServerRequestObservationConvention`, which sits on
the observation path shared by **both** metrics and traces:

- `normalizeUri(String)` holds the single `/\d+(?=/|$)` -> `/{id}` implementation.
- `uri(...)` applies it to the resolved path, so the low-cardinality tag is bounded
  wherever it is consumed.
- `getContextualName(...)` appends the normalized path when Spring resolved no
  handler pattern, so spans are named `http get /api/v1/users/{id}`.

`MetricsConfig` now delegates to that same method, keeping its `MeterFilter` purely
as a backstop for meters recorded by other instrumentation, and retaining the
cardinality limit (raised from 100 to 650 on 2026-09-15, above the ~470 declared routes). The raw path remains available on the span as
`http.url` for debugging an individual request.

### Verification
```
span name : http get /api/v1/users/{id}
uri       : /api/v1/users/{id}
http.url  : /fineract-provider/api/v1/users/1
```
`{span.uri="/api/v1/offices/{id}"}` in TraceQL returns the matching traces; the
`http_server_requests` output is unchanged, still with zero `UNKNOWN`.

### Note on the Change #1 root cause
Change #1 attributes `uri="UNKNOWN"` to Micrometer masking high-cardinality values.
That is not the mechanism. Fineract's API is served by **Jersey (JAX-RS)**, not Spring
MVC, so `DefaultServerRequestObservationConvention` never finds a handler-mapping
pattern and substitutes the literal string `UNKNOWN` regardless of cardinality. The
fix shipped in Change #1 is correct - it is the explanation that misleads, and it
matters because it points a reader at the cardinality limit rather than at the
convention when tuning this area.

---
