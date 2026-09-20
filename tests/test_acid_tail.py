"""制酸前馈、尾气监视、往安全侧带联动与超排证据闭环。"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest

from flashsmelter.application import Application
from flashsmelter.errors import GuardViolation, NotFoundError
from flashsmelter.runtime import iso_from_epoch

from .helpers import feed_heat, make_app, make_root, start_furnace


class AcidForecastTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()

    def test_forecast_within_capacity_tracks_and_logs_demand(self) -> None:
        status = self.app.acid.forecast("ops", gas_flow_nm3h=100000.0, so2_percent=8.0)
        self.assertEqual("tracking", status["state"])
        forecast = status["last_forecast"]
        self.assertTrue(forecast["within_capacity"])
        self.assertAlmostEqual(0.99825, forecast["required_conversion_rate"], places=4)
        self.assertEqual([], forecast["violations"])
        demands = self.app.acid.demands()
        self.assertEqual(1, len(demands))
        self.assertEqual(8.0, demands[0]["so2_percent"])
        self.assertEqual("ops", demands[0]["actor"])

    def test_forecast_beyond_capacity_constrains(self) -> None:
        status = self.app.acid.forecast("ops", gas_flow_nm3h=100000.0, so2_percent=11.0)
        self.assertEqual("constrained", status["state"])
        self.assertIn("conversion-beyond-guarantee", status["active_violations"])
        status = self.app.acid.forecast("ops", gas_flow_nm3h=130000.0, so2_percent=13.0)
        self.assertIn("gas-flow-above-design", status["active_violations"])
        self.assertIn("converter-inlet-so2-too-high", status["active_violations"])

    def test_forecast_rejects_invalid_inputs(self) -> None:
        with self.assertRaises(GuardViolation):
            self.app.acid.forecast("ops", gas_flow_nm3h=0.0, so2_percent=8.0)
        with self.assertRaises(GuardViolation):
            self.app.acid.forecast("ops", gas_flow_nm3h=100000.0, so2_percent=0.0)
        with self.assertRaises(GuardViolation):
            self.app.acid.forecast("ops", gas_flow_nm3h=100000.0, so2_percent=101.0)

    def test_constrained_derates_furnace_and_blocks_feed(self) -> None:
        start_furnace(self.app)
        feed_heat(self.app, "H-1")
        self.assertTrue(self.app.conc.is_flowing())
        self.app.acid.forecast("ops", gas_flow_nm3h=100000.0, so2_percent=11.0)
        self.assertEqual("constrained", self.app.acid.state)
        self.assertEqual("paused", self.app.conc.state)
        derate = self.app.furnace.status()["last_derate"]
        self.assertEqual("acid", derate["source"])
        self.assertTrue(derate["feed_paused"])
        with self.assertRaises(GuardViolation) as blocked:
            self.app.furnace.feed("ops", heat_id="H-1", rate_tph=150.0, tons=100.0)
        self.assertIn("acid", blocked.exception.details)

    def test_clear_requires_note_and_clean_conditions(self) -> None:
        self.app.acid.forecast("ops", gas_flow_nm3h=100000.0, so2_percent=11.0)
        with self.assertRaises(GuardViolation):
            self.app.acid.clear("ops", note="")
        with self.assertRaises(GuardViolation) as still_bad:
            self.app.acid.clear("ops", note="已调整")
        self.assertIn("violations", still_bad.exception.details)
        # 烟气条件回到转化吸收能力内：状态仍保持 constrained，待人工确认。
        self.app.acid.forecast("ops", gas_flow_nm3h=100000.0, so2_percent=8.0)
        self.assertEqual("constrained", self.app.acid.state)
        status = self.app.acid.clear("ops", note="转化器提温完成，负荷已降")
        self.assertEqual("tracking", status["state"])
        self.assertEqual([], status["active_violations"])

    def test_update_checks_conversion_and_acid_windows(self) -> None:
        self.app.acid.forecast("ops", gas_flow_nm3h=100000.0, so2_percent=8.0)
        status = self.app.acid.update(
            "acid-plant", conversion_rate=0.999, acid_conc_93=93.0, acid_conc_98=98.0
        )
        self.assertEqual("tracking", status["state"])
        status = self.app.acid.update(
            "acid-plant", conversion_rate=0.999, acid_conc_93=94.5, acid_conc_98=98.0
        )
        self.assertEqual("constrained", status["state"])
        self.assertIn("acid-93-out-of-window", status["active_violations"])
        status = self.app.acid.update(
            "acid-plant", conversion_rate=0.999, acid_conc_93=93.0, acid_conc_98=99.5
        )
        self.assertIn("acid-98-out-of-window", status["active_violations"])
        self.app.acid.update("acid-plant", conversion_rate=0.999, acid_conc_93=93.0, acid_conc_98=98.0)
        status = self.app.acid.clear("ops", note="酸浓已调回窗口")
        self.assertEqual("tracking", status["state"])

    def test_update_conversion_below_requirement_constrains(self) -> None:
        self.app.acid.forecast("ops", gas_flow_nm3h=100000.0, so2_percent=8.0)
        status = self.app.acid.update(
            "acid-plant", conversion_rate=0.997, acid_conc_93=93.0, acid_conc_98=98.0
        )
        self.assertEqual("constrained", status["state"])
        self.assertIn("conversion-below-requirement", status["active_violations"])

    def test_update_rejects_invalid_inputs(self) -> None:
        with self.assertRaises(GuardViolation):
            self.app.acid.update(
                "acid-plant", conversion_rate=1.2, acid_conc_93=93.0, acid_conc_98=98.0
            )
        with self.assertRaises(GuardViolation):
            self.app.acid.update(
                "acid-plant", conversion_rate=0.999, acid_conc_93=0.0, acid_conc_98=98.0
            )

    def test_state_survives_restart(self) -> None:
        root = make_root()
        app = make_app(root=root)
        app.acid.forecast("ops", gas_flow_nm3h=100000.0, so2_percent=11.0)
        restarted = Application(app.settings, clock=app.clock)
        self.assertEqual("constrained", restarted.acid.state)
        self.assertIn("conversion-beyond-guarantee", restarted.acid.status()["active_violations"])
        self.assertTrue(restarted.store.verify().ok)


class TailGasTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()

    def test_reading_levels(self) -> None:
        self.assertEqual("normal", self.app.tail.reading("cems", so2_mg_nm3=100.0)["state"])
        self.assertEqual("watch", self.app.tail.reading("cems", so2_mg_nm3=350.0)["state"])
        self.assertEqual("exceed", self.app.tail.reading("cems", so2_mg_nm3=450.0)["state"])
        status = self.app.tail.reading("cems", so2_mg_nm3=200.0)
        self.assertEqual("normal", status["state"])
        self.assertIsNone(status["active_episode"])

    def test_reading_rejects_negative_and_future(self) -> None:
        with self.assertRaises(GuardViolation):
            self.app.tail.reading("cems", so2_mg_nm3=-1.0)
        future = iso_from_epoch(self.app.clock.timestamp() + 600.0)
        with self.assertRaises(GuardViolation):
            self.app.tail.reading("cems", so2_mg_nm3=100.0, observed_at=future)

    def test_exceed_derates_furnace_and_blocks_feed(self) -> None:
        start_furnace(self.app)
        feed_heat(self.app, "H-1")
        self.assertTrue(self.app.conc.is_flowing())
        status = self.app.tail.reading("cems", so2_mg_nm3=500.0)
        self.assertEqual("exceed", status["state"])
        self.assertEqual("EX-0001", status["active_episode"]["episode_id"])
        self.assertEqual("paused", self.app.conc.state)
        derate = self.app.furnace.status()["last_derate"]
        self.assertEqual("tail", derate["source"])
        self.assertTrue(derate["feed_paused"])
        with self.assertRaises(GuardViolation) as blocked:
            self.app.furnace.feed("ops", heat_id="H-1", rate_tph=150.0, tons=100.0)
        self.assertIn("tail", blocked.exception.details)

    def test_episode_close_and_disposition_leave_evidence(self) -> None:
        self.app.tail.reading("cems", so2_mg_nm3=500.0)
        self.app.clock.advance(30.0)
        self.app.tail.reading("cems", so2_mg_nm3=600.0)
        self.app.clock.advance(30.0)
        status = self.app.tail.reading("cems", so2_mg_nm3=100.0)
        self.assertEqual("normal", status["state"])
        pending = status["pending_dispositions"]
        self.assertEqual(1, len(pending))
        episode = pending[0]
        self.assertEqual("EX-0001", episode["episode_id"])
        self.assertEqual(600.0, episode["peak_mg_nm3"])
        self.assertAlmostEqual(550.0, episode["avg_mg_nm3"], places=3)
        self.assertEqual(2, episode["samples"])
        self.assertAlmostEqual(60.0, episode["duration_seconds"], places=3)
        status = self.app.tail.disposition(
            "shift-lead",
            episode_id="EX-0001",
            cause="转化器一段入口温度偏低，转化率下滑",
            measures="转化器提温、炉子降负荷，复查转化率回稳",
        )
        self.assertEqual([], status["pending_dispositions"])
        self.assertEqual(1, len(status["completed"]))
        evidence = self.app.tail.exceedances()
        self.assertEqual(["open", "close", "disposition"], [item["type"] for item in evidence])
        self.assertEqual("shift-lead", evidence[-1]["actor"])
        self.assertTrue(self.app.store.verify().ok)

    def test_disposition_requires_pending_episode_and_fields(self) -> None:
        self.app.tail.reading("cems", so2_mg_nm3=500.0)
        self.app.tail.reading("cems", so2_mg_nm3=100.0)
        with self.assertRaises(GuardViolation):
            self.app.tail.disposition("ops", episode_id="EX-0001", cause="", measures="x")
        with self.assertRaises(NotFoundError):
            self.app.tail.disposition("ops", episode_id="EX-9999", cause="c", measures="m")
        self.app.tail.disposition("ops", episode_id="EX-0001", cause="c", measures="m")
        with self.assertRaises(NotFoundError):
            self.app.tail.disposition("ops", episode_id="EX-0001", cause="c", measures="m")

    def test_feed_allowed_again_after_reading_drops(self) -> None:
        start_furnace(self.app)
        feed_heat(self.app, "H-1")
        self.app.tail.reading("cems", so2_mg_nm3=500.0)
        self.assertEqual("paused", self.app.conc.state)
        self.app.tail.reading("cems", so2_mg_nm3=150.0)
        feed_heat(self.app, "H-1", tons=100.0)
        self.assertTrue(self.app.conc.is_flowing())

    def test_active_episode_survives_restart(self) -> None:
        root = make_root()
        app = make_app(root=root)
        app.tail.reading("cems", so2_mg_nm3=500.0)
        restarted = Application(app.settings, clock=app.clock)
        status = restarted.tail.status()
        self.assertEqual("exceed", status["state"])
        self.assertEqual("EX-0001", status["active_episode"]["episode_id"])
        restarted.clock.advance(45.0)
        restarted.tail.reading("cems", so2_mg_nm3=100.0)
        pending = restarted.tail.status()["pending_dispositions"]
        self.assertEqual(1, len(pending))
        self.assertAlmostEqual(45.0, pending[0]["duration_seconds"], places=3)
        self.assertTrue(restarted.store.verify().ok)

    def test_actions_registered_on_application(self) -> None:
        result = self.app.invoke(
            "acid.forecast", {"actor": "ops", "gas_flow_nm3h": 100000.0, "so2_percent": 8.0}
        )
        self.assertEqual("tracking", result["state"])
        result = self.app.invoke("tail.reading", {"so2_mg_nm3": 500.0})
        self.assertEqual("exceed", result["state"])
        names = set(self.app.actions)
        for action in (
            "acid.forecast",
            "acid.update",
            "acid.clear",
            "tail.reading",
            "tail.disposition",
        ):
            self.assertIn(action, names)


class EmissionsCliTest(unittest.TestCase):
    def test_emissions_command_prints_evidence(self) -> None:
        root = make_root("flashsmelter-cli-")
        reading = subprocess.run(
            [
                sys.executable,
                "-m",
                "flashsmelter",
                "--root",
                str(root),
                "call",
                "tail.reading",
                "--param",
                "so2_mg_nm3=500",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        self.assertEqual(0, reading.returncode, msg=reading.stderr)
        dropped = subprocess.run(
            [
                sys.executable,
                "-m",
                "flashsmelter",
                "--root",
                str(root),
                "call",
                "tail.reading",
                "--param",
                "so2_mg_nm3=100",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        self.assertEqual(0, dropped.returncode, msg=dropped.stderr)
        completed = subprocess.run(
            [sys.executable, "-m", "flashsmelter", "--root", str(root), "emissions"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        self.assertEqual(0, completed.returncode, msg=completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(["open", "close"], [item["type"] for item in payload["exceedances"]])
        self.assertEqual(1, len(payload["tail"]["pending_dispositions"]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
