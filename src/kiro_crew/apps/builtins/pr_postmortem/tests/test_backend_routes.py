"""Tests for the PR Postmortem builtin's HTTP routes.

Runs against the real ``aiohttp`` and the real ``kiro_crew`` package -- the
external-app version of this file stubbed both into ``sys.modules``, which inside
this repository would shadow the real package for every other test.

The handlers are exercised directly with a light request double rather than
through an aiohttp test server: what needs proving here is the guard order (gate,
then session, then input validation) and the input handling, not aiohttp's own
routing.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from typing import cast, get_type_hints
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.pr_postmortem.backend import routes
from kiro_crew.apps.builtins.pr_postmortem.engine import store
from kiro_crew.apps.builtins.pr_postmortem.tests.test_store import ANALYSIS, ATTRIBUTION


class _FakeRequest:
    """The slice of web.Request these handlers touch."""

    def __init__(self, user="alice", match_info=None, body=None, bad_json=False, query=None):
        self._user = user
        self.match_info = match_info or {}
        self.query = query or {}
        self._body = body
        self._bad_json = bad_json

    def get(self, key, default=None):
        return self._user if key == "user" else default

    async def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._body


def _req(**kwargs: object) -> web.Request:
    """A typed handle on the request double.

    These handlers touch exactly three things on a request: ``.get("user")``,
    ``.match_info`` and ``await .json()``. Building a real ``web.Request`` needs a
    transport, protocol and payload stream that add no coverage, so the double
    stands in and the cast is confined to this one place.
    ``TestRealRequestCompatibility`` proves the double is faithful by running a
    handler against a genuine aiohttp request.
    """
    return cast(web.Request, _FakeRequest(**kwargs))  # type: ignore[arg-type]


def _run(coro):
    return asyncio.run(coro)


def _payload(resp: web.Response) -> dict:
    return json.loads(resp.text or "{}")


def _report(fix_pr: int) -> dict:
    """Load a report and assert it exists, so tests can index it directly."""
    rep = store.load_report(fix_pr)
    assert rep is not None, f"expected a report for #{fix_pr}"
    return rep


class RoutesTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prpm-routes-")
        self._prev = os.environ.get("PRPM_DATA_DIR")
        os.environ["PRPM_DATA_DIR"] = self.tmp
        store.save_attribution(ATTRIBUTION)
        with open(
            os.path.join(store.analysis_dir(), "analysis-4242.json"),
            "w",
            encoding="utf-8",
        ) as fh:
            json.dump(ANALYSIS, fh)
        # Default: app enabled, so tests exercise the handler rather than the gate.
        self._enabled = mock.patch.object(routes, "is_app_enabled", return_value=True)
        self._enabled.start()

    def tearDown(self):
        self._enabled.stop()
        if self._prev is None:
            os.environ.pop("PRPM_DATA_DIR", None)
        else:
            os.environ["PRPM_DATA_DIR"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestRouteRegistration(unittest.TestCase):
    def test_registers_full_paths_on_the_router(self):
        app = web.Application()
        routes.register_routes(app)
        # aiohttp's add_get also registers a HEAD route for the same handler
        # (allow_head defaults true), so filter to the methods actually declared.
        seen = {
            (r.method, r.resource.canonical)
            for r in app.router.routes()
            if r.resource is not None and r.method in {"GET", "POST"}
        }
        self.assertEqual(
            seen,
            {
                ("GET", "/api/apps/pr-postmortem/reports"),
                ("GET", "/api/apps/pr-postmortem/reports/{fix_pr}"),
                ("GET", "/api/apps/pr-postmortem/reports/{fix_pr}/bundle"),
                ("POST", "/api/apps/pr-postmortem/reports/{fix_pr}/reattribute"),
                ("POST", "/api/apps/pr-postmortem/reports/{fix_pr}/link"),
                ("POST", "/api/apps/pr-postmortem/proposals/{fix_pr}/{index}/decision"),
                ("GET", "/api/apps/pr-postmortem/backlog"),
                ("GET", "/api/apps/pr-postmortem/backlog/{cluster_id}/apply-plan"),
                ("POST", "/api/apps/pr-postmortem/backlog/{cluster_id}/application"),
                ("GET", "/api/apps/pr-postmortem/state"),
            },
        )

    def test_registration_returns_none(self):
        # The builtin contract is side-effecting registration, not a route list.
        hints = get_type_hints(routes.register_routes)
        self.assertIs(hints.get("return"), type(None))

    def test_every_route_is_wrapped_in_the_enabled_gate(self):
        app = web.Application()
        routes.register_routes(app)
        for route in app.router.routes():
            path = route.resource.canonical if route.resource is not None else "?"
            with self.subTest(path=path):
                # functools.wraps sets __wrapped__ on the gate's inner function.
                self.assertTrue(
                    hasattr(route.handler, "__wrapped__"), f"{path} is not gated"
                )

    def test_app_name_matches_the_manifest(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(routes.__file__)))
        with open(os.path.join(here, "app.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        # A mismatch would make the gate refuse every request for an enabled app.
        self.assertEqual(manifest["name"], store.APP_NAME)


class TestRealRequestCompatibility(RoutesTestCase):
    """Guards the request double: if aiohttp's Request contract drifts from what
    ``_FakeRequest`` models, this test fails while the doubles keep passing."""

    def test_handler_works_with_a_genuine_aiohttp_request(self):
        req = make_mocked_request("GET", "/api/apps/pr-postmortem/reports")
        req["user"] = "alice"  # Request is a MutableMapping; middleware stores state here
        resp = _run(routes.get_reports(req))
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(_payload(resp)["reports"]), 1)

    def test_real_request_without_a_session_401s(self):
        req = make_mocked_request("GET", "/api/apps/pr-postmortem/reports")
        self.assertEqual(_run(routes.get_reports(req)).status, 401)

    def test_real_request_match_info_reaches_the_handler(self):
        req = make_mocked_request(
            "GET", "/api/apps/pr-postmortem/reports/4242", match_info={"fix_pr": "4242"}
        )
        req["user"] = "alice"
        resp = _run(routes.get_report(req))
        self.assertEqual(_payload(resp)["fix_pr"], 4242)


class TestEnabledGate(RoutesTestCase):
    def test_disabled_app_refuses_with_403(self):
        with mock.patch.object(routes, "is_app_enabled", return_value=False):
            gated = routes._require_enabled(routes.get_reports)
            resp = _run(gated(_req()))
        self.assertEqual(resp.status, 403)
        self.assertIn("disabled", _payload(resp)["error"])

    def test_gate_runs_before_the_session_check(self):
        # A disabled app must not disclose whether a caller is authenticated.
        with mock.patch.object(routes, "is_app_enabled", return_value=False):
            gated = routes._require_enabled(routes.get_reports)
            resp = _run(gated(_req(user=None)))
        self.assertEqual(resp.status, 403)


class TestAuthGuard(RoutesTestCase):
    def test_every_handler_401s_without_a_session(self):
        handlers = [
            (routes.get_reports, {}),
            (routes.get_report, {"fix_pr": "4242"}),
            (routes.get_bundle, {"fix_pr": "4242"}),
            (routes.get_state, {}),
            (routes.get_backlog, {}),
            (routes.get_apply_plan, {"cluster_id": "abc"}),
            (routes.post_reattribute, {"fix_pr": "4242"}),
            (routes.post_link_decision, {"fix_pr": "4242"}),
            (routes.post_application, {"cluster_id": "abc"}),
            (routes.post_proposal_decision, {"fix_pr": "4242", "index": "0"}),
        ]
        self.assertEqual(len(handlers), 10, "cover every registered handler")
        for handler, match in handlers:
            with self.subTest(handler=handler.__name__):
                resp = _run(handler(_req(user=None, match_info=match, body={})))
                self.assertEqual(resp.status, 401)


class TestReads(RoutesTestCase):
    def test_get_reports_includes_scan_state(self):
        resp = _run(routes.get_reports(_req()))
        self.assertEqual(resp.status, 200)
        body = _payload(resp)
        self.assertEqual(len(body["reports"]), 1)
        self.assertIn("last_scan", body)

    def test_get_report_ok(self):
        resp = _run(routes.get_report(_req(match_info={"fix_pr": "4242"})))
        self.assertEqual(_payload(resp)["fix_pr"], 4242)

    def test_get_report_404(self):
        resp = _run(routes.get_report(_req(match_info={"fix_pr": "5"})))
        self.assertEqual(resp.status, 404)

    def test_non_numeric_fix_pr_rejected(self):
        # The value reaches a filename, so traversal attempts must 400, not 404.
        for raw in ("../etc/passwd", "4242/../..", "abc", "", "1" * 12):
            with self.subTest(raw=raw):
                resp = _run(routes.get_report(_req(match_info={"fix_pr": raw})))
                self.assertEqual(resp.status, 400)

    def test_get_bundle_404_when_absent(self):
        resp = _run(routes.get_bundle(_req(match_info={"fix_pr": "4242"})))
        self.assertEqual(resp.status, 404)


class TestProposalDecision(RoutesTestCase):
    def _post(self, index="0", body=None, fix="4242", **kw):
        req = _req(match_info={"fix_pr": fix, "index": index}, body=body, **kw)
        return _run(routes.post_proposal_decision(req))

    def test_accept_persists(self):
        resp = self._post(body={"decision": "accept", "note": "doing it"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(_report(4242)["proposals"][0]["decision"], "accept")

    def test_bad_decision_400(self):
        self.assertEqual(self._post(body={"decision": "approve"}).status, 400)

    def test_missing_body_400(self):
        self.assertEqual(self._post(body=None).status, 400)

    def test_malformed_json_400_not_500(self):
        self.assertEqual(self._post(bad_json=True).status, 400)

    def test_unknown_proposal_index_404(self):
        self.assertEqual(self._post(index="9", body={"decision": "accept"}).status, 404)

    def test_unknown_report_404(self):
        self.assertEqual(self._post(fix="777", body={"decision": "accept"}).status, 404)

    def test_non_numeric_index_400(self):
        self.assertEqual(self._post(index="x", body={"decision": "accept"}).status, 400)


class TestLinkDecision(RoutesTestCase):
    def test_not_a_culprit_persists(self):
        req = _req(
            match_info={"fix_pr": "4242"},
            body={"decision": "not_a_culprit", "note": "mover"},
        )
        self.assertEqual(_run(routes.post_link_decision(req)).status, 200)
        self.assertEqual(
            _report(4242)["human_link_decision"], "not_a_culprit"
        )

    def test_bad_link_decision_400(self):
        req = _req(match_info={"fix_pr": "4242"}, body={"decision": "yes"})
        self.assertEqual(_run(routes.post_link_decision(req)).status, 400)


class TestBacklogRoutes(RoutesTestCase):
    def _clusters(self):
        return _payload(_run(routes.get_backlog(_req())))["clusters"]

    def test_backlog_lists_clusters_and_themes(self):
        body = _payload(_run(routes.get_backlog(_req())))
        self.assertEqual(len(body["clusters"]), 2)
        self.assertEqual(
            body["themes"][0]["root_cause_class"], "error_handling_gap"
        )
        self.assertEqual(body["totals"]["applicable"], 0)

    def test_apply_plan_refused_without_an_accept(self):
        # The "nothing applied silently" guarantee, enforced server-side.
        cid = self._clusters()[0]["id"]
        resp = _run(routes.get_apply_plan(_req(match_info={"cluster_id": cid})))
        self.assertEqual(resp.status, 409)
        self.assertIn("accept one first", _payload(resp)["error"])

    def test_apply_plan_served_after_an_accept(self):
        store.set_proposal_decision("4242:0", "accept")
        cluster = next(c for c in self._clusters() if c["applicable"])
        resp = _run(
            routes.get_apply_plan(_req(match_info={"cluster_id": cluster["id"]}))
        )
        self.assertEqual(resp.status, 200)
        self.assertIn("SECURITY", _payload(resp)["prompt"])

    def test_rule_cluster_defaults_to_the_steering_target(self):
        store.set_proposal_decision("4242:1", "accept")  # the `test`-bucket member
        store.set_proposal_decision("4242:0", "accept")  # the `gate`-bucket member
        for cluster in self._clusters():
            if not cluster["applicable"]:
                continue
            resp = _run(
                routes.get_apply_plan(_req(match_info={"cluster_id": cluster["id"]}))
            )
            plan = _payload(resp)
            with self.subTest(bucket=cluster["bucket"]):
                self.assertEqual(plan["target"], plan["allowed_targets"][0])

    def test_target_query_parameter_selects_the_landing_place(self):
        store.set_proposal_decision("4242:0", "accept")
        cluster = next(c for c in self._clusters() if c["applicable"])
        resp = _run(
            routes.get_apply_plan(
                _req(match_info={"cluster_id": cluster["id"]}, query={"target": "issue"})
            )
        )
        # The seeded proposal is a `gate`, which permits issue as an alternative.
        self.assertEqual(resp.status, 200)
        self.assertEqual(_payload(resp)["target"], "issue")

    def test_unknown_target_400(self):
        store.set_proposal_decision("4242:0", "accept")
        cluster = next(c for c in self._clusters() if c["applicable"])
        resp = _run(
            routes.get_apply_plan(
                _req(match_info={"cluster_id": cluster["id"]}, query={"target": "slack"})
            )
        )
        self.assertEqual(resp.status, 400)
        self.assertIn("unknown target", _payload(resp)["error"])

    def test_target_the_bucket_forbids_400(self):
        store.set_proposal_decision("4242:0", "accept")
        cluster = next(c for c in self._clusters() if c["applicable"])
        resp = _run(
            routes.get_apply_plan(
                _req(
                    match_info={"cluster_id": cluster["id"]},
                    query={"target": "steering"},
                )
            )
        )
        # A `gate` proposal must not silently land as a steering rule.
        self.assertEqual(resp.status, 400)
        self.assertIn("not valid for", _payload(resp)["error"])

    def test_apply_plan_unknown_cluster_404(self):
        req = _req(match_info={"cluster_id": "deadbeef01"})
        self.assertEqual(_run(routes.get_apply_plan(req)).status, 404)

    def test_apply_plan_bad_cluster_id_400(self):
        for raw in ("../etc", "a b", "", "x" * 41):
            with self.subTest(raw=raw):
                req = _req(match_info={"cluster_id": raw})
                self.assertEqual(_run(routes.get_apply_plan(req)).status, 400)

    def test_application_recorded_and_surfaced(self):
        # Accept first: recording an application now requires the same accept gate
        # as `get_apply_plan`, because an application against a never-accepted
        # cluster is an orphan that inflates the "applied" total.
        store.set_proposal_decision("4242:0", "accept")
        cid = next(c for c in self._clusters() if c["applicable"])["id"]
        req = _req(
            match_info={"cluster_id": cid},
            body={"status": "applied", "target": "issue", "url": "https://x/1"},
        )
        self.assertEqual(_run(routes.post_application(req)).status, 200)
        again = next(c for c in self._clusters() if c["id"] == cid)
        self.assertEqual(again["application"]["url"], "https://x/1")

    def test_application_refused_without_an_accept(self):
        """The accept gate covers the RECORD endpoint too, not just apply-plan.

        Without this, a caller could skip the plan and post an application
        directly, and the backlog's `applied` count -- the one number a human
        reads to judge whether prevention is landing -- would count work nobody
        accepted.
        """
        cid = self._clusters()[0]["id"]
        req = _req(
            match_info={"cluster_id": cid},
            body={"status": "applied", "target": "issue"},
        )
        resp = _run(routes.post_application(req))
        self.assertEqual(resp.status, 409)
        self.assertEqual(_payload(resp)["code"], "needs_accepted_proposal")

    def test_application_for_an_unknown_cluster_404(self):
        req = _req(
            match_info={"cluster_id": "deadbeef01"},
            body={"status": "applied", "target": "issue"},
        )
        resp = _run(routes.post_application(req))
        self.assertEqual(resp.status, 404)
        self.assertEqual(_payload(resp)["code"], "cluster_not_found")

    def test_application_bad_status_400(self):
        cid = self._clusters()[0]["id"]
        req = _req(match_info={"cluster_id": cid}, body={"status": "done"})
        self.assertEqual(_run(routes.post_application(req)).status, 400)


class TestReattribute(RoutesTestCase):
    def test_no_repo_configured_is_a_400_not_a_crash(self):
        resp = _run(routes.post_reattribute(_req(match_info={"fix_pr": "4242"})))
        self.assertEqual(resp.status, 400)
        self.assertIn("no repo configured", _payload(resp)["error"])

    def test_missing_repo_path_reported(self):
        store.save_state(
            {"repos": [{"repo": "o/n", "repo_path": "/nonexistent/x", "branch": "main"}]}
        )
        resp = _run(routes.post_reattribute(_req(match_info={"fix_pr": "4242"})))
        self.assertEqual(resp.status, 400)
        self.assertIn("repo_path not found", _payload(resp)["error"])


if __name__ == "__main__":
    unittest.main()
