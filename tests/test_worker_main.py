from unittest.mock import MagicMock, patch

import pytest


class TestWorkerMain:
    def test_run_scheduler_cycle_runs_due_checks_and_returns_interval(self) -> None:
        mock_settings = MagicMock()
        mock_settings.scheduler_interval_seconds = 45

        mock_session = MagicMock()
        mock_session.__enter__.return_value = mock_session
        mock_session.__exit__.return_value = False

        mock_runner = MagicMock()
        mock_runner.run_due_checks.return_value = ["check-1"]

        mock_supervisor = MagicMock()
        mock_repo = MagicMock()
        mock_repo.get.return_value = mock_settings

        with (
            patch("app.worker.__main__.SessionLocal", return_value=mock_session) as session_local,
            patch("app.worker.__main__.HealthCheckRunner", return_value=mock_runner) as runner_cls,
            patch("app.worker.__main__.MonitoringSettingsRepository", return_value=mock_repo) as repo_cls,
            patch("app.worker.__main__.VpnSessionSupervisor") as supervisor_cls,
            patch("app.worker.__main__._maybe_run_datagate_autosync") as autosync,
        ):
            supervisor_cls.instance.return_value = mock_supervisor
            from app.worker.__main__ import run_scheduler_cycle

            interval = run_scheduler_cycle()

        assert interval == 45
        supervisor_cls.instance.assert_called()
        mock_supervisor.sync.assert_called_once()
        session_local.assert_called_once()
        autosync.assert_called_once()
        runner_cls.assert_called_once_with(mock_session)
        repo_cls.assert_called_once_with(mock_session)
        mock_repo.get.assert_called_once()
        mock_session.commit.assert_called_once()
        mock_runner.run_due_checks.assert_called_once()

    def test_main_one_iteration_then_stop_via_keyboard_interrupt(self) -> None:
        mock_supervisor = MagicMock()
        sleep_calls: list[int] = []

        def fake_sleep(seconds: int) -> None:
            sleep_calls.append(seconds)
            raise KeyboardInterrupt

        with (
            patch("app.worker.__main__.run_scheduler_cycle", return_value=12) as cycle,
            patch("app.worker.__main__.time.sleep", side_effect=fake_sleep),
            patch("app.worker.__main__.VpnSessionSupervisor") as supervisor_cls,
        ):
            supervisor_cls.instance.return_value = mock_supervisor
            from app.worker.__main__ import main

            with pytest.raises(KeyboardInterrupt):
                main()

        cycle.assert_called_once()
        assert sleep_calls == [12]
        mock_supervisor.stop_all.assert_called_once()

    def test_main_exception_path_sleeps_thirty_seconds(self) -> None:
        mock_supervisor = MagicMock()
        sleep_calls: list[int] = []

        def boom() -> int:
            raise RuntimeError("cycle failed")

        def fake_sleep(seconds: int) -> None:
            sleep_calls.append(seconds)
            raise StopIteration

        with (
            patch("app.worker.__main__.run_scheduler_cycle", side_effect=boom),
            patch("app.worker.__main__.time.sleep", side_effect=fake_sleep),
            patch("app.worker.__main__.VpnSessionSupervisor") as supervisor_cls,
        ):
            supervisor_cls.instance.return_value = mock_supervisor
            from app.worker.__main__ import main

            with pytest.raises(StopIteration):
                main()

        assert sleep_calls == [30]
        mock_supervisor.stop_all.assert_called_once()
