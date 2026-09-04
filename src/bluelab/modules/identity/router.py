"""The HTTP routes this module owns, mounted by `bluelab.api.v1`.

**Auth is the exception, and it lives in `bluelab.api.auth`.** A route needs
`bluelab.api.deps` for the resolved principal and the session store, and
`bluelab.modules` sits below `bluelab.api` in the layers contract — so a router
here importing `deps` is an upward import and a build failure. The Auth
*behaviour* is still this module's: `service.py` and `gates.py` hold every
decision, and the routes only render them.

The rest of identity's surface — the Legal tag, and provisioning's ops routes —
mounts from here when it is written.
"""
