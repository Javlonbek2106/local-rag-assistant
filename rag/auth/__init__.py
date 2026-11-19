"""Authentication foundation for the database connector (roadmap M4.1a, ADR-P16).

Only the connector's own routes are protected; the document and ask endpoints stay as they are (ADR-007, decision D-1).
Everything here is inert unless `config.CONNECTOR_ENABLED` is true.
"""
