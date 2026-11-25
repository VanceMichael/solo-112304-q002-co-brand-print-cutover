import unittest

from packaging_tool.authorization import (
    AuthorizationRegistry,
    assess_carton,
    resolve_carries,
)
from packaging_tool.model import Carry, Carton, LineState, Notice
from packaging_tool.sources import SourceReader

from tests.helpers import FakeTransport, FixtureServer

FR7_XML = (
    '<authorization id="FR-7" region="EU" lang="fr" issued="2026-11-01" style="G20">'
    '<claim component="H7" field="capacity">18 Wh</claim>'
    '<claim field="composition">85% PA</claim>'
    "</authorization>"
)
FR8_XML = (
    '<authorization id="FR-8" region="EU" lang="fr" issued="2026-11-20" style="G20">'
    '<claim component="H7" field="capacity">16 Wh</claim>'
    '<carry field="composition" from="FR-7"/></authorization>'
)


def carton(carton_id="C-1", lot=410, region="EU", lang="fr",
           component="H7", style="G20", line="P-1", qty=5):
    return Carton(carton_id, style, component, lot, region, lang, qty, line, "L120")


def notice(carry=True):
    return Notice(
        notice_id="N-FR-8", style="G20", component="H7", from_lot=405,
        region="EU", new_edition="FR-8",
        signed_url="http://x/brand/signed/FR-8.xml",
        source_url="http://x/brand/notices",
        carry=((Carry(field_name="composition", source_edition="FR-7",
                      component="H7"),) if carry else ()),
    )


def reader_with(registry, fr7_status=200, fr7_xml=FR7_XML, fr8_xml=FR8_XML):
    routes = {
        "http://x/brand/signed/FR-7.xml": (
            fr7_status, fr7_xml if fr7_status == 200 else {"message": "gone"}),
        "http://x/brand/signed/FR-8.xml": (200, fr8_xml),
    }
    reader = SourceReader(FakeTransport(routes))
    reader.registry = registry
    return reader


class FixtureAssessmentTests(unittest.TestCase):
    def _service_snapshot(self, fixture):
        service = fixture.service()
        return service, service.take_snapshot()

    def test_scene_zero_ready_and_suspended_lines(self):
        with FixtureServer() as fixture:
            service, snapshot = self._service_snapshot(fixture)
            order = service.evaluate(snapshot)
        by_id = {a.carton.carton_id: a for a in order.assessments}
        # 低批号与内销箱授权齐全
        for carton_id in ("C-401", "C-402", "C-403"):
            self.assertEqual(by_id[carton_id].state, LineState.READY)
        self.assertEqual(by_id["C-401"].edition, "FR-7")
        self.assertEqual(by_id["C-403"].edition, "CN-2")
        # H8 组件没有任何签署声明：只停需要该声明的订单行
        h8 = by_id["C-404"]
        self.assertEqual(h8.state, LineState.SUSPENDED)
        self.assertTrue(any("H8" in item for item in h8.missing))
        # 证据保留适用对象与来源
        evidence_urls = " ".join(h8.evidence)
        self.assertIn("FR-7", evidence_urls)

    def test_notice_switches_edition_by_actual_component_lot(self):
        with FixtureServer() as fixture:
            fixture.scene(1)
            service, snapshot = self._service_snapshot(fixture)
            order = service.evaluate(snapshot)
        by_id = {a.carton.carton_id: a for a in order.assessments}
        low_lot, high_lot = by_id["C-401"], by_id["C-402"]
        # 在途货按实际组件领用批号决定能否用原贴
        self.assertEqual(low_lot.state, LineState.READY)
        self.assertEqual(low_lot.edition, "FR-7")
        self.assertEqual(high_lot.state, LineState.AWAITING_EDITION)
        self.assertEqual(high_lot.edition, "FR-8")
        # 内销与 H8 箱不受欧区 H7 公告影响
        self.assertEqual(by_id["C-403"].edition, "CN-2")
        self.assertEqual(by_id["C-404"].state, LineState.SUSPENDED)
        # 沿用字段必须能回指到旧版同一对象
        self.assertTrue(any("FR-7" in reason and "composition" in reason
                            for reason in high_lot.reasons))
        self.assertTrue(any(url.endswith("/brand/signed/FR-7.xml")
                            for url in high_lot.evidence))


class CarryVerificationTests(unittest.TestCase):
    def _registry(self):
        from packaging_tool.sources import parse_authorization
        return AuthorizationRegistry([
            parse_authorization(FR7_XML, "http://x/brand/signed/FR-7.xml"),
            parse_authorization(FR8_XML, "http://x/brand/signed/FR-8.xml"),
        ])

    def test_carry_verified_against_old_signed_edition(self):
        registry = self._registry()
        reader = reader_with(registry)
        new_auth = registry.edition("FR-8")
        result = resolve_carries(notice(), new_auth, reader, registry)
        self.assertTrue(result.ok)
        self.assertIn("http://x/brand/signed/FR-7.xml", result.evidence)

    def test_carry_fails_when_old_edition_no_longer_checkable(self):
        registry = self._registry()
        reader = reader_with(registry, fr7_status=404)
        assessment = assess_carton(carton(lot=410), registry, [notice()], reader)
        self.assertEqual(assessment.state, LineState.PENDING_SUPERVISOR)
        self.assertIn("沿用字段可核对性", assessment.missing)

    def test_notice_and_xml_carry_mismatch_goes_to_supervisor(self):
        fr8_no_carry = FR8_XML.replace('<carry field="composition" from="FR-7"/>', '')
        from packaging_tool.sources import parse_authorization
        registry = AuthorizationRegistry([
            parse_authorization(FR7_XML, "http://x/brand/signed/FR-7.xml"),
            parse_authorization(fr8_no_carry, "http://x/brand/signed/FR-8.xml"),
        ])
        reader = reader_with(registry, fr8_xml=fr8_no_carry)
        assessment = assess_carton(carton(lot=410), registry, [notice()], reader)
        self.assertEqual(assessment.state, LineState.PENDING_SUPERVISOR)
        self.assertTrue(any("不一致" in reason for reason in assessment.reasons))

    def test_old_claim_missing_for_target_component_blocks_carry(self):
        # 旧版只声明整款 composition、没有组件级 capacity 之外的字段场景：
        # 构造一个旧版缺目标组件字段的沿用要求。
        from packaging_tool.model import Carry
        old_xml = (
            '<authorization id="FR-7" region="EU" lang="fr" issued="2026-11-01" style="G20">'
            '<claim component="H7" field="capacity">18 Wh</claim></authorization>'
        )
        new_xml = (
            '<authorization id="FR-8" region="EU" lang="fr" issued="2026-11-20" style="G20">'
            '<claim component="H7" field="capacity">16 Wh</claim>'
            '<carry field="composition" from="FR-7"/></authorization>'
        )
        from packaging_tool.sources import parse_authorization
        registry = AuthorizationRegistry([
            parse_authorization(old_xml, "http://x/brand/signed/FR-7.xml"),
            parse_authorization(new_xml, "http://x/brand/signed/FR-8.xml"),
        ])
        reader = reader_with(registry, fr7_xml=old_xml, fr8_xml=new_xml)
        assessment = assess_carton(
            carton(lot=410), registry,
            [Notice("N", "G20", "H7", 405, "EU", "FR-8",
                    "http://x/brand/signed/FR-8.xml", "http://x/brand/notices",
                    (Carry("composition", "FR-7", "H7"),))],
            reader)
        self.assertEqual(assessment.state, LineState.PENDING_SUPERVISOR)


class VariantClaimTests(unittest.TestCase):
    def test_variant_specific_claim_takes_precedence_and_does_not_cross(self):
        from packaging_tool.sources import parse_authorization
        xml = (
            '<authorization id="V-1" region="EU" lang="fr" issued="2026-11-01" style="G20">'
            '<claim component="H7" variant="cold" field="capacity">16 Wh</claim>'
            '<claim component="H7" field="capacity">18 Wh</claim>'
            '<claim field="composition">85% PA</claim></authorization>'
        )
        auth = parse_authorization(xml, "http://x/V-1.xml")
        self.assertEqual(
            auth.claim_for("capacity", "H7", "cold").value, "16 Wh")
        # 没有该变体声明时回落到组件级，而不是借用别的变体
        self.assertEqual(
            auth.claim_for("capacity", "H7", "warm").value, "18 Wh")
        self.assertEqual(
            auth.claim_for("composition", "H7", "cold").value, "85% PA")


class ScopeIsolationTests(unittest.TestCase):
    def test_missing_language_authorization_suspends_only_that_line(self):
        from packaging_tool.sources import parse_authorization
        registry = AuthorizationRegistry([
            parse_authorization(FR7_XML, "http://x/brand/signed/FR-7.xml")])
        reader = SourceReader(FakeTransport({}))
        reader.registry = registry
        french = assess_carton(carton(lot=390), registry, [], reader)
        german = assess_carton(carton(lot=390, lang="de"), registry, [], reader)
        self.assertEqual(french.state, LineState.READY)
        self.assertEqual(german.state, LineState.SUSPENDED)
        self.assertEqual(german.missing, ("该语种销售授权",))

    def test_authorization_never_extends_to_another_style_or_region(self):
        from packaging_tool.sources import parse_authorization
        registry = AuthorizationRegistry([
            parse_authorization(FR7_XML, "http://x/brand/signed/FR-7.xml")])
        reader = SourceReader(FakeTransport({}))
        reader.registry = registry
        other_style = assess_carton(carton(style="G99"), registry, [], reader)
        other_region = assess_carton(carton(region="CN", lang="fr"), registry, [], reader)
        self.assertEqual(other_style.state, LineState.SUSPENDED)
        self.assertEqual(other_region.state, LineState.SUSPENDED)


if __name__ == "__main__":
    unittest.main()
