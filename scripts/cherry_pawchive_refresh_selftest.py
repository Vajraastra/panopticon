"""Pruebas de actualización de rutas Pawchive sin red ni catálogos reales."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.cherry_pawchive_refresh import audit_source
from third_party.cherry_dl.catalog import FileRoute, RouteTable
from third_party.cherry_dl.templates.base import ArtistInfo
from third_party.cherry_dl.templates.pawchive import PawchiveTemplate


SOURCE = "https://pawchive.pw/patreon/user/123"
AVAILABLE = "a" * 64
WAITING = "b" * 64
CHANGED = "c" * 64


def legacy_route(
    sha: str, post_id: str | None, *, table: RouteTable = "pending_queue", remote_hash: str = "",
) -> FileRoute:
    return FileRoute(
        table=table,
        profile_url_id=7,
        url_source=f"pawchive://patreon/123/post/{post_id}/primary/0",
        download_url=f"https://file.pawchive.pw/data/{sha[:2]}/{sha[2:4]}/{sha}.png?f=sample.png",
        remote_hash=remote_hash,
        post_id=post_id,
    )


def post_entry(post_id: str, sha: str, *, node: int = 2, state: str = "scraped") -> dict:
    return {
        "id": post_id,
        "user": "123",
        "service": "patreon",
        "preview_state": state,
        "has_full": True,
        "file": {
            "server": f"https://n{node}.pawchive.pw",
            "path": f"/1{sha[:2]}/{sha[2:4]}/{sha}.png",
            "name": "sample.png",
        },
        "attachments": [],
    }


class FixtureClient:
    def __init__(self, pages: dict[int, list[dict]]) -> None:
        self.pages = pages
        self.offsets = []
        self.sampled = []

    async def page(self, artist: ArtistInfo, offset: int) -> list[dict]:
        if artist.url != SOURCE:
            raise AssertionError("El perfil de la consulta no coincide")
        self.offsets.append(offset)
        return self.pages[offset]

    async def sample(self, url: str) -> str:
        self.sampled.append(url)
        return "range_ok"


class RouteAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.artist = ArtistInfo("123", "Artist", "patreon", "pawchive", SOURCE)
        self.template = PawchiveTemplate(SimpleNamespace())

    async def test_updates_pending_and_waiting_with_same_sha_and_post(self):
        pending = legacy_route(AVAILABLE, "1")
        waiting = legacy_route(WAITING, "2", table="awaiting_archive", remote_hash=WAITING)
        first = {
            "id": "1", "user": "123", "service": "patreon",
            "file": {}, "attachments": [post_entry("1", AVAILABLE)["file"]],
        }
        client = FixtureClient({0: [first, post_entry("2", WAITING, node=4)]})

        result = await audit_source(client, self.template, self.artist, [pending, waiting])

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["pages"], 1)
        self.assertEqual(result["sample"], "range_ok")
        self.assertEqual(result["unresolved"], 0)
        self.assertCountEqual(result["changes"], [
            {
                "row": asdict(pending),
                "download_url": f"https://n2.pawchive.pw/data/1aa/aa/{AVAILABLE}.png?f=sample.png",
                "remote_hash": AVAILABLE,
            },
            {
                "row": asdict(waiting),
                "download_url": f"https://n4.pawchive.pw/data/1bb/bb/{WAITING}.png?f=sample.png",
                "remote_hash": WAITING,
            },
        ])
        self.assertEqual(client.offsets, [0])
        self.assertEqual(len(client.sampled), 1)

    async def test_does_not_swap_hash_reuse_another_post_or_assume_missing_id(self):
        recorded = legacy_route(AVAILABLE, "1", remote_hash=AVAILABLE)
        without_post = legacy_route(CHANGED, None)
        client = FixtureClient({0: [post_entry("1", CHANGED), post_entry("2", AVAILABLE)]})

        result = await audit_source(client, self.template, self.artist, [recorded, without_post])

        self.assertEqual(result["changes"], [])
        self.assertEqual(result["unresolved"], 2)
        self.assertEqual(client.offsets, [0])

    async def test_no_original_keeps_waiting_and_skips_sample(self):
        waiting = legacy_route(WAITING, "1", table="awaiting_archive")
        client = FixtureClient({0: [post_entry("1", WAITING, state="pending")]})

        result = await audit_source(client, self.template, self.artist, [waiting])

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["sample"], "no_original")
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["unresolved"], 1)
        self.assertFalse(client.sampled)

    async def test_does_not_use_host_outside_the_announced_nodes(self):
        pending = legacy_route(AVAILABLE, "1")
        announcement = post_entry("1", AVAILABLE)
        announcement["file"]["server"] = "https://n2.pawchive.pw.example"
        client = FixtureClient({0: [announcement]})

        result = await audit_source(client, self.template, self.artist, [pending])

        self.assertEqual(result["changes"], [])
        self.assertEqual(result["unresolved"], 1)
        self.assertFalse(client.sampled)

    async def test_verifies_profile_even_without_pending_routes(self):
        client = FixtureClient({0: [post_entry("1", AVAILABLE)]})

        result = await audit_source(client, self.template, self.artist, [])

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["sample"], "range_ok")
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["unresolved"], 0)
        self.assertEqual(client.offsets, [0])
        self.assertEqual(len(client.sampled), 1)

    async def test_walks_second_page_and_stops_when_all_routes_are_found(self):
        pending = legacy_route(AVAILABLE, "200")
        first_page = [post_entry(str(i), CHANGED) for i in range(1, 51)]
        second_page = [post_entry("200", AVAILABLE)] + [
            post_entry(str(i), CHANGED) for i in range(51, 100)
        ]
        client = FixtureClient({0: first_page, 50: second_page})

        result = await audit_source(client, self.template, self.artist, [pending])

        self.assertEqual(client.offsets, [0, 50])
        self.assertEqual(len(client.sampled), 1)
        self.assertEqual(result["pages"], 2)
        self.assertEqual(result["unresolved"], 0)
        self.assertEqual(len(result["changes"]), 1)

    async def test_stops_after_repeated_page(self):
        pending = legacy_route(AVAILABLE, "200")
        page = [post_entry(str(i), CHANGED) for i in range(1, 51)]
        client = FixtureClient({0: page, 50: page})

        result = await audit_source(client, self.template, self.artist, [pending])

        self.assertEqual(client.offsets, [0, 50])
        self.assertEqual(result["status"], "repeated_page")
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["unresolved"], 1)


if __name__ == "__main__":
    unittest.main()
