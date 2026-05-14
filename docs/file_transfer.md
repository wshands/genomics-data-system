# File Transfer — Streaming Architecture

## The Problem

A BAM file can be 50–100 GB. Lambda has at most ~10 GB of memory and a 15-minute timeout. Downloading the whole file into memory and then uploading it would exhaust RAM and do the work twice. The solution is to read a small piece, compute checksums on it, hand it off to S3, then read the next piece — keeping only a few hundred MB in memory at any moment regardless of file size.

---

## Layer 1 — Chunked reads from the source platform

Both connectors open the remote file and read it in 64 MB pieces. The DNAnexus connector illustrates the pattern:

```python
chunk_size = 64 * 1024 * 1024  # 64 MB

def chunked_stream() -> Iterator[bytes]:
    with dx_file.open() as f:
        while True:
            chunk = f.read(chunk_size)  # ask the network for 64 MB
            if not chunk:
                break
            md5.update(chunk)       # checksum computed in-flight
            sha256.update(chunk)
            bytes_transferred += len(chunk)
            yield chunk             # hand this chunk to the consumer
```

`yield` makes this a Python **generator** — it pauses and hands the 64 MB chunk to its consumer, then waits. It does not read the next chunk until the consumer asks for more. At any moment only one 64 MB chunk exists in memory inside this function.

The HealthOmics connector uses the same 64 MB chunk size, iterating over the streaming response body returned by `omics.get_read_set()`.

---

## Layer 2 — Checksum computation while data is in transit

`md5.update(chunk)` and `sha256.update(chunk)` are incremental — they consume the chunk, update running internal state, and then the chunk is released. When the loop finishes, `md5.hexdigest()` produces the final checksum of the whole file without ever having held the whole file in memory. This is the only pass through the data — there is no second read to verify.

---

## Layer 3 — The generator-to-file-object adapter

`boto3.upload_fileobj()` expects a file-like object with a `.read(n)` method that returns exactly `n` bytes on demand. A Python generator does not have a `.read()` method — it yields chunks of whatever size it happens to produce. The `_IterableToFileObj` class in `connectors/dnanexus.py` bridges this gap:

```python
class _IterableToFileObj:
    def __init__(self, iterable):
        self._iter = iterable
        self._buffer = b""

    def read(self, size=-1):
        try:
            while size < 0 or len(self._buffer) < size:
                self._buffer += next(self._iter)   # pull another 64 MB chunk
        except StopIteration:
            pass
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data
```

When boto3 calls `.read(104857600)` (asking for 100 MB), the adapter pulls 64 MB chunks from the generator and accumulates them in `_buffer` until it has at least 100 MB, then returns the first 100 MB and keeps the remainder. The buffer never grows beyond roughly two generator chunks (~128 MB) because boto3 pulls data at about the same rate the generator produces it.

### Why 64 MB chunks instead of 100 MB?

The 64 MB read size is an arbitrary choice that predates the upload part size — there is no strong technical reason to prefer it. The mismatch means the adapter does slightly more bookkeeping than necessary: every 100 MB upload part costs roughly 1.5 generator pulls on average, and there is always a small leftover fragment carried in `_buffer` between calls.

If the generator yielded 100 MB chunks instead, each `.read(100MB)` call would pull exactly one chunk from the generator, the buffer would drain to zero after each call, and the adapter's loop would rarely iterate more than once. It would be a cleaner design.

The adapter is still required regardless of chunk size because boto3 needs a `.read()` interface, not a generator. One additional caveat: `f.read(chunk_size)` on a streaming HTTP response is not guaranteed to return exactly `chunk_size` bytes — the network may deliver less if its internal buffer is smaller. So even with the generator chunk size set to 100 MB, the adapter's accumulation loop is still necessary to handle partial reads. Under normal conditions on a fast internal network the reads come back full, but the adapter must be correct for the edge case.

---

## Layer 4 — boto3 multipart upload

`TransferConfig` tells boto3 how to split the incoming stream into S3 parts:

```python
config = TransferConfig(
    multipart_threshold=100 * 1024 * 1024,   # use multipart if file > 100 MB
    multipart_chunksize=100 * 1024 * 1024,   # each part is 100 MB
    max_concurrency=4,                         # 4 parts in-flight simultaneously
    use_threads=True,
)
```

S3 multipart upload works in three phases:

1. **CreateMultipartUpload** — S3 returns an upload ID.
2. **UploadPart** (repeated) — each 100 MB slice is uploaded as a separate HTTP PUT with a part number (1, 2, 3, …). S3 stores each part independently and returns an ETag.
3. **CompleteMultipartUpload** — you send S3 the ordered list of part numbers and their ETags. S3 assembles them into the final object atomically.

`max_concurrency=4` means boto3 maintains a thread pool of 4 workers. While one thread is waiting for S3 to acknowledge part 1, another is uploading part 2, and so on. For a 100 GB BAM split into 1,000 parts of 100 MB each, this gives roughly 4× the throughput of a single sequential upload, bounded by the Lambda's network bandwidth rather than per-request latency.

---

## End-to-end data flow

```
DNAnexus / HealthOmics API
         │  (network stream)
         ▼
chunked_stream() generator
   reads 64 MB at a time
   updates MD5 + SHA256 in-flight
         │  (yields 64 MB chunks)
         ▼
_IterableToFileObj adapter
   buffers generator output until boto3 asks
         │  (.read(100 MB) calls from boto3)
         ▼
boto3 upload_fileobj
   thread pool: 4 workers, each holding one 100 MB part
   UploadPart × N running in parallel
         │
         ▼
S3 CompleteMultipartUpload
   final object assembled by S3 from ordered parts
```

---

## Memory usage

Peak Lambda memory at any moment is roughly:

| Component | Memory |
|---|---|
| 4 upload threads × 100 MB part | ~400 MB |
| Adapter buffer (≤ 2 generator chunks) | ~128 MB |
| Python runtime + SDK overhead | ~200 MB |
| **Total** | **~730 MB** |

This is constant regardless of file size — transferring a 100 GB BAM uses the same peak memory as a 1 GB FASTQ. The `transfer_file` Lambda is provisioned with 3 GB of RAM to give comfortable headroom above this ceiling and to take advantage of Lambda's CPU-to-memory ratio for the checksum computation.

---

## S3 ETag caveat

For multipart uploads, the S3 ETag is **not** a plain MD5 of the file content. It is computed as the MD5 of the concatenated part ETags, suffixed with `-N` where N is the part count. The pipeline therefore does not compare the S3 ETag to the computed MD5 after upload — instead `register_metadata` stores the MD5 and SHA256 computed in-flight during the transfer, and separately verifies that the object exists in S3 via a `head_object` call. This is documented as a known limitation in `CLAUDE.md`.
