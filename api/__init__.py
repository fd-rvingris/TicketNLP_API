"""The ticket analysis API: the server detector and the taxonomy classifier over HTTP (/v1) and MCP.

Imports only the ticket_analysis package, never tools/, and needs only requirements-api.txt. See
docs/api.md for the settings and local runs, and docs/azure-deploy.md for Azure Container Apps.
"""

# The contract version. Within /v1 changes are additive (new optional request fields, new response
# fields, new enum values); the minor version rises with each.
API_VERSION = "1.0.0"
