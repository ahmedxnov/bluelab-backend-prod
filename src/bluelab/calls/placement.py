"""Placement: select a runtime instance, mint the LiveKit access token with the
agent-dispatch metadata of api/02 §1, and redirect the browser to the placed
session. Placement must be known before the participant is told the call is
starting.

The dispatch metadata deliberately carries **no drill content, no concealed set,
no facts, no prompt material** — the token reaches the browser, so its metadata is
participant-visible.
"""
