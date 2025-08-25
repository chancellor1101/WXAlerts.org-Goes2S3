# GOES Image Uploader (to MinIO/S3)

Watches a directory recursively (e.g., `/srv/goes/output`) for finalized image files
(JPG/PNG/TIFF/etc.), uploads them to an S3-compatible bucket (MinIO), and deletes them
**after** a verified successful upload.

## Why this design
- Uses a **polling** scanner rather than inotify so it works reliably on NFS.
- Waits for a **quiet period** (default 5s) to avoid grabbing partially written files.
- Case-insensitive extension filter (configurable via `IMAGE_EXTS`).
- Verifies upload via `HEAD` (size match) before deleting.
- Stores an MD5 of the local file in object metadata (`x-amz-meta-md5`) for later integrity checks.

## Quick start

1. Create a `.env` (optional) or set the environment variables in `docker-compose`.
2. Build and run:

```bash
docker compose -f docker-compose.example.yml up -d --build
```

3. The container will:
   - Watch `/data` (mapped to `/srv/goes/output` on the host)
   - Upload to your MinIO at `S3_ENDPOINT`
   - Place objects into `S3_BUCKET` under optional prefix `S3_PREFIX`
   - Delete local files after verification

## Key environment variables

| Variable | Default | Notes |
|---|---|---|
| `WATCH_ROOT` | `/data` | Path **inside the container** to watch (map host path via a volume) |
| `IMAGE_EXTS` | `jpg,jpeg,png,gif,bmp,tif,tiff,pdf` | Comma-separated, case-insensitive |
| `QUIET_SECONDS` | `5` | Min seconds since last write before a file is eligible |
| `SCAN_INTERVAL` | `2.0` | Seconds between scans |
| `CONCURRENCY` | `4` | Uploader worker threads |
| `DELETE_AFTER_UPLOAD` | `true` | Delete local file after a verified upload |
| `S3_ENDPOINT` | `http://minio:9000` | Your MinIO endpoint (e.g., `http://10.10.0.14:9000`) |
| `S3_ACCESS_KEY` | `minioadmin` | MinIO access key |
| `S3_SECRET_KEY` | `minioadmin` | MinIO secret key |
| `S3_BUCKET` | `goes-artifacts` | Bucket to upload into (auto-created if missing) |
| `S3_REGION` | `us-east-1` | Keep as `us-east-1` for MinIO unless you require otherwise |
| `S3_VERIFY_SSL` | `true` | Set `false` for self-signed HTTP or dev environments |
| `S3_ADDRESSING_STYLE` | `path` | Use `path` for MinIO |
| `S3_PREFIX` | _empty_ | Optional folder prefix (e.g., `emwin`) |
| `EXTRA_METADATA` | _empty_ | `key=value,key2=value2` becomes object metadata |
| `LOG_LEVEL` | `INFO` | `DEBUG` for extra verbosity |

## Compose volume mapping

```
volumes:
  - /srv/goes/output:/data:rw
```

The `:rw` is required because the container deletes files on success.

## Notes

- If you want to **archive** instead of delete, set `DELETE_AFTER_UPLOAD="false"`
  and use another process to move/clean files.
- This service only uploads **files**. Directories are mirrored as S3 keys using the
  relative path under `WATCH_ROOT`.
- On first run the container ensures `S3_BUCKET` exists.

## Systemd-style log follow

```bash
docker logs -f goes-uploader
```

## Troubleshooting

- Files never upload: check `WATCH_ROOT` mapping and that your files have one of the extensions in `IMAGE_EXTS`.
- Size mismatch: very large files may still be in flux; increase `QUIET_SECONDS`.
- MinIO auth issues: validate keys, endpoint, and that `S3_VERIFY_SSL` matches your TLS setup.
