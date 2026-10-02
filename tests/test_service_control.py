import unittest
from unittest.mock import patch

from agent.executor import JobExecutor


class ServiceControlTest(unittest.TestCase):
    def setUp(self):
        self.executor = JobExecutor.__new__(JobExecutor)
        self.executor.allowed_exact = {"odoo-server-demo.service"}
        self.executor.allowed_prefix = ()
        self.payload = {"service_name": "odoo-server-demo.service"}

    def test_start_enables_and_starts_service(self):
        with (
            patch("agent.executor.run", return_value={"output": "enabled"}) as run,
            patch("agent.executor.systemd_status", return_value={"active_state": "active"}),
        ):
            result = self.executor.service_start(self.payload)

        run.assert_called_once_with(
            ["systemctl", "enable", "--now", "odoo-server-demo.service"],
            timeout=120,
        )
        self.assertEqual(result["action"], "start")
        self.assertEqual(result["status"]["active_state"], "active")

    def test_stop_disables_and_stops_service(self):
        with (
            patch("agent.executor.run", return_value={"output": "disabled"}) as run,
            patch("agent.executor.systemd_status", return_value={"active_state": "inactive"}),
        ):
            result = self.executor.service_stop(self.payload)

        run.assert_called_once_with(
            ["systemctl", "disable", "--now", "odoo-server-demo.service"],
            timeout=120,
        )
        self.assertEqual(result["action"], "stop")
        self.assertEqual(result["status"]["active_state"], "inactive")

    def test_restart_preserves_enablement(self):
        with (
            patch("agent.executor.run", return_value={"output": ""}) as run,
            patch("agent.executor.systemd_status", return_value={"active_state": "active"}),
        ):
            self.executor.service_restart(self.payload)

        run.assert_called_once_with(
            ["systemctl", "restart", "odoo-server-demo.service"],
            timeout=120,
        )


if __name__ == "__main__":
    unittest.main()
