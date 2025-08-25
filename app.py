import os
import time
import logging
import threading
import queue
import hashlib
from pathlib import Path
from typing import Set, List
import boto3
from botocore.exceptions import ClientError
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler

# ----------- Config via env -----------
WATCH_ROOT = os.getenv("WATCH_ROOT", "/data").rstrip("/")
IMAGE_EXTS = os.getenv("IMAGE_EXTS", "jpg,jpeg,png,gif,bmp,tif,tiff,pdf").split(",")
QUIET_SECONDS = int(os.getenv("QUIET_SECONDS", "5"))  # time with no modification before upload
SCAN_INTERVAL = float(os.getenv("SCAN_INTERVAL", "2.0"))
CONCURRENCY = int(os.getenv("CONCURRENCY", "4"))
DELETE_AFTER_UPLOAD = os.getenv("DELETE_AFTER_UPLOAD", "true").lower() in ("1","true","yes","y")
S3_BUCKET = os.getenv("S3_BUCKET", "goes-artifacts")
S3_PREFIX = os.getenv("S3_PREFIX", "").strip().strip("/")
S3_ENDPOINT = os.getenv("S3_ENDPOINT", "http://minio:9000").strip()
S3_REGION = os.getenv("S3_REGION", "us-east-1").strip()
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "minioadmin").strip()
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "minioadmin").strip()
S3_VERIFY_SSL = os.getenv("S3_VERIFY_SSL", "true").lower() in ("1","true","yes","y")
S3_ADDRESSING_STYLE = os.getenv("S3_ADDRESSING_STYLE", "path")  # 'path' for MinIO is safest
EXTRA_METADATA = os.getenv("EXTRA_METADATA", "")  # 'key1=val1,key2=val2'

# ----------- Logging -----------
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(threadName)s | %(message)s"
)
logger = logging.getLogger("goes-uploader")

# ----------- Helpers -----------
def is_interesting_file(path: Path) -> bool:
    if not path.is_file():
        return False
    # Reject temp/partial files common with decoders
    name = path.name
    if name.startswith(".") or name.endswith(".part") or name.endswith(".tmp"):
        return False
    ext = path.suffix[1:].lower()
    return ext in {e.strip().lower() for e in IMAGE_EXTS if e.strip()}

def stable_enough(path: Path) -> bool:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return False
    age = time.time() - stat.st_mtime
    size = stat.st_size
    return age >= QUIET_SECONDS and size > 0

def md5sum(path: Path, blocksize=1024*1024) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(blocksize), b""):
            h.update(chunk)
    return h.hexdigest()

def parse_extra_metadata():
    md = {}
    if not EXTRA_METADATA:
        return md
    for pair in EXTRA_METADATA.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            if k.strip():
                md[k.strip()] = v.strip()
    return md

# ----------- S3 client -----------
s3 = boto3.client(
    "s3",
    endpoint_url=S3_ENDPOINT,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
    region_name=S3_REGION,
    verify=S3_VERIFY_SSL,
    config=boto3.session.Config(s3={"addressing_style": S3_ADDRESSING_STYLE})
)

def ensure_bucket(bucket: str):
    try:
        s3.head_bucket(Bucket=bucket)
        logger.info(f"Bucket exists: {bucket}")
    except ClientError as e:
        code = int(e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
        if code == 404 or e.response.get("Error", {}).get("Code") in ("404", "NoSuchBucket"):
            logger.info(f"Creating bucket: {bucket}")
            create_kwargs = {"Bucket": bucket}
            if S3_REGION and S3_REGION != "us-east-1":
                create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": S3_REGION}
            s3.create_bucket(**create_kwargs)
        else:
            raise

ensure_bucket(S3_BUCKET)

# ----------- Uploader worker -----------
q = queue.Queue()
in_flight: Set[Path] = set()
meta_extra = parse_extra_metadata()

def key_for(path: Path) -> str:
    rel = path.relative_to(WATCH_ROOT)
    key = str(rel).replace("\\", "/")
    if S3_PREFIX:
        key = f"{S3_PREFIX}/{key}"
    return key

def upload_one(path: Path, attempts: int = 5) -> bool:
    key = key_for(path)
    md5 = md5sum(path)
    metadata = {"md5": md5}
    metadata.update(meta_extra)

    for i in range(1, attempts + 1):
        try:
            logger.info(f"Uploading {path} -> s3://{S3_BUCKET}/{key} (attempt {i})")
            s3.upload_file(
                Filename=str(path),
                Bucket=S3_BUCKET,
                Key=key,
                ExtraArgs={
                    "Metadata": metadata,
                },
            )
            # Verify by HEAD
            head = s3.head_object(Bucket=S3_BUCKET, Key=key)
            remote_len = head.get("ContentLength", -1)
            local_len = path.stat().st_size
            if remote_len == local_len:
                logger.info(f"Verified upload size for {key} ({remote_len} bytes)")
                if DELETE_AFTER_UPLOAD:
                    try:
                        path.unlink()
                        logger.info(f"Deleted local file: {path}")
                    except Exception as de:
                        logger.warning(f"Failed to delete {path}: {de}")
                return True
            else:
                logger.warning(f"Size mismatch for {key}: remote={remote_len} local={local_len}")
        except Exception as e:
            logger.warning(f"Upload failed for {path} on attempt {i}: {e}")
        time.sleep(min(2 ** i, 30))
    return False

class Scanner(threading.Thread):
    daemon = True
    def run(self):
        logger.info(f"Scanning {WATCH_ROOT} every {SCAN_INTERVAL}s (quiet={QUIET_SECONDS}s)")
        root = Path(WATCH_ROOT)
        while True:
            try:
                for p in root.rglob("*"):
                    if not is_interesting_file(p):
                        continue
                    if not stable_enough(p):
                        continue
                    if p in in_flight:
                        continue
                    in_flight.add(p)
                    q.put(p)
            except Exception as e:
                logger.error(f"Scanner error: {e}")
            time.sleep(SCAN_INTERVAL)

class Worker(threading.Thread):
    daemon = True
    def run(self):
        while True:
            path: Path = q.get()
            try:
                if path.exists():
                    upload_one(path)
            finally:
                in_flight.discard(path)
                q.task_done()

def main():
    # Spin up worker pool
    for i in range(CONCURRENCY):
        Worker(name=f"worker-{i+1}").start()
    # Start scanner
    Scanner(name="scanner").start()
    # Keep alive
    while True:
        time.sleep(3600)

if __name__ == "__main__":
    main()
