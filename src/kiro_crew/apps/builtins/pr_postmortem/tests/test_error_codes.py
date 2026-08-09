"""Every error this app returns carries a machine-readable ``code``.

The repository-wide ratchet in ``test/test_error_code_contract.py`` catches a NEW
un-coded response by counting AST sites, which is the right shape for a
cross-cutting gate but tells you nothing about what a given endpoint actually
sends. These tests assert the wire format of the app's own errors, and that the
codes the page localizes are the codes the backend really emits -- a mismatch
there renders the English fallback in every language, silently.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import cast

from aiohttp import web

from kiro_crew.apps.builtins.pr_postmortem.backend import routes

APP_DIR = Path(routes.__file__).resolve().parent.parent
# .../src/kiro_crew/apps/builtins/pr_postmortem -> repo root is five levels up.
REPO_ROOT = APP_DIR.parents[4]
API_TS = REPO_ROOT / "website" / "src" / "apps" / "pr-postmortem" / "api.ts"


class _FakeRequest:
    """Minimal stand-in for aiohttp's request (see TestRealRequestCompatibility)."""

    def __init__(self, user="alice", match_info=None, query=None):
        self._user = user
        self.match_info = match_info or {}
        self.query = query or {}

    def get(self, key, default=None):
        return self._user if key == "user" else default


def _body(resp: web.Response) -> dict:
    return json.loads(resp.text or "{}")


class TestEveryErrorCarriesACode(unittest.TestCase):
    def test_the_status_helpers_all_emit_a_code(self):
        helpers = [
            (routes._bad_request, 400),
            (routes._needs_auth, 401),
            (routes._forbidden, 403),
            (routes._not_found, 404),
            (routes._conflict, 409),
            (routes._server_error, 500),
        ]
        for helper, status in helpers:
            resp = helper("some_code", "some prose")
            body = _body(resp)
            self.assertEqual(resp.status, status, helper.__name__)
            self.assertEqual(body["code"], "some_code", helper.__name__)
            self.assertEqual(body["error"], "some prose", helper.__name__)

    def test_an_unauthenticated_request_is_coded(self):
        resp = routes._unauthorized(cast(web.Request, _FakeRequest(user=None)))
        assert resp is not None
        self.assertEqual(resp.status, 401)
        self.assertEqual(_body(resp)["code"], "unauthorized")

    def test_a_bad_path_parameter_is_coded(self):
        """Covered against the real handler in TestCodedErrorsFromHandlers."""


class TestCodedErrorsFromHandlers(unittest.IsolatedAsyncioTestCase):
    """Drives the handlers directly.

    An async test case rather than an ``asyncio.run`` wrapper: test/
    test_spawn_audit.py matches the bare attribute name ``run``, so a wrapper
    would have to be allowlisted as a non-spawn -- an allowlist entry earned by
    test scaffolding, which is the wrong way to spend one.
    """

    async def test_a_bad_path_parameter_is_coded(self):
        request = cast(web.Request, _FakeRequest(match_info={"fix_pr": "nope"}))
        resp = await routes.get_report(request)
        self.assertEqual(resp.status, 400)
        self.assertEqual(_body(resp)["code"], "bad_fix_pr")

    async def test_an_unauthenticated_read_is_coded(self):
        request = cast(web.Request, _FakeRequest(user=None))
        resp = await routes.get_reports(request)
        self.assertEqual(resp.status, 401)
        self.assertEqual(_body(resp)["code"], "unauthorized")

    def test_every_error_site_uses_a_status_helper(self):
        """No error may be built inline, bypassing the coded helpers."""
        src = (APP_DIR / "backend" / "routes.py").read_text(encoding="utf-8")
        # Every json_response with a 4xx/5xx status must be one of the helpers,
        # and the helpers are the only place a bare `status=4xx` appears.
        inline = [
            line.strip()
            for line in src.splitlines()
            if "json_response" in line and re.search(r"status=[45]\d\d", line)
        ]
        self.assertEqual(
            len(inline), 6,
            "expected exactly the six status helpers to build error responses, "
            f"found {len(inline)}:\n" + "\n".join(inline),
        )

    def test_codes_are_lower_snake_case(self):
        src = (APP_DIR / "backend" / "routes.py").read_text(encoding="utf-8")
        codes = set(re.findall(r'_(?:bad_request|needs_auth|forbidden|not_found'
                              r'|conflict|server_error)\(\s*\n?\s*"([^"]+)"', src))
        self.assertTrue(codes, "no codes found -- the regex has drifted")
        for code in codes:
            self.assertRegex(code, r"^[a-z][a-z0-9_]*$", code)


class TestThePageLocalizesRealCodes(unittest.TestCase):
    """The frontend maps codes to message keys; those codes must exist here."""

    def _mapped_codes(self) -> set[str]:
        if not API_TS.exists():  # pragma: no cover - frontend absent in sdist
            self.skipTest(f"{API_TS} not present")
        src = API_TS.read_text(encoding="utf-8")
        block = re.search(
            r"ERROR_MESSAGE_KEY[^=]*=\s*\{(.*?)\}", src, re.DOTALL
        )
        assert block is not None, "ERROR_MESSAGE_KEY not found in api.ts"
        return set(re.findall(r"^\s*([a-z_]+):", block.group(1), re.MULTILINE))

    def _emitted_codes(self) -> set[str]:
        src = (APP_DIR / "backend" / "routes.py").read_text(encoding="utf-8")
        return set(re.findall(r'_(?:bad_request|needs_auth|forbidden|not_found'
                             r'|conflict|server_error)\(\s*\n?\s*"([^"]+)"', src))

    def test_every_localized_code_is_one_the_backend_sends(self):
        unknown = self._mapped_codes() - self._emitted_codes()
        self.assertEqual(
            unknown, set(),
            "api.ts localizes code(s) this backend never emits, so the message "
            f"is dead: {sorted(unknown)}",
        )


if __name__ == "__main__":
    unittest.main()
