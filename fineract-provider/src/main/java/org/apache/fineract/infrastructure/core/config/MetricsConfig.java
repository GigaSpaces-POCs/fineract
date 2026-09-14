/**
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements. See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership. The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License. You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied. See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */

package org.apache.fineract.infrastructure.core.config;

import io.micrometer.core.aop.TimedAspect;
import io.micrometer.core.instrument.Meter;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Tag;
import io.micrometer.core.instrument.config.MeterFilter;
import java.util.regex.Pattern;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.EnableAspectJAutoProxy;
import org.springframework.http.server.observation.ServerRequestObservationConvention;

@Configuration
@EnableAspectJAutoProxy
public class MetricsConfig {

    // Patterns to normalize URIs by replacing numeric IDs with placeholders
    private static final Pattern NUMERIC_ID_PATTERN = Pattern.compile("/\\d+(?=/|\\?|$)");

    @Bean
    public TimedAspect timedAspect(MeterRegistry registry) {
        return new TimedAspect(registry);
    }

    /**
     * Replaces Spring Boot's default "uri=UNKNOWN" fallback (which always applies to Fineract's
     * Jersey-routed API, see {@link FineractServerRequestObservationConvention}) with the actual
     * request path, so the MeterFilter below has something real to normalize.
     */
    @Bean
    public ServerRequestObservationConvention serverRequestObservationConvention() {
        return new FineractServerRequestObservationConvention();
    }

    /**
     * Normalizes HTTP server request URIs by replacing numeric IDs with {id} placeholders.
     * This reduces cardinality explosion by grouping similar requests:
     * 
     * Examples:
     * - /api/v1/clients/123 → /api/v1/clients/{id}
     * - /api/v1/clients/456/loans/789 → /api/v1/clients/{id}/loans/{id}
     * - /api/v1/loans/123/transactions/456 → /api/v1/loans/{id}/transactions/{id}
     * 
     * This works by transforming the Meter.Id before it's recorded, so URIs never become "UNKNOWN".
     */
    @Bean
    public MeterFilter httpServerRequestsUriNormalization() {
        return new MeterFilter() {
            @Override
            public Meter.Id map(Meter.Id id) {
                if (!"http.server.requests".equals(id.getName())) {
                    return id;
                }
                
                String uri = id.getTag("uri");
                if (uri == null || "UNKNOWN".equals(uri) || "NOT_FOUND".equals(uri)) {
                    return id;
                }
                
                // Normalize the URI by replacing all numeric IDs with {id}
                String normalizedUri = NUMERIC_ID_PATTERN.matcher(uri).replaceAll("/{id}");
                
                if (!uri.equals(normalizedUri)) {
                    // Return a new Meter.Id with the normalized URI
                    return id.withTag(Tag.of("uri", normalizedUri));
                }
                
                return id;
            }
        };
    }

    /**
     * Limits the number of unique URI tag values to prevent unbounded cardinality growth.
     * When more than 100 unique URIs are detected, further new URIs are denied from metrics.
     * 
     * This is a safety measure to prevent cardinality explosion in Prometheus.
     */
    @Bean
    public MeterFilter httpServerRequestsCardinalityLimit() {
        return MeterFilter.maximumAllowableTags("http.server.requests", "uri", 100, MeterFilter.deny());
    }
}
