"""制酸与尾气监护组件。

烟气离开余热锅炉后进入制酸段。组件在冶炼侧把三件事管起来：

1. **前馈提要求**：按入口烟气量与 SO2 浓度算出 SO2 质量负荷，提前给转化吸收段
   下达所需转化率/酸浓目标，并在高负荷时反推冶炼喷吹上限，而不是等尾气超标
   再打电话；
2. **越线先带安全侧**：酸浓或尾气顶到限值（硬线）时，联锁保持并要求炉子先
   暂停喷吹、降富氧，把烟气负荷带下来；
3. **全程留证**：每次硬线超标开立一条排放事件（incident），超标时段的全部
   测点样本、每一步处置动作与复位/解除都逐条进事件流水，事件只能在尾气连续
   低于解除阈值足够时长后关闭。

和其它组件一样：意图先落盘再动作，复位必须写处理说明并满足最短保持时长。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..component import Component, ensure_actor
from ..errors import GuardViolation, StateTransitionError
from ..machine import StateMachine
from ..ports import FurnaceSafetyPort
from ..runtime import RuntimeContext

STATES = ("offline", "normal", "warning", "latched")

TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "offline": ("normal", "warning"),
    "normal": ("warning", "latched", "offline"),
    "warning": ("normal", "latched", "offline"),
    "latched": ("normal", "warning"),
}

INCIDENT_STREAM = "acid/incidents"

# 标态下 1 标准立方米 SO2 的千克数（摩尔质量 64 kg/kmol ÷ 22.4 Nm3/kmol）。
SO2_KG_PER_NM3 = 64.0 / 22.4

# 事件时间线在组件状态里保留的条数；完整明细始终在事件流水里。
TIMELINE_CAP = 64
RECENT_INCIDENT_CAP = 8


class AcidPlant(Component):
    name = "acid"

    def __init__(self, ctx: RuntimeContext, *, furnace: FurnaceSafetyPort | None = None) -> None:
        super().__init__(ctx)
        self._machine = StateMachine("acid", "offline", TRANSITIONS, ctx.clock)
        self._furnace = furnace
        self._gas_flow_nm3h: float | None = None
        self._so2_fraction: float | None = None
        self._acid_strength: float | None = None
        self._tail_so2_mgm3: float | None = None
        self._baseline_value: float | None = None
        self._baseline_at: str | None = None
        self._baseline_epoch: float | None = None
        self._baseline_source: str | None = None
        self._last_sample_at: str | None = None
        self._last_sample_epoch: float | None = None
        self._open_incident: dict[str, Any] | None = None
        self._clear_since_epoch: float | None = None
        self._latch_reason: str | None = None
        self._latched_at: float | None = None
        self._latch_count = 0
        self._sample_count = 0
        self._incident_count = 0
        self._recent_incidents: list[dict[str, Any]] = []
        restored = self.restore()
        if restored is not None:
            self._machine.restore(restored)
            self._gas_flow_nm3h = restored.get("gas_flow_nm3h")
            self._so2_fraction = restored.get("so2_fraction")
            self._acid_strength = restored.get("acid_strength")
            self._tail_so2_mgm3 = restored.get("tail_so2_mgm3")
            baseline = restored.get("baseline") or {}
            if baseline:
                self._baseline_value = baseline.get("value")
                self._baseline_at = baseline.get("at")
                self._baseline_epoch = baseline.get("epoch")
                self._baseline_source = baseline.get("source")
            self._last_sample_at = restored.get("last_sample_at")
            self._last_sample_epoch = restored.get("last_sample_epoch")
            self._open_incident = restored.get("open_incident")
            self._clear_since_epoch = restored.get("clear_since_epoch")
            self._latch_reason = restored.get("latch_reason")
            self._latched_at = restored.get("latched_at")
            self._latch_count = int(restored.get("latch_count", 0))
            self._sample_count = int(restored.get("sample_count", 0))
            self._incident_count = int(restored.get("incident_count", 0))
            recent = restored.get("recent_incidents")
            if isinstance(recent, list):
                self._recent_incidents = [entry for entry in recent if isinstance(entry, dict)]
        self._refresh_gauges()

    def bind_furnace(self, furnace: FurnaceSafetyPort) -> None:
        """注入炉子安全侧端口：制酸硬线越限时由炉子执行降负荷。"""

        self._furnace = furnace

    # ------------------------------------------------------------------ 动作
    def online(
        self,
        actor: str,
        *,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "online",
            "acid",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            if self._machine.state != "offline":
                raise StateTransitionError("制酸段已在运行", details={"state": self._machine.state})
            self._require_baseline_fresh()
            self._machine.to("normal", actor, "制酸段投运，分析仪基线有效")
            record = self._persist(reason="online")
            trace.attach(record)
            return self.status()

    def offline(
        self,
        actor: str,
        *,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "offline",
            "acid",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
            bump_generation=True,
        ) as trace:
            if self._machine.state == "offline":
                raise StateTransitionError("制酸段已离线", details={"state": self._machine.state})
            if self._machine.state == "latched":
                raise GuardViolation("联锁未处置复位，禁止直接停运制酸段")
            if self._open_incident is not None:
                raise GuardViolation("仍有未关闭的排放事件，禁止停运制酸段")
            self._machine.to("offline", actor, "制酸段停运")
            record = self._persist(reason="offline")
            trace.attach(record)
            return self.status()

    def set_baseline(
        self,
        actor: str,
        *,
        value: float,
        source: str,
        observed_at: str | None = None,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "set_baseline",
            "acid",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            if not 0.0 <= value <= 1.0:
                raise GuardViolation("SO2 分析仪基线必须是 0~1 的体积分数", details={"value": value})
            if not source:
                raise GuardViolation("基线必须标注来源")
            from ..runtime import epoch_from_iso

            epoch = self.clock.timestamp()
            at = observed_at or self.clock.timestamp_iso()
            if observed_at is not None:
                epoch = epoch_from_iso(observed_at)
                if epoch > self.clock.timestamp():
                    raise GuardViolation("基线观测时间晚于当前时刻")
            self._baseline_value = float(value)
            self._baseline_at = at
            self._baseline_epoch = epoch
            self._baseline_source = source
            record = self._persist(reason="set_baseline")
            trace.attach(record).note("value", value).note("source", source)
            return self.status()

    def sample(
        self,
        actor: str,
        *,
        gas_flow_nm3h: float,
        so2_fraction: float,
        acid_strength: float,
        tail_so2_mgm3: float,
        observed_at: str | None = None,
        correlation_id: str | None = None,
    ) -> Mapping[str, Any]:
        """录入一组入口烟气/酸浓/尾气测点，按分级规则即时处置并留证。"""

        actor = ensure_actor(actor)
        with self.action("sample", "acid", actor, correlation_id=correlation_id) as trace:
            if self._machine.state == "offline":
                raise StateTransitionError(
                    "制酸段未投运，不能录入样本", details={"state": self._machine.state}
                )
            self._validate_readings(gas_flow_nm3h, so2_fraction, acid_strength, tail_so2_mgm3)
            from ..runtime import epoch_from_iso

            epoch = self.clock.timestamp() if observed_at is None else epoch_from_iso(observed_at)
            if epoch > self.clock.timestamp():
                raise GuardViolation("样本观测时间晚于当前时刻")
            if self._last_sample_epoch is not None and epoch < float(self._last_sample_epoch):
                raise GuardViolation(
                    "样本时间早于最近一次样本，属于滞后样本，已拒绝入库",
                    details={"previous_at": self._last_sample_at},
                )
            self._gas_flow_nm3h = gas_flow_nm3h
            self._so2_fraction = so2_fraction
            self._acid_strength = acid_strength
            self._tail_so2_mgm3 = tail_so2_mgm3
            self._last_sample_at = observed_at or self.clock.timestamp_iso()
            self._last_sample_epoch = epoch
            self._sample_count += 1

            classification = self._classify(acid_strength, tail_so2_mgm3)
            band = str(classification["band"])
            hard_reasons = list(classification["hard_reasons"])
            load_kgph = self.so2_mass_load_kgph()
            conversion_efficiency = self._conversion_efficiency(
                gas_flow_nm3h, so2_fraction, tail_so2_mgm3
            )
            sample_record = {
                "seq": self._sample_count,
                "at": self._last_sample_at,
                "epoch": epoch,
                "gas_flow_nm3h": round(gas_flow_nm3h, 1),
                "so2_fraction": round(so2_fraction, 6),
                "acid_strength": round(acid_strength, 6),
                "tail_so2_mgm3": round(tail_so2_mgm3, 2),
                "so2_load_kgph": None if load_kgph is None else round(load_kgph, 1),
                "conversion_efficiency": conversion_efficiency,
                "band": band,
            }
            trace.note("band", band)

            if band == "hard":
                # 硬线：开/续排放事件，联锁保持，并把炉子带到安全侧（仅首次进入）。
                incident = self._open_or_continue_incident(
                    actor, hard_reasons, sample_record, correlation_id=trace.correlation_id
                )
                entered_latch = self._machine.state != "latched"
                if entered_latch:
                    reason = hard_reasons[0]
                    intent = self.write_intent(
                        "safeguard",
                        {
                            "action": "safeguard",
                            "reason": reason,
                            "all_reasons": hard_reasons,
                            "sample": sample_record,
                            "incident_id": incident["incident_id"],
                            "at": self.clock.timestamp_iso(),
                            "actor": actor,
                        },
                    )
                    self._machine.to("latched", actor, f"制酸联锁：{reason}")
                    self._latch_reason = reason
                    self._latched_at = self.clock.timestamp()
                    self._latch_count += 1
                    self._clear_since_epoch = None
                    trace.note("intent_version", intent.version)
                    self._command_furnace_safe_side(actor, reason, sample_record, trace.correlation_id)
            else:
                if self._machine.state == "latched":
                    # 闩锁是保持型的：测点回落不自动解锁，必须人工 reset；
                    # 但样本照常入事件时间线，并累计尾气解除计时。
                    pass
                elif band == "warning":
                    if self._machine.state == "normal":
                        self._machine.to("warning", actor, "酸浓/尾气进入预警带")
                elif self._machine.state == "warning":
                    self._machine.to("normal", actor, "测点回到正常带")
                if self._open_incident is not None:
                    self._append_timeline({"kind": "sample", **sample_record})
                self._track_clear_window(epoch, tail_so2_mgm3)
                # 只有联锁已经人工复位后，才允许按解除计时自动关闭事件
                if self._machine.state != "latched":
                    self._maybe_autoclose_incident(actor, epoch)

            record = self._persist(reason="sample")
            trace.attach(record)
            payload = self.status()
            trace.note("incident_open", self._open_incident is not None)
            return payload

    def safeguard(
        self,
        actor: str,
        *,
        reason: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        """人工紧急联锁（DCS 急停按钮/值班员判断），与硬线样本同等待遇。"""

        actor = ensure_actor(actor)
        with self.action(
            "safeguard",
            "acid",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
            bump_generation=True,
        ) as trace:
            if not reason:
                raise GuardViolation("人工联锁必须给出原因")
            if self._machine.state == "offline":
                raise StateTransitionError("制酸段未投运", details={"state": self._machine.state})
            if self._machine.state == "latched":
                raise StateTransitionError("制酸段已处于联锁", details={"state": self._machine.state})
            latest = self._latest_sample_record(actor)
            incident = self._open_or_continue_incident(
                actor, [f"manual:{reason}"], latest, correlation_id=trace.correlation_id, manual=True
            )
            intent = self.write_intent(
                "safeguard",
                {
                    "action": "safeguard",
                    "reason": reason,
                    "manual": True,
                    "incident_id": incident["incident_id"],
                    "at": self.clock.timestamp_iso(),
                    "actor": actor,
                },
            )
            self._machine.to("latched", actor, f"人工联锁：{reason}")
            self._latch_reason = f"manual:{reason}"
            self._latched_at = self.clock.timestamp()
            self._latch_count += 1
            self._clear_since_epoch = None
            trace.note("intent_version", intent.version)
            self._command_furnace_safe_side(actor, f"manual:{reason}", latest, trace.correlation_id)
            record = self._persist(reason="safeguard")
            trace.attach(record)
            return self.status()

    def reset(
        self,
        actor: str,
        *,
        note: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        """联锁复位：最短保持时长 + 处理说明 + 当前测点必须退出硬线。"""

        actor = ensure_actor(actor)
        with self.action(
            "reset",
            "acid",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
            bump_generation=True,
        ) as trace:
            self._machine.require("latched", "制酸联锁复位")
            if not note:
                raise GuardViolation("复位必须填写处理说明")
            remaining = self._hold_remaining()
            if remaining > 0:
                raise GuardViolation(
                    "联锁最短保持时长未到，禁止复位",
                    details={
                        "remaining_seconds": round(remaining, 3),
                        "min_hold_seconds": self.settings.acid_min_hold_seconds,
                    },
                )
            if self._sample_is_stale():
                raise GuardViolation(
                    "缺少未失效的尾气/酸浓样本，禁止复位",
                    details={"stale_after_seconds": self.settings.acid_sample_stale_seconds},
                )
            classification = self._classify(self._acid_strength, self._tail_so2_mgm3)
            if classification["band"] == "hard":
                raise GuardViolation(
                    "酸浓/尾气仍在硬线外，禁止复位", details=classification
                )
            incident_id = self._open_incident["incident_id"] if self._open_incident is not None else None
            intent = self.write_intent(
                "reset",
                {
                    "action": "reset",
                    "note": note,
                    "latch_reason": self._latch_reason,
                    "incident_id": incident_id,
                    "at": self.clock.timestamp_iso(),
                    "actor": actor,
                },
            )
            self._append_disposition(
                kind="reset",
                actor=actor,
                note=note,
                latch_reason=self._latch_reason,
                intent_version=intent.version,
            )
            target_state = str(classification["band"]) if classification["band"] in ("warning", "normal") else "warning"
            self._machine.to(target_state, actor, f"联锁复位：{note}")
            self._latch_reason = None
            self._latched_at = None
            # 复位后开始计算尾气解除计时
            if self._last_sample_epoch is not None and self._tail_below_clear():
                self._clear_since_epoch = float(self._last_sample_epoch)
            else:
                self._clear_since_epoch = None
            record = self._persist(reason="reset")
            trace.attach(record).note("note", note).note("intent_version", intent.version)
            return self.status()

    def record_disposition(
        self,
        actor: str,
        *,
        note: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        """记录一条处置过程说明（调阀、联系制酸、降负荷等），挂到当前排放事件。"""

        actor = ensure_actor(actor)
        with self.action(
            "record_disposition",
            "acid",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            if not note:
                raise GuardViolation("处置记录必须填写说明")
            entry = self._append_disposition(kind="disposition", actor=actor, note=note)
            record = self._persist(reason="record_disposition")
            trace.attach(record).note("incident_id", entry.get("incident_id"))
            return self.status()

    def close_incident(
        self,
        actor: str,
        *,
        note: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        """关闭排放事件：联锁已复位、尾气连续低于解除阈值足够时长，方可关闭。"""

        actor = ensure_actor(actor)
        with self.action(
            "close_incident",
            "acid",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
            bump_generation=True,
        ) as trace:
            if self._open_incident is None:
                raise GuardViolation("当前没有未关闭的排放事件")
            if not note:
                raise GuardViolation("关闭事件必须填写处置结论")
            if self._machine.state == "latched":
                raise GuardViolation("联锁尚未复位，不能关闭排放事件")
            blockers = self._close_blockers()
            if blockers:
                raise GuardViolation("排放事件关闭条件未满足", details={"blockers": blockers})
            incident_id = self._close_incident(actor, note, "manual-close")
            record = self._persist(reason="close_incident")
            trace.attach(record).note("incident_id", incident_id)
            return self.status()

    # ------------------------------------------------------------------ 前馈/门控
    def so2_mass_load_kgph(self) -> float | None:
        """SO2 质量流量 kg/h = 烟气量 Nm3/h × SO2 体积分数 × 单位换算。"""

        if self._gas_flow_nm3h is None or self._so2_fraction is None:
            return None
        return float(self._gas_flow_nm3h) * float(self._so2_fraction) * SO2_KG_PER_NM3

    def demand(self) -> Mapping[str, Any]:
        """按当前烟气量与 SO2 浓度，提前给转化吸收段与冶炼喷吹的要求。"""

        load = self.so2_mass_load_kgph()
        design = self.settings.acid_so2_load_design_kgph
        high_ratio = self.settings.acid_so2_load_high_ratio
        if load is None:
            return {
                "available": False,
                "reason": "offline" if self._machine.state == "offline" else "no-sample",
                "gas_flow_nm3h": None,
                "so2_fraction": None,
                "so2_load_kgph": None,
                "design_load_kgph": design,
                "load_ratio": None,
                "required_conversion": self.settings.acid_required_conversion_floor,
                "target_acid_strength": self.settings.acid_acid_strength_target,
                "feed_cap_tph": None,
                "high_load": False,
            }
        load_ratio = load / design
        high_load = load_ratio >= high_ratio
        # 反推在设计负荷（留高负荷裕量）下允许的喷吹速率。
        # 入口烟气 = 固定漏风底数 + 喷吹速率 × 吨矿烟气量；
        # SO2 质量负荷 = 该烟气量 × SO2 体积分数 × 单位换算。
        gas_base = self.settings.acid_gas_base_nm3h
        per_ton = self.settings.acid_gas_nm3_per_ton_feed
        so2_unit = float(self._so2_fraction) * SO2_KG_PER_NM3
        feed_cap_tph: float | None = None
        if so2_unit > 0:
            feed_cap_tph = round(
                max(0.0, (design * high_ratio / so2_unit - gas_base) / per_ton), 2
            )
        # 尾气限值反推的最低总转化率（入口质量口径）。
        required_conversion = self._required_conversion(float(self._gas_flow_nm3h))
        return {
            "available": True,
            "gas_flow_nm3h": round(float(self._gas_flow_nm3h), 1),
            "so2_fraction": round(float(self._so2_fraction), 6),
            "so2_load_kgph": round(load, 1),
            "design_load_kgph": design,
            "load_ratio": round(load_ratio, 4),
            "required_conversion": required_conversion,
            "target_acid_strength": self.settings.acid_acid_strength_target,
            "feed_cap_tph": feed_cap_tph,
            "high_load": high_load,
        }

    def feed_gate(self) -> Mapping[str, Any]:
        """冶炼喷吹门控：联锁硬停、失效样本保守降量、高负荷给出速率上限。"""

        if self._machine.state == "offline":
            return {
                "allowed": True,
                "mode": "offline",
                "hard_block": False,
                "max_rate_tph": None,
                "blockers": [],
                "demand": self.demand(),
            }
        blockers: list[str] = []
        if self._machine.state == "latched":
            blockers.append("acid-latched")
        stale = self._sample_is_stale()
        if stale:
            blockers.append("acid-sample-stale")
        demand = self.demand()
        max_rate: float | None = demand.get("feed_cap_tph")
        mode = "normal"
        if blockers:
            mode = "latched" if self._machine.state == "latched" else "stale"
        elif demand["high_load"]:
            mode = "load-capped"
        return {
            "allowed": self._machine.state != "latched",
            "mode": mode,
            "hard_block": self._machine.state == "latched",
            "stale": stale,
            "max_rate_tph": max_rate,
            "blockers": blockers,
            "demand": demand,
        }

    def is_latched(self) -> bool:
        return self._machine.state == "latched"

    def hold_remaining(self) -> float:
        return self._hold_remaining()

    # ------------------------------------------------------------------ 查询
    @property
    def state(self) -> str:
        return self._machine.state

    def open_incident(self) -> Mapping[str, Any] | None:
        return None if self._open_incident is None else dict(self._open_incident)

    def incidents(self, *, limit: int = 20) -> list[Mapping[str, Any]]:
        return [entry.payload for entry in self.store.read_stream(INCIDENT_STREAM, limit=limit)]

    def evidence_bundle(self, incident_id: str) -> Mapping[str, Any]:
        """导出一条排放事件的完整证据：事件头、时间线、处置动作与落盘版本。"""

        records = [
            entry.payload
            for entry in self.store.read_stream(INCIDENT_STREAM, limit=10_000)
            if entry.payload.get("incident_id") == incident_id
        ]
        if not records:
            from ..errors import NotFoundError

            raise NotFoundError("排放事件不存在", details={"incident_id": incident_id})
        header = records[0]
        timeline = []
        closures = []
        for payload in records[1:]:
            if payload.get("event") == "close":
                closures.append(payload)
            else:
                timeline.append(payload)
        return {
            "incident_id": incident_id,
            "header": dict(header),
            "timeline": timeline,
            "closures": closures,
            "entries": len(records),
        }

    def status(self) -> Mapping[str, Any]:
        classification = None
        if self._acid_strength is not None and self._tail_so2_mgm3 is not None:
            classification = self._classify(self._acid_strength, self._tail_so2_mgm3)
        return {
            "state": self._machine.state,
            "gas_flow_nm3h": self._gas_flow_nm3h,
            "so2_fraction": self._so2_fraction,
            "acid_strength": self._acid_strength,
            "acid_strength_target": self.settings.acid_acid_strength_target,
            "acid_strength_warn_band": [
                self.settings.acid_acid_strength_warn_low,
                self.settings.acid_acid_strength_warn_high,
            ],
            "acid_strength_hard_band": [
                self.settings.acid_acid_strength_hard_low,
                self.settings.acid_acid_strength_hard_high,
            ],
            "tail_so2_mgm3": self._tail_so2_mgm3,
            "tail_so2_warn_mgm3": self.settings.acid_tail_so2_warn_mgm3,
            "tail_so2_limit_mgm3": self.settings.acid_tail_so2_limit_mgm3,
            "tail_so2_clear_mgm3": self.settings.acid_tail_so2_clear_mgm3,
            "classification": classification,
            "sample_stale": self._sample_is_stale(),
            "sample_stale_after_seconds": self.settings.acid_sample_stale_seconds,
            "last_sample_at": self._last_sample_at,
            "baseline": self._baseline_status(),
            "so2_load_kgph": self.so2_mass_load_kgph(),
            "demand": self.demand(),
            "feed_gate": self.feed_gate(),
            "latch_reason": self._latch_reason,
            "latched_at": self._latched_at,
            "latch_count": self._latch_count,
            "hold_remaining_seconds": round(self._hold_remaining(), 3),
            "clear_dwell_remaining_seconds": self._clear_dwell_remaining(),
            "open_incident_id": None if self._open_incident is None else self._open_incident["incident_id"],
            "sample_count": self._sample_count,
            "incident_count": self._incident_count,
            "history": list(self._machine.history),
        }

    # ------------------------------------------------------------------ 内部分级
    def _classify(self, acid_strength: float | None, tail_so2_mgm3: float | None) -> dict[str, Any]:
        s = self.settings
        warn: list[str] = []
        hard: list[str] = []
        if acid_strength is not None:
            if acid_strength < s.acid_acid_strength_hard_low:
                hard.append("acid-strength-low")
            elif acid_strength > s.acid_acid_strength_hard_high:
                hard.append("acid-strength-high")
            elif acid_strength < s.acid_acid_strength_warn_low:
                warn.append("acid-strength-low")
            elif acid_strength > s.acid_acid_strength_warn_high:
                warn.append("acid-strength-high")
        if tail_so2_mgm3 is not None:
            if tail_so2_mgm3 >= s.acid_tail_so2_limit_mgm3:
                hard.append("tail-so2-over-limit")
            elif tail_so2_mgm3 >= s.acid_tail_so2_warn_mgm3:
                warn.append("tail-so2-warning")
        band = "hard" if hard else ("warning" if warn else "normal")
        return {
            "band": band,
            "warn_reasons": warn,
            "hard_reasons": hard,
            "tail_so2_mgm3": tail_so2_mgm3,
            "acid_strength": acid_strength,
        }

    def _conversion_efficiency(
        self, gas_flow_nm3h: float, so2_fraction: float, tail_so2_mgm3: float
    ) -> float | None:
        """由入口 SO2 质量流量与尾气 SO2 质量流量估算的总转化率。"""

        inlet = gas_flow_nm3h * so2_fraction * SO2_KG_PER_NM3  # kg/h
        outlet = gas_flow_nm3h * tail_so2_mgm3 / 1_000_000.0  # mg/m3 → kg/Nm3
        if inlet <= 0:
            return None
        return round(max(0.0, min(1.0, 1.0 - outlet / inlet)), 6)

    def _required_conversion(self, gas_flow_nm3h: float) -> float:
        """满足尾气限值与最低转化率双约束所需的总转化率（取更严者）。"""

        s = self.settings
        if self._so2_fraction is None or gas_flow_nm3h <= 0:
            return s.acid_required_conversion_floor
        inlet_concentration_mgm3 = self._so2_fraction * SO2_KG_PER_NM3 * 1_000_000.0
        by_limit = 1.0 - s.acid_tail_so2_limit_mgm3 / inlet_concentration_mgm3 if inlet_concentration_mgm3 > 0 else 1.0
        return round(max(s.acid_required_conversion_floor, min(1.0, by_limit)), 6)

    # ------------------------------------------------------------------ 事件留证
    def _open_or_continue_incident(
        self,
        actor: str,
        reasons: list[str],
        sample_record: Mapping[str, Any] | None,
        *,
        correlation_id: str,
        manual: bool = False,
    ) -> dict[str, Any]:
        now_iso = self.clock.timestamp_iso()
        if self._open_incident is None:
            self._incident_count += 1
            incident_id = f"ACI-{self._incident_count:04d}"
            incident: dict[str, Any] = {
                "incident_id": incident_id,
                "opened_at": now_iso,
                "opened_epoch": self.clock.timestamp(),
                "opened_by": actor,
                "trigger_reasons": list(reasons),
                "manual": manual,
                "correlation_id": correlation_id,
                "peak_tail_so2_mgm3": None,
                "min_acid_strength": None,
                "max_acid_strength": None,
                "closed_at": None,
                "status": "open",
            }
            self._open_incident = incident
            self.store.append(
                INCIDENT_STREAM,
                {"event": "open", **incident},
            )
        else:
            incident = self._open_incident
            for reason in reasons:
                if reason not in incident["trigger_reasons"]:
                    incident["trigger_reasons"].append(reason)
        if sample_record is not None:
            self._append_timeline({"kind": "sample", **dict(sample_record)})
        self._update_incident_extremes(sample_record)
        return incident

    def _append_timeline(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        if self._open_incident is None:
            return {}
        payload = {
            "incident_id": self._open_incident["incident_id"],
            "at": self.clock.timestamp_iso(),
            "epoch": self.clock.timestamp(),
            **dict(entry),
        }
        timeline = self._open_incident.setdefault("timeline", [])
        timeline.append(payload)
        del timeline[:-TIMELINE_CAP]
        self.store.append(INCIDENT_STREAM, payload)
        return payload

    def _append_disposition(self, *, kind: str, actor: str, note: str, **extra: Any) -> dict[str, Any]:
        if self._open_incident is None:
            raise GuardViolation("当前没有进行中的排放事件，处置记录无法挂接")
        entry = {
            "kind": kind,
            "actor": actor,
            "note": note,
            **extra,
        }
        return self._append_timeline(entry)

    def _update_incident_extremes(self, sample_record: Mapping[str, Any] | None) -> None:
        if self._open_incident is None or sample_record is None:
            return
        tail = sample_record.get("tail_so2_mgm3")
        strength = sample_record.get("acid_strength")
        peak = self._open_incident["peak_tail_so2_mgm3"]
        low = self._open_incident["min_acid_strength"]
        high = self._open_incident["max_acid_strength"]
        if tail is not None:
            tail_value = float(tail)
            self._open_incident["peak_tail_so2_mgm3"] = (
                tail_value if peak is None else round(max(float(peak), tail_value), 3)
            )
        if strength is not None:
            strength_value = float(strength)
            self._open_incident["min_acid_strength"] = (
                strength_value if low is None else round(min(float(low), strength_value), 6)
            )
            self._open_incident["max_acid_strength"] = (
                strength_value if high is None else round(max(float(high), strength_value), 6)
            )

    def _close_incident(self, actor: str, note: str, how: str) -> str:
        assert self._open_incident is not None
        incident = self._open_incident
        incident["status"] = "closed"
        closed_epoch = self.clock.timestamp()
        incident["closed_at"] = self.clock.timestamp_iso()
        incident["closed_by"] = actor
        incident["close_note"] = note
        incident_id = str(incident["incident_id"])
        self.store.append(
            INCIDENT_STREAM,
            {
                "event": "close",
                "incident_id": incident_id,
                "at": incident["closed_at"],
                "epoch": closed_epoch,
                "actor": actor,
                "note": note,
                "how": how,
                "peak_tail_so2_mgm3": incident["peak_tail_so2_mgm3"],
                "duration_seconds": round(closed_epoch - float(incident["opened_epoch"]), 3),
            },
        )
        self._recent_incidents.append(
            {k: incident.get(k) for k in ("incident_id", "opened_at", "closed_at", "trigger_reasons", "peak_tail_so2_mgm3")}
        )
        del self._recent_incidents[:-RECENT_INCIDENT_CAP]
        self._open_incident = None
        self._clear_since_epoch = None
        return incident_id

    def _maybe_autoclose_incident(self, actor: str, epoch: float) -> None:
        if self._open_incident is None or self._machine.state == "latched":
            return
        if not self._close_blockers():
            self._close_incident(actor, "尾气连续低于解除阈值，事件自动关闭", "auto-close")

    def _close_blockers(self) -> list[str]:
        blockers: list[str] = []
        classification = self._classify(self._acid_strength, self._tail_so2_mgm3)
        if classification["band"] == "hard":
            blockers.append("still-over-hard-line")
        if self._sample_is_stale():
            blockers.append("sample-stale")
        if not self._tail_below_clear():
            blockers.append("tail-above-clear-threshold")
        remaining = self._clear_dwell_remaining()
        if remaining > 0:
            blockers.append("clear-dwell-not-satisfied")
        return blockers

    def _tail_below_clear(self) -> bool:
        return self._tail_so2_mgm3 is not None and float(self._tail_so2_mgm3) < self.settings.acid_tail_so2_clear_mgm3

    def _track_clear_window(self, epoch: float, tail_so2_mgm3: float) -> None:
        if tail_so2_mgm3 < self.settings.acid_tail_so2_clear_mgm3:
            if self._clear_since_epoch is None:
                self._clear_since_epoch = epoch
        else:
            self._clear_since_epoch = None

    def _clear_dwell_remaining(self) -> float | None:
        if self._open_incident is None:
            return None
        if self._clear_since_epoch is None:
            return self.settings.acid_clear_dwell_seconds
        elapsed = max(0.0, self.clock.timestamp() - float(self._clear_since_epoch))
        return round(max(0.0, self.settings.acid_clear_dwell_seconds - elapsed), 3)

    # ------------------------------------------------------------------ 其它内部
    def _command_furnace_safe_side(
        self, actor: str, reason: str, sample_record: Mapping[str, Any] | None, correlation_id: str
    ) -> None:
        """越线第一条动作：把炉子带到安全侧；炉子侧自身再落一份审计。"""

        if self._furnace is None:
            return
        detail = {"sample": dict(sample_record or {}), "source_component": "acid"}
        self._append_timeline(
            {
                "kind": "furnace-safe-side",
                "actor": actor,
                "reason": reason,
                "detail": detail,
            }
        )
        furnace_state = self._furnace.status()["state"]
        if furnace_state == "safeguarded":
            # 炉子已经在安全态（闩锁保持期间再次冲高），只记账，不重复驱动。
            self._append_timeline(
                {
                    "kind": "furnace-safe-side-skip",
                    "actor": actor,
                    "reason": reason,
                    "detail": {"furnace_state": furnace_state},
                }
            )
            return
        self._furnace.bring_to_safe_side(
            actor, reason=reason, detail=detail, correlation_id=correlation_id
        )

    def _latest_sample_record(self, actor: str) -> dict[str, Any] | None:
        if self._last_sample_epoch is None:
            return None
        return {
            "seq": self._sample_count,
            "at": self._last_sample_at,
            "epoch": self._last_sample_epoch,
            "gas_flow_nm3h": None if self._gas_flow_nm3h is None else round(float(self._gas_flow_nm3h), 1),
            "so2_fraction": self._so2_fraction,
            "acid_strength": self._acid_strength,
            "tail_so2_mgm3": self._tail_so2_mgm3,
            "so2_load_kgph": None
            if self.so2_mass_load_kgph() is None
            else round(float(self.so2_mass_load_kgph()), 1),
            "band": "manual",
        }

    def _validate_readings(
        self, gas_flow_nm3h: float, so2_fraction: float, acid_strength: float, tail_so2_mgm3: float
    ) -> None:
        if gas_flow_nm3h < 0:
            raise GuardViolation("烟气量不能为负", details={"gas_flow_nm3h": gas_flow_nm3h})
        if not 0.0 <= so2_fraction <= 1.0:
            raise GuardViolation("SO2 浓度必须是 0~1 的体积分数", details={"so2_fraction": so2_fraction})
        if not 0.0 < acid_strength < 1.0:
            raise GuardViolation("酸浓必须是 0~1 的质量分数", details={"acid_strength": acid_strength})
        if tail_so2_mgm3 < 0:
            raise GuardViolation("尾气 SO2 浓度不能为负", details={"tail_so2_mgm3": tail_so2_mgm3})

    def _sample_is_stale(self) -> bool:
        if self._last_sample_epoch is None:
            return True
        return self.clock.timestamp() - float(self._last_sample_epoch) > self.settings.acid_sample_stale_seconds

    def _hold_remaining(self) -> float:
        if self._latched_at is None:
            return 0.0
        elapsed = max(0.0, self.clock.timestamp() - float(self._latched_at))
        return max(0.0, self.settings.acid_min_hold_seconds - elapsed)

    def _baseline_status(self) -> dict[str, Any]:
        if self._baseline_value is None or self._baseline_epoch is None:
            return {
                "value": None,
                "at": None,
                "age_seconds": None,
                "fresh": False,
                "window_seconds": self.settings.acid_analyzer_window_seconds,
                "source": None,
            }
        age = max(0.0, self.clock.timestamp() - float(self._baseline_epoch))
        return {
            "value": float(self._baseline_value),
            "at": self._baseline_at,
            "age_seconds": round(age, 3),
            "fresh": age <= self.settings.acid_analyzer_window_seconds,
            "window_seconds": self.settings.acid_analyzer_window_seconds,
            "source": self._baseline_source,
        }

    def _require_baseline_fresh(self) -> None:
        baseline = self._baseline_status()
        if baseline["value"] is None:
            raise GuardViolation("缺少 SO2 分析仪基线，必须先标定基线")
        if not baseline["fresh"]:
            raise GuardViolation(
                "SO2 分析仪基线已过期，制酸段禁止投运",
                details={
                    "age_seconds": baseline["age_seconds"],
                    "window_seconds": baseline["window_seconds"],
                },
            )

    def _persist(self, *, reason: str) -> Any:
        payload = {
            "state": self._machine.state,
            "reason": reason,
            "written_epoch": self.clock.timestamp(),
            "written_at": self.clock.timestamp_iso(),
            "gas_flow_nm3h": self._gas_flow_nm3h,
            "so2_fraction": self._so2_fraction,
            "acid_strength": self._acid_strength,
            "tail_so2_mgm3": self._tail_so2_mgm3,
            "baseline": {
                "value": self._baseline_value,
                "at": self._baseline_at,
                "epoch": self._baseline_epoch,
                "source": self._baseline_source,
            },
            "last_sample_at": self._last_sample_at,
            "last_sample_epoch": self._last_sample_epoch,
            "open_incident": self._open_incident,
            "clear_since_epoch": self._clear_since_epoch,
            "latch_reason": self._latch_reason,
            "latched_at": self._latched_at,
            "latch_count": self._latch_count,
            "sample_count": self._sample_count,
            "incident_count": self._incident_count,
            "recent_incidents": list(self._recent_incidents),
            "history": list(self._machine.history),
        }
        record = self.persist_state(payload)
        self._refresh_gauges()
        return record

    def _refresh_gauges(self) -> None:
        self.metrics.observe(
            "acid.state_code", float(("offline", "normal", "warning", "latched").index(self._machine.state))
        )
        load = self.so2_mass_load_kgph()
        if load is not None:
            self.metrics.observe("acid.so2_load_kgph", round(load, 1))
        if self._tail_so2_mgm3 is not None:
            self.metrics.observe("acid.tail_so2_mgm3", float(self._tail_so2_mgm3))
        if self._acid_strength is not None:
            self.metrics.observe("acid.acid_strength", float(self._acid_strength))
        self.metrics.observe("acid.latch_count", float(self._latch_count))
        self.metrics.observe("acid.incident_count", float(self._incident_count))


__all__ = ["AcidPlant", "STATES", "TRANSITIONS", "INCIDENT_STREAM", "SO2_KG_PER_NM3"]
