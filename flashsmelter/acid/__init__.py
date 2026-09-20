"""制酸系统监控组件。

烟气过了余热锅炉就送去制酸：转化器把 SO2 氧化成 SO3，吸收塔循环酸吸收成酸。
转化率与酸浓由酸厂自己调，本平台不越俎代庖，但要把两件事看住：

* 前馈：按烟气量与 SO2 浓度提前算好对转化吸收的要求（达标所需最低转化率、
  酸浓窗口、负荷上限），逐条写入前馈要求流水，酸厂接班与调整都有据可依；
* 门控：烟气负荷超出转化吸收能力，或酸厂回传的转化率/酸浓顶到限值时，先把
  炉子往安全侧带，并保持 constrained 状态直到人工确认恢复——确认必须留说明。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..component import Component, ensure_actor
from ..errors import GuardViolation
from ..machine import StateMachine
from ..ports import SafetySidePort
from ..runtime import RuntimeContext

STATES = ("idle", "tracking", "constrained")

TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "idle": ("tracking", "constrained"),
    "tracking": ("constrained",),
    "constrained": ("tracking",),
}

DEMAND_STREAM = "acid/demands"

# 标况下 SO2 密度：摩尔质量 64 kg/kmol ÷ 摩尔体积 22.4 Nm³/kmol。
SO2_KG_PER_NM3 = 64.0 / 22.4


class AcidPlant(Component):
    name = "acid"

    def __init__(self, ctx: RuntimeContext) -> None:
        super().__init__(ctx)
        self._machine = StateMachine("acid", "idle", TRANSITIONS, ctx.clock)
        self._last_forecast: dict[str, Any] | None = None
        self._last_update: dict[str, Any] | None = None
        self._active_violations: list[str] = []
        self._safety_port: SafetySidePort | None = None
        restored = self.restore()
        if restored is not None:
            self._machine.restore(restored)
            forecast = restored.get("last_forecast")
            if isinstance(forecast, dict):
                self._last_forecast = forecast
            update = restored.get("last_update")
            if isinstance(update, dict):
                self._last_update = update
            violations = restored.get("active_violations")
            if isinstance(violations, list):
                self._active_violations = [str(item) for item in violations]
        self._refresh_gauges()

    def bind_safety_port(self, port: SafetySidePort) -> None:
        """注入安全侧执行端口：顶到限值时由它把炉子往安全侧带。"""

        self._safety_port = port

    # ------------------------------------------------------------------ 动作
    def forecast(
        self,
        actor: str,
        *,
        gas_flow_nm3h: float,
        so2_percent: float,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        """前馈：按烟气量与 SO2 浓度给转化吸收提要求，要求逐条落流水。"""

        actor = ensure_actor(actor)
        with self.action(
            "forecast",
            "acid",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            if gas_flow_nm3h <= 0:
                raise GuardViolation("烟气量必须为正", details={"gas_flow_nm3h": gas_flow_nm3h})
            if not 0 < so2_percent <= 100:
                raise GuardViolation(
                    "SO2 浓度超出量程", details={"so2_percent": so2_percent, "max": 100.0}
                )
            so2_load_kgh = gas_flow_nm3h * (so2_percent / 100.0) * SO2_KG_PER_NM3
            allowed_kgh = self.settings.tail_so2_limit_mg_nm3 * gas_flow_nm3h / 1e6
            required = 0.0 if so2_load_kgh <= 0 else max(0.0, 1.0 - allowed_kgh / so2_load_kgh)
            violations: list[str] = []
            if gas_flow_nm3h > self.settings.acid_gas_flow_max_nm3h:
                violations.append("gas-flow-above-design")
            if so2_percent > self.settings.acid_converter_so2_max_percent:
                violations.append("converter-inlet-so2-too-high")
            if required > self.settings.acid_conversion_guaranteed:
                violations.append("conversion-beyond-guarantee")
            demand = {
                "gas_flow_nm3h": round(gas_flow_nm3h, 3),
                "so2_percent": round(so2_percent, 4),
                "so2_load_kgh": round(so2_load_kgh, 3),
                "allowed_tail_kgh": round(allowed_kgh, 3),
                "required_conversion_rate": round(required, 6),
                "guaranteed_conversion_rate": self.settings.acid_conversion_guaranteed,
                "acid_conc93_window": [self.settings.acid_conc93_min, self.settings.acid_conc93_max],
                "acid_conc98_window": [self.settings.acid_conc98_min, self.settings.acid_conc98_max],
                "violations": violations,
                "within_capacity": not violations,
                "at": self.clock.timestamp_iso(),
            }
            self._last_forecast = demand
            # 前馈要求入流水：给转化吸收提的要求逐条留痕，酸厂可回溯。
            self.store.append(DEMAND_STREAM, {**demand, "actor": actor})
            self._evaluate(actor, source="forecast")
            record = self._persist(reason="forecast")
            trace.attach(record).note("within_capacity", not violations).note(
                "violations", list(violations)
            )
            return self.status()

    def update(
        self,
        actor: str,
        *,
        conversion_rate: float,
        acid_conc_93: float,
        acid_conc_98: float,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        """酸厂回传：转化率与 93/98 酸浓，逐项对照前馈要求与酸浓窗口。"""

        actor = ensure_actor(actor)
        with self.action(
            "update",
            "acid",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            if not 0 < conversion_rate < 1:
                raise GuardViolation(
                    "转化率必须是 (0,1) 区间比例", details={"conversion_rate": conversion_rate}
                )
            for label, value in (("acid_conc_93", acid_conc_93), ("acid_conc_98", acid_conc_98)):
                if not 0 < value <= 100:
                    raise GuardViolation("酸浓超出量程", details={label: value, "max": 100.0})
            violations: list[str] = []
            required = (
                None
                if self._last_forecast is None
                else float(self._last_forecast["required_conversion_rate"])
            )
            if required is not None and conversion_rate < required:
                violations.append("conversion-below-requirement")
            if not self.settings.acid_conc93_min <= acid_conc_93 <= self.settings.acid_conc93_max:
                violations.append("acid-93-out-of-window")
            if not self.settings.acid_conc98_min <= acid_conc_98 <= self.settings.acid_conc98_max:
                violations.append("acid-98-out-of-window")
            self._last_update = {
                "conversion_rate": round(conversion_rate, 6),
                "required_conversion_rate": required,
                "acid_conc_93": round(acid_conc_93, 3),
                "acid_conc_98": round(acid_conc_98, 3),
                "violations": violations,
                "at": self.clock.timestamp_iso(),
            }
            self._evaluate(actor, source="update")
            record = self._persist(reason="update")
            trace.attach(record).note("violations", list(violations))
            return self.status()

    def clear(
        self,
        actor: str,
        *,
        note: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        """确认恢复：酸厂调整到位后人工解除 constrained，必须填写处理说明。"""

        actor = ensure_actor(actor)
        with self.action(
            "clear",
            "acid",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._machine.require("constrained", "确认恢复")
            if not note:
                raise GuardViolation("恢复必须填写处理说明")
            if self._active_violations:
                raise GuardViolation(
                    "越限未消除，禁止确认恢复",
                    details={"violations": list(self._active_violations)},
                )
            self._machine.to("tracking", actor, f"恢复跟踪：{note}")
            record = self._persist(reason="clear")
            trace.attach(record).note("note", note)
            return self.status()

    # ------------------------------------------------------------------ 查询
    @property
    def state(self) -> str:
        return self._machine.state

    def feed_guard(self) -> Mapping[str, Any]:
        """喷吹门控：constrained 期间禁止继续向炉内喷吹。"""

        constrained = self._machine.state == "constrained"
        return {
            "ok": not constrained,
            "state": self._machine.state,
            "violations": list(self._active_violations),
            "required_conversion_rate": (
                None
                if self._last_forecast is None
                else self._last_forecast["required_conversion_rate"]
            ),
        }

    def demands(self, *, limit: int = 20) -> list[Mapping[str, Any]]:
        """前馈要求流水：每次 forecast 一条，酸厂接班可逐条回溯。"""

        return [entry.payload for entry in self.store.read_stream(DEMAND_STREAM, limit=limit)]

    def status(self) -> Mapping[str, Any]:
        return {
            "state": self._machine.state,
            "active_violations": list(self._active_violations),
            "last_forecast": None if self._last_forecast is None else dict(self._last_forecast),
            "last_update": None if self._last_update is None else dict(self._last_update),
            "limits": {
                "gas_flow_max_nm3h": self.settings.acid_gas_flow_max_nm3h,
                "converter_so2_max_percent": self.settings.acid_converter_so2_max_percent,
                "conversion_guaranteed": self.settings.acid_conversion_guaranteed,
                "acid_conc93_window": [self.settings.acid_conc93_min, self.settings.acid_conc93_max],
                "acid_conc98_window": [self.settings.acid_conc98_min, self.settings.acid_conc98_max],
                "tail_so2_limit_mg_nm3": self.settings.tail_so2_limit_mg_nm3,
            },
            "history": list(self._machine.history),
        }

    # ------------------------------------------------------------------ 内部
    def _evaluate(self, actor: str, *, source: str) -> None:
        """综合前馈与回传数据判定状态；顶到限值先把炉子往安全侧带。

        constrained 是保持型的：数据回到限值内只清空越限项，状态必须等
        ``clear`` 人工确认后才回到 tracking，处理说明随之落盘。
        """

        violations: list[str] = []
        if self._last_forecast is not None:
            violations.extend(self._last_forecast["violations"])
        if self._last_update is not None:
            violations.extend(self._last_update["violations"])
        merged = sorted(set(violations))
        if merged:
            entering = self._machine.state != "constrained"
            changed = merged != self._active_violations
            self._active_violations = merged
            if entering:
                self._machine.to("constrained", actor, f"越限: {', '.join(merged)}")
            if (entering or changed) and self._safety_port is not None:
                self._safety_port.bring_to_safe_side(
                    actor,
                    reason=f"制酸越限({source}): {', '.join(merged)}",
                    source="acid",
                )
        else:
            self._active_violations = []
            if self._machine.state == "idle":
                self._machine.to("tracking", actor, "烟气条件在转化吸收能力内")

    def _persist(self, *, reason: str) -> Any:
        payload = {
            "state": self._machine.state,
            "reason": reason,
            "written_epoch": self.clock.timestamp(),
            "written_at": self.clock.timestamp_iso(),
            "active_violations": list(self._active_violations),
            "last_forecast": self._last_forecast,
            "last_update": self._last_update,
            "history": list(self._machine.history),
        }
        record = self.persist_state(payload)
        self._refresh_gauges()
        return record

    def _refresh_gauges(self) -> None:
        self.metrics.observe("acid.state_code", float(STATES.index(self._machine.state)))
        self.metrics.observe("acid.active_violations", float(len(self._active_violations)))


__all__ = ["AcidPlant", "STATES", "TRANSITIONS", "DEMAND_STREAM", "SO2_KG_PER_NM3"]
