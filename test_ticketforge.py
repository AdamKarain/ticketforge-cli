"""Tests for the TicketForge client (API mocked; no network).

Run: python -m pytest test_ticketforge.py -v
  or: python -m unittest test_ticketforge -v
"""

import unittest
from unittest.mock import patch, MagicMock

from ticketforge import TicketForgeClient, TicketForgeError, parse_depends


def fake_response(status=200, body=None, headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.ok = 200 <= status < 300
    resp.headers = headers or {}
    resp.content = b"x" if body is not None else b""
    resp.json.return_value = body
    return resp


def make_client():
    return TicketForgeClient("https://example.test", "user", "pass")


class TestClient(unittest.TestCase):

    @patch("ticketforge.requests.Session.request")
    def test_create_sends_publish_body(self, mock_req):
        mock_req.return_value = fake_response(
            body={"workitem": {"ref": "TF-1", "title": "t", "stage": "open"}})
        t = make_client().create_ticket("t", "d", ["TF-0"])
        self.assertEqual(t["ref"], "TF-1")
        method, url = mock_req.call_args[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/api/tforge/workitem/publish"))
        self.assertEqual(mock_req.call_args.kwargs["json"],
                         {"title": "t", "description": "d", "dependsOn": ["TF-0"]})

    @patch("ticketforge.requests.Session.request")
    def test_list_returns_items_and_pagination(self, mock_req):
        mock_req.return_value = fake_response(body={
            "workitems": [{"ref": "TF-1"}],
            "pagination": {"limit": 5, "hasMore": True}})
        items, pag = make_client().list_tickets(limit=5)
        self.assertEqual(len(items), 1)
        self.assertTrue(pag["hasMore"])
        self.assertEqual(mock_req.call_args.kwargs["params"], {"limit": 5})

    @patch("ticketforge.requests.Session.request")
    def test_list_all_follows_cursor(self, mock_req):
        page1 = fake_response(body={"workitems": [{"ref": "TF-1"}],
                                    "pagination": {"hasMore": True, "nextCursor": "c1"}})
        page2 = fake_response(body={"workitems": [{"ref": "TF-2"}],
                                    "pagination": {"hasMore": False}})
        mock_req.side_effect = [page1, page2]
        items, _ = make_client().list_all_tickets(limit=1)
        self.assertEqual([t["ref"] for t in items], ["TF-1", "TF-2"])
        # second request carried the cursor
        self.assertEqual(mock_req.call_args_list[1].kwargs["params"],
                         {"limit": 1, "cursor": "c1"})

    @patch("ticketforge.requests.Session.request")
    def test_list_all_stops_if_cursor_ignored(self, mock_req):
        same = {"workitems": [{"ref": "TF-1"}],
                "pagination": {"hasMore": True, "nextCursor": "c1"}}
        mock_req.side_effect = [fake_response(body=same), fake_response(body=same)]
        items, _ = make_client().list_all_tickets()
        self.assertEqual(len(items), 1)          # no infinite loop, no duplicates
        self.assertEqual(mock_req.call_count, 2)

    @patch("ticketforge.requests.Session.request")
    def test_update_is_read_modify_write(self, mock_req):
        current = {"workitem": {"ref": "TF-1", "title": "old", "description": "d",
                                "stage": "open", "dependsOn": ["TF-0"]}}
        updated = {"workitem": {"ref": "TF-1", "title": "new", "stage": "open"}}
        mock_req.side_effect = [fake_response(body=current), fake_response(body=updated)]
        t = make_client().update_ticket("TF-1", title="new")
        self.assertEqual(t["title"], "new")
        # second call is the PUT with the FULL merged object
        put_call = mock_req.call_args_list[1]
        self.assertEqual(put_call[0][0], "PUT")
        self.assertEqual(put_call.kwargs["json"], {
            "title": "new", "description": "d", "stage": "open",
            "dependsOn": ["TF-0"], "customFields": {}})

    @patch("ticketforge.time.sleep")
    @patch("ticketforge.requests.Session.request")
    def test_429_retries_then_succeeds(self, mock_req, mock_sleep):
        ok = fake_response(body={"workitems": [], "pagination": {}})
        mock_req.side_effect = [fake_response(status=429), ok]
        items, _ = make_client().list_tickets()
        self.assertEqual(items, [])
        self.assertEqual(mock_req.call_count, 2)
        mock_sleep.assert_called_once()

    @patch("ticketforge.requests.Session.request")
    def test_401_raises_clear_error(self, mock_req):
        mock_req.return_value = fake_response(status=401)
        with self.assertRaises(TicketForgeError) as ctx:
            make_client().list_tickets()
        self.assertIn("Authentication", str(ctx.exception))

    def test_parse_depends(self):
        self.assertIsNone(parse_depends(None))
        self.assertEqual(parse_depends(""), [])
        self.assertEqual(parse_depends("TF-1, TF-2"), ["TF-1", "TF-2"])


if __name__ == "__main__":
    unittest.main()
