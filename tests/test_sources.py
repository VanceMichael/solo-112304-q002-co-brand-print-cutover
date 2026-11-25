import unittest
from urllib.parse import urlsplit

from packaging_tool.sources import (
    SourceReader,
    parse_authorization,
    scan_links,
)
from packaging_tool.transport import HttpTransport, Response

from tests.helpers import FakeTransport, FixtureServer


class ParsingTests(unittest.TestCase):
    def test_scan_links_marks_details_section(self):
        html = (
            '<main><a href="/top.html">顶层</a>'
            '<details><summary>法国市场声明</summary>'
            '<a hreflang="fr" href="/brand/signed/FR-7.xml">授权 FR-7</a>'
            "</details></main>"
        )
        links = scan_links(html)
        self.assertEqual(len(links), 2)
        self.assertFalse(links[0]["in_details"])
        self.assertTrue(links[1]["in_details"])
        self.assertEqual(links[1]["href"], "/brand/signed/FR-7.xml")

    def test_parse_authorization_keeps_claim_scope(self):
        xml = (
            '<authorization id="FR-7" region="EU" lang="fr" issued="2026-11-01" style="G20">'
            '<claim component="H7" field="capacity">18 Wh</claim>'
            '<claim field="composition">85% PA</claim>'
            "</authorization>"
        )
        auth = parse_authorization(xml, "http://x/brand/signed/FR-7.xml")
        self.assertEqual((auth.style, auth.region, auth.lang), ("G20", "EU", "fr"))
        self.assertEqual(auth.claim_for("capacity", "H7").value, "18 Wh")
        self.assertEqual(auth.claim_for("composition", "H7").value, "85% PA")
        self.assertIsNone(auth.claim_for("capacity", "H8"))  # 不跨组件借用


class DiscoveryTests(unittest.TestCase):
    def test_discovery_against_live_fixture(self):
        with FixtureServer() as fixture:
            reader = SourceReader(HttpTransport())
            authorizations, pages, unsigned = reader.discover(
                fixture.endpoints()["catalog"])
            eu_url = fixture.origin + "/brand/eu/g20"
            manual_url = fixture.origin + "/brand/shared/manual.html"
        editions = {auth.edition_id for auth in authorizations}
        self.assertEqual(editions, {"FR-7", "CN-2"})
        eu_page = next(page for page in pages if page.url == eu_url)
        self.assertEqual((eu_page.style, eu_page.region), ("G20", "EU"))
        # 本地语言声明在展开区里
        self.assertTrue(any(url.endswith("/brand/signed/FR-7.xml")
                            for url in eu_page.expanded_detail_links))
        # 共用说明页没有签署链接，只能列为未签署来源
        self.assertIn(manual_url, unsigned)
        signed_urls = {url for page in pages for url in page.signed_links}
        self.assertNotIn(manual_url, signed_urls)

    def test_discovery_follows_printable_version_links(self):
        routes = {
            "http://x/catalog": Response(
                "http://x/catalog", 200, "text/html",
                '<main><a href="/product">单品</a></main>'),
            "http://x/product": Response(
                "http://x/product", 200, "text/html",
                '<article data-style="G20" data-region="EU">'
                '<a href="/printable">可打印版本</a></article>'),
            "http://x/printable": Response(
                "http://x/printable", 200, "text/html",
                '<a href="/signed/X-1.xml">签署</a>'),
            "http://x/signed/X-1.xml": Response(
                "http://x/signed/X-1.xml", 200, "application/xml",
                '<authorization id="X-1" region="EU" lang="fr" issued="2026-11-01" style="G20"/>'
            ),
        }
        reader = SourceReader(FakeTransport(routes))
        authorizations, pages, unsigned = reader.discover("http://x/catalog")
        self.assertEqual([auth.edition_id for auth in authorizations], ["X-1"])
        self.assertEqual({urlsplit(page.url).path for page in pages},
                         {"/catalog", "/product", "/printable"})
        self.assertEqual(unsigned, [])


if __name__ == "__main__":
    unittest.main()
