"""制酸与尾气联动：前馈负荷门控、硬线安全侧处置、排放事件留证与恢复。"""

from __future__ import annotations

import unittest

from flashsmelter.application import Application
from flashsmelter.errors import GuardViolation, StateTransitionError

from .helpers import feed_heat, make_app, make_root, settle_pool, start_furnace

# 一组高 SO2 负荷但尚未越线的正常样本：50000 Nm3/h、12% SO2、98.5% 酸、尾气 80 mg/m3。
NORMAL_SAMPLE = dict(
    gas_flow_nm3h=50000.0,
    so2_fraction=0.12,
    acid_strength=0.985,
    tail_so2_mgm3=80.0,
)

OVER_LIMIT_SAMPLE = dict(
    gas_flow_nm3h=50000.0,
    so2_fraction=0.12,
    acid_strength=0.985,
    tail_so2_mgm3=450.0,  # 超过 400 mg/Nm3 排放限值
)

ACID_LOW_SAMPLE = dict(
    gas_flow_nm3h=50000.0,
    so2_fraction=0.12,
    acid_strength=0.970,  # 低于硬下限 0.975
    tail_so2_mgm3=120.0,
)

CLEAR_SAMPLE = dict(
    gas_flow_nm3h=50000.0,
    so2_fraction=0.12,
    acid_strength=0.985,
    tail_so2_mgm3=60.0,  # 低于解除阈值 100
)


def bring_acid_online(app: Application) -> None:
    app.acid.set_baseline("tester", value=0.12, source="so2-analyzer-a")
    app.acid.online("tester")


class AcidDemandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()

    def test_offline_demand_is_advisory_only(self) -> None:
        demand = self.app.acid.demand()
        self.assertFalse(demand["available"])
        gate = self.app.acid.feed_gate()
        self.assertTrue(gate["allowed"])
        self.assertEqual("offline", gate["mode"])

    def test_so2_mass_load_and_feed_cap(self) -> None:
        bring_acid_online(self.app)
        status = self.app.acid.sample("analyzer", **NORMAL_SAMPLE)
        # 50000 × 0.12 × 64/22.4 ≈ 1714.3 kg/h
        self.assertAlmostEqual(50000.0 * 0.12 * 64.0 / 22.4, status["so2_load_kgph"], places=1)
        demand = status["demand"]
        self.assertTrue(demand["available"])
        self.assertGreater(demand["required_conversion"], self.app.settings.acid_required_conversion_floor)
        self.assertEqual(self.app.settings.acid_acid_strength_target, demand["target_acid_strength"])
        # 低负荷下喷吹上限宽裕（高于常用喷吹速率即不构成约束）
        self.assertGreater(demand["feed_cap_tph"], 140.0)
        self.assertEqual("normal", status["state"])

    def test_high_load_caps_feed_rate(self) -> None:
        bring_acid_online(self.app)
        # 70000 Nm3/h × 18% SO2 ≈ 3600 kg/h，超过设计 3000×0.85=2550 kg/h
        high = dict(NORMAL_SAMPLE, gas_flow_nm3h=70000.0, so2_fraction=0.18)
        status = self.app.acid.sample("analyzer", **high)
        demand = status["demand"]
        self.assertTrue(demand["high_load"])
        self.assertLess(demand["feed_cap_tph"], self.app.settings.feed_rate_max_tph)
        self.assertEqual("load-capped", status["feed_gate"]["mode"])

    def test_warning_band_does_not_block(self) -> None:
        bring_acid_online(self.app)
        warning = dict(NORMAL_SAMPLE, tail_so2_mgm3=250.0)  # 200~400 预警带
        status = self.app.acid.sample("analyzer", **warning)
        self.assertEqual("warning", status["state"])
        self.assertTrue(status["feed_gate"]["allowed"])

    def test_stale_sample_freezes_rate_increase(self) -> None:
        start_furnace(self.app)
        bring_acid_online(self.app)
        settle_pool(self.app)
        self.app.acid.sample("analyzer", **NORMAL_SAMPLE)
        self.app.furnace.feed("ops", heat_id="H-1", rate_tph=120.0, tons=100.0)
        self.app.clock.advance(self.app.settings.acid_sample_stale_seconds + 1)
        gate = self.app.acid.feed_gate()
        self.assertTrue(gate["stale"])
        self.assertTrue(gate["allowed"])
        # 加负荷被拒，维持/降负荷允许
        with self.assertRaises(GuardViolation):
            self.app.furnace.feed("ops", heat_id="H-1", rate_tph=150.0, tons=50.0)
        self.app.furnace.feed("ops", heat_id="H-1", rate_tph=100.0, tons=50.0)


class AcidFurnaceGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()
        start_furnace(self.app)
        bring_acid_online(self.app)
        settle_pool(self.app)

    def test_high_load_feed_rate_is_rejected(self) -> None:
        high = dict(NORMAL_SAMPLE, gas_flow_nm3h=70000.0, so2_fraction=0.18)
        self.app.acid.sample("analyzer", **high)
        cap = self.app.acid.demand()["feed_cap_tph"]
        with self.assertRaises(GuardViolation) as blocked:
            self.app.furnace.feed("ops", heat_id="H-1", rate_tph=150.0, tons=100.0)
        self.assertEqual(cap, blocked.exception.details["max_rate_tph"])
        # 按上限以下速率仍允许
        self.app.furnace.feed("ops", heat_id="H-1", rate_tph=min(cap - 1, 150.0), tons=100.0)
        self.assertEqual("smelting", self.app.furnace.state)

    def test_stale_sample_blocks_rate_increase(self) -> None:
        self.app.acid.sample("analyzer", **NORMAL_SAMPLE)
        self.app.furnace.feed("ops", heat_id="H-1", rate_tph=120.0, tons=100.0)
        self.app.clock.advance(self.app.settings.acid_sample_stale_seconds + 1)
        with self.assertRaises(GuardViolation):
            self.app.furnace.feed("ops", heat_id="H-1", rate_tph=150.0, tons=100.0)

    def test_over_limit_triggers_furnace_safe_side(self) -> None:
        feed_heat(self.app, "H-1", tons=200.0)
        self.assertEqual("injecting", self.app.conc.state)
        status = self.app.acid.sample("analyzer", **OVER_LIMIT_SAMPLE)
        self.assertEqual("latched", status["state"])
        self.assertEqual("tail-so2-over-limit", status["latch_reason"])
        # 炉子已被带到安全侧：喷吹停、富氧降
        self.assertEqual("safeguarded", self.app.furnace.state)
        self.assertEqual("stopped", self.app.conc.state)
        self.assertEqual("degraded", self.app.oxygen.state)
        self.assertIn("acid-latched", status["feed_gate"]["blockers"])
        # 联锁期间任何喷吹都被硬拒
        self.app.clock.advance(self.app.settings.furnace_min_smelt_dwell_seconds)
        with self.assertRaises(GuardViolation):
            self.app.furnace.feed("ops", heat_id="H-1", rate_tph=80.0, tons=50.0)


class AcidIncidentEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()
        start_furnace(self.app)
        bring_acid_online(self.app)
        settle_pool(self.app)
        feed_heat(self.app, "H-1", tons=200.0)

    def _incident_id(self) -> str:
        incident_id = self.app.acid.status()["open_incident_id"]
        self.assertIsNotNone(incident_id)
        return str(incident_id)

    def test_incident_records_overrun_samples_and_dispositions(self) -> None:
        self.app.acid.sample("analyzer", **OVER_LIMIT_SAMPLE)
        incident_id = self._incident_id()
        # 超标持续期间再来两个样本（同一事件续期，峰值取最大）
        self.app.clock.advance(30)
        self.app.acid.sample("analyzer", **dict(OVER_LIMIT_SAMPLE, tail_so2_mgm3=520.0))
        self.app.acid.record_disposition("ops", note="联系制酸降转化器一段温度、加碱液喷淋")
        bundle = self.app.acid.evidence_bundle(incident_id)
        self.assertEqual("ACI-0001", incident_id)
        kinds = [entry.get("kind") for entry in bundle["timeline"]]
        self.assertIn("sample", kinds)
        self.assertIn("furnace-safe-side", kinds)
        self.assertIn("disposition", kinds)
        header = bundle["header"]
        self.assertAlmostEqual(520.0, header["peak_tail_so2_mgm3"], places=2)
        self.assertEqual("open", header["status"])
        self.assertTrue(bundle["entries"] >= 4)

    def test_reset_requires_hold_note_and_recovery(self) -> None:
        self.app.acid.sample("analyzer", **OVER_LIMIT_SAMPLE)
        with self.assertRaises(GuardViolation) as early:
            self.app.acid.reset("ops", note="处置完成")
        self.assertIn("remaining_seconds", early.exception.details)
        self.app.clock.advance(self.app.settings.acid_min_hold_seconds + 1)
        # 仍超标不允许复位
        with self.assertRaises(GuardViolation):
            self.app.acid.reset("ops", note="处置完成")
        # 无说明不允许复位
        clear = CLEAR_SAMPLE
        self.app.acid.sample("analyzer", **clear)
        with self.assertRaises(GuardViolation):
            self.app.acid.reset("ops", note="")
        acid_status = self.app.acid.reset("ops", note="转化器温度恢复，尾气回落")
        self.assertIn(acid_status["state"], ("normal", "warning"))

    def test_incident_autocloses_after_clear_dwell(self) -> None:
        self.app.acid.sample("analyzer", **OVER_LIMIT_SAMPLE)
        incident_id = self._incident_id()
        self.app.clock.advance(self.app.settings.acid_min_hold_seconds + 1)
        self.app.acid.sample("analyzer", **CLEAR_SAMPLE)
        self.app.acid.reset("ops", note="转化器温度恢复，尾气回落")
        # 未连续达标足够时长，不能手动关闭
        with self.assertRaises(GuardViolation) as waiting:
            self.app.acid.close_incident("ops", note="关闭")
        self.assertIn("clear-dwell-not-satisfied", waiting.exception.details["blockers"])
        # 连续低于解除阈值达到 dwell 后自动关闭
        self.app.clock.advance(self.app.settings.acid_clear_dwell_seconds + 1)
        self.app.acid.sample("analyzer", **CLEAR_SAMPLE)
        self.assertIsNone(self.app.acid.status()["open_incident_id"])
        bundle = self.app.acid.evidence_bundle(incident_id)
        self.assertEqual(1, len(bundle["closures"]))
        self.assertEqual("auto-close", bundle["closures"][0]["how"])

    def test_rebound_during_dwell_restarts_clock(self) -> None:
        self.app.acid.sample("analyzer", **OVER_LIMIT_SAMPLE)
        self.app.clock.advance(self.app.settings.acid_min_hold_seconds + 1)
        self.app.acid.sample("analyzer", **CLEAR_SAMPLE)
        self.app.acid.reset("ops", note="处置")
        self.app.clock.advance(self.app.settings.acid_clear_dwell_seconds - 10)
        # 再次冲高（仍打开同一事件），重新联锁
        self.app.acid.sample("analyzer", **OVER_LIMIT_SAMPLE)
        self.assertEqual("latched", self.app.acid.state)
        self.assertEqual("safeguarded", self.app.furnace.state)
        self.app.clock.advance(self.app.settings.acid_min_hold_seconds + 1)
        self.app.acid.sample("analyzer", **CLEAR_SAMPLE)
        self.app.acid.reset("ops", note="再次处置")
        remaining = self.app.acid.status()["clear_dwell_remaining_seconds"]
        self.assertGreater(remaining, 0.0)

    def test_acid_strength_low_also_latches(self) -> None:
        status = self.app.acid.sample("analyzer", **ACID_LOW_SAMPLE)
        self.assertEqual("latched", status["state"])
        self.assertEqual("acid-strength-low", status["latch_reason"])
        self.assertEqual("safeguarded", self.app.furnace.state)


class ManualSafeguardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()
        start_furnace(self.app)
        bring_acid_online(self.app)
        settle_pool(self.app)
        feed_heat(self.app, "H-1", tons=200.0)

    def test_manual_safeguard_and_release_cycle(self) -> None:
        self.app.acid.sample("analyzer", **NORMAL_SAMPLE)
        status = self.app.acid.safeguard("ops", reason="制酸主风机跳闸")
        self.assertEqual("latched", status["state"])
        self.assertEqual("safeguarded", self.app.furnace.state)
        incident_id = status["open_incident_id"]
        # 未复位不能解除安全态
        with self.assertRaises(GuardViolation):
            self.app.furnace.release_safeguard("ops", note="恢复")
        self.app.clock.advance(self.app.settings.acid_min_hold_seconds + 1)
        self.app.acid.sample("analyzer", **CLEAR_SAMPLE)
        self.app.acid.reset("ops", note="主风机恢复，工况正常")
        # 此时事件未关但门控已允许，可以解除安全态恢复生产
        furnace = self.app.furnace.release_safeguard("ops", note="尾气正常，恢复喷吹")
        self.assertEqual(furnace["state"], "smelting")
        self.app.clock.advance(self.app.settings.acid_clear_dwell_seconds + 1)
        self.app.acid.sample("analyzer", **CLEAR_SAMPLE)
        self.assertIsNone(self.app.acid.status()["open_incident_id"])
        self.assertIsNotNone(incident_id)

    def test_double_safeguard_rejected(self) -> None:
        self.app.acid.safeguard("ops", reason="x")
        with self.assertRaises(StateTransitionError):
            self.app.acid.safeguard("ops", reason="y")


class AcidRestartTest(unittest.TestCase):
    def test_open_incident_survives_restart(self) -> None:
        root = make_root()
        app = make_app(root=root)
        start_furnace(app)
        bring_acid_online(app)
        settle_pool(app)
        feed_heat(app, "H-1", tons=200.0)
        app.acid.sample("analyzer", **OVER_LIMIT_SAMPLE)
        incident_id = app.acid.status()["open_incident_id"]

        restarted = Application(app.settings, clock=app.clock)
        self.assertEqual("latched", restarted.acid.state)
        self.assertEqual(incident_id, restarted.acid.status()["open_incident_id"])
        self.assertEqual("safeguarded", restarted.furnace.state)
        self.assertEqual("stopped", restarted.conc.state)
        bundle = restarted.acid.evidence_bundle(str(incident_id))
        self.assertTrue(bundle["entries"] >= 1)
        self.assertTrue(restarted.store.verify().ok)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
