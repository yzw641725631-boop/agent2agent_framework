"""Private CPU embedding endpoint. No database, credentials, downloads or remote code."""

import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

MODEL_ID = "bge-small-zh-v1.5-fp32-75c43b0-cls-mean384s320-v1"
DIMENSIONS = 512
PREFIX = "为这个句子生成表示以用于检索相关文章："


class Encoder:
    def __init__(self, directory):
        directory = Path(directory)
        manifest = json.loads((directory / "manifest.json").read_text())
        trusted = json.loads(
            Path(__file__).with_name("model-manifest.json").read_text()
        )
        if manifest != trusted:
            raise ValueError("Untrusted model manifest")
        if manifest["model_id"] != MODEL_ID or manifest["dimensions"] != DIMENSIONS:
            raise ValueError("Model identity mismatch")
        for name, expected in manifest["files"].items():
            if (
                Path(name).name != name
                or hashlib.sha256((directory / name).read_bytes()).hexdigest()
                != expected
            ):
                raise ValueError("Model integrity check failed")
        self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        ort.disable_telemetry_events()
        self.session = ort.InferenceSession(
            str(directory / "model.onnx"),
            options,
            providers=["CPUExecutionProvider"],
        )
        self.names = {item.name for item in self.session.get_inputs()}

    def encode(self, value, kind):
        ids = self.tokenizer.encode(
            (PREFIX if kind == "query" else "") + value, add_special_tokens=False
        ).ids
        windows = [
            ids[start : start + 384] for start in range(0, max(1, len(ids)), 320)
        ]
        if len(windows) > 32:
            raise ValueError("Input exceeds window budget")
        vectors = []
        for window in windows:
            tokens = np.array([[101, *window, 102]], dtype=np.int64)
            inputs = {
                "input_ids": tokens,
                "attention_mask": np.ones_like(tokens),
                "token_type_ids": np.zeros_like(tokens),
            }
            output = self.session.run(
                None, {key: value for key, value in inputs.items() if key in self.names}
            )[0]
            vector = output[0, 0, :].astype(np.float64)
            vectors.append(vector / np.linalg.norm(vector))
        mean = np.mean(vectors, axis=0)
        result = mean / np.linalg.norm(mean)
        if result.shape != (DIMENSIONS,) or not np.isfinite(result).all():
            raise ValueError("Invalid model output")
        return result.tolist(), len(windows), len(ids)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def process_request(self, request, address):
        # Bound threads as well as inference. Reject excess connections immediately.
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, address)

    def process_request_thread(self, request, address):
        try:
            request.settimeout(5)
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_):
        pass  # No project text or queries in access logs.

    def reply(self, status, body):
        data = json.dumps(body, allow_nan=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.reply(
            200 if self.path == "/healthz" else 404,
            {"model": MODEL_ID, "dimensions": DIMENSIONS},
        )

    def do_POST(self):
        if self.path != "/embed":
            return self.reply(404, {"code": "NOT_FOUND"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 1 <= size <= 100000 or self.headers.get("Transfer-Encoding"):
                raise ValueError()
            body = json.loads(self.rfile.read(size))
            if set(body) != {"model", "kind", "text"} or body["model"] != MODEL_ID:
                raise ValueError()
            if body["kind"] not in {"query", "passage"} or not isinstance(
                body["text"], str
            ):
                raise ValueError()
            if (
                not 1
                <= len(body["text"].encode())
                <= (2400 if body["kind"] == "query" else 20000)
            ):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            return self.reply(422, {"code": "INVALID_INPUT"})
        if not self.server.inference.acquire(timeout=0.1):
            return self.reply(503, {"code": "EMBEDDING_BUSY"})
        try:
            vector, windows, tokens = self.server.encoder.encode(
                body["text"], body["kind"]
            )
            self.reply(
                200,
                {
                    "model": MODEL_ID,
                    "dimensions": DIMENSIONS,
                    "vector": vector,
                    "windows": windows,
                    "input_tokens": tokens,
                },
            )
        except ValueError:
            self.reply(422, {"code": "INVALID_INPUT"})
        except Exception:
            self.reply(503, {"code": "EMBEDDING_FAILED"})
        finally:
            self.server.inference.release()


if __name__ == "__main__":
    encoder = Encoder(os.environ.get("MODEL_DIRECTORY", "/model"))
    server = Server(("0.0.0.0", 8766), Handler)
    server.encoder, server.slots, server.inference = (
        encoder,
        threading.BoundedSemaphore(8),
        threading.Lock(),
    )
    print(
        json.dumps({"ready": True, "model": MODEL_ID, "dimensions": DIMENSIONS}),
        flush=True,
    )
    server.serve_forever()
