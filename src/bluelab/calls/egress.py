"""Recording egress. The runtime *requests* start; the application plane *executes*
it against the provider with its own credentials, so ADR-0071 rule 1 holds. Owns
the `recording_object_key` / `recording_status` lifecycle (api/01 §6-§7).
"""
