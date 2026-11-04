import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_market import sample_frame

from chengchuan.application.errors import MarketError
from chengchuan.infrastructure.market_sources import ConfiguredMarketSources
from chengchuan.infrastructure.meoz_client import save_private_key


class MarketSourcesTests(unittest.TestCase):
    def test_source_choice_and_capabilities_follow_configuration_without_requests(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch("chengchuan.infrastructure.market_sources.configured") as configured,
            patch("chengchuan.infrastructure.meoz_client.urlopen") as http,
        ):
            sources = ConfiguredMarketSources(folder)
            configured.return_value = False
            basic = sources.connect("auto")
            self.assertEqual(basic.source_id, "tencent")
            self.assertEqual(basic.intervals, ("day", "minute"))
            self.assertIn("仅分时／日K", basic.connection_notice)
            explicit = sources.connect("meoz")
            self.assertEqual(len(explicit.intervals), 12)
            self.assertIn("本机未配置", explicit.connection_notice)
            configured.return_value = True
            full = sources.connect("auto")
            self.assertEqual(full.source_id, "meoz")
            self.assertEqual(len(full.intervals), 12)
            self.assertNotEqual(full.cache.path, basic.cache.path)
            http.assert_not_called()

    def test_key_save_is_atomic_private_and_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps({"history_line": "normal"}), encoding="utf-8")
            value = "nc_" + "a" * 32
            save_private_key(value, path)
            self.assertEqual(
                json.loads(path.read_text()), {"history_line": "normal", "api_key": value}
            )
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "格式无效"):
                save_private_key("invalid-secret", path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_switching_to_tencent_offline_never_uses_network(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch("chengchuan.infrastructure.market_data.urlopen") as http,
        ):
            service = ConfiguredMarketSources(folder, offline=True).connect("tencent")
            with self.assertRaisesRegex(MarketError, "离线模式缺少"):
                service.load("002318", "day")
            self.assertTrue(service.search("久立").cached)
            frame = sample_frame()
            service.cache.save(frame)
            self.assertTrue(service.load("002318", "day", refresh=True).cached)
            http.assert_not_called()
