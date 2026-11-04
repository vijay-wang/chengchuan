"""Local credential configuration and usage inspection; never prints an API key."""

import argparse
import getpass
import json
import os
import re
import tempfile
from pathlib import Path

from chengchuan.infrastructure.meoz_client import MeozStore, configured


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("configure-key", "status", "usage"))
    args = parser.parse_args(argv)
    if args.command == "configure-key":
        value = getpass.getpass("MeoZ API Key（隐藏输入）: ").strip()
        if not re.fullmatch(r"nc_[A-Za-z0-9_-]{32}", value):
            parser.exit(2, "API Key格式无效\n")
        path = Path.home() / ".meoz/config.json"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        obj = json.loads(path.read_text()) if path.exists() else {}
        obj["api_key"] = value
        descriptor, name = tempfile.mkstemp(prefix=".config-", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(obj, stream, ensure_ascii=False)
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
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
