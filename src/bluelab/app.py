"""FastAPI application factory for the application plane.

Composes the four routed surfaces of api/00 §2 and nothing else:

    /api/v1     customer surface        bluelab.api.v1
    /ops/v1     BlueLab ops surface     bluelab.api.ops_v1
    /hooks/*    integration receivers   bluelab.api.hooks
    /internal   the call-plane seam     bluelab.api.internal   (HMAC; ADR-0071)

It also wires the RFC 9457 problem handlers, the scope-context dependency that
sets the transaction-local GUCs (ADR-0031), the OpenTelemetry instrumentation
(ADR-0027), and the startup cookie-attribute audit (SEC-002).

No route is declared in this file. Routes belong to the module that owns their
requirements — that is the whole point of ADR-0002.
"""
