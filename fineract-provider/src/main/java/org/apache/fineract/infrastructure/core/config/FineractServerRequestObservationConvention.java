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

import io.micrometer.common.KeyValue;
import jakarta.servlet.http.HttpServletRequest;
import java.util.regex.Pattern;
import org.springframework.http.server.observation.DefaultServerRequestObservationConvention;
import org.springframework.http.server.observation.ServerRequestObservationContext;

/**
 * Fineract's REST API is served by Jersey (JAX-RS, see JerseyConfig), not Spring MVC's
 * DispatcherServlet. Spring's {@link DefaultServerRequestObservationConvention} only resolves a real
 * "uri" tag from a Spring MVC handler-mapping pattern; since that never happens here, every
 * Jersey-routed request is tagged with the literal string "UNKNOWN" in the http.server.requests
 * metric, regardless of the actual path requested.
 *
 * This overrides that fallback to use the actual servlet request path, normalized so that numeric
 * path segments collapse to {id}. Normalizing here rather than in a MeterFilter is deliberate: a
 * MeterFilter only ever sees the meter registry, so spans would keep the raw id and reintroduce the
 * unbounded cardinality in the tracing backend that the normalization exists to prevent.
 */
public class FineractServerRequestObservationConvention extends DefaultServerRequestObservationConvention {

    private static final String UNKNOWN_URI = "UNKNOWN";

    /**
     * Matches a path segment made up solely of digits, e.g. the "/123" in "/api/v1/clients/123/loans".
     * The lookahead keeps the trailing delimiter out of the match so it survives the replacement.
     */
    private static final Pattern NUMERIC_ID_PATTERN = Pattern.compile("/\\d+(?=/|$)");

    /**
     * Replaces every numeric path segment with the literal "{id}", so "/api/v1/clients/123/loans/456"
     * becomes "/api/v1/clients/{id}/loans/{id}". Applying this to an already-normalized uri is a no-op.
     */
    public static String normalizeUri(String uri) {
        if (uri == null || uri.isEmpty()) {
            return uri;
        }
        return NUMERIC_ID_PATTERN.matcher(uri).replaceAll("/{id}");
    }

    @Override
    protected KeyValue uri(ServerRequestObservationContext context) {
        KeyValue defaultUri = super.uri(context);
        if (!UNKNOWN_URI.equals(defaultUri.getValue())) {
            return defaultUri;
        }
        String path = normalizedRequestPath(context);
        return path == null ? defaultUri : KeyValue.of("uri", path);
    }

    /**
     * Spring derives the span name from the same handler-mapping pattern, so a Jersey route yields a
     * bare "http get" for every endpoint and every Fineract span looks alike in the tracing backend.
     * Append the normalized path whenever no pattern was resolved.
     */
    @Override
    public String getContextualName(ServerRequestObservationContext context) {
        String contextualName = super.getContextualName(context);
        if (contextualName != null && contextualName.indexOf('/') >= 0) {
            return contextualName;
        }
        String path = normalizedRequestPath(context);
        return path == null ? contextualName : contextualName + " " + path;
    }

    private String normalizedRequestPath(ServerRequestObservationContext context) {
        HttpServletRequest request = context.getCarrier();
        if (request == null) {
            return null;
        }
        String path = request.getRequestURI();
        if (path == null) {
            return null;
        }
        String contextPath = request.getContextPath();
        if (contextPath != null && !contextPath.isEmpty() && path.startsWith(contextPath)) {
            path = path.substring(contextPath.length());
        }
        return normalizeUri(path);
    }
}
