class StorageError(RuntimeError):
    """可报告给用户的本地持久化错误。"""


class MarketError(RuntimeError):
    """数据源失败、无行情或协议异常。"""
