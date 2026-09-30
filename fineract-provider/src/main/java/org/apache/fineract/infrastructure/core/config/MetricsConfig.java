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
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.EnableAspectJAutoProxy;
import org.springframework.http.server.observation.ServerRequestObservationConvention;

@Configuration
@EnableAspectJAutoProxy
public class MetricsConfig {

    private static final String HTTP_SERVER_REQUESTS = "http.server.requests";

    private static final String URI_TAG = "uri";

    /**
     * Maximum number of distinct uri tag values kept for {@value #HTTP_SERVER_REQUESTS}. Beyond this,
     * further new values are denied outright - see {@link #httpServerRequestsCardinalityLimit()}.
     *
     * The API declares roughly 470 distinct JAX-RS path templates, so this is sized above the full
     * route surface with headroom rather than as a tight budget: a limit below the number of routes
     * would silently drop whichever endpoints happened to be called last.
     */
    private static final int MAX_URI_TAG_VALUES = 650;

    @Bean
    public TimedAspect timedAspect(MeterRegistry registry) {
        return new TimedAspect(registry);
    }

    /**
     * Replaces Spring Boot's default "uri=UNKNOWN" fallback (which always applies to Fineract's
     * Jersey-routed API) with the actual request path, normalized to bound its cardinality. This
     * convention drives both metrics and traces.
     */
    @Bean
    public ServerRequestObservationConvention serverRequestObservationConvention() {
        return new FineractServerRequestObservationConvention();
    }

    /**
     * Backstop for meters that do not pass through
     * {@link FineractServerRequestObservationConvention} - for example a uri recorded by another
     * instrumentation path. Normalizing an already-normalized uri is a no-op, so this is safe to layer
     * on top of the convention.
     */
    @Bean
    public MeterFilter httpServerRequestsUriNormalization() {
        return new MeterFilter() {

            @Override
            public Meter.Id map(Meter.Id id) {
                if (!HTTP_SERVER_REQUESTS.equals(id.getName())) {
                    return id;
                }
                String uri = id.getTag(URI_TAG);
                if (uri == null) {
                    return id;
                }
                String normalizedUri = FineractServerRequestObservationConvention.normalizeUri(uri);
                return uri.equals(normalizedUri) ? id : id.withTag(Tag.of(URI_TAG, normalizedUri));
            }
        };
    }

    /**
     * Limits the number of distinct uri tag values to prevent unbounded cardinality growth. Paths that
     * normalization cannot collapse - external ids, dates, report names - still consume slots here, so
     * this remains a meaningful safety net.
     */
    @Bean
    public MeterFilter httpServerRequestsCardinalityLimit() {
        return MeterFilter.maximumAllowableTags(HTTP_SERVER_REQUESTS, URI_TAG, MAX_URI_TAG_VALUES, MeterFilter.deny());
    }
}
