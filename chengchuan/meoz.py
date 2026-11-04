"""Local credential configuration and usage inspection; never prints an API key."""

import argparse
import getpass
import json
from pathlib import Path

from chengchuan.infrastructure.meoz_client import MeozStore, configured, save_private_key


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("configure-key", "status", "usage"))
    args = parser.parse_args(argv)
    if args.command == "configure-key":
        value = getpass.getpass("MeoZ API Key（隐藏输入）: ").strip()
        try:
            save_private_key(value)
        except (OSError, ValueError, TypeError):
            parser.exit(2, "API Key保存失败，请检查格式和本机配置文件权限\n")
        print("API Key已保存到本机私有配置；未进行联网请求。")
        return 0
    store = MeozStore()
    result = dict(
        configured=configured(),
        usage=store.usage(),
        note="本机应用预留次数（含失败／中断），不含其他客户端；切换周期优先复用缓存。",
    )
    if args.command == "status":
        result["cache_path"] = str(store.path)
        result["credential_path"] = str(Path.home() / ".meoz/config.json")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
