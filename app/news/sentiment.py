"""FinBERT sentiment via onnxruntime (no PyTorch at runtime).

The model is built once by tools/install-finbert.sh into DATA_DIR/models/finbert
(model.onnx, vocab.txt, meta.json). If it is missing, sentiment is skipped and
the Status page says so; everything else keeps working.

Score = P(positive) - P(negative), in [-1, 1]. FinBERT was trained on financial
news sentences; on short ASX headlines it is often "neutral" and sometimes wrong
(e.g. "Net loss widened" can read positive), so treat it as a noisy signal.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import numpy as np

from app.config import get_settings

log = logging.getLogger(__name__)
MODEL_NAME = "finbert"
_lock = threading.Lock()


def model_dir() -> Path:
    return get_settings().models_dir / "finbert"


def model_info() -> dict | None:
    d = model_dir()
    if not (d / "model.onnx").exists() or not (d / "vocab.txt").exists():
        return None
    try:
        meta = json.loads((d / "meta.json").read_text())
    except (OSError, ValueError):
        meta = {}
    meta["size_mb"] = round((d / "model.onnx").stat().st_size / 1e6)
    return meta


class FinBert:
    """Load once per job; call close() (or let it go out of scope) to free ~0.5 GB."""

    def __init__(self, threads: int = 2, max_length: int = 64) -> None:
        import onnxruntime as ort
        from tokenizers import BertWordPieceTokenizer

        d = model_dir()
        meta = model_info() or {}
        self.labels = meta.get("labels", ["positive", "negative", "neutral"])
        self.max_length = int(meta.get("max_length", max_length))
        self.tok = BertWordPieceTokenizer(str(d / "vocab.txt"), lowercase=True)
        self.tok.enable_truncation(self.max_length)
        self.tok.enable_padding(pad_id=0, pad_token="[PAD]")
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.sess = ort.InferenceSession(str(d / "model.onnx"), so, providers=["CPUExecutionProvider"])

    def predict(self, texts: list[str], batch_size: int = 32) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(texts), batch_size):
            enc = self.tok.encode_batch([t or "" for t in texts[i:i + batch_size]])
            feed = {
                "input_ids": np.array([e.ids for e in enc], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64),
                "token_type_ids": np.array([e.type_ids for e in enc], dtype=np.int64),
            }
            with _lock:
                logits = self.sess.run(None, feed)[0]
            e = np.exp(logits - logits.max(axis=1, keepdims=True))
            p = e / e.sum(axis=1, keepdims=True)
            for row in p:
                probs = dict(zip(self.labels, (float(x) for x in row)))
                label = self.labels[int(row.argmax())]
                out.append({"positive": probs.get("positive", 0.0), "negative": probs.get("negative", 0.0),
                            "neutral": probs.get("neutral", 0.0), "label": label,
                            "score": probs.get("positive", 0.0) - probs.get("negative", 0.0)})
        return out

    def close(self) -> None:
        self.sess = None
