"""One-off: export ProsusAI/finbert to int8 ONNX for CPU inference without torch.

Runs inside a throwaway container (see install-finbert.sh). Loads the official
safetensors weights (no pickle), exports to ONNX, applies dynamic int8
quantisation, and compares both ONNX variants with the fp32 PyTorch model on real
headlines (OUT_DIR/_check.txt, one per line). int8 is kept only if it agrees on
>= 97% of labels; otherwise the fp32 ONNX model is installed.
Writes model.onnx, vocab.txt, config.json and meta.json to OUT_DIR.
"""
import json
import os
import shutil
import sys
from pathlib import Path

import numpy as np
import torch
from onnxruntime import InferenceSession
from onnxruntime.quantization import QuantType, quantize_dynamic
from transformers import AutoModelForSequenceClassification, AutoTokenizer

REPO = "ProsusAI/finbert"
REVISION = os.environ.get("FINBERT_REVISION", "0bc2ac10dda6929bcb1f77ccc43c7ec5c424f031")
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/out")
OUT.mkdir(parents=True, exist_ok=True)
tmp = OUT / "_fp32.onnx"

tok = AutoTokenizer.from_pretrained(REPO, revision=REVISION)
model = AutoModelForSequenceClassification.from_pretrained(REPO, revision=REVISION, use_safetensors=True)
model.eval()

enc = tok(["Profit rose strongly"], return_tensors="pt", padding="max_length", max_length=16)
with torch.no_grad():
    torch.onnx.export(
        model, (enc["input_ids"], enc["attention_mask"], enc["token_type_ids"]), str(tmp),
        input_names=["input_ids", "attention_mask", "token_type_ids"], output_names=["logits"],
        dynamic_axes={k: {0: "batch", 1: "seq"} for k in ("input_ids", "attention_mask", "token_type_ids")}
        | {"logits": {0: "batch"}},
        opset_version=14, do_constant_folding=True,
    )
variants = {"fp32": tmp}
# Plain dynamic int8 (per-tensor). Per-channel int8 was tested on 2026-11-13 and broke this model
# (13% label agreement on 493 real headlines), so it is not tried.
quantize_dynamic(str(tmp), str(OUT / "_q.onnx"), weight_type=QuantType.QInt8)
variants["int8"] = OUT / "_q.onnx"

# --- agreement check vs fp32 torch on real headlines ---------------------------
check = OUT / "_check.txt"
samples = [l for l in check.read_text().splitlines() if l.strip()] if check.exists() else ["Profit rose"]
softmax = lambda x: np.exp(x - x.max(1, keepdims=True)) / np.exp(x - x.max(1, keepdims=True)).sum(1, keepdims=True)
def run_onnx(path):
    sess = InferenceSession(str(path), providers=["CPUExecutionProvider"])
    out = []
    for i in range(0, len(samples), 32):
        b = tok(samples[i:i + 32], return_tensors="np", padding=True, truncation=True, max_length=64)
        out.append(sess.run(None, {k: b[k].astype(np.int64) for k in ("input_ids", "attention_mask", "token_type_ids")})[0])
    return softmax(np.concatenate(out))
with torch.no_grad():
    ref = []
    for i in range(0, len(samples), 32):
        tb = tok(samples[i:i + 32], return_tensors="pt", padding=True, truncation=True, max_length=64)
        ref.append(model(**tb).logits.numpy())
    pf = softmax(np.concatenate(ref))
labels = [model.config.id2label[i] for i in range(pf.shape[1])]
results = {}
for name, path in variants.items():
    pq = run_onnx(path)
    agree = float((pq.argmax(1) == pf.argmax(1)).mean())
    diff = np.abs(pq - pf).max(1)
    results[name] = {"label_agreement": round(agree, 4), "mean_prob_diff": round(float(diff.mean()), 4),
                     "p95_prob_diff": round(float(np.percentile(diff, 95)), 4),
                     "size_mb": round(Path(path).stat().st_size / 1e6, 1)}
    print(name, results[name])
print("fp32 label mix:", {l: int((pf.argmax(1) == i).sum()) for i, l in enumerate(labels)}, "of", len(samples))
# Prefer a small int8 model if it agrees with fp32 on >= 97% of headlines, else ship fp32.
ok = [n for n in ("int8",) if results[n]["label_agreement"] >= 0.97]
chosen = ok[0] if ok else "fp32"
shutil.move(str(variants[chosen]), OUT / "model.onnx")
for p in OUT.glob("_*.onnx"):
    p.unlink()
agree, max_diff = results[chosen]["label_agreement"], results[chosen]["p95_prob_diff"]
print("chosen:", chosen)

tok.save_vocabulary(str(OUT))
shutil.copy(Path(model.config._name_or_path) / "config.json", OUT / "config.json") if Path(model.config._name_or_path, "config.json").exists() else (OUT / "config.json").write_text(model.config.to_json_string())
(OUT / "meta.json").write_text(json.dumps({
    "repo": REPO, "revision": REVISION, "labels": labels, "max_length": 64, "lowercase": True,
    "variant": chosen, "opset": 14,
    "check": {"vs": "fp32 PyTorch", "samples": len(samples), "results": results},
    "license": "CC-BY-NC-SA-4.0 (personal, non-commercial use)",
}, indent=2))
print("wrote", sorted(p.name for p in OUT.iterdir()))
