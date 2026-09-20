"""尾气排放监视组件。

尾气 SO2 浓度按预警线与排放限值两级管理：到预警线只提示值班人员，到限值立刻
开超排事件并先把炉子往安全侧带。超排事件的开启、关闭与处置登记全部写入追加型
证据流水（``tail/exceedances``，逐行校验和，``verify`` 可全库校验）；事件关闭后
必须登记处置（原因与措施）才算闭环，未闭环的事件一直挂在待办里。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..component import Component, ensure_actor
from ..errors import GuardViolation, NotFoundError
from ..machine import StateMachine
from ..ports import SafetySidePort
from ..runtime import RuntimeContext, epoch_from_iso

STATES = ("normal", "watch", "exceed")

TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "normal": ("watch", "exceed"),
    "watch": ("normal", "exceed"),
    "exceed": ("watch", "normal"),
}

EXCEEDANCE_STREAM = "tail/exceedances"


class TailGasMonitor(Component):
    name = "tail"

    def __init__(self, ctx: RuntimeContext) -> None:
        super().__init__(ctx)
        self._machine = StateMachine("tail", "normal", TRANSITIONS, ctx.clock)
        self._last_reading: dict[str, Any] | None = None
        self._readings: list[dict[str, Any]] = []
        self._active_episode: dict[str, Any] | None = None
        self._episode_counter = 0
        self._pending_dispositions: list[dict[str, Any]] = []
        self._completed: list[dict[str, Any]] = []
        self._safety_port: SafetySidePort | None = None
        restored = self.restore()
        if restored is not None:
            self._machine.restore(restored)
            last = restored.get("last_reading")
            if isinstance(last, dict):
                self._last_reading = last
            readings = restored.get("readings")
            if isinstance(readings, list):
                self._readings = [entry for entry in readings if isinstance(entry, dict)]
            episode = restored.get("active_episode")
            if isinstance(episode, dict):
                self._active_episode = episode
            self._episode_counter = int(restored.get("episode_counter", 0))
            pending = restored.get("pending_dispositions")
            if isinstance(pending, list):
                self._pending_dispositions = [entry for entry in pending if isinstance(entry, dict)]
            completed = restored.get("completed")
            if isinstance(completed, list):
                self._completed = [entry for entry in completed if isinstance(entry, dict)]
        self._refresh_gauges()

    def bind_safety_port(self, port: SafetySidePort) -> None:
        """注入安全侧执行端口：超排瞬间由它把炉子往安全侧带。"""

        self._safety_port = port

    # ------------------------------------------------------------------ 动作
    def reading(
        self,
        actor: str,
        *,
        so2_mg_nm3: float,
        observed_at: str | None = None,
        correlation_id: str | None = None,
    ) -> Mapping[str, Any]:
        """接收一次尾气 SO2 读数，按预警线/限值分级并维护超排事件。"""

        actor = ensure_actor(actor)
        with self.action("reading", "tail", actor, correlation_id=correlation_id) as trace:
            if so2_mg_nm3 < 0:
                raise GuardViolation(
                    "尾气 SO2 读数不能为负", details={"so2_mg_nm3": so2_mg_nm3}
                )
            epoch = self.clock.timestamp() if observed_at is None else epoch_from_iso(observed_at)
            if epoch > self.clock.timestamp():
                raise GuardViolation("读数观测时间晚于当前时刻")
            value = round(so2_mg_nm3, 3)
            at = observed_at or self.clock.timestamp_iso()
            self._last_reading = {"value": value, "at": at}
            self._readings.append(dict(self._last_reading))
            self._readings = self._readings[-8:]
            if value >= self.settings.tail_so2_limit_mg_nm3:
                target = "exceed"
            elif value >= self.settings.tail_so2_warn_mg_nm3:
                target = "watch"
            else:
                target = "normal"
            previous = self._machine.state
            if target != previous:
                self._machine.to(target, actor, f"尾气 SO2 {value} mg/Nm³")
            if target == "exceed":
                if self._active_episode is None:
                    self._open_episode(actor, value)
                else:
                    self._update_episode(value)
                if previous != "exceed" and self._safety_port is not None:
                    # 顶到排放限值：先把炉子往安全侧带，再留证据。
                    self._safety_port.bring_to_safe_side(
                        actor,
                        reason=(
                            f"尾气 SO2 {value} mg/Nm³ 超过限值 "
                            f"{self.settings.tail_so2_limit_mg_nm3} mg/Nm³"
                        ),
                        source="tail",
                    )
            elif self._active_episode is not None:
                self._close_episode(actor, value)
            record = self._persist(reason="reading")
            trace.attach(record).note("value", value).note("level", target)
            return self.status()

    def disposition(
        self,
        actor: str,
        *,
        episode_id: str,
        cause: str,
        measures: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        """对已关闭的超排事件登记处置：原因与措施必填，登记后事件闭环。"""

        actor = ensure_actor(actor)
        with self.action(
            "disposition",
            f"tail/{episode_id}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            if not episode_id:
                raise GuardViolation("必须指定超排事件编号")
            if not cause or not measures:
                raise GuardViolation("处置登记必须填写原因与措施")
            pending = None
            for item in self._pending_dispositions:
                if item["episode_id"] == episode_id:
                    pending = item
                    break
            if pending is None:
                raise NotFoundError(
                    "超排事件不存在或已处置闭环", details={"episode_id": episode_id}
                )
            entry = {
                "type": "disposition",
                "episode_id": episode_id,
                "cause": cause,
                "measures": measures,
                "actor": actor,
                "at": self.clock.timestamp_iso(),
            }
            self.store.append(EXCEEDANCE_STREAM, entry)
            self._pending_dispositions = [
                item for item in self._pending_dispositions if item["episode_id"] != episode_id
            ]
            self._completed.append({**pending, "disposition": entry})
            self._completed = self._completed[-8:]
            record = self._persist(reason="disposition")
            trace.attach(record).note("episode_id", episode_id)
            return self.status()

    # ------------------------------------------------------------------ 查询
    @property
    def state(self) -> str:
        return self._machine.state

    def feed_guard(self) -> Mapping[str, Any]:
        """喷吹门控：超排期间禁止继续向炉内喷吹。"""

        exceeding = self._machine.state == "exceed"
        return {
            "ok": not exceeding,
            "state": self._machine.state,
            "active_episode": (
                None if self._active_episode is None else self._active_episode["episode_id"]
            ),
            "last_reading": None if self._last_reading is None else dict(self._last_reading),
            "limit_mg_nm3": self.settings.tail_so2_limit_mg_nm3,
        }

    def exceedances(self, *, limit: int = 20) -> list[Mapping[str, Any]]:
        """超排证据流水：开启、关闭、处置登记逐条可查。"""

        return [entry.payload for entry in self.store.read_stream(EXCEEDANCE_STREAM, limit=limit)]

    def status(self) -> Mapping[str, Any]:
        return {
            "state": self._machine.state,
            "last_reading": None if self._last_reading is None else dict(self._last_reading),
            "warn_mg_nm3": self.settings.tail_so2_warn_mg_nm3,
            "limit_mg_nm3": self.settings.tail_so2_limit_mg_nm3,
            "active_episode": None if self._active_episode is None else dict(self._active_episode),
            "pending_dispositions": [dict(item) for item in self._pending_dispositions],
            "completed": [dict(item) for item in self._completed[-4:]],
            "episode_count": self._episode_counter,
            "readings": list(self._readings[-5:]),
            "history": list(self._machine.history),
        }

    # ------------------------------------------------------------------ 内部
    def _open_episode(self, actor: str, value: float) -> None:
        self._episode_counter += 1
        episode = {
            "episode_id": f"EX-{self._episode_counter:04d}",
            "started_at": self.clock.timestamp_iso(),
            "started_epoch": self.clock.timestamp(),
            "limit_mg_nm3": self.settings.tail_so2_limit_mg_nm3,
            "warn_mg_nm3": self.settings.tail_so2_warn_mg_nm3,
            "peak_mg_nm3": value,
            "peak_at": self.clock.timestamp_iso(),
            "samples": 1,
            "sum_mg_nm3": value,
            "opened_by": actor,
        }
        self._active_episode = episode
        self.store.append(EXCEEDANCE_STREAM, {"type": "open", **episode})

    def _update_episode(self, value: float) -> None:
        assert self._active_episode is not None
        episode = self._active_episode
        episode["samples"] = int(episode["samples"]) + 1
        episode["sum_mg_nm3"] = round(float(episode["sum_mg_nm3"]) + value, 3)
        if value > float(episode["peak_mg_nm3"]):
            episode["peak_mg_nm3"] = value
            episode["peak_at"] = self.clock.timestamp_iso()

    def _close_episode(self, actor: str, value: float) -> None:
        assert self._active_episode is not None
        episode = self._active_episode
        ended_epoch = self.clock.timestamp()
        close = {
            "type": "close",
            "episode_id": episode["episode_id"],
            "started_at": episode["started_at"],
            "ended_at": self.clock.timestamp_iso(),
            "duration_seconds": round(max(0.0, ended_epoch - float(episode["started_epoch"])), 3),
            "peak_mg_nm3": episode["peak_mg_nm3"],
            "avg_mg_nm3": round(float(episode["sum_mg_nm3"]) / max(1, int(episode["samples"])), 3),
            "samples": episode["samples"],
            "limit_mg_nm3": episode["limit_mg_nm3"],
            "closed_by": actor,
            "close_reading_mg_nm3": value,
        }
        self.store.append(EXCEEDANCE_STREAM, close)
        pending = {key: value for key, value in close.items() if key != "type"}
        self._pending_dispositions.append(pending)
        self._active_episode = None

    def _persist(self, *, reason: str) -> Any:
        payload = {
            "state": self._machine.state,
            "reason": reason,
            "written_epoch": self.clock.timestamp(),
            "written_at": self.clock.timestamp_iso(),
            "last_reading": self._last_reading,
            "readings": list(self._readings[-8:]),
            "active_episode": self._active_episode,
            "episode_counter": self._episode_counter,
            "pending_dispositions": list(self._pending_dispositions),
            "completed": list(self._completed[-8:]),
            "history": list(self._machine.history),
        }
        record = self.persist_state(payload)
        self._refresh_gauges()
        return record

    def _refresh_gauges(self) -> None:
        self.metrics.observe("tail.state_code", float(STATES.index(self._machine.state)))
        if self._last_reading is not None:
            self.metrics.observe("tail.so2_mg_nm3", float(self._last_reading["value"]))
        self.metrics.observe("tail.pending_dispositions", float(len(self._pending_dispositions)))


__all__ = ["TailGasMonitor", "STATES", "TRANSITIONS", "EXCEEDANCE_STREAM"]
