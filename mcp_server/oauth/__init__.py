"""Wallet-as-login OAuth 2.1 authorization server for the public MCP connector.

Only active when PUBLIC_MODE=true (see vanna_core/config.py and MCP_CONNECTOR_PLAN.md). The Copilot's
private service never loads this package's routes and keeps its WorkOS machine-to-machine auth.
"""
