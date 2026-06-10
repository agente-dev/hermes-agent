"""
agente_desktop_adapter

Stable shim isolating all Agente Desktop / API compatibility logic.

Per hermes-upstream-compatibility-plan.md:
- All Agente-specific Desktop/API compatibility lives here.
- The only integration point in upstream is the single hook:
  register_agente_desktop_routes(app, adapter)
  inside gateway/platforms/api_server.py .
- Upstream internals carry zero Agente edits.

This enables daily rebase from NousResearch/hermes-agent without recurring
merge conflicts on Desktop surfaces (tools, workflows, routines, BYOK, WhatsApp triage, JSON-RPC, etc.).
"""
from . import tool_discovery, jsonrpc_compat, workflow_routine_bridge, byok_session_aliases, whatsapp_ticket_integration, subscription_oauth

def register_agente_desktop_routes(app, adapter=None):
    """
    Register Desktop-specific routes and patches on the API app.

    Called from api_server.py after base routes.
    """
    tool_discovery.register(app, adapter)
    jsonrpc_compat.register(app, adapter)
    workflow_routine_bridge.register(app, adapter)
    byok_session_aliases.register(app, adapter)
    whatsapp_ticket_integration.register(app, adapter)


def register_gateway_methods(server_module):
    import tui_gateway.server as _svr

    def _emit(event, sid, payload=None):
        params = {"type": event, "session_id": sid}
        if payload is not None:
            params["payload"] = payload
        _svr.write_json({"jsonrpc": "2.0", "method": "event", "params": params})

    def _start(rid, params):
        return subscription_oauth.handle_start_subscription_oauth(rid, params, _emit)

    def _submit(rid, params):
        return subscription_oauth.handle_submit_oauth_code(rid, params, _emit)

    def _poll(rid, params):
        return subscription_oauth.handle_poll_subscription_oauth(rid, params, _emit)

    _svr._methods["auth.start_subscription_oauth"] = _start
    _svr._methods["auth.submit_oauth_code"] = _submit
    _svr._methods["auth.poll_subscription_oauth"] = _poll
