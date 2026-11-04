"""Stable default ranking: percentage descending, then main net amount descending."""


def rank_members(members):
    return tuple(
        sorted(
            members,
            key=lambda m: (
                m.change_pct is None,
                -(m.change_pct or 0),
                m.main_net_yuan is None,
                -(m.main_net_yuan or 0),
                m.symbol,
            ),
        )
    )
