"""The surfaces a dialog can belong to.

A dialog is identified by `(user_id, channel)`, so the channel decides *which*
conversation a request touches. Core treats it as an opaque string — the
surfaces are a property of this deployment, not of the library — which is why
the list of real ones lives here, at the edge that serves them.
"""

#: The channel of a browser talking to the service directly. Every other one
#: is named by the surface that serves it — which channels exist at all is a
#: property of what a deployment installed, not of this module.
WEB_CHANNEL = "web"
