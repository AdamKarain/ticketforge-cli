#!/usr/bin/env python3
"""TicketForge CLI: manage TicketForge workitems (tickets) from the terminal.

Commands:
  setup     configure connection (base URL + credentials)
  create    create a ticket
  list      list your tickets
  show      show one ticket in full (dependencies, owner)
  update    update fields of a ticket
  fields    list or add custom fields

Run `python ticketforge.py <command> -h` for options.
"""

import argparse
import getpass
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

CONFIG_PATH = Path.home() / ".ticketforge.json"
DEFAULT_BASE_URL = "https://integrations-assignment-ticketforge.vercel.app"
MAX_RETRIES = 3


# ---------------- config ----------------

def load_config():
    if not CONFIG_PATH.exists():
        sys.exit("Not configured. Run: python ticketforge.py setup")
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(CONFIG_PATH, 0o600)  # credentials inside: owner-only read
    print(f"Saved config to {CONFIG_PATH}")


# ---------------- API client ----------------

class TicketForgeError(Exception):
    pass


class TicketForgeClient:
    """Thin client for the TicketForge HTTP API (reverse-engineered).

    All /api/tforge endpoints use HTTP Basic auth and return JSON.
    Rate limit: 50 requests per window, advertised via x-ratelimit-* headers;
    429 responses are retried with backoff based on x-ratelimit-reset.
    """

    def __init__(self, base_url, username, password):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.auth = (username, password)
        self.last_rate = {}  # remaining/limit/reset from the latest response

    def _request(self, method, path, **kwargs):
        url = f"{self.base_url}{path}"
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.request(method, url, timeout=15, **kwargs)
            except requests.ConnectionError:
                raise TicketForgeError(f"Cannot reach {self.base_url}: check your network/base URL")

            self._remember_rate(resp)

            if resp.status_code == 429:
                wait = self._retry_wait(resp, attempt)
                print(f"Rate limited (50 req/window). Waiting {wait:.0f}s, "
                      f"attempt {attempt}/{MAX_RETRIES}...", file=sys.stderr)
                time.sleep(wait)
                continue
            if resp.status_code == 401:
                raise TicketForgeError("Authentication failed (401): check username/password in setup")
            if not resp.ok:
                try:
                    detail = resp.json()
                except ValueError:
                    detail = resp.text[:200]
                raise TicketForgeError(f"API error {resp.status_code} on {method} {path}: {detail}")
            return resp.json() if resp.content else None
        raise TicketForgeError(f"Still rate-limited after {MAX_RETRIES} retries on {method} {path}")

    def _remember_rate(self, resp):
        h = resp.headers
        if "x-ratelimit-remaining" in h:
            self.last_rate = {
                "remaining": h.get("x-ratelimit-remaining"),
                "limit": h.get("x-ratelimit-limit"),
                "reset": h.get("x-ratelimit-reset"),
            }

    @staticmethod
    def _retry_wait(resp, attempt):
        """Prefer the server's reset time; fall back to exponential backoff."""
        reset = resp.headers.get("x-ratelimit-reset")
        if reset:
            try:
                reset_at = datetime.fromisoformat(reset.replace("Z", "+00:00"))
                delta = (reset_at - datetime.now(timezone.utc)).total_seconds()
                if 0 < delta < 120:
                    return delta + 1
            except ValueError:
                pass
        return float(2 ** attempt)

    # --- workitems (tickets) ---

    def create_ticket(self, title, description="", depends_on=None):
        body = {"title": title, "description": description,
                "dependsOn": depends_on or None}
        return self._request("POST", "/api/tforge/workitem/publish", json=body)["workitem"]

    def list_tickets(self, limit=None, cursor=None):
        params = {}
        if limit:
            params["limit"] = limit
        if cursor:
            params["cursor"] = cursor
        data = self._request("GET", "/api/tforge/workitems/mine", params=params)
        return data["workitems"], data.get("pagination", {})

    def list_all_tickets(self, limit=None):
        """Follow nextCursor until hasMore is false. Guarded against a
        wrong/ignored cursor param: stops if a page brings nothing new."""
        tickets, pagination = self.list_tickets(limit)
        seen = {t["ref"] for t in tickets}
        while pagination.get("hasMore") and pagination.get("nextCursor"):
            page, pagination = self.list_tickets(limit, pagination["nextCursor"])
            new = [t for t in page if t["ref"] not in seen]
            if not new:
                break
            tickets.extend(new)
            seen.update(t["ref"] for t in new)
        return tickets, pagination

    def get_ticket(self, ref):
        data = self._request("GET", f"/api/tforge/workitem/{ref}", params={"view": "deep"})
        return data["workitem"]

    def update_ticket(self, ref, title=None, description=None, stage=None,
                      depends_on=None, custom_fields=None):
        """PUT expects the FULL object, so read-modify-write."""
        current = self.get_ticket(ref)
        body = {
            "title": title if title is not None else current["title"],
            "description": description if description is not None else current.get("description", ""),
            "stage": stage if stage is not None else current.get("stage", "open"),
            "dependsOn": depends_on if depends_on is not None else current.get("dependsOn", []),
            "customFields": custom_fields if custom_fields is not None else current.get("customFields", {}),
        }
        return self._request("PUT", f"/api/tforge/workitem/{ref}", json=body)["workitem"]

    # --- custom fields ---

    def list_fields(self):
        return self._request("GET", "/api/tforge/custom-fields")["customFields"]

    def add_field(self, name, label):
        body = {"name": name, "label": label}
        return self._request("POST", "/api/tforge/custom-fields", json=body)["customField"]


# ---------------- output helpers ----------------

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

STAGE_COLORS = {"open": "32", "in_progress": "33", "in progress": "33",
                "done": "90", "closed": "90"}


def paint(text, code):
    """Wrap text in an ANSI color code when stdout is a terminal."""
    if not USE_COLOR:
        return str(text)
    return f"\033[{code}m{text}\033[0m"


def cell(col, value):
    if col == "ref":
        return paint(value, "36")                             # cyan refs
    if col == "stage":
        return paint(value, STAGE_COLORS.get(str(value).lower(), "0"))
    return str(value)


def print_table(rows, cols):
    if not rows:
        print("Nothing to show.")
        return
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  ".join(paint(c.upper().ljust(widths[c]), "1") for c in cols))  # bold header
    print("-" * (sum(widths.values()) + 2 * (len(cols) - 1)))
    for r in rows:
        line = []
        for c in cols:
            raw = str(r.get(c, ""))
            # pad on the raw text, colorize after, so ANSI codes don't break alignment
            line.append(cell(c, raw) + " " * (widths[c] - len(raw)))
        print("  ".join(line))


def print_rate(client):
    r = client.last_rate
    if r and r.get("remaining") is not None and int(r["remaining"]) <= 10:
        print(f"(rate limit: {r['remaining']}/{r['limit']} requests left this window)",
              file=sys.stderr)


# ---------------- CLI ----------------

def build_parser():
    parser = argparse.ArgumentParser(prog="ticketforge", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup", help="configure connection and credentials")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--username")
    p.add_argument("--password")

    p = sub.add_parser("create", help="create a ticket")
    p.add_argument("--title", required=True)
    p.add_argument("--description", default="")
    p.add_argument("--depends-on", help="comma-separated ticket refs, e.g. TF-695,TF-696")

    p = sub.add_parser("list", help="list your tickets")
    p.add_argument("--limit", type=int, help="page size")
    p.add_argument("--all", action="store_true",
                   help="follow pagination cursors until every ticket is fetched")

    p = sub.add_parser("show", help="show one ticket in full")
    p.add_argument("ref", help="ticket ref, e.g. TF-695")

    p = sub.add_parser("update", help="update a ticket (only the flags you pass change)")
    p.add_argument("ref")
    p.add_argument("--title")
    p.add_argument("--description")
    p.add_argument("--stage", help='e.g. "open" (values as the web app shows them)')
    p.add_argument("--depends-on", help="comma-separated refs; pass empty string to clear")

    p = sub.add_parser("fields", help="list or add custom fields")
    p.add_argument("action", choices=["list", "add"])
    p.add_argument("--name", help="machine name, e.g. priority")
    p.add_argument("--label", help="display label, e.g. Priority")

    return parser


def parse_depends(value):
    if value is None:
        return None
    value = value.strip()
    return [] if value == "" else [r.strip() for r in value.split(",") if r.strip()]


def main():
    args = build_parser().parse_args()

    if args.command == "setup":
        username = args.username or input("TicketForge username: ")
        password = args.password or getpass.getpass("TicketForge password: ")
        save_config({"base_url": args.base_url, "username": username, "password": password})
        return

    cfg = load_config()
    client = TicketForgeClient(cfg["base_url"], cfg["username"], cfg["password"])

    try:
        if args.command == "create":
            t = client.create_ticket(args.title, args.description, parse_depends(args.depends_on))
            print(f"Created {t['ref']}: {t['title']} (stage: {t.get('stage', '?')})")

        elif args.command == "list":
            if args.all:
                tickets, pagination = client.list_all_tickets(args.limit)
            else:
                tickets, pagination = client.list_tickets(args.limit)
            print_table(tickets, ["ref", "title", "stage", "updated"])
            if pagination.get("hasMore") and not args.all:
                print("\nMore tickets exist. Rerun with --all to fetch every page.")

        elif args.command == "show":
            t = client.get_ticket(args.ref)
            print(json.dumps(t, indent=2))

        elif args.command == "update":
            t = client.update_ticket(args.ref, title=args.title, description=args.description,
                                     stage=args.stage, depends_on=parse_depends(args.depends_on))
            print(f"Updated {t['ref']}: {t['title']} (stage: {t.get('stage', '?')})")

        elif args.command == "fields":
            if args.action == "list":
                print_table(client.list_fields(), ["name", "label", "type", "id"])
            else:
                if not args.name or not args.label:
                    sys.exit("fields add needs --name and --label")
                f = client.add_field(args.name, args.label)
                print(f"Added custom field '{f['name']}' (label: {f['label']}, type: {f['type']})")

        print_rate(client)

    except TicketForgeError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
