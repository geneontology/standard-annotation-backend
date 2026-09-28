"""Provide shared clients for external services.

Integration modules may depend on the Python standard library and third-party
HTTP or validation libraries. They must not import SAB feature packages such as
`auth` or `ontology`, or the API, service, persistence, or domain layers.

Feature-specific adapters may import this package to make external requests.
Those adapters remain responsible for resource configuration, converting
responses to application values, and translating integration errors for their
callers.
"""
