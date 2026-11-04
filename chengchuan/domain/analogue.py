"""可复算相似日分类器；预处理只拟合当前给定的过去训练样本。"""

import hashlib
import json
from dataclasses import asdict
from heapq import nsmallest
from statistics import mean, median, pstdev

from chengchuan.domain.forecast import DIRECTIONS, ModelOutput, direction


class AnalogueModel:
    version = "standardized-knn-v1-unvalidated"

    def predict(self, query, training, config):
        if len(training) < config.min_train:
            raise ValueError("训练样本不足")
        columns = list(zip(*(point.values for point in training)))
        medians = [
            median([v for v in col if v is not None]) if any(v is not None for v in col) else 0.0
            for col in columns
        ]
        filled = [tuple(m if v is None else v for v, m in zip(p.values, medians)) for p in training]
        scales = [pstdev(col) for col in zip(*filled)]
        query = tuple(m if v is None else v for v, m in zip(query, medians))

        def distance(item):
            i, row = item
            return sum(
                min(6.0, abs(a - b) / s) ** 2 for a, b, s in zip(query, row, scales) if s > 1e-12
            )

        nearest = nsmallest(min(config.neighbors, len(training)), enumerate(filled), key=distance)
        points = [training[i] for i, _ in nearest]
        counts = tuple(sum(direction(p.return_pct) == d for p in points) for d in DIRECTIONS)
        winners = [d for d, n in zip(DIRECTIONS, counts) if n == max(counts)]
        raw = tuple((n + 1) / (len(points) + 3) for n in counts)
        payload = [self.version, asdict(config), [asdict(p) for p in training]]
        model_id = hashlib.sha256(
            json.dumps(payload, default=str, sort_keys=True).encode()
        ).hexdigest()
        return ModelOutput(
            model_id,
            winners[0] if len(winners) == 1 else None,
            raw,
            counts,
            mean(p.return_pct for p in points),
            len(training),
            tuple(p.origin.isoformat() for p in points),
        )
