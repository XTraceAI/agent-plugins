"""Comparison REST calls over MemHub's existing authenticated transport."""
from urllib.parse import urlencode
from pak import api_base
from _memhub_auth import resolve_bearer
import mcp_http
from .state import Unavailable, identifier

class Client:
    def __init__(self, org_id):
        self.org = identifier(org_id)
        self.origin = None

    def call(self, method, suffix, body=None, *, headers=None, path='/v1/team/session-comparisons', query=None):
        url, bearer = resolve_bearer()
        if not bearer:
            raise Unavailable('memhub_login_required')
        self.origin = api_base(url)
        url = self.origin + path + suffix + ('?' + urlencode(query) if query else '')
        try:
            return mcp_http.rest(url, bearer, method, body,
                                headers={**(headers or {}), 'X-Org-Id': self.org}, timeout=15).data
        except mcp_http.McpError as exc:
            # Do not echo backend error bodies: they may contain user instructions.
            raise Unavailable('comparison_api_failed') from exc

    def get(self, comparison):
        return self.call('GET', '/' + identifier(comparison))

    def source(self, native_session_id, harness='codex'):
        # Codex captures are namespaced; Claude Code captures use the raw ID.
        session_id = ('codex-' if harness == 'codex' else '') + identifier(native_session_id)
        rows = self.call('GET', '', path='/v1/team/conversations', query={
            'source_id': 'eq.' + session_id, 'scope': 'own', 'limit': 2,
            'select': 'conv_id,workspace_id,session_id'})
        if not isinstance(rows, list) or len(rows) != 1 or rows[0].get('session_id') != session_id:
            raise Unavailable('captured_source_unavailable')
        return {key: identifier(rows[0][key]) for key in ('workspace_id', 'conv_id')}

    def create(self, body, key):
        return self.call('POST', '', body, headers={'Idempotency-Key': key})

    def claim(self, comparison, run, client_id, claim_id):
        return self.call('POST', f'/{identifier(comparison)}/runs/{identifier(run)}/claim',
                         {'client_id': client_id, 'claim_id': claim_id, 'protocol_version': 1})

    def report(self, comparison, run, body):
        return self.call('POST', f'/{identifier(comparison)}/runs/{identifier(run)}/reports', body)
