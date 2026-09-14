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
import org.springframework.http.server.observation.DefaultServerRequestObservationConvention;
import org.springframework.http.server.observation.ServerRequestObservationContext;

/**
 * Fineract's REST API is served by Jersey (JAX-RS, see JerseyConfig), not Spring MVC's
 * DispatcherServlet. Spring's {@link DefaultServerRequestObservationConvention} only resolves a
 * real "uri" tag from a Spring MVC handler-mapping pattern; since that never happens here, every
 * Jersey-routed request is tagged with the literal string "UNKNOWN" in the http.server.requests
 * metric, regardless of the actual path requested.
 *
 * This overrides that fallback to use the raw servlet request path instead, so the
 * {@link MetricsConfig} MeterFilter has a real path to normalize numeric IDs on.
 */
public class FineractServerRequestObservationConvention extends DefaultServerRequestObservationConvention {

    @Override
    protected KeyValue uri(ServerRequestObservationContext context) {
        KeyValue defaultUri = super.uri(context);
        if (!"UNKNOWN".equals(defaultUri.getValue())) {
            return defaultUri;
        }
        HttpServletRequest request = context.getCarrier();
        if (request == null) {
            return defaultUri;
        }
        String path = request.getRequestURI();
        String contextPath = request.getContextPath();
        if (contextPath != null && !contextPath.isEmpty() && path.startsWith(contextPath)) {
            path = path.substring(contextPath.length());
        }
        return KeyValue.of("uri", path);
    }
}
