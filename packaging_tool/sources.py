"""客户网页与合作端来源的读取和解析。

网页可能把本地语言声明放在款式展开区（details/summary），单品页可能跳到
共用说明页或另一个可打印版本。这里用标准库 HTMLParser 做有界同源遍历，
把每个页面上的签署链接、外链和展开区链接分别记录下来。
"""
from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET

from .model import Authorization, Carry, Carton, Claim, Notice, PrintLine, ProductPage
from .transport import TransportError


class _PageScanner(HTMLParser):
    """记录页面链接，并标记链接是否处于 details/summary 展开区内。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[dict] = []
        self._details_depth = 0
        self._pending: dict | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "details":
            self._details_depth += 1
        if tag == "a":
            link = dict(attrs)
            link["in_details"] = self._details_depth > 0
            self._pending = link

    def handle_endtag(self, tag):
        if tag == "details" and self._details_depth > 0:
            self._details_depth -= 1
        if tag == "a" and self._pending is not None:
            self.links.append(self._pending)
            self._pending = None

    def handle_data(self, data):
        if self._pending is not None:
            self._pending.setdefault("text", "")
            self._pending["text"] += data


def scan_links(html: str) -> list[dict]:
    scanner = _PageScanner()
    scanner.feed(html)
    scanner.close()
    return scanner.links


def _same_origin(url: str, base_origin: str) -> bool:
    parsed = urlsplit(url)
    base = urlsplit(base_origin)
    return parsed.scheme in ("http", "https") and (
        parsed.scheme, parsed.netloc) == (base.scheme, base.netloc)


def parse_authorization(xml_text: str, source_url: str) -> Authorization:
    """解析签署件 XML；声明的适用对象按 component/variant 精确保留。"""
    root = ET.fromstring(xml_text)
    claims: list[Claim] = []
    carries: list[Carry] = []
    for node in root:
        if node.tag == "claim":
            claims.append(Claim(
                field_name=node.attrib["field"],
                value=(node.text or "").strip(),
                component=node.attrib.get("component"),
                variant=node.attrib.get("variant"),
            ))
        elif node.tag == "carry":
            carries.append(Carry(
                field_name=node.attrib["field"],
                source_edition=node.attrib["from"],
                component=node.attrib.get("component"),
            ))
    return Authorization(
        edition_id=root.attrib["id"],
        style=root.attrib["style"],
        region=root.attrib["region"],
        lang=root.attrib["lang"],
        issued=root.attrib["issued"],
        source_url=source_url,
        claims=tuple(claims),
        carries=tuple(carries),
    )


class SourceReader:
    """按 URL 读取来源，并把客户站点爬成授权清单与页面证据。"""

    def __init__(self, transport):
        self.transport = transport

    def _get(self, url: str) -> Response:
        response = self.transport.get(url)
        if response.status != 200:
            raise TransportError(f"读取失败 {url} -> HTTP {response.status}")
        return response

    def discover(self, catalog_url: str, max_pages: int = 50) -> tuple[list[Authorization], list[ProductPage], list[str]]:
        """从地区目录出发做有界 BFS，返回签署件、页面证据和未签署来源。"""
        origin = f"{urlsplit(catalog_url).scheme}://{urlsplit(catalog_url).netloc}"
        queue = [catalog_url]
        seen: set[str] = set()
        authorizations: list[Authorization] = []
        pages: list[ProductPage] = []
        unsigned: list[str] = []
        while queue and len(seen) < max_pages:
            url = queue.pop(0)
            if url in seen or not _same_origin(url, origin):
                continue
            seen.add(url)
            response = self._get(url)
            if "html" not in response.content_type:
                continue
            links = scan_links(response.text)
            signed_here: list[str] = []
            expanded: list[str] = []
            outbound: list[str] = []
            for link in links:
                href = link.get("href")
                if not href:
                    continue
                absolute = urljoin(url, href)
                if not _same_origin(absolute, origin):
                    continue
                if link.get("in_details"):
                    expanded.append(absolute)
                if self._looks_signed(absolute, link.get("text", ""), link.get("hreflang", "")):
                    signed_here.append(absolute)
                else:
                    outbound.append(absolute)
                if absolute not in seen:
                    queue.append(absolute)
            article = self._article_context(response.text)
            pages.append(ProductPage(
                url=url,
                style=article.get("style"),
                region=article.get("region"),
                lang=article.get("lang"),
                signed_links=tuple(signed_here),
                outbound_links=tuple(outbound),
                expanded_detail_links=tuple(expanded),
            ))
            for signed_url in signed_here:
                if signed_url in seen:
                    continue
                signed_response = self._get(signed_url)
                authorizations.append(
                    parse_authorization(signed_response.text, signed_url))
                seen.add(signed_url)
            if not signed_here and not outbound and not expanded and url != catalog_url:
                # 既没有签署链接、也没有可继续追踪链接的叶子页（如共用说明页），
                # 只能作参考，不能代替签署件。中转页不列入。
                unsigned.append(url)
        return authorizations, pages, unsigned

    @staticmethod
    def _looks_signed(url: str, label: str, hreflang: str) -> bool:
        path = urlsplit(url).path.lower()
        return "/signed/" in path or path.endswith(".xml")

    @staticmethod
    def _article_context(html: str) -> dict:
        context = {}
        # 单品页用 article 上的 data-* 标明区域与款式；没有则留空待上层判定。
        marker = "data-style="
        if marker in html:
            for attr in ("style", "region", "lang"):
                token = f'data-{attr}="'
                if token in html:
                    context[attr] = html.split(token, 1)[1].split('"', 1)[0]
        root_lang = _extract_tag_attr(html, "article", "lang") or _extract_tag_attr(html, "html", "lang")
        if root_lang:
            context.setdefault("lang", root_lang)
        return context

    def fetch_authorization(self, url: str) -> Authorization:
        return parse_authorization(self._get(url).text, url)

    def load_notices(self, url: str) -> list[Notice]:
        body = self._get(url).json()
        notices = []
        for item in body.get("notices", []):
            carries = tuple(
                Carry(field_name=field_name, source_edition=edition, component=item.get("component"))
                for field_name, edition in item.get("carry", {}).items()
            )
            notices.append(Notice(
                notice_id=item["id"],
                style=item.get("style"),
                component=item["component"],
                from_lot=int(item["from_lot"]),
                region=item["region"],
                new_edition=item["new"],
                signed_url=urljoin(url, item["signed"]),
                source_url=url,
                carry=carries,
            ))
        return notices

    def load_cartons(self, url: str) -> list[Carton]:
        body = self._get(url).json()
        return [Carton(
            carton_id=item["carton"],
            style=item["style"],
            component=item["component"],
            component_lot=int(item["component_lot"]),
            region=item["region"],
            lang=item["lang"],
            qty=int(item["qty"]),
            print_line=item["print_line"],
            format=item["format"],
            required_fields=tuple(item.get("required_fields", ())),
            variant_code=item.get("variant_code"),
        ) for item in body["cartons"]]

    def load_print_lines(self, url: str) -> list[PrintLine]:
        body = self._get(url).json()
        return [PrintLine(
            line_id=item["id"],
            edition=item["edition"],
            printed=int(item["printed"]),
            queued=int(item["queued"]),
            cancelled=int(item.get("cancelled", 0)),
        ) for item in body["lines"]]


def _extract_tag_attr(html: str, tag: str, attr: str) -> str | None:
    token = f"<{tag}"
    start = html.find(token)
    if start < 0:
        return None
    end = html.find(">", start)
    segment = html[start:end]
    marker = f'{attr}="'
    if marker not in segment:
        return None
    return segment.split(marker, 1)[1].split('"', 1)[0] or None
