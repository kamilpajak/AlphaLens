import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock


def _classified(action):
    from alphalens.watchdog.classifier import ClassifiedEvent, Severity
    from alphalens.watchdog.portfolio import Relevance
    from alphalens.watchdog.types import Event, FormType

    return ClassifiedEvent(
        event=Event(
            ticker="AAPL",
            form_type=FormType.FORM_8K,
            accession_number="ACC-1",
            filed_at=datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc),
            url="https://sec.gov/x",
            raw_data={},
        ),
        severity=Severity.HIGH,
        relevance=Relevance.HELD,
        action=action,
    )


class TestDispatchRouter(unittest.TestCase):
    def test_routes_per_action_to_registered_handler(self):
        from alphalens.watchdog.classifier import Action
        from alphalens.watchdog.dispatch.router import DispatchRouter

        auto = MagicMock()
        approval = MagicMock()
        digest = MagicMock()

        router = DispatchRouter({
            Action.AUTO_TRIGGER: [auto],
            Action.APPROVAL: [approval],
            Action.DIGEST: [digest],
            Action.IGNORE: [],
        })

        router.dispatch(_classified(Action.AUTO_TRIGGER))
        router.dispatch(_classified(Action.APPROVAL))
        router.dispatch(_classified(Action.DIGEST))

        auto.handle.assert_called_once()
        approval.handle.assert_called_once()
        digest.handle.assert_called_once()

    def test_fanout_to_multiple_handlers(self):
        from alphalens.watchdog.classifier import Action
        from alphalens.watchdog.dispatch.router import DispatchRouter

        h1 = MagicMock()
        h2 = MagicMock()
        router = DispatchRouter({Action.APPROVAL: [h1, h2]})

        router.dispatch(_classified(Action.APPROVAL))

        h1.handle.assert_called_once()
        h2.handle.assert_called_once()

    def test_ignore_action_does_not_dispatch(self):
        from alphalens.watchdog.classifier import Action
        from alphalens.watchdog.dispatch.router import DispatchRouter

        h = MagicMock()
        router = DispatchRouter({Action.IGNORE: [h]})
        router.dispatch(_classified(Action.IGNORE))

        h.handle.assert_not_called()

    def test_handler_error_does_not_stop_other_handlers(self):
        from alphalens.watchdog.classifier import Action
        from alphalens.watchdog.dispatch.router import DispatchRouter

        failing = MagicMock()
        failing.handle.side_effect = RuntimeError("boom")
        working = MagicMock()

        router = DispatchRouter({Action.APPROVAL: [failing, working]})
        router.dispatch(_classified(Action.APPROVAL))

        working.handle.assert_called_once()


if __name__ == "__main__":
    unittest.main()
