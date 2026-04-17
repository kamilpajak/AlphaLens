import unittest


class TestEventSourceABC(unittest.TestCase):
    def test_cannot_instantiate_abstract_class_directly(self):
        from tradingagents.watchdog.sources.base import EventSource

        with self.assertRaises(TypeError):
            EventSource()  # type: ignore[abstract]

    def test_subclass_without_detect_cannot_be_instantiated(self):
        from tradingagents.watchdog.sources.base import EventSource

        class IncompleteSource(EventSource):
            pass

        with self.assertRaises(TypeError):
            IncompleteSource()  # type: ignore[abstract]

    def test_subclass_with_detect_instantiates_and_returns_list(self):
        from tradingagents.watchdog.sources.base import EventSource

        class DummySource(EventSource):
            def detect(self):
                return []

        source = DummySource()
        self.assertEqual(source.detect(), [])


if __name__ == "__main__":
    unittest.main()
